#!/usr/bin/env python3
"""Read-only local web viewer for mail backups (stdlib only); currently reads Proton Mail exports.

Usage: python server.py [EXPORT_DIR] [--port 8025] [--cli PATH] [--config FILE]

EXPORT_DIR is a mail dir (contains labels.json) or an account dir (its newest mail_* is shown).
Without EXPORT_DIR the saved choice from the config file is used; if there is none, the page
shows a directory browser and the user picks one (optionally remembering it).
With the export CLI available, POST /api/fetch fetches fresh mail into the account dir.
"""

from __future__ import annotations

import argparse
import base64
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM_FOLDER_ORDER = ["0", "7", "5", "4", "3", "6", "8"]
MAX_LIMIT = 200
META_SUFFIX = ".metadata.json"
MSG_RE = re.compile(r"^/api/message/([^/]+)$")
ATT_RE = re.compile(r"^/api/message/([^/]+)/attachment/(\d+)$")
CID_RE = re.compile(r"cid:([^\s\"'<>()]+)")
CLI_NAME = "proton-mail-export-cli"
MAX_BROWSE_ENTRIES = 2000
MAX_BODY = 64 * 1024
FETCH_LOG_LINES = 50

Json = dict[str, Any]


@dataclass
class Index:
    export_dir: str
    labels: list[Json]
    messages: dict[str, Json]  # filename stem (= message ID) -> metadata Payload


def build_index(export_dir: str) -> Index:
    """Load labels.json and all *.metadata.json; broken metadata files are skipped."""
    with open(os.path.join(export_dir, "labels.json"), encoding="utf-8") as f:
        labels = json.load(f)["Payload"]
    messages: dict[str, Json] = {}
    for name in sorted(os.listdir(export_dir)):
        if not name.endswith(META_SUFFIX):
            continue
        try:
            with open(os.path.join(export_dir, name), encoding="utf-8") as f:
                payload = json.load(f)["Payload"]
            if not isinstance(payload, dict):
                raise ValueError("Payload is not an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"skipping {name}: {exc}", file=sys.stderr)
            continue
        messages[name[: -len(META_SUFFIX)]] = payload
    return Index(export_dir, labels, messages)


def mail_dirs(parent: str) -> list[str]:
    """mail_* subdirectories of parent that contain labels.json, oldest name first."""
    if not os.path.isdir(parent):
        return []
    names = sorted(
        n for n in os.listdir(parent)
        if n.startswith("mail_") and os.path.isfile(os.path.join(parent, n, "labels.json"))
    )
    return [os.path.join(parent, n) for n in names]


def resolve_mail_dir(path: str) -> str:
    """A mail dir (has labels.json) is used as is; an account dir yields its newest mail_* export."""
    if os.path.isfile(os.path.join(path, "labels.json")):
        return path
    found = mail_dirs(path)
    if not found:
        raise ValueError(f"no labels.json in {path} or in any mail_* subdirectory")
    return found[-1]


def account_dir_of(mail_dir: str) -> str:
    path = os.path.abspath(mail_dir)
    return os.path.dirname(path) if os.path.basename(path).startswith("mail_") else path


def account_email(account_dir: str) -> str:
    name = os.path.basename(os.path.abspath(account_dir))
    return name if "@" in name else ""


def is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_cli(account_dir: str) -> str | None:
    """Look for the export CLI in the parent, grandparent and great-grandparent of account_dir."""
    d = os.path.abspath(account_dir)
    for _ in range(3):
        d = os.path.dirname(d)
        candidate = os.path.join(d, CLI_NAME)
        if is_executable(candidate):
            return candidate
    return None


def is_openable(path: str) -> bool:
    """True if path is a mail dir or an account dir holding at least one mail_* export."""
    try:
        return os.path.isfile(os.path.join(path, "labels.json")) or bool(mail_dirs(path))
    except OSError:  # unreadable directories are simply not openable
        return False


def browse(path: str) -> Json:
    """List the visible subdirectories of path for the directory picker. Raises ValueError."""
    p = os.path.abspath(os.path.expanduser(path or "~"))
    try:
        names = sorted(os.listdir(p), key=str.casefold)
    except OSError as exc:
        raise ValueError(f"cannot read {p}: {exc.strerror or exc}") from exc
    dirs: list[Json] = []
    for name in names:
        full = os.path.join(p, name)
        if name.startswith(".") or not os.path.isdir(full):
            continue
        dirs.append({"name": name, "openable": is_openable(full)})
        if len(dirs) >= MAX_BROWSE_ENTRIES:
            break
    return {
        "path": p,
        "parent": None if p == os.path.dirname(p) else os.path.dirname(p),
        "home": os.path.expanduser("~"),
        "openable": is_openable(p),
        "dirs": dirs,
    }


def default_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "mail-backup-viewer", "config.json")


