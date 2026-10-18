"""Proton export directories, the export CLI and its fetch environment."""

from __future__ import annotations

import filecmp
import json
import os
from urllib.parse import quote

from mailviewer.cleanup import is_regular, remove_quiet
from mailviewer.store import META_SUFFIX

CLI_NAME = "proton-mail-export-cli"


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
