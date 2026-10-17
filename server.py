#!/usr/bin/env python3
"""Read-only local web viewer for a library of mail backups (stdlib only).

Usage: python server.py [LIBRARY_DIR] [--port 8025] [--cli PATH] [--config FILE]

LIBRARY_DIR holds library.json, which lists the mail sources (Proton accounts or maildirs) shown
together. Its messages are indexed in archive.db (a cache next to library.json). Tags and stars
live in tags.db beside it, which an index rebuild never touches. Without
LIBRARY_DIR the library remembered in the config file is opened; if there is none, the page
asks for a library folder (optionally creating one or remembering the choice).
Mail files are only read, except for a cleanup of an imported Outlook source (POST /api/cleanup): its
duplicate mails become hardlinks to a reference source, or are deleted. Each run logs to
cleanup-<time>.log in the library.
Optional: .pst/.ost files can be imported as sources when the pypff module is installed.
"""

from __future__ import annotations

import argparse
import base64
import filecmp
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import asdict
from email import policy
from email.message import Message
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from cleanup import ACTIONS, check_dirs, is_regular, plan_cleanup, remove_quiet, run_cleanup
from library import ICON_DIR, Library, LibraryError, Settings, Source, detect_kind, is_library
from outlook import (
    INSTALL_HINT,
    NO_MAIL_HINT,
    ImportReport,
    ImportUnavailable,
    convert,
    open_reader,
    pypff_available,
)
from store import META_SUFFIX, Store, addr, addr_list, to_int
from tags import TagStore, TagStoreError

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_LIMIT = 200
MSG_RE = re.compile(r"^/api/message/([^/]+)/([^/]+)$")
ATT_RE = re.compile(r"^/api/message/([^/]+)/([^/]+)/attachment/(\d+)$")
SOURCE_RE = re.compile(r"^/api/sources/([^/]+)(?:/(remove|icon))?$")
TAG_RE = re.compile(r"^/api/tags/(\d+)(?:/(remove))?$")
MARKS_RE = re.compile(r"^/api/message/([^/]+)/([^/]+)/marks$")
ICON_RE = re.compile(r"^/api/sources/([^/]+)/icon$")
CID_RE = re.compile(r"cid:([^\s\"'<>()]+)")
CLI_NAME = "proton-mail-export-cli"
MAX_BROWSE_ENTRIES = 2000
MAX_BODY = 64 * 1024
MAX_ICON = 512 * 1024
ICON_TYPES = {"image/png": "png", "image/svg+xml": "svg"}  # Content-Type -> file extension
ICON_CTYPES = {ext: ctype for ctype, ext in ICON_TYPES.items()}
# an SVG is only ever shown as an image document: no scripts, no external loads
ICON_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; sandbox"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SOURCE_FIELDS = ("name", "icon", "hidden", "position")
SETTINGS_FIELDS = tuple(asdict(Settings()))
POST_ROUTES = ("/api/library", "/api/fetch", "/api/sources", "/api/settings", "/api/import", "/api/tags",
               "/api/cleanup")
TAG_FIELDS = ("name", "color")
MARK_FIELDS = ("starred", "tags")
MAX_TAG_NAME = 40
TAG_COLORS = ["#0a66c2", "#2e7d32", "#7b3fb0", "#b03a2e", "#b35c00", "#00796b", "#4a5568", "#8a6d00"]
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
IMPORT_FIELDS = ("path", "name")
CLEANUP_FIELDS = ("target", "reference", "action")
IMPORT_EXTENSIONS = (".pst", ".ost")
SOURCES_DIR = "sources"  # below the library root; imported Outlook files live here
IMPORT_TMP_PREFIX = ".import-"
SLUG_EXCLUDED = re.compile(r"[^a-z0-9äöüß]")
SLUG_MAX = 40
LOG_LINES = 50
INDEX_DB = "archive.db"
TAGS_DB = "tags.db"

Json = dict[str, Any]


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


def browse(path: str, files: bool = False) -> Json:
    """List the visible subdirectories of path for the directory picker. Raises ValueError.

    With files=True also list visible .pst/.ost files (any case) under "files".
    """
    p = os.path.abspath(os.path.expanduser(path or "~"))
    try:
        names = sorted(os.listdir(p), key=str.casefold)
    except OSError as exc:
        raise ValueError(f"cannot read {p}: {exc.strerror or exc}") from exc
    dirs: list[Json] = []
    found: list[Json] = []
    for name in names:
        if len(dirs) + len(found) >= MAX_BROWSE_ENTRIES:
            break
        full = os.path.join(p, name)
        if name.startswith("."):
            continue
        if os.path.isdir(full):
            dirs.append({"name": name, "openable": is_openable(full), "library": is_library(full)})
        elif files and name.lower().endswith((".pst", ".ost")) and os.path.isfile(full):
            try:
                found.append({"name": name, "size": os.path.getsize(full)})
            except OSError:  # vanished or unreadable: not listed
                continue
    result: Json = {
        "path": p,
        "parent": None if p == os.path.dirname(p) else os.path.dirname(p),
        "home": os.path.expanduser("~"),
        "openable": is_openable(p),
        "library": is_library(p),
        "dirs": dirs,
    }
    if files:
        result["files"] = found
    return result


def default_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "mail-backup-viewer", "config.json")


def _config_value(config_path: str, key: str) -> str | None:
    try:
        with open(config_path, encoding="utf-8") as f:
            value = json.load(f).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def load_saved(config_path: str) -> str | None:
    return _config_value(config_path, "library")


def load_legacy(config_path: str) -> str | None:
    """Export dir of the viewer before libraries existed; offered once, then added as a source."""
    return _config_value(config_path, "export_dir")


def _update_config(config_path: str, updates: dict[str, str | None]) -> None:
    """Set or (value None) remove keys of the config file; unknown keys are kept. The file is
    replaced atomically, and removed when nothing is left in it."""
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key, value in updates.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    if not data:
        remove_quiet(config_path)
        return
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    tmp = f"{config_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, config_path)


def save_saved(config_path: str, library_dir: str | None) -> None:
    """Persist the library folder; None forgets it."""
    _update_config(config_path, {"library": library_dir})


def free_name(parent: str, name: str) -> str:
    candidate, n = name, 1
    while os.path.exists(os.path.join(parent, candidate)):
        candidate = f"{name}_{n}"
        n += 1
    return candidate


