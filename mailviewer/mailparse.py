"""Reading one .eml: body, inline images and attachments, and how attachments are previewed."""

from __future__ import annotations

import base64
import os
import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from typing import Any
from urllib.parse import quote, unquote

from mailviewer.store import addr, addr_list, to_int

Json = dict[str, Any]

CID_RE = re.compile(r"cid:([^\s\"'<>()]+)")
# Images get a sandbox CSP. PDFs and audio get none: the browser's PDF viewer and media player do not work in a
# sandboxed document, and neither type runs code of its own on our origin
PREVIEW_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "audio/mpeg"})
PREVIEW_ALIASES = {"audio/mp3": "audio/mpeg"}  # non-standard type some mailers write
IMAGE_PREVIEW_CSP = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"


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


def preview_type(ctype: str) -> str | None:
    """The type an attachment is shown inline with, or None if it is only downloaded."""
    ctype = PREVIEW_ALIASES.get(ctype, ctype)
    return ctype if ctype in PREVIEW_TYPES else None


def attachment_headers(ctype: str, name: str, inline: bool) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Content-Disposition and extra headers. Only types the browser shows without running the file's own code
    are served inline; anything else (SVG, HTML, ...) stays a download, since it would run on our origin."""
    quoted = quote(name, safe="")
    if inline and ctype in PREVIEW_TYPES:
        extra = (("Content-Security-Policy", IMAGE_PREVIEW_CSP),) if ctype.startswith("image/") else ()
        return f"inline; filename*=UTF-8''{quoted}", extra
    return f"attachment; filename*=UTF-8''{quoted}", ()


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
