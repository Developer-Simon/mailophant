"""SQLite index over all library sources (archive.db).

The DB is a cache: it can be deleted and rebuilt from the sources at any time. Mail files are only read.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any, Callable

META_SUFFIX = ".metadata.json"
SCHEMA_VERSION = 2  # 2: FTS columns hold casefolded text
COMMIT_EVERY = 1000
SYSTEM_FOLDER_ORDER = ["0", "7", "5", "4", "3", "6", "8"]

Json = dict[str, Any]

SCHEMA = """
CREATE TABLE sources (id TEXT PRIMARY KEY, mail_dir TEXT NOT NULL);
CREATE TABLE messages (source_id TEXT, mid TEXT, stat TEXT NOT NULL, ident TEXT NOT NULL,
  time INTEGER NOT NULL, unread INTEGER NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY (source_id, mid));
CREATE INDEX messages_time ON messages (time DESC, mid);
CREATE INDEX messages_ident ON messages (ident);
CREATE TABLE labels (source_id TEXT, label_id TEXT, name TEXT, path TEXT, color TEXT, type INTEGER,
  PRIMARY KEY (source_id, label_id));
CREATE TABLE message_labels (source_id TEXT, mid TEXT, label_id TEXT, PRIMARY KEY (source_id, mid, label_id));
CREATE VIRTUAL TABLE messages_fts USING fts5(source_id UNINDEXED, mid UNINDEXED, subject, people,
  tokenize = 'trigram case_sensitive 0');