def load_saved(config_path: str) -> str | None:
    try:
        with open(config_path, encoding="utf-8") as f:
            value = json.load(f).get("export_dir")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def save_saved(config_path: str, export_dir: str | None) -> None:
    """Persist the chosen directory; None forgets it. The file is replaced atomically."""
    if export_dir is None:
        remove_quiet(config_path)
        return
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    tmp = f"{config_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"export_dir": export_dir}, f)
    os.replace(tmp, config_path)


def free_name(parent: str, name: str) -> str:
    candidate, n = name, 1
    while os.path.exists(os.path.join(parent, candidate)):
        candidate = f"{name}_{n}"
        n += 1
    return candidate


def addr(d: Any) -> Json:
    d = d or {}
    return {"name": d.get("Name") or "", "address": d.get("Address") or ""}


def addr_list(items: Any) -> list[Json]:
    return [addr(x) for x in items or []]


def folder_key(f: Json) -> tuple[int, int, str]:
    if f["id"] in SYSTEM_FOLDER_ORDER:
        return (0, SYSTEM_FOLDER_ORDER.index(f["id"]), "")
    return (1, 0, f["name"].casefold())


def list_folders(index: Index) -> list[Json]:
    out = []
    for label in index.labels:
        members = [m for m in index.messages.values() if label["ID"] in (m.get("LabelIDs") or [])]
        if members:
            out.append({
                "id": label["ID"],
                "name": label.get("Name", ""),
                "path": label.get("Path", ""),
                "color": label.get("Color", ""),
                "count": len(members),
                "unread": sum(m.get("Unread") == 1 for m in members),
            })
    return sorted(out, key=folder_key)


def summarize(mid: str, meta: Json) -> Json:
    return {
        "id": mid,
        "subject": meta.get("Subject") or "",
        "sender": addr(meta.get("Sender")),
        "to": addr_list(meta.get("ToList")),
        "time": int(meta.get("Time") or 0),
        "unread": meta.get("Unread") == 1,
        "attachments": int(meta.get("NumAttachments") or 0),
    }


def matches(meta: Json, needle: str) -> bool:
    """needle must already be casefolded."""
    fields = [meta.get("Subject") or ""]
    for person in [meta.get("Sender"), *(meta.get("ToList") or [])]:
        fields.extend(addr(person).values())
    return any(needle in f.casefold() for f in fields)


def list_messages(index: Index, folder: str | None, needle: str, offset: int, limit: int) -> Json:
    items = [
        (mid, m) for mid, m in index.messages.items()
        if (not folder or folder in (m.get("LabelIDs") or []))
        and (not needle or matches(m, needle))
    ]
    items.sort(key=lambda it: (int(it[1].get("Time") or 0), it[0]), reverse=True)
    page = items[offset : offset + limit]
    return {"total": len(items), "messages": [summarize(mid, m) for mid, m in page]}


def load_eml(index: Index, mid: str) -> Message:
    # mid must already be validated against index.messages, so no path escapes export_dir
    with open(os.path.join(index.export_dir, mid + ".eml"), "rb") as f:
        return BytesParser(policy=policy.default).parsebytes(f.read())


def cid_key(ref: str) -> str:
    return unquote(ref.strip().strip("<>"))


def html_body(msg: Message) -> str | None:
    part = msg.get_body(preferencelist=("html",))
    return part.get_content() if part is not None else None


def attachment_parts(msg: Message) -> list[Message]:
    """Leaf parts that are attachments; inline images already referenced by the HTML are excluded."""
    used = {cid_key(m.group(1)) for m in CID_RE.finditer(html_body(msg) or "")}
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        cid = cid_key(str(part.get("Content-ID") or ""))
        if part.get_content_disposition() == "attachment" or (part.get_filename() and cid not in used):
            out.append(part)
    return out


