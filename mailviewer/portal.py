"""Paths behind the Flatpak document portal.

In the Flatpak the app sees only folders and files picked in the system dialog, under
/run/user/<uid>/doc/<id>/<name>. That FUSE file system takes hardlinks and renames, but not the
shared memory of SQLite's WAL mode, and its paths mean nothing to the user.
"""

from __future__ import annotations

import ast
import functools
import re
import sqlite3
import subprocess

DOC_RE = re.compile(r"^/run/(?:user/\d+|flatpak)/doc/([^/]+)/([^/]+)(/.*)?$")
BYTES_RE = re.compile(r"""b'(?:[^'\\]|\\.)*'|b"(?:[^"\\]|\\.)*\"""")
GDBUS_TIMEOUT_S = 5


def is_portal_path(path: str) -> bool:
    return DOC_RE.match(path) is not None


def journal_mode(db_path: str) -> str:
    """WAL needs shared memory next to the database, which the document portal does not offer."""
    return "DELETE" if is_portal_path(db_path) else "WAL"


def leave_wal(db_path: str) -> None:
    """A database made in WAL mode outside the Flatpak cannot be opened on a portal path. In exclusive locking
    mode SQLite keeps the WAL index in memory, so the file can be switched to a rollback journal once."""
    if not is_portal_path(db_path):
        return
    try:
        with open(db_path, "rb") as f:
            header = f.read(20)
    except OSError:
        return  # not there yet: it is created in the right mode
    if len(header) < 20 or header[18] != 2:  # file format version 2 = WAL
        return
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA locking_mode = EXCLUSIVE").fetchone()
        con.execute("PRAGMA journal_mode = DELETE").fetchone()
    finally:
        con.close()


def host_path(path: str) -> str:
    """The path on the host for display; the path itself if it is no portal path or the portal cannot tell."""
    m = DOC_RE.match(path)
    if not m:
        return path
    root = _host_root(m[1])
    return root + (m[3] or "") if root else path


@functools.lru_cache(maxsize=256)
def _host_root(doc_id: str) -> str | None:
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Documents",
             "--object-path", "/org/freedesktop/portal/documents",
             "--method", "org.freedesktop.portal.Documents.GetHostPaths", f"['{doc_id}']"],
            capture_output=True, text=True, timeout=GDBUS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    m = BYTES_RE.search(out.stdout) if out.returncode == 0 else None
    if not m:
        return None
    try:
        raw: bytes = ast.literal_eval(m[0])
    except (ValueError, SyntaxError):
        return None
    return raw.rstrip(b"\0").decode("utf-8", "surrogateescape") or None