def load_eml(mail_dir: str, mid: str) -> Message:
    # mid must already be confirmed by Store.message, so no path escapes mail_dir
    with open(os.path.join(mail_dir, mid + ".eml"), "rb") as f:
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


def load_message(mail_dir: str, mid: str, payload: Json) -> Json:
    out: Json = {
        "id": mid,
        "subject": payload.get("Subject") or "",
        "from": addr(payload.get("Sender")),
        "to": addr_list(payload.get("ToList")),
        "cc": addr_list(payload.get("CCList")),
        "time": to_int(payload.get("Time")),
        "text": None,
        "html": None,
        "attachments": [],
        "error": None,
    }
    try:
        out.update(parse_body(load_eml(mail_dir, mid)))
    except FileNotFoundError:
        out["error"] = "message file (.eml) not found"
    except Exception as exc:  # untrusted input: any parser failure becomes an error field
        out["error"] = f"cannot parse .eml: {exc}"
    return out


def get_attachment(mail_dir: str, mid: str, n: int) -> Message | None:
    try:
        parts = attachment_parts(load_eml(mail_dir, mid))
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
    """Hide each secret, also as the CLI may echo it: URL-quoted or JSON-escaped. Longest first, so a
    secret that contains another one is fully hidden."""
    forms: set[str] = set()
    for secret in secrets:
        if secret:
            forms.update((secret, quote(secret, safe=""), json.dumps(secret)[1:-1],
                          json.dumps(secret, ensure_ascii=False)[1:-1]))
    for form in sorted((f for f in forms if f), key=len, reverse=True):
        text = text.replace(form, "***")
    return text


def newest_fetched(tmp: str) -> str | None:
    """Newest mail dir the CLI wrote below tmp (layout: tmp/<email>/mail_*)."""
    found = [d for name in os.listdir(tmp) for d in mail_dirs(os.path.join(tmp, name))]
    return max(found, key=os.path.basename, default=None)


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


