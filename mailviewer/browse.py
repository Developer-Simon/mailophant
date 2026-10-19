"""Directory listing behind the folder picker."""

from __future__ import annotations

import os
from typing import Any

from mailviewer import portal
from mailviewer.exports import mail_dirs
from mailviewer.library import is_library

Json = dict[str, Any]

MAX_BROWSE_ENTRIES = 2000


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
        "shown": portal.host_path(p),  # the host path behind a Flatpak portal path
        "parent": None if p == os.path.dirname(p) else os.path.dirname(p),
        "home": os.path.expanduser("~"),
        "openable": is_openable(p),
        "library": is_library(p),
        "dirs": dirs,
    }
    if files:
        result["files"] = found
    return result
