#!/usr/bin/env python3
"""Read-only local web viewer for a Proton Mail export directory (stdlib only).

Usage: python server.py EXPORT_DIR [--port 8025]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
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


def _arg(query: dict[str, list[str]], key: str) -> str:
    return query.get(key, [""])[0]


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def make_handler(index: Index, static_dir: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MailViewer/1.0"
        sys_version = ""

        def do_GET(self) -> None:
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed:  # DNS-rebinding protection
                return self._json(403, {"error": "host not allowed"})
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                return self._index_page()
            if url.path == "/api/folders":
                return self._json(200, list_folders(index))
            if url.path == "/api/messages":
                return self._json(200, self._list(query))
            if m := ATT_RE.match(url.path):
                return self._attachment(unquote(m[1]), int(m[2]))
            if m := MSG_RE.match(url.path):
                mid = unquote(m[1])
                if mid not in index.messages:
                    return self._json(404, {"error": "unknown message"})
                return self._json(200, load_message(index, mid))
            self._json(404, {"error": "not found"})

        def _list(self, query: dict[str, list[str]]) -> Json:
            folder = _arg(query, "folder") or None
            needle = _arg(query, "q").strip().casefold()
            offset = max(0, _int(_arg(query, "offset"), 0))
            limit = min(MAX_LIMIT, max(1, _int(_arg(query, "limit"), 50)))
            return list_messages(index, folder, needle, offset, limit)

        def _attachment(self, mid: str, n: int) -> None:
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


def make_server(export_dir: str, port: int = 0, static_dir: str = HERE) -> ThreadingHTTPServer:
    """Build the index and bind 127.0.0.1:port. Read server.server_port for the bound port."""
    index = build_index(export_dir)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(index, static_dir))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read-only local viewer for a mail export.")
    parser.add_argument("export_dir")
    parser.add_argument("--port", type=int, default=8025)
    args = parser.parse_args(argv)
    try:
        server = make_server(args.export_dir, args.port)
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