def has_control(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def check_text(value: str, what: str) -> str:
    if has_control(value):
        raise ValueError(f"invalid {what}")
    return value


def check_email(email: str) -> str:
    """The email becomes a directory name (the CLI's output dir), so no separators, dot names,
    control characters or surrounding spaces; RFC 5321 caps an address at 254 characters."""
    if (len(email) > 254 or has_control(email) or email != email.strip()
            or "/" in email or "\\" in email or email in (".", "..")):
        raise ValueError("invalid email")
    return email


def check_tag_name(value: str) -> str:
    """Trimmed, 1 to MAX_TAG_NAME characters, no control characters. Uniqueness is checked by TagStore."""
    check_text(value, "Tag-Name")
    name = value.strip()
    if not 1 <= len(name) <= MAX_TAG_NAME:
        raise ValueError(f"Der Tag-Name muss 1 bis {MAX_TAG_NAME} Zeichen lang sein.")
    return name


def check_tag_color(value: str | None) -> str:
    """Exactly #rrggbb; stored in lowercase."""
    if not isinstance(value, str) or not COLOR_RE.match(value):
        raise ValueError("Die Farbe muss im Format #rrggbb angegeben werden.")
    return value.lower()


def tag_color(value: str | None, name: str) -> str:
    """The given color, or for None a default picked from TAG_COLORS by a stable hash of the folded name."""
    if value is None:
        return TAG_COLORS[zlib.crc32(name.casefold().encode("utf-8")) % len(TAG_COLORS)]
    return check_tag_color(value)


def tag_list(store: Store, tags: TagStore, source_ids: list[str]) -> Json:
    """The star count and every tag with its message count, for the given sources."""
    counts = store.tag_counts(source_ids, dedupe=len(source_ids) > 1)
    return {"star": counts["star"], "tags": [
        {**tag, "count": counts.get(str(tag["id"]), 0)} for tag in tags.list_tags()]}


def parse_fetch_body(raw: bytes) -> tuple[str, str, str, str, str]:
    """Return (source, email, password, mailbox_password, totp). An empty email means the source's
    default, which only the viewer knows. ValueError messages are shown to the client."""
    body = load_json_object(raw)
    source = _text(body, "source").strip()
    if not source:
        raise ValueError("missing source")
    password = check_text(_text(body, "password"), "password")
    if not password:
        raise ValueError("missing password")
    email = check_email(_text(body, "email"))
    mailbox = check_text(_text(body, "mailbox_password"), "mailbox_password")
    return source, email, password, mailbox, check_text(_text(body, "totp"), "totp").strip()


def load_json_object(raw: bytes) -> Json:
    """Parse a request body that must be a JSON object. ValueError messages are shown to the client."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError) as exc:  # nesting too deep is just as invalid
        raise ValueError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


def only_keys(body: Json, allowed: tuple[str, ...]) -> None:
    """Refuse fields the endpoint does not know, instead of silently ignoring them."""
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise ValueError(f"Unbekanntes Feld: {', '.join(unknown)}")


def valid_icon(ext: str, data: bytes) -> bool:
    """Cheap content check before an upload is stored: a PNG signature, or SVG-looking UTF-8 text."""
    if ext == "png":
        return data.startswith(PNG_SIGNATURE)
    try:
        text = data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return False
    return text.startswith("<") and "<svg" in text


def import_path(path: str) -> str:
    """The resolved path of an existing .pst or .ost file. Symlinks are resolved first, so the
    extension and the file test apply to the file that is actually read. ValueError otherwise."""
    real = os.path.realpath(os.path.expanduser(path))
    if not real.lower().endswith(IMPORT_EXTENSIONS) or not os.path.isfile(real):
        raise ValueError("not an existing .pst or .ost file")
    return real


def import_slug(name: str) -> str:
    """Directory name for an imported source: lower case, only [a-z0-9äöüß] kept, the rest "-".
    The result never starts with a dot, so it cannot clash with the temp dirs."""
    slug = SLUG_EXCLUDED.sub("-", name.lower())[:SLUG_MAX]
    return slug or "outlook"


def remove_tree(path: str) -> None:
    """Remove a directory tree, or a file or symlink, without following links. Errors are ignored."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        remove_quiet(path)


def import_log_lines(report: ImportReport) -> list[str]:
    """Counts and folder paths only: no subject, address or other mail content is logged."""
    lines = [f"Importiert: {report.imported}", f"Zusammengeführt: {report.merged}"]
    if report.skipped:
        lines.append("Übersprungen: " + ", ".join(f"{cls} {n}" for cls, n in report.skipped.items()))
    if report.unreadable:
        lines.append(f"Nicht lesbar: {report.unreadable}")
    if report.failed_folders:
        lines.append("Ordner nicht lesbar: " + ", ".join(report.failed_folders))
    return lines


class Busy(Exception):
    """The job slot is taken: the library cannot be switched or a second job started meanwhile."""


class FetchUnavailable(Exception):
    """No export CLI is known for the source."""


class Job:
    """The one background job (index or fetch). Its state is served at /api/job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Json = {"state": "idle", "kind": None, "log": [], "message": None,
                             "progress": None, "started": None, "result": None}

    def start(self, kind: str, target: Callable[[Job], None]) -> bool:
        """Run target in a thread unless a job is running. An exception ends in state "error"."""
        with self._lock:
            if self._state["state"] == "running":
                return False
            self._state = {"state": "running", "kind": kind, "log": [], "message": None,
                           "progress": None, "started": int(time.time()), "result": None}
        threading.Thread(target=self._run, args=(target,), daemon=True).start()
        return True

    def _run(self, target: Callable[[Job], None]) -> None:
        try:
            target(self)
        except Exception as exc:  # reported in the status, never raised into the thread
            self._finish("error", str(exc) or type(exc).__name__)
        else:
            self._finish("done")

    def log(self, line: str) -> None:
        with self._lock:
            log = self._state["log"]
            log.append(line)
            del log[:-LOG_LINES]

    def progress(self, done: int, total: int) -> None:
        with self._lock:
            self._state["progress"] = {"done": done, "total": total}

    def status(self) -> Json:
        with self._lock:
            return {**self._state, "log": list(self._state["log"])}

    def running(self) -> bool:
        with self._lock:
            return self._state["state"] == "running"

    def set_result(self, result: Json) -> None:
        with self._lock:
            self._state["result"] = result

    def _finish(self, state: str, message: str | None = None) -> None:
        with self._lock:
            self._state.update(state=state, message=message)


def _open_stores(lib: Library) -> tuple[Store, TagStore]:
    """Open <root>/tags.db, then <root>/archive.db, which attaches it. Then drop the index rows of
    sources that left library.json. tags.db comes first because Store expects its schema to exist."""
    try:
        tags = TagStore(os.path.join(lib.root, TAGS_DB))
    except (TagStoreError, sqlite3.Error) as exc:
        raise LibraryError(f"Die Tag-Datenbank kann nicht geöffnet werden: {exc}") from exc
    try:
        store = Store(os.path.join(lib.root, INDEX_DB), os.path.join(lib.root, TAGS_DB))
        try:
            kept = {s.id for s in lib.sources}
            for n in range(1, lib.next_id):  # source ids are never reused, so this covers every removed one
                sid = f"s{n}"
                if sid not in kept and store.mail_dir(sid) is not None:
                    store.drop_source(sid)
        finally:
            store.close()  # this thread's connection; readers reconnect lazily
    except sqlite3.Error as exc:
        tags.close()
        raise LibraryError(f"Der Index kann nicht geöffnet werden: {exc}") from exc
    return store, tags


def _has_path(lib: Library, path: str) -> bool:
    real = os.path.realpath(path)
    return any(os.path.realpath(lib.abspath(s)) == real for s in lib.sources)


class Viewer:
    """State shared by all handler threads: the open library with its index, the background job
    and the remembered choice. A new library replaces library and store as a whole."""

    def __init__(self, cli: str | None = None, explicit_cli: bool = False,
                 config_path: str | None = None) -> None:
        self.cli = cli
        self.explicit_cli = explicit_cli
        self.config_path = config_path
        self.saved: str | None = load_saved(config_path) if config_path else None
        self.saved_error: str | None = None
        self.library: Library | None = None
        self.store: Store | None = None
        self.tags: TagStore | None = None
        self.job = Job()
        self._lock = threading.Lock()  # serializes open_library

    def open_library(self, path: str, create: bool = False, remember: bool | None = None,
                     add_legacy: bool = False) -> None:
        """Open or create the library at path and index all its sources in the background.

        Everything is prepared before the current library is replaced, so a failure leaves the
        old one in place. remember=True saves the choice, False forgets a saved one, None leaves
        it alone. Raises LibraryError, or Busy while an index job runs."""
        root = os.path.abspath(os.path.expanduser(path))
        with self._lock:
            if self.job.status()["state"] == "running":
                raise Busy("index job running")
            legacy = load_legacy(self.config_path) if add_legacy and self.config_path else None
            if legacy:  # checked before anything is written, so a failed request leaves no library.json
                try:
                    detect_kind(os.path.abspath(os.path.expanduser(legacy)))
                except LibraryError as exc:
                    raise LibraryError(f"Bisheriges Verzeichnis {legacy} nicht gefunden. "
                                       "Haken entfernen oder Verzeichnis anschließen.") from exc
            lib = Library.create(root) if create else Library.open(root)
            if legacy and not _has_path(lib, legacy):
                lib.add_source(legacy)
            store, tags = _open_stores(lib)
            updates: dict[str, str | None] = {}
            if remember is not None:
                updates["library"] = root if remember else None
            if legacy:  # consumed: stop offering it
                updates["export_dir"] = None
            if updates and self.config_path:
                try:
                    _update_config(self.config_path, updates)
                except OSError as exc:
                    raise LibraryError(f"Die Auswahl kann nicht gespeichert werden: "
                                       f"{exc.strerror or exc}") from exc
            if remember is not None:
                self.saved = root if remember else None
                self.saved_error = None
            self.library, self.store, self.tags = lib, store, tags
            self.start_index()

    def _admin_library(self, idle: bool) -> tuple[Library, Store]:
        """Library and store for an admin change; the caller holds _lock. Busy while a job runs if idle."""
        lib, store = self.library, self.store
        if lib is None or store is None:
            raise Busy("no library chosen")
        if idle and self.job.running():
            raise Busy("index job running")
        return lib, store

    def add_source(self, path: str, name: str) -> Source:
        """Add a source and index only it. Busy while a job runs, and then nothing is added."""
        with self._lock:
            lib, _ = self._admin_library(idle=True)
            src = lib.add_source(os.path.abspath(os.path.expanduser(path)), name)
            self.start_index([src.id])
            return src

    def update_source(self, source_id: str, *, name: str | None, icon: Json | None,
                      hidden: bool | None, position: int | None) -> None:
        """Rename, icon, hide and reorder touch library.json only, so they may run during a job."""
        with self._lock:
            lib, _ = self._admin_library(idle=False)
            lib.update_source(source_id, name=name, icon=icon, hidden=hidden)
            if position is not None:
                lib.move_source(source_id, position)

    def remove_source(self, source_id: str) -> None:
        """Forget the source and its index rows. Its mail files are never touched."""
        with self._lock:
            lib, store = self._admin_library(idle=True)
            lib.get(source_id)  # KeyError before anything changes
            store.drop_source(source_id)  # first: if it fails, library.json and the icons stay as they are
            lib.remove_source(source_id)

    def set_icon(self, source_id: str, ext: str, data: bytes) -> None:
        """Store the image as icons/<id>.<ext>, replacing the other type's file, and point the source at it."""
        with self._lock:
            lib, _ = self._admin_library(idle=True)
            src = lib.get(source_id)  # KeyError for unknown ids: the file name never comes from the client
            icons = os.path.join(lib.root, ICON_DIR)
            os.makedirs(icons, exist_ok=True)
            name = f"{src.id}.{ext}"
            path = os.path.join(icons, name)
            tmp = f"{path}.tmp"
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                remove_quiet(tmp)
                raise
            lib.update_source(src.id, icon={"type": "image", "file": name})
            for other in ICON_CTYPES:
                if other != ext:
                    remove_quiet(os.path.join(icons, f"{src.id}.{other}"))

    def update_settings(self, fields: Json) -> None:
        with self._lock:
            lib, _ = self._admin_library(idle=False)
            lib.update_settings(**fields)

    def available(self, source: Source) -> bool:
        if self.library is None:
            return False
        try:
            detect_kind(self.library.abspath(source))
        except LibraryError:
            return False
        return True

    def mail_dir_for(self, source: Source) -> str:
        path = self.library.abspath(source)
        return resolve_mail_dir(path) if source.kind == "proton" else path

    def cli_for(self, source: Source) -> str | None:
        """--cli if given, else the library's cli setting if it is an absolute executable path, else a CLI
        found near the source."""
        if self.explicit_cli and self.cli:
            return self.cli
        lib = self.library
        setting = lib.settings.cli if lib is not None else ""
        if os.path.isabs(setting) and is_executable(setting):
            return setting
        return find_cli(lib.abspath(source))

    def start_index(self, source_ids: list[str] | None = None) -> bool:
        """Index the given sources (all if None) in the background. False if a job is running or
        no library is open. Unavailable sources are logged and skipped."""
        lib, store = self.library, self.store
        if lib is None or store is None:
            return False
        sources = list(lib.sources) if source_ids is None else [lib.get(sid) for sid in source_ids]

        def index(job: Job) -> None:
            try:
                for src in sources:
                    if not self.available(src):
                        job.log(f"{src.name}: nicht erreichbar")
                        continue
                    try:  # one broken source must not stop the others
                        result = store.sync_source(src.id, self.mail_dir_for(src), job.progress)
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        job.log(f"{src.name}: Fehler: {exc}")
                        continue
                    job.log(f"{src.name}: +{result.added} ~{result.updated} -{result.removed}")
            finally:
                store.close()  # the job thread's connection would otherwise be left unclosed

        return self.job.start("index", index)

    def start_fetch(self, source_id: str, email: str, password: str, mailbox: str, totp: str) -> None:
        """Fetch fresh mail for one proton source as the job. Raises ValueError (bad request),
        FetchUnavailable (no CLI), or Busy while another job runs. Secrets are passed on, never stored.
        Checked and started under _lock, like open_library, so no job can start in between."""
        with self._lock:
            lib, store = self.library, self.store
            if lib is None or store is None:
                raise Busy("no library chosen")
            try:
                src = lib.get(source_id)
            except KeyError as exc:
                raise ValueError("unknown source") from exc
            if src.kind != "proton":
                raise ValueError("not a proton source")
            cli = self.cli_for(src)
            if cli is None:
                raise FetchUnavailable("fetch not available")
            account = account_dir_of(lib.abspath(src))
            email = email or lib.settings.proton_email or account_email(account)
            if not email:
                raise ValueError("missing email")
            check_email(email)

            def fetch(job: Job) -> None:
                try:
                    self._fetch(job, store, src, cli, account, email, password, mailbox, totp)
                finally:
                    store.close()  # the job thread's connection, as in start_index

            if not self.job.start("fetch", fetch):
                raise Busy("job running")

    def start_import(self, path: str, name: str) -> None:
        """Import a PST or OST file as a new source of kind outlook, as the job. The file is only read.
        Raises ValueError (bad request), Busy (no library, or a job runs), or ImportUnavailable (no pypff).
        Checked and started under _lock, like start_fetch."""
        real = import_path(path)
        name = check_text(name, "name").strip()
        if not name:
            raise ValueError("missing name")
        with self._lock:
            lib, store = self.library, self.store
            if lib is None or store is None:
                raise Busy("no library chosen")
            if self.job.running():
                raise Busy("job running")
            if not pypff_available():
                raise ImportUnavailable(INSTALL_HINT)
            sources = os.path.join(lib.root, SOURCES_DIR)
            os.makedirs(sources, exist_ok=True)
            for entry in os.listdir(sources):  # left behind by an interrupted import; no job runs now
                if entry.startswith(IMPORT_TMP_PREFIX):
                    remove_tree(os.path.join(sources, entry))

            def import_job(job: Job) -> None:
                try:
                    self._import(job, store, lib, sources, real, name)
                finally:
                    store.close()  # the job thread's connection, as in start_index

            if not self.job.start("import", import_job):
                raise Busy("job running")

    def _import(self, job: Job, store: Store, lib: Library, sources: str, path: str, name: str) -> None:
        """Convert the file into a temp dir below sources, move it to a free sources/<slug> and add it as
        a source. If the source is not added, both dirs are removed, so a failure leaves nothing behind."""
        tmp: str | None = None
        final: str | None = None
        added = False
        reader = None
        try:
            tmp = tempfile.mkdtemp(prefix=IMPORT_TMP_PREFIX, dir=sources)
            reader = open_reader(path)
            report = convert(reader, tmp, job.progress)
            for line in import_log_lines(report):
                job.log(line)
            if report.imported == 0:
                raise RuntimeError(NO_MAIL_HINT)
            final = os.path.join(sources, free_name(sources, import_slug(name)))
            os.rename(tmp, final)
            tmp = None  # moved; final is removed below if the source is not added
            with self._lock:
                src = lib.add_source(final, name, kind="outlook")
            added = True
            try:
                result = store.sync_source(src.id, final, job.progress)
            except Exception as exc:  # the files are complete; the next index run picks them up
                job.log(f"{src.name}: Fehler: {exc}")
            else:
                job.log(f"{src.name}: +{result.added} ~{result.updated} -{result.removed}")
            job.set_result({"source": src.id, **asdict(report)})
        finally:
            if reader is not None:
                reader.close()
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            if final is not None and not added:
                shutil.rmtree(final, ignore_errors=True)

    def check_cleanup(self, target: str, reference: str) -> tuple[Source, Source]:
        """Check the two sources of a cleanup. The preview and the run both use it, so they refuse the same
        pairs. KeyError for unknown ids (404), ValueError for unsuitable ones (400), Busy without a library."""
        lib = self.library
        if lib is None or self.store is None:
            raise Busy("no library chosen")
        if not target or not reference:
            raise ValueError("Ziel und Referenz fehlen.")
        tgt, ref = lib.get(target), lib.get(reference)
        if target == reference:
            raise ValueError("Ziel und Referenz müssen verschiedene Quellen sein.")
        if tgt.kind != "outlook":
            raise ValueError("Nur importierte Outlook-Quellen können aufgeräumt werden.")
        if ref.hidden:
            raise ValueError(f"Die Referenz-Quelle {ref.name} ist ausgeblendet.")
        for src in (tgt, ref):
            if not self.available(src):
                raise ValueError(f"Die Quelle {src.name} ist nicht erreichbar.")
        return tgt, ref

    def start_cleanup(self, target: str, reference: str, action: str) -> None:
        """Replace or delete the copies in target that reference holds, as the job, then reindex target.
        Raises KeyError (unknown source), ValueError (bad pair, or a hardlink across file systems), or Busy.
        Checked and started under _lock, like start_fetch."""
        with self._lock:
            tgt, ref = self.check_cleanup(target, reference)
            lib, store = self.library, self.store  # not None: check_cleanup raised Busy otherwise
            tgt_dir, ref_dir = self.mail_dir_for(tgt), self.mail_dir_for(ref)
            link_possible, reason = check_dirs(tgt_dir, ref_dir)  # ValueError for the same folder
            if action == "link" and not link_possible:
                raise ValueError(reason)

            def cleanup(job: Job) -> None:
                try:
                    self._cleanup(job, store, lib, tgt, ref, tgt_dir, ref_dir, action)
                finally:
                    store.close()  # the job thread's connection, as in start_index

            if not self.job.start("cleanup", cleanup):
                raise Busy("job running")

    def _cleanup(self, job: Job, store: Store, lib: Library, tgt: Source, ref: Source,
                 tgt_dir: str, ref_dir: str, action: str) -> None:
        """Run the cleanup with its log in the library, then reindex the target. The reindex also runs
        after an error, so the index never keeps rows for files that were removed."""
        name = f"cleanup-{time.strftime('%Y%m%d-%H%M%S')}.log"
        with open(os.path.join(lib.root, name), "x", encoding="utf-8") as f:  # never overwrites a log
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\nZiel: {tgt.name}\n"
                    f"Referenz: {ref.name}\nAktion: {action}\n")

            def log(line: str) -> None:
                job.log(line)
                f.write(line + "\n")

            try:
                result = run_cleanup(store.duplicate_pairs(tgt.id, ref.id), tgt_dir, ref_dir, action, log)
                job.set_result({**asdict(result), "log_file": name})
            finally:
                sync = store.sync_source(tgt.id, tgt_dir, job.progress)
                job.log(f"{tgt.name}: +{sync.added} ~{sync.updated} -{sync.removed}")

    def _fetch(self, job: Job, store: Store, src: Source, cli: str, account: str, email: str,
               password: str, mailbox: str, totp: str) -> None:
        """Run the CLI into a temp dir below the account dir, then move the export in, link unchanged
        mails to the previous export and index it. Any failure leaves the old index and exports as they are."""
        secrets = [s for s in (password, mailbox, totp) if s]
        try:
            previous = self.mail_dir_for(src)
        except ValueError:  # no export yet
            previous = None
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix=".fetch-", dir=account)
            code = self._run_cli(cli, cli_env(email, tmp, password, mailbox, totp), secrets)
            if code != 0:
                raise RuntimeError(f"CLI exited with code {code}")
            found = newest_fetched(tmp)
            if found is None:
                raise RuntimeError("CLI produced no mail export")
            dest = os.path.join(account, free_name(account, os.path.basename(found)))
            os.rename(found, dest)
            self._link_previous(previous, dest)
            try:
                result = store.sync_source(src.id, dest, job.progress)
            except Exception:
                self._restore_index(job, store, src, previous, secrets)
                shutil.rmtree(dest, ignore_errors=True)  # only links and copies of the new export
                raise
            job.log(f"{src.name}: +{result.added} ~{result.updated} -{result.removed}")
            job.set_result({"source": src.id, "export": os.path.basename(dest)})
        except Exception as exc:  # the message may echo a secret
            raise RuntimeError(redact(str(exc) or type(exc).__name__, secrets)) from None
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def _restore_index(self, job: Job, store: Store, src: Source, previous: str | None,
                       secrets: list[str]) -> None:
        """A failed sync may have committed part of the new export: point the index back at the
        previous export, or drop the source's rows if there is none. Best effort, logged on failure."""
        try:
            if previous is None:
                store.drop_source(src.id)
            else:
                store.sync_source(src.id, previous)
        except Exception as exc:
            job.log(redact(f"{src.name}: Index konnte nicht wiederhergestellt werden: {exc}", secrets))

    def _run_cli(self, cli: str, env: dict[str, str], secrets: list[str]) -> int:
        # CLI 1.0.7 ignores ET_DIR for backups and would prompt for the path on stdin,
        # so pass it as --dir too (a path is no secret)
        proc = subprocess.Popen([cli, "--dir", env["ET_DIR"]], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        with proc:
            for line in proc.stdout:
                self.job.log(redact(line.rstrip("\r\n"), secrets))
            return proc.wait()

    def _link_previous(self, previous: str | None, dest: str) -> None:
        """Hardlink unchanged mails to the previous export. Failures only cost disk space, so never raise."""
        if previous is None or not os.path.isdir(previous):
            return
        try:
            linked, saved = dedupe_with_previous(previous, dest)
        except Exception as exc:
            self.job.log(f"Hardlinks fehlgeschlagen: {exc}")
            return
        if linked:
            self.job.log(f"Hardlinks: {linked} Dateien, {human_size(saved)} gespart")
        else:
            self.job.log("Hardlinks: keine identischen Dateien")


def selected_sources(lib: Library, query: dict[str, list[str]]) -> list[str] | None:
    """Ids from ?sources=s1,s2; without the parameter all visible sources. None if an id is unknown."""
    if "sources" not in query:
        return [s.id for s in lib.sources if not s.hidden]
    ids = [x for x in _arg(query, "sources").split(",") if x]
    known = {s.id for s in lib.sources}
    return None if any(x not in known for x in ids) else ids


def source_list(viewer: Viewer, lib: Library, store: Store) -> list[Json]:
    """All sources in library order, hidden ones included, with their index counts."""
    stats = store.source_stats([s.id for s in lib.sources])
    out = []
    for src in lib.sources:
        count, unread = stats[src.id]
        out.append({
            "id": src.id, "name": src.name, "kind": src.kind, "icon": src.icon, "hidden": src.hidden,
            "available": viewer.available(src),
            "fetchable": src.kind == "proton" and viewer.cli_for(src) is not None,
            "count": count, "unread": unread,
        })
    return out


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


LIBRARY_ROUTES = ("/api/sources", "/api/folders", "/api/messages", "/api/tags", "/api/cleanup/preview")


def make_handler(viewer: Viewer, static_dir: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MailViewer/1.0"
        sys_version = ""

        def finish(self) -> None:
            try:
                super().finish()
            finally:
                # Request threads come and go; their SQLite connection is closed with the request
                if viewer.store is not None:
                    viewer.store.close()
                if viewer.tags is not None:
                    viewer.tags.close()

        def do_GET(self) -> None:
            if self.headers.get("Host") not in _hosts(self.server.server_port):  # DNS-rebinding protection
                return self._json(403, {"error": "host not allowed"})
            url = urlsplit(self.path)
            if url.path == "/":
                return self._index_page()
            if url.path == "/api/config":
                return self._json(200, self._config())
            if url.path == "/api/browse":
                try:
                    query = parse_qs(url.query)
                    return self._json(200, browse(_arg(query, "path") or viewer.saved or "~",
                                                  files=_arg(query, "files") == "outlook"))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
            if url.path == "/api/job":
                return self._json(200, viewer.job.status())
            if url.path in LIBRARY_ROUTES or url.path.startswith("/api/message/") or ICON_RE.match(url.path):
                lib, store, tags = viewer.library, viewer.store, viewer.tags  # one snapshot, even on a switch
                if lib is None or store is None or tags is None:
                    return self._json(409, {"error": "no library chosen"})
                return self._library_get(lib, store, tags, url)
            self._json(404, {"error": "not found"})

        def _library_get(self, lib: Library, store: Store, tags: TagStore, url: Any) -> None:
            query = parse_qs(url.query)
            if m := ICON_RE.match(url.path):
                return self._icon(lib, unquote(m[1]))
            if url.path == "/api/sources":
                return self._json(200, source_list(viewer, lib, store))
            if url.path == "/api/tags":
                source_ids = selected_sources(lib, query)
                if source_ids is None:
                    return self._json(400, {"error": "unknown source"})
                return self._json(200, tag_list(store, tags, source_ids))
            if url.path == "/api/cleanup/preview":
                return self._cleanup_preview(store, query)
            if url.path in ("/api/folders", "/api/messages"):
                source_ids = selected_sources(lib, query)
                if source_ids is None:
                    return self._json(400, {"error": "unknown source"})
                merge = len(source_ids) > 1  # copies of one mail count once only when several sources are shown
                if url.path == "/api/folders":
                    return self._json(200, store.folders(source_ids, dedupe=merge))
                folder = _arg(query, "folder") or None
                needle = _arg(query, "q").strip().casefold()
                offset = max(0, _int(_arg(query, "offset"), 0))
                limit = min(MAX_LIMIT, max(1, _int(_arg(query, "limit"), 50)))
                tag = _arg(query, "tag") or None  # "star", a tag ID, or no filter
                try:
                    body = store.messages(source_ids, folder, needle, offset, limit, tag, dedupe=merge)
                except ValueError:
                    return self._json(400, {"error": "invalid tag"})
                return self._json(200, body)
            if m := ATT_RE.match(url.path):
                return self._attachment(store, unquote(m[1]), unquote(m[2]), int(m[3]))
            if m := MSG_RE.match(url.path):
                src, mid = unquote(m[1]), unquote(m[2])
                hit = store.message(src, mid)  # the id must be indexed, so no path escapes a mail dir
                if hit is None:
                    return self._json(404, {"error": "unknown message"})
                ident = store.ident(src, mid) or ""  # "" if the .eml is missing: no marks then
                mark = tags.marks([ident])[ident]
                names = {s.id: s.name for s in lib.sources}
                copies = [c | {"name": names[c["source"]]}
                          for c in store.copies(ident, selected_sources(lib, {}) or [])]
                return self._json(200, load_message(hit[0], mid, hit[1]) | {
                    "starred": mark["starred"], "tags": mark["tags"], "markable": ident != "",
                    "copies": copies})
            self._json(404, {"error": "not found"})

        def _cleanup_preview(self, store: Store, query: dict[str, list[str]]) -> None:
            """What a cleanup would do, from the index and the files. Changes nothing."""
            try:
                tgt, ref = viewer.check_cleanup(_arg(query, "target"), _arg(query, "reference"))
                plan = plan_cleanup(store.duplicate_pairs(tgt.id, ref.id),
                                    viewer.mail_dir_for(tgt), viewer.mail_dir_for(ref))
            except Busy as exc:
                return self._json(409, {"error": str(exc)})
            except KeyError:
                return self._json(404, {"error": "unknown source"})
            except (ValueError, OSError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, asdict(plan) | {"target": tgt.id, "reference": ref.id})

        def _attachment(self, store: Store, src: str, mid: str, n: int) -> None:
            hit = store.message(src, mid)
            part = get_attachment(hit[0], mid, n) if hit is not None else None
            if part is None:
                return self._json(404, {"error": "no such attachment"})
            name = quote(attachment_name(part, n), safe="")
            self._send(200, attachment_payload(part), part.get_content_type(),
                       (("Content-Disposition", f"attachment; filename*=UTF-8''{name}"),))

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            m = SOURCE_RE.match(path)
            t = TAG_RE.match(path)
            mk = MARKS_RE.match(path)
            limit = MAX_ICON if m is not None and m[2] == "icon" else MAX_BODY
            raw = self._read_body(limit)  # read first, so refused requests leave no unread body behind
            port = self.server.server_port
            if self.headers.get("Host") not in _hosts(port):
                return self._json(403, {"error": "host not allowed"})
            if self.headers.get("Origin") not in _origins(port):  # a missing Origin is refused too
                return self._json(403, {"error": "origin not allowed"})
            if m is None and t is None and mk is None and path not in POST_ROUTES:
                return self._json(404, {"error": "not found"})
            if len(raw) > limit:
                return self._json(400, {"error": "body too large"})
            if t is not None:
                if t[2] == "remove":
                    return self._tags_admin(lambda tags: tags.delete_tag(int(t[1])))
                return self._tag_update(int(t[1]), raw)
            if mk is not None:
                return self._marks_update(unquote(mk[1]), unquote(mk[2]), raw)
            if m is None:
                if path == "/api/tags":
                    return self._tag_create(raw)
                if path == "/api/fetch":
                    return self._fetch(raw)
                if path == "/api/library":
                    return self._set_library(raw)
                if path == "/api/sources":
                    return self._add_source(raw)
                if path == "/api/import":
                    return self._import(raw)
                if path == "/api/cleanup":
                    return self._cleanup(raw)
                return self._settings(raw)
            source_id = unquote(m[1])
            if m[2] == "icon":
                return self._set_icon(source_id, raw)
            if m[2] == "remove":
                return self._remove_source(source_id)
            self._update_source(source_id, raw)

        def _admin(self, change: Callable[[], None], reply: Callable[[], Any]) -> None:
            """Run an admin change, then answer with reply(). Busy is 409, unknown ids 404, bad input 400."""
            try:
                change()
                body = reply()
            except Busy as exc:
                return self._json(409, {"error": str(exc)})
            except KeyError:
                return self._json(404, {"error": "unknown source"})
            except sqlite3.Error as exc:
                return self._json(500, {"error": f"index error: {exc}"})
            except (ValueError, OSError) as exc:  # LibraryError included
                return self._json(400, {"error": str(exc)})
            self._json(200, body)

        def _tags_admin(self, change: Callable[[TagStore], Any]) -> None:
            """Run a tag change without Viewer._lock and without a job check, then answer with the tag
            list of all visible sources. KeyError is 404, ValueError 400, sqlite3 errors 500; no library is 409."""
            lib, store, tags = viewer.library, viewer.store, viewer.tags  # one snapshot for the whole request
            if lib is None or store is None or tags is None:
                return self._json(409, {"error": "no library chosen"})
            try:
                change(tags)
                reply = tag_list(store, tags, selected_sources(lib, {}))
            except KeyError:
                return self._json(404, {"error": "unknown tag"})
            except sqlite3.Error as exc:
                return self._json(500, {"error": f"index error: {exc}"})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, reply)

        def _tag_create(self, raw: bytes) -> None:
            def change(tags: TagStore) -> None:
                body = load_json_object(raw)
                only_keys(body, TAG_FIELDS)
                name = check_tag_name(_text(body, "name"))
                tags.create_tag(name, tag_color(body.get("color"), name))
            self._tags_admin(change)

        def _tag_update(self, tag_id: int, raw: bytes) -> None:
            def change(tags: TagStore) -> None:
                body = load_json_object(raw)
                only_keys(body, TAG_FIELDS)
                name = check_tag_name(_text(body, "name")) if "name" in body else None
                color = check_tag_color(body["color"]) if "color" in body else None
                tags.update_tag(tag_id, name, color)
            self._tags_admin(change)

        def _marks_update(self, src: str, mid: str, raw: bytes) -> None:
            """Set a mail's star and tags by its identity. Like the tag routes: no Viewer._lock, no job check."""
            lib, store, tags = viewer.library, viewer.store, viewer.tags  # one snapshot for the whole request
            if lib is None or store is None or tags is None:
                return self._json(409, {"error": "no library chosen"})
            try:
                body = load_json_object(raw)
                only_keys(body, MARK_FIELDS)
                starred = body.get("starred")
                if "starred" in body and not isinstance(starred, bool):
                    raise ValueError("Das Feld starred muss true oder false sein.")
                tag_ids = body.get("tags")
                if "tags" in body and not (isinstance(tag_ids, list) and all(type(x) is int for x in tag_ids)):
                    raise ValueError("Das Feld tags muss eine Liste von Tag-IDs sein.")
                ident = store.ident(src, mid)  # None for unknown messages and unknown sources
                if ident is None:
                    return self._json(404, {"error": "unknown message"})
                if ident == "":
                    raise ValueError("Diese Mail kann nicht markiert werden, die .eml-Datei fehlt.")
                reply = tags.set_marks(ident, starred, tag_ids)
            except KeyError:
                return self._json(404, {"error": "unknown tag"})
            except sqlite3.Error as exc:
                return self._json(500, {"error": f"index error: {exc}"})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, reply)

        def _sources_reply(self) -> list[Json]:
            lib, store = viewer.library, viewer.store
            if lib is None or store is None:
                raise Busy("no library chosen")
            return source_list(viewer, lib, store)

        def _add_source(self, raw: bytes) -> None:
            def change() -> None:
                body = load_json_object(raw)
                path = body.get("path")
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("path required")
                viewer.add_source(path.strip(), check_text(_text(body, "name"), "name"))
            self._admin(change, self._sources_reply)

        def _update_source(self, source_id: str, raw: bytes) -> None:
            def change() -> None:
                body = load_json_object(raw)
                only_keys(body, SOURCE_FIELDS)
                icon = body.get("icon")
                if isinstance(icon, dict) and icon.get("type") == "image":
                    raise ValueError("Bild-Icons werden über den Upload gesetzt.")
                position = body.get("position")
                if "position" in body and type(position) is not int:
                    raise ValueError("position muss eine ganze Zahl sein.")
                viewer.update_source(source_id, name=body.get("name"), icon=icon,
                                     hidden=body.get("hidden"), position=position)
            self._admin(change, self._sources_reply)

        def _remove_source(self, source_id: str) -> None:
            self._admin(lambda: viewer.remove_source(source_id), self._sources_reply)

        def _set_icon(self, source_id: str, raw: bytes) -> None:
            ext = ICON_TYPES.get(self.headers.get("Content-Type") or "")
            if ext is None:
                return self._json(400, {"error": "Das Icon muss PNG oder SVG sein."})
            if not valid_icon(ext, raw):
                return self._json(400, {"error": "invalid image"})
            self._admin(lambda: viewer.set_icon(source_id, ext, raw), self._sources_reply)

        def _settings(self, raw: bytes) -> None:
            def change() -> None:
                body = load_json_object(raw)
                only_keys(body, SETTINGS_FIELDS)
                viewer.update_settings(body)
            self._admin(change, self._config)

        def _icon(self, lib: Library, source_id: str) -> None:
            """Serve the stored icon only. The file must be <id>.png|svg and resolve directly inside icons/."""
            try:
                src = lib.get(source_id)
            except KeyError:
                return self._json(404, {"error": "unknown source"})
            if src.icon.get("type") != "image":
                return self._json(404, {"error": "no image icon"})
            ctype = {f"{src.id}.{ext}": ct for ext, ct in ICON_CTYPES.items()}.get(src.icon.get("file"))
            if ctype is None:
                return self._json(404, {"error": "no image icon"})
            icons = os.path.realpath(os.path.join(lib.root, ICON_DIR))
            path = os.path.realpath(os.path.join(icons, src.icon["file"]))
            if os.path.dirname(path) != icons or not os.path.isfile(path):  # e.g. a symlink out of icons/
                return self._json(404, {"error": "no image icon"})
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, {"error": "no image icon"})
            self._send(200, body, ctype, (("Content-Security-Policy", ICON_CSP),))

        def _fetch(self, raw: bytes) -> None:
            if viewer.library is None:
                return self._json(409, {"error": "no library chosen"})
            try:
                source, email, password, mailbox, totp = parse_fetch_body(raw)
                viewer.start_fetch(source, email, password, mailbox, totp)
            except Busy as exc:
                return self._json(409, {"error": str(exc)})
            except FetchUnavailable as exc:
                return self._json(404, {"error": str(exc)})
            except ValueError as exc:  # LibraryError included
                return self._json(400, {"error": str(exc)})
            self._json(202, {"state": "running"})

        def _import(self, raw: bytes) -> None:
            try:
                body = load_json_object(raw)
                only_keys(body, IMPORT_FIELDS)
                path = _text(body, "path").strip()
                if not path:
                    raise ValueError("path required")
                viewer.start_import(path, _text(body, "name"))
            except Busy as exc:
                return self._json(409, {"error": str(exc)})
            except ImportUnavailable:
                return self._json(503, {"error": INSTALL_HINT})
            except (ValueError, OSError) as exc:  # LibraryError included
                return self._json(400, {"error": str(exc)})
            self._json(200, {"started": True})

        def _cleanup(self, raw: bytes) -> None:
            try:
                body = load_json_object(raw)
                only_keys(body, CLEANUP_FIELDS)
                target, reference = _text(body, "target"), _text(body, "reference")
                if body.get("action") not in ACTIONS:
                    raise ValueError("Die Aktion muss link oder delete sein.")
                viewer.start_cleanup(target, reference, body["action"])
            except Busy as exc:
                return self._json(409, {"error": str(exc)})
            except KeyError:
                return self._json(404, {"error": "unknown source"})
            except (ValueError, OSError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(202, {"state": "running"})

        def _set_library(self, raw: bytes) -> None:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):  # invalid UTF-8 and nesting too deep included
                return self._json(400, {"error": "invalid JSON body"})
            try:
                path = data["path"]
                if not isinstance(path, str) or not path.strip():
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "path required"})
            try:
                # an absent "remember" leaves the saved choice alone
                remember = bool(data["remember"]) if "remember" in data else None
                viewer.open_library(path.strip(), create=bool(data.get("create")),
                                    remember=remember,
                                    add_legacy=bool(data.get("add_legacy")))
            except Busy:
                return self._json(409, {"error": "index job running"})
            except (ValueError, OSError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, self._config())

        def _read_body(self, limit: int = MAX_BODY) -> bytes:
            """Drain the declared request body, keeping at most limit + 1 bytes."""
            left = max(0, _int(self.headers.get("Content-Length") or "", 0))
            kept = b""
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
                kept = (kept + chunk)[: limit + 1]
            return kept

        def _config(self) -> Json:
            lib = viewer.library
            return {
                "configured": lib is not None,
                "library": lib.root if lib is not None else None,
                "saved": viewer.saved,
                "saved_error": viewer.saved_error,
                "legacy": load_legacy(viewer.config_path) if viewer.config_path else None,
                "cli_override": viewer.cli if viewer.explicit_cli else None,  # --cli wins over the setting
                "home": os.path.expanduser("~"),
                "settings": asdict(lib.settings) if lib is not None else None,
                "outlook_import": pypff_available(),
                "install_hint": INSTALL_HINT,
            }

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