def attachment_name(part: Message, n: int) -> str:
    return part.get_filename() or f"attachment-{n}"


def attachment_payload(part: Message) -> bytes:
    return part.get_payload(decode=True) or b""


def rewrite_cids(msg: Message, html: str) -> str:
    parts = {cid_key(str(p["Content-ID"])): p for p in msg.walk() if p.get("Content-ID")}

    def to_data_uri(m: re.Match[str]) -> str:
        part = parts.get(cid_key(m.group(1)))
        if part is None:
            return m.group(0)
        b64 = base64.b64encode(attachment_payload(part)).decode("ascii")
        return f"data:{part.get_content_type()};base64,{b64}"

    return CID_RE.sub(to_data_uri, html)


def parse_body(msg: Message) -> Json:
    text_part = msg.get_body(preferencelist=("plain",))
    html = html_body(msg)
    return {
        "text": text_part.get_content() if text_part is not None else None,
        "html": rewrite_cids(msg, html) if html is not None else None,
        "attachments": [
            {"index": i, "name": attachment_name(p, i), "size": len(attachment_payload(p)),
             "mime": p.get_content_type()}
            for i, p in enumerate(attachment_parts(msg))
        ],
    }


def load_message(index: Index, mid: str) -> Json:
    meta = index.messages[mid]
    out: Json = {
        "id": mid,
        "subject": meta.get("Subject") or "",
        "from": addr(meta.get("Sender")),
        "to": addr_list(meta.get("ToList")),
        "cc": addr_list(meta.get("CCList")),
        "time": int(meta.get("Time") or 0),
        "text": None,
        "html": None,
        "attachments": [],
        "error": None,
    }
    try:
        out.update(parse_body(load_eml(index, mid)))
    except FileNotFoundError:
        out["error"] = "message file (.eml) not found"
    except Exception as exc:  # untrusted input: any parser failure becomes an error field
        out["error"] = f"cannot parse .eml: {exc}"
    return out


def get_attachment(index: Index, mid: str, n: int) -> Message | None:
    try:
        parts = attachment_parts(load_eml(index, mid))
    except Exception:  # missing or unparsable .eml: nothing to serve
        return None
    return parts[n] if 0 <= n < len(parts) else None


def cli_env(email: str, tmp: str, password: str, mailbox: str, totp: str) -> dict[str, str]:
    """Environment for the export CLI. Secrets only travel here, never in argv."""
    env = dict(os.environ)
    env.pop("ET_USER_MAILBOX_PASSWORD", None)  # set only when non-empty
    env.pop("ET_TOTP_CODE", None)
    env.update(ET_OPERATION="backup", ET_DIR=tmp, ET_USER_EMAIL=email,
               ET_USER_PASSWORD=password, ET_TELEMETRY_OFF="1")
    if mailbox:
        env["ET_USER_MAILBOX_PASSWORD"] = mailbox
    if totp:
        env["ET_TOTP_CODE"] = totp
    return env


def redact(text: str, secrets: list[str]) -> str:
    # longest first, so a secret that contains another one is fully hidden
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def newest_fetched(tmp: str) -> str | None:
    """Newest mail dir the CLI wrote below tmp (layout: tmp/<email>/mail_*)."""
    found = [d for name in os.listdir(tmp) for d in mail_dirs(os.path.join(tmp, name))]
    return max(found, key=os.path.basename, default=None)


def is_regular(path: str) -> bool:
    return os.path.isfile(path) and not os.path.islink(path)


def remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def link_identical(prev_path: str, new_path: str) -> int:
    """Replace new_path with a hardlink to prev_path if both are identical regular files.
    Returns the bytes saved, or 0 when nothing was linked."""
    tmp = os.path.join(os.path.dirname(new_path), "." + os.path.basename(new_path) + ".link-tmp")
    try:
        if not (is_regular(prev_path) and is_regular(new_path)):
            return 0
        size = os.path.getsize(new_path)
        if os.path.samefile(prev_path, new_path) or size != os.path.getsize(prev_path):
            return 0
        if not filecmp.cmp(prev_path, new_path, shallow=False):
            return 0
        os.link(prev_path, tmp)
        os.replace(tmp, new_path)  # atomic: new_path is never missing
        return size
    except OSError:  # cross-device, permissions, unsupported FS: keep the file as it is
        return 0
    finally:
        if os.path.lexists(tmp):
            remove_quiet(tmp)