"""


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0  # broken metadata files; counted on every sync because they are never stored


def mail_ident(eml_path: str) -> str:
    """Identity for spotting the same mail in several sources; "" if the .eml is missing."""
    try:
        with open(eml_path, "rb") as f:
            try:
                msg = BytesParser(policy=policy.default).parse(f, headersonly=True)
                mid = str(msg["Message-ID"] or "").strip().strip("<>").strip().lower()
            except Exception:  # odd headers must not break indexing; fall back to the content hash
                mid = ""
            if mid:
                return "mid:" + mid
            f.seek(0)
            h = hashlib.sha256()
            while chunk := f.read(1 << 20):
                h.update(chunk)
            return "sha256:" + h.hexdigest()
    except OSError:
        return ""


def read_labels(mail_dir: str) -> list[Json]:
    try:
        with open(os.path.join(mail_dir, "labels.json"), encoding="utf-8") as f:
            labels = json.load(f)["Payload"]
        return [x for x in labels if isinstance(x, dict) and x.get("ID")]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def people_text(meta: Json) -> str:
    parts = []
    for person in [meta.get("Sender"), *(meta.get("ToList") or [])]:
        person = person or {}
        parts += [person.get("Name") or "", person.get("Address") or ""]
    return " ".join(p for p in parts if p)


def addr(d: Any) -> Json:
    d = d or {}
    return {"name": d.get("Name") or "", "address": d.get("Address") or ""}


def addr_list(items: Any) -> list[Json]:
    return [addr(x) for x in items or []]


def summarize(mid: str, meta: Json) -> Json:
    return {
        "id": mid,
        "subject": meta.get("Subject") or "",
        "sender": addr(meta.get("Sender")),
        "to": addr_list(meta.get("ToList")),
        "time": to_int(meta.get("Time")),
        "unread": meta.get("Unread") == 1,
        "attachments": to_int(meta.get("NumAttachments")),
    }


def folder_key(label_id: str, path: str) -> str:
    """Same folder across sources: system folders by ID, user labels by path (case-insensitive)."""
    if label_id in SYSTEM_FOLDER_ORDER:
        return "sys:" + label_id
    return "path:" + path.casefold()


def folder_sort_key(folder: Json) -> tuple[int, int, str]:
    if folder["key"].startswith("sys:"):
        return (0, SYSTEM_FOLDER_ORDER.index(folder["key"][4:]), "")
    return (1, 0, folder["name"].casefold())


def casefold_sql(value: Any) -> Any:
    """Unicode casefolding for LIKE, which SQLite only folds for ASCII."""
    return value.casefold() if isinstance(value, str) else value


def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()  # one writer at a time; readers stay lock-free (WAL)
        con = self._conn()
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(db_path + suffix)
                except FileNotFoundError:
                    pass
            con = self._conn()
            con.executescript(SCHEMA)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            con.commit()

    def _conn(self) -> sqlite3.Connection:
        """Each thread gets its own connection, opened lazily."""
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = sqlite3.connect(self.db_path, timeout=30)
            con.create_function("casefold", 1, casefold_sql, deterministic=True)
            con.execute("PRAGMA journal_mode = WAL")
        return con

    def close(self) -> None:
        """Closes the calling thread's connection only; other threads' connections are not reachable."""
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    def mail_dir(self, source_id: str) -> str | None:
        row = self._conn().execute("SELECT mail_dir FROM sources WHERE id = ?", (source_id,)).fetchone()
        return row[0] if row else None

    def drop_source(self, source_id: str) -> None:
        con = self._conn()
        with self._write_lock:
            try:
                for table in ("messages_fts", "message_labels", "labels", "messages"):
                    con.execute(f"DELETE FROM {table} WHERE source_id = ?", (source_id,))
                con.execute("DELETE FROM sources WHERE id = ?", (source_id,))
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def sync_source(self, source_id: str, mail_dir: str,
                    progress: Callable[[int, int], None] | None = None) -> SyncResult:
        """Bring the index of one source in line with mail_dir; only new or changed metadata files are parsed."""
        con = self._conn()
        with self._write_lock:
            try:
                return self._sync(con, source_id, mail_dir, progress)
            except BaseException:
                con.rollback()  # leave no half-done transaction on this thread's connection
                raise

    def _sync(self, con: sqlite3.Connection, source_id: str, mail_dir: str,
              progress: Callable[[int, int], None] | None) -> SyncResult:
        known = dict(con.execute("SELECT mid, stat FROM messages WHERE source_id = ?", (source_id,)))
        on_disk: dict[str, str] = {}
        for name in sorted(os.listdir(mail_dir)):
            if name.endswith(META_SUFFIX):
                st = os.stat(os.path.join(mail_dir, name))
                on_disk[name[: -len(META_SUFFIX)]] = f"{st.st_mtime_ns}:{st.st_size}"

        removed = [mid for mid in known if mid not in on_disk]
        todo = [mid for mid, stat in on_disk.items() if known.get(mid) != stat]
        changed = [mid for mid in todo if mid in known]
        result = SyncResult(removed=len(removed))

        con.execute("INSERT INTO sources (id, mail_dir) VALUES (?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET mail_dir = excluded.mail_dir", (source_id, mail_dir))
        con.execute("DELETE FROM labels WHERE source_id = ?", (source_id,))
        con.executemany("INSERT INTO labels VALUES (?, ?, ?, ?, ?, ?)", [
            (source_id, str(x["ID"]), x.get("Name"), x.get("Path"), x.get("Color"), x.get("Type"))
            for x in read_labels(mail_dir)])
        stale = removed + changed
        if stale:
            con.execute("CREATE TEMP TABLE IF NOT EXISTS stale (mid TEXT PRIMARY KEY)")
            con.execute("DELETE FROM stale")
            con.executemany("INSERT INTO stale VALUES (?)", [(m,) for m in stale])
            for table in ("messages_fts", "message_labels", "messages"):
                con.execute(f"DELETE FROM {table} WHERE source_id = ? AND mid IN (SELECT mid FROM stale)",
                            (source_id,))

        total = len(todo)
        for done, mid in enumerate(todo, 1):
            name = mid + META_SUFFIX
            try:
                with open(os.path.join(mail_dir, name), encoding="utf-8") as f:
                    meta = json.load(f)["Payload"]
                if not isinstance(meta, dict):
                    raise ValueError("Payload is not an object")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"skipping {name}: {exc}", file=sys.stderr)
                result.skipped += 1
            else:
                if mid in known:
                    result.updated += 1
                else:
                    result.added += 1
                ident = mail_ident(os.path.join(mail_dir, mid + ".eml"))
                con.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", (
                    source_id, mid, on_disk[mid], ident, to_int(meta.get("Time")),
                    int(meta.get("Unread") == 1), json.dumps(meta, ensure_ascii=False)))
                con.executemany("INSERT OR IGNORE INTO message_labels VALUES (?, ?, ?)", [
                    (source_id, mid, str(label)) for label in meta.get("LabelIDs") or []])
                con.execute("INSERT INTO messages_fts (source_id, mid, subject, people) VALUES (?, ?, ?, ?)",
                            (source_id, mid, (meta.get("Subject") or "").casefold(), people_text(meta).casefold()))
            if done % COMMIT_EVERY == 0:
                con.commit()
            if progress:
                progress(done, total)
        con.commit()
        return result

    def folders(self, source_ids: list[str]) -> list[Json]:
        """Folders with at least one message, merged across sources by folder_key; counts are distinct messages."""
        if not source_ids:
            return []
        rank = {sid: i for i, sid in enumerate(dict.fromkeys(source_ids))}
        marks = ",".join("?" for _ in rank)
        rows = self._conn().execute(
            "SELECT ml.source_id, ml.mid, ml.label_id, l.name, l.path, l.color, m.unread "
            "FROM message_labels ml "
            "JOIN labels l ON l.source_id = ml.source_id AND l.label_id = ml.label_id "
            "JOIN messages m ON m.source_id = ml.source_id AND m.mid = ml.mid "
            f"WHERE ml.source_id IN ({marks})", list(rank)).fetchall()
        merged: dict[str, Json] = {}
        members: dict[str, set[tuple[str, str]]] = {}
        for sid, mid, label_id, name, path, color, unread in sorted(rows, key=lambda r: rank[r[0]]):
            key = folder_key(label_id, path or "")
            entry = merged.setdefault(key, {"key": key, "name": name or "", "path": path or "",
                                            "color": color or "", "count": 0, "unread": 0})
            seen = members.setdefault(key, set())
            if (sid, mid) in seen:  # two labels with the same folder key on one mail count once
                continue
            seen.add((sid, mid))
            entry["count"] += 1
            entry["unread"] += unread
        return sorted(merged.values(), key=folder_sort_key)

    def messages(self, source_ids: list[str], folder: str | None, needle: str, offset: int, limit: int) -> Json:
        """Newest first across sources; needle must already be casefolded."""
        if not source_ids:
            return {"total": 0, "messages": []}
        limit, offset = max(1, limit), max(0, offset)
        con = self._conn()
        sources = list(dict.fromkeys(source_ids))
        marks = ",".join("?" for _ in sources)
        where = [f"m.source_id IN ({marks})"]
        args: list[Any] = list(sources)
        if folder is not None:
            pairs = [(sid, lid) for sid, lid, path in con.execute(
                f"SELECT source_id, label_id, path FROM labels WHERE source_id IN ({marks})", sources)
                if folder_key(lid, path or "") == folder]
            if not pairs:
                return {"total": 0, "messages": []}
            values = ", ".join("(?, ?)" for _ in pairs)
            where.append("EXISTS (SELECT 1 FROM message_labels ml WHERE ml.source_id = m.source_id "
                         f"AND ml.mid = m.mid AND (ml.source_id, ml.label_id) IN (VALUES {values}))")
            args += [x for pair in pairs for x in pair]
        if len(needle) >= 3:  # as a phrase, so FTS operators and quotes in the input stay literal
            where.append("(m.source_id, m.mid) IN (SELECT source_id, mid FROM messages_fts WHERE messages_fts MATCH ?)")
            args.append('"' + needle.replace('"', '""') + '"')
        elif needle:
            like = "%" + escape_like(needle) + "%"
            where.append("(m.source_id, m.mid) IN (SELECT source_id, mid FROM messages_fts "
                         "WHERE casefold(subject) LIKE ? ESCAPE '\\' OR casefold(people) LIKE ? ESCAPE '\\')")
            args += [like, like]
        clause = " AND ".join(where)
        total = con.execute(f"SELECT COUNT(*) FROM messages m WHERE {clause}", args).fetchone()[0]
        rows = con.execute(
            f"SELECT m.source_id, m.mid, m.payload, m.unread FROM messages m WHERE {clause} "
            "ORDER BY m.time DESC, m.mid DESC, m.source_id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset]).fetchall()
        return {"total": total, "messages": [
            summarize(mid, json.loads(payload)) | {"source": sid, "unread": bool(unread)}
            for sid, mid, payload, unread in rows]}

    def source_stats(self, source_ids: list[str]) -> dict[str, tuple[int, int]]:
        """(message count, unread count) per source; ids without rows map to (0, 0)."""
        if not source_ids:
            return {}
        ids = list(dict.fromkeys(source_ids))
        marks = ",".join("?" for _ in ids)
        stats = {sid: (0, 0) for sid in ids}
        for sid, count, unread in self._conn().execute(
                f"SELECT source_id, COUNT(*), COALESCE(SUM(unread), 0) FROM messages "
                f"WHERE source_id IN ({marks}) GROUP BY source_id", ids):
            stats[sid] = (count, unread)
        return stats

    def message(self, source_id: str, mid: str) -> tuple[str, Json] | None:
        row = self._conn().execute(
            "SELECT s.mail_dir, m.payload FROM messages m JOIN sources s ON s.id = m.source_id "
            "WHERE m.source_id = ? AND m.mid = ?", (source_id, mid)).fetchone()
        return (row[0], json.loads(row[1])) if row else None