def make_server(library_dir: str | None = None, port: int = 0, static_dir: str = HERE,
                cli: str | None = None, config_path: str | None = None) -> ThreadingHTTPServer:
    """Bind 127.0.0.1:port. cli=None auto-detects the export CLI per source (see Viewer.cli_for).

    An explicit library_dir must hold a library (LibraryError otherwise). Without it the library
    saved in config_path is opened; if that fails or none is saved, the viewer starts empty and
    the page asks for a library folder."""
    config_path = config_path or default_config_path()
    explicit_cli = cli is not None
    cli_path = os.path.abspath(cli) if cli is not None else None
    if cli_path is not None and not is_executable(cli_path):
        cli_path = None  # fetching is simply unavailable
    viewer = Viewer(cli_path, explicit_cli, config_path)
    if library_dir is not None:
        viewer.open_library(library_dir)
    elif viewer.saved:
        try:
            viewer.open_library(viewer.saved)
        except (ValueError, OSError) as exc:
            viewer.saved_error = f"{viewer.saved}: {exc}"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(viewer, static_dir))
    httpd.viewer = viewer  # the state, for tests and tooling
    return httpd


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read-only local viewer for a library of mail exports.")
    parser.add_argument("library_dir", nargs="?",
                        help="library folder with library.json (default: the remembered library)")
    parser.add_argument("--port", type=int, default=8025)
    parser.add_argument("--cli", help="path to proton-mail-export-cli (default: look near each source)")
    parser.add_argument("--config", help=f"config file (default: {default_config_path()})")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.library_dir, args.port, cli=args.cli, config_path=args.config)
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