def dedupe_with_previous(previous_dir: str, new_dir: str) -> tuple[int, int]:
    """Hardlink unchanged .eml and .metadata.json files of new_dir to their twins in previous_dir.
    Returns (files_linked, bytes_saved). The previous dir is only read."""
    try:
        names = os.listdir(new_dir)
    except OSError:
        return 0, 0
    linked = saved = 0
    for name in names:
        if not name.endswith((".eml", META_SUFFIX)):
            continue
        size = link_identical(os.path.join(previous_dir, name), os.path.join(new_dir, name))
        if size:
            linked += 1
            saved += size
    return linked, saved


def human_size(n: int) -> str:
    """Byte count as a short German label with 1000-based units, e.g. "1,2 GB"."""
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1000:
            return f"{size:.1f} {unit}".replace(".", ",")
        size /= 1000
    return f"{size:.1f} GB".replace(".", ",")


def _text(body: Json, key: str) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def parse_fetch_body(raw: bytes, default_email: str) -> tuple[str, str, str, str]:
    """Return (email, password, mailbox_password, totp); ValueError messages are shown to the client."""
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise ValueError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    email = _text(body, "email").strip() or default_email
    password = _text(body, "password")
    if not password:
        raise ValueError("missing password")
    if not email:
        raise ValueError("missing email")
    if "/" in email or "\\" in email or email in (".", ".."):
        raise ValueError("invalid email")
    return email, password, _text(body, "mailbox_password"), _text(body, "totp").strip()


def _idle_status() -> Json:
    return {"state": "idle", "log": [], "message": None, "export": None, "started": None}


class Busy(Exception):
    """The working directory cannot change while a fetch is running."""


