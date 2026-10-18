"""Checks for request bodies and their fields."""

from __future__ import annotations

import json
import re
import zlib
from typing import Any
from mailviewer.i18n import UserError

Json = dict[str, Any]

MAX_BODY = 64 * 1024
MAX_ICON = 512 * 1024
MAX_TRANSFER = 4 * 1024 * 1024  # up to 5000 items of ids
MAX_TRANSFER_ITEMS = 5000
MAX_ID = 512
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_TAG_NAME = 40
TAG_COLORS = ["#0a66c2", "#2e7d32", "#7b3fb0", "#b03a2e", "#b35c00", "#00796b", "#4a5568", "#8a6d00"]
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def text_field(body: Json, key: str) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def opt_text(body: Json, key: str) -> str | None:
    """None for an absent or null field, else the checked string."""
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return check_text(value, key)


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


def check_id(value: str, what: str) -> str:
    """A source or mail ID from a request: a non-empty string of at most MAX_ID characters."""
    check_text(value, what)
    if not 1 <= len(value) <= MAX_ID:
        raise UserError("err.invalid_id", what=what)
    return value


def check_tag_name(value: str) -> str:
    """Trimmed, 1 to MAX_TAG_NAME characters, no control characters. Uniqueness is checked by TagStore."""
    check_text(value, "Tag-Name")
    name = value.strip()
    if not 1 <= len(name) <= MAX_TAG_NAME:
        raise UserError("err.tag_name_length", max=MAX_TAG_NAME)
    return name


def check_tag_color(value: str | None) -> str:
    """Exactly #rrggbb; stored in lowercase."""
    if not isinstance(value, str) or not COLOR_RE.match(value):
        raise UserError("err.tag_color")
    return value.lower()


def tag_color(value: str | None, name: str) -> str:
    """The given color, or for None a default picked from TAG_COLORS by a stable hash of the folded name."""
    if value is None:
        return TAG_COLORS[zlib.crc32(name.casefold().encode("utf-8")) % len(TAG_COLORS)]
    return check_tag_color(value)


def parse_fetch_body(raw: bytes) -> tuple[str, str, str, str, str]:
    """Return (source, email, password, mailbox_password, totp). An empty email means the source's
    default, which only the viewer knows. ValueError messages are shown to the client."""
    body = load_json_object(raw)
    source = text_field(body, "source").strip()
    if not source:
        raise ValueError("missing source")
    password = check_text(text_field(body, "password"), "password")
    if not password:
        raise UserError("err.missing_password")
    email = check_email(text_field(body, "email"))
    mailbox = check_text(text_field(body, "mailbox_password"), "mailbox_password")
    return source, email, password, mailbox, check_text(text_field(body, "totp"), "totp").strip()


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
        raise UserError("err.unknown_field", names=", ".join(unknown))


def valid_icon(ext: str, data: bytes) -> bool:
    """Cheap content check before an upload is stored: a PNG signature, or SVG-looking UTF-8 text."""
    if ext == "png":
        return data.startswith(PNG_SIGNATURE)
    try:
        text = data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return False
    return text.startswith("<") and "<svg" in text