class Viewer:
    """State shared by all handler threads: the current index (None until a directory is
    chosen), the single fetch slot and the remembered choice."""

    def __init__(self, index: Index | None, account_dir: str, cli: str | None,
                 explicit_cli: bool = False, config_path: str | None = None) -> None:
        self.account_dir = account_dir
        self.cli = cli
        self.explicit_cli = explicit_cli
        self.config_path = config_path
        self.saved: str | None = load_saved(config_path) if config_path else None
        self.saved_error: str | None = None
        self._index = index
        self._lock = threading.Lock()  # guards _index, _status and the directory fields
        self._status = _idle_status()

    @property
    def index(self) -> Index | None:
        with self._lock:
            return self._index

    def open_dir(self, path: str, remember: bool | None = None) -> None:
        """Make path (a mail dir or account dir) the working directory.

        remember=True saves the choice, False forgets a saved one, None leaves it alone.
        Raises ValueError for an unusable directory and Busy during a fetch."""
        mail_dir = resolve_mail_dir(os.path.abspath(os.path.expanduser(path)))
        index = build_index(mail_dir)
        account = account_dir_of(mail_dir)
        with self._lock:
            if self._status["state"] == "running":
                raise Busy("fetch running")
            self._index = index
            self.account_dir = account
            if not self.explicit_cli:
                self.cli = find_cli(account)
            self._status = _idle_status()
        if remember is not None and self.config_path:
            try:
                save_saved(self.config_path, os.path.abspath(os.path.expanduser(path)) if remember else None)
            except OSError as exc:
                raise ValueError(f"cannot save the choice: {exc.strerror or exc}") from exc
            self.saved = os.path.abspath(os.path.expanduser(path)) if remember else None
            self.saved_error = None

    def status(self) -> Json:
        with self._lock:
            return {**self._status, "log": list(self._status["log"])}

    def start_fetch(self, email: str, password: str, mailbox: str, totp: str) -> bool:
        """Start a background fetch unless one is running. Secrets are passed on, not stored."""
        with self._lock:
            if self._status["state"] == "running":
                return False
            self._status = {"state": "running", "log": [], "message": None, "export": None,
                            "started": int(time.time())}
        threading.Thread(target=self._fetch, args=(email, password, mailbox, totp), daemon=True).start()
        return True

    def _fetch(self, email: str, password: str, mailbox: str, totp: str) -> None:
        secrets = [s for s in (password, mailbox, totp) if s]
        previous = self.index.export_dir if self.index else ""  # the index about to be replaced
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix=".fetch-", dir=self.account_dir)
            code = self._run_cli(cli_env(email, tmp, password, mailbox, totp), secrets)
            if code != 0:
                raise RuntimeError(f"CLI exited with code {code}")
            found = newest_fetched(tmp)
            if found is None:
                raise RuntimeError("CLI produced no mail export")
            name = free_name(self.account_dir, os.path.basename(found))
            dest = os.path.join(self.account_dir, name)
            os.rename(found, dest)
            self._link_previous(previous, dest)
            self._set_index(build_index(dest))
            self._finish("done", export=name)
        except Exception as exc:  # the old index stays in place
            self._finish("error", message=redact(str(exc) or type(exc).__name__, secrets))
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def _run_cli(self, env: dict[str, str], secrets: list[str]) -> int:
        # CLI 1.0.7 ignores ET_DIR for backups and would prompt for the path on stdin,
        # so pass it as --dir too (a path is no secret)
        proc = subprocess.Popen([self.cli, "--dir", env["ET_DIR"]], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        with proc:
            for line in proc.stdout:
                self._log(redact(line.rstrip("\r\n"), secrets))
            return proc.wait()

    def _link_previous(self, previous: str, dest: str) -> None:
        """Hardlink unchanged mails to the previous export. Failures only cost disk space, so never raise."""
        if not os.path.isdir(previous):
            return
        try:
            linked, saved = dedupe_with_previous(previous, dest)
        except Exception as exc:
            self._log(f"Hardlinks fehlgeschlagen: {exc}")
            return
        if linked:
            self._log(f"Hardlinks: {linked} Dateien, {human_size(saved)} gespart")
        else:
            self._log("Hardlinks: keine identischen Dateien")

    def _log(self, line: str) -> None:
        with self._lock:
            log = self._status["log"]
            log.append(line)
            del log[:-FETCH_LOG_LINES]

    def _finish(self, state: str, message: str | None = None, export: str | None = None) -> None:
        with self._lock:
            self._status.update(state=state, message=message, export=export)

    def _set_index(self, index: Index) -> None:
        with self._lock:
            self._index = index


def _arg(query: dict[str, list[str]], key: str) -> str:
    return query.get(key, [""])[0]


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def _hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def _origins(port: int) -> set[str]:
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def make_handler(viewer: Viewer, static_dir: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MailViewer/1.0"
        sys_version = ""

        def do_GET(self) -> None:
            if self.headers.get("Host") not in _hosts(self.server.server_port):  # DNS-rebinding protection
                return self._json(403, {"error": "host not allowed"})
            index = viewer.index
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                return self._index_page()
            if url.path == "/api/config":
                return self._json(200, self._config(index))
            if url.path == "/api/browse":
                try:
                    return self._json(200, browse(_arg(query, "path") or viewer.saved or "~"))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
            if url.path == "/api/fetch/status":
                return self._json(200, viewer.status())
            if index is None:
                return self._json(409, {"error": "no directory chosen"})
            if url.path == "/api/folders":
                return self._json(200, list_folders(index))
            if url.path == "/api/messages":
                return self._json(200, self._list(index, query))
            if m := ATT_RE.match(url.path):
                return self._attachment(index, unquote(m[1]), int(m[2]))
            if m := MSG_RE.match(url.path):
                mid = unquote(m[1])
                if mid not in index.messages:
                    return self._json(404, {"error": "unknown message"})
                return self._json(200, load_message(index, mid))
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            raw = self._read_body()  # read first, so refused requests leave no unread body behind
            port = self.server.server_port
            if self.headers.get("Host") not in _hosts(port):
                return self._json(403, {"error": "host not allowed"})
            if self.headers.get("Origin") not in _origins(port):  # a missing Origin is refused too
                return self._json(403, {"error": "origin not allowed"})
            path = urlsplit(self.path).path
            if path not in ("/api/fetch", "/api/workdir"):
                return self._json(404, {"error": "not found"})
            if len(raw) > MAX_BODY:
                return self._json(400, {"error": "body too large"})
            if path == "/api/workdir":
                return self._set_workdir(raw)
            if viewer.cli is None or viewer.index is None:
                return self._json(404, {"error": "fetch not available"})
            try:
                email, password, mailbox, totp = parse_fetch_body(raw, account_email(viewer.account_dir))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            if not viewer.start_fetch(email, password, mailbox, totp):
                return self._json(409, {"error": "fetch already running"})
            self._json(202, {"state": "running"})

        def _set_workdir(self, raw: bytes) -> None:
            try:
                data = json.loads(raw.decode("utf-8"))
                path, remember = data["path"], bool(data.get("remember"))
                if not isinstance(path, str) or not path.strip():
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "path required"})
            try:
                viewer.open_dir(path.strip(), remember)
            except Busy:
                return self._json(409, {"error": "fetch running"})
            except (ValueError, OSError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, self._config(viewer.index))

        def _read_body(self) -> bytes:
            """Drain the declared request body, keeping at most MAX_BODY + 1 bytes."""
            left = max(0, _int(self.headers.get("Content-Length") or "", 0))
            kept = b""
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
                kept = (kept + chunk)[: MAX_BODY + 1]
            return kept

        def _config(self, index: Index | None) -> Json:
            return {
                "configured": index is not None,
                "fetch_available": index is not None and viewer.cli is not None,
                "email": account_email(viewer.account_dir),
                "export": os.path.basename(index.export_dir) if index else "",
                "workdir": viewer.account_dir,
                "saved": viewer.saved,
                "saved_error": viewer.saved_error,
                "home": os.path.expanduser("~"),
            }

        def _list(self, index: Index, query: dict[str, list[str]]) -> Json:
            folder = _arg(query, "folder") or None
            needle = _arg(query, "q").strip().casefold()
            offset = max(0, _int(_arg(query, "offset"), 0))
            limit = min(MAX_LIMIT, max(1, _int(_arg(query, "limit"), 50)))
            return list_messages(index, folder, needle, offset, limit)

        def _attachment(self, index: Index, mid: str, n: int) -> None:
            part = get_attachment(index, mid, n) if mid in index.messages else None
            if part is None:
                return self._json(404, {"error": "no such attachment"})
            name = quote(attachment_name(part, n), safe="")
            self._send(200, attachment_payload(part), part.get_content_type(),
                       (("Content-Disposition", f"attachment; filename*=UTF-8''{name}"),))

        def _index_page(self) -> None:
            try:
                with open(os.path.join(static_dir, "index.html"), "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, {"error": "index.html not found"})
            self._send(200, body, "text/html; charset=utf-8")

        def _json(self, status: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _send(self, status: int, body: bytes, ctype: str, extra: tuple[tuple[str, str], ...] = ()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def make_server(export_dir: str | None = None, port: int = 0, static_dir: str = HERE,
                cli: str | None = None, config_path: str | None = None) -> ThreadingHTTPServer:
    """Bind 127.0.0.1:port. cli=None auto-detects the export CLI (see find_cli).

    Without export_dir the choice saved in config_path is opened; if that fails or nothing is
    saved, the viewer starts empty and the page asks for a directory."""
    config_path = config_path or default_config_path()
    explicit_cli = cli is not None
    cli_path = os.path.abspath(cli) if cli is not None else None
    if cli_path is not None and not is_executable(cli_path):
        cli_path = None  # fetching is simply unavailable
    viewer = Viewer(None, "", cli_path, explicit_cli, config_path)
    if export_dir is not None:
        viewer.open_dir(export_dir)
    elif viewer.saved:
        try:
            viewer.open_dir(viewer.saved)
        except (ValueError, OSError) as exc:
            viewer.saved_error = f"{viewer.saved}: {exc}"
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(viewer, static_dir))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read-only local viewer for a mail export.")
    parser.add_argument("export_dir", nargs="?",
                        help="mail or account dir (default: the saved choice, else pick in the browser)")
    parser.add_argument("--port", type=int, default=8025)
    parser.add_argument("--cli", help="path to proton-mail-export-cli (default: look near the account dir)")
    parser.add_argument("--config", help=f"config file (default: {default_config_path()})")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.export_dir, args.port, cli=args.cli, config_path=args.config)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: {exc}")
    print(f"Serving on http://127.0.0.1:{server.server_port}/ (Ctrl+C to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
