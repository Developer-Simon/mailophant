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
from typing import Any, Callable, Collection

import tags

META_SUFFIX = ".metadata.json"
SCHEMA_VERSION = 3  # 2: FTS columns hold casefolded text; 3: labels.ord
COMMIT_EVERY = 1000
SYSTEM_FOLDER_ORDER = ["0", "7", "5", "4", "3", "6", "8"]
IN_CHUNK = 200  # identities per IN (...) lookup, well below SQLite's variable limit
VIRTUAL_ARCHIVED = "virtual:archived"  # mails with a copy in an archive source
# One group per mail across sources; mails without an identity (no .eml) stay one group per copy
GROUP_KEY_SQL = "CASE WHEN m.ident = '' THEN m.source_id || '/' || m.mid ELSE m.ident END"

Json = dict[str, Any]

SCHEMA = """
CREATE TABLE sources (id TEXT PRIMARY KEY, mail_dir TEXT NOT NULL);
CREATE TABLE messages (source_id TEXT, mid TEXT, stat TEXT NOT NULL, ident TEXT NOT NULL,
  time INTEGER NOT NULL, unread INTEGER NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY (source_id, mid));
CREATE INDEX messages_time ON messages (time DESC, mid);
CREATE INDEX messages_ident ON messages (ident);
CREATE TABLE labels (source_id TEXT, label_id TEXT, name TEXT, path TEXT, color TEXT, type INTEGER,
  ord INTEGER, PRIMARY KEY (source_id, label_id));
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


def label_order(label: Json) -> int | None:
    value = label.get("Order")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return to_int(value)


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


def archived_clause(archive: list[str]) -> tuple[str, list[str]]:
    """Rows (alias m) outside archive whose mail also has a copy in archive."""
    marks = ",".join("?" for _ in archive)
    clause = (f"m.source_id NOT IN ({marks}) AND m.ident <> '' "
              f"AND m.ident IN (SELECT ident FROM messages WHERE source_id IN ({marks}))")
    return clause, [*archive, *archive]


class Store:
    def __init__(self, db_path: str, tags_db: str | None = None):
        self.db_path = db_path
        self.tags_db = tags_db if tags_db is not None else ":memory:"  # tests: empty tags, one per connection
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
            con.execute("ATTACH DATABASE ? AS tags", (self.tags_db,))
            if self.tags_db == ":memory:":
                tags.create_schema(con, "tags")
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
        con.executemany("INSERT INTO labels VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (source_id, str(x["ID"]), x.get("Name"), x.get("Path"), x.get("Color"), x.get("Type"),
             label_order(x)) for x in read_labels(mail_dir)])
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

    def folders(self, source_ids: list[str], dedupe: bool = False,
                include_empty: Collection[str] = ()) -> list[Json]:
        """Folders with at least one message, merged across sources by folder_key; counts are distinct messages.

        With dedupe, a count is a group of copies (see GROUP_KEY_SQL); unread comes from the best-ranked copy.
        Sources in include_empty also list their folders without messages (count 0), except All Mail.
        Each folder has order (first Order in source_ids) and labels (source -> label ID of every source holding it).
        """
        if not source_ids:
            return []
        rank = {sid: i for i, sid in enumerate(dict.fromkeys(source_ids))}
        marks = ",".join("?" for _ in rank)
        con = self._conn()
        rows = con.execute(
            "SELECT ml.source_id, ml.mid, ml.label_id, l.name, l.path, l.color, m.unread, m.ident "
            "FROM message_labels ml "
            "JOIN labels l ON l.source_id = ml.source_id AND l.label_id = ml.label_id "
            "JOIN messages m ON m.source_id = ml.source_id AND m.mid = ml.mid "
            f"WHERE ml.source_id IN ({marks})", list(rank)).fetchall()
        merged: dict[str, Json] = {}
        members: dict[str, set[Any]] = {}
        for sid, mid, label_id, name, path, color, unread, ident in sorted(rows, key=lambda r: (rank[r[0]], r[1])):
            key = folder_key(label_id, path or "")
            entry = merged.setdefault(key, {"key": key, "name": name or "", "path": path or "",
                                            "color": color or "", "count": 0, "unread": 0})
            seen = members.setdefault(key, set())
            member = ident if dedupe and ident else (sid, mid)
            if member in seen:  # two labels with the same folder key on one mail count once
                continue
            seen.add(member)
            entry["count"] += 1
            entry["unread"] += unread
        labels_of: dict[str, Json] = {}
        order: dict[str, int] = {}
        empty: dict[str, Json] = {}
        label_rows = con.execute(
            f"SELECT source_id, label_id, name, path, color, ord FROM labels WHERE source_id IN ({marks})",
            list(rank)).fetchall()
        for sid, label_id, name, path, color, position in sorted(label_rows, key=lambda r: rank[r[0]]):
            key = folder_key(label_id, path or "")
            labels_of.setdefault(key, {}).setdefault(sid, label_id)
            if position is not None:
                order.setdefault(key, position)
            if sid in include_empty and label_id != "5":
                empty.setdefault(key, {"key": key, "name": name or "", "path": path or "",
                                       "color": color or "", "count": 0, "unread": 0})
        for key, entry in empty.items():
            merged.setdefault(key, entry)
        for key, entry in merged.items():
            entry["order"] = order.get(key)
            entry["labels"] = labels_of.get(key, {})
        return sorted(merged.values(), key=folder_sort_key)

    def messages(self, source_ids: list[str], folder: str | None, needle: str, offset: int, limit: int,
                 tag: str | None = None, dedupe: bool = False, archive_ids: Collection[str] = ()) -> Json:
        """Newest first across sources; needle must already be casefolded.

        tag is "star", a tag ID as decimal string, or None. Rows carry ident, starred and tags from tags.db.
        With dedupe, copies of one mail collapse into one row (see GROUP_KEY_SQL): the copy from the
        first source in source_ids that matches the filters, and copies counts the sources holding it.
        Rows outside archive_ids list the archive sources holding a copy as archived; folder VIRTUAL_ARCHIVED
        keeps only those rows.
        """
        if tag is not None and tag != "star":
            if not (tag.isascii() and tag.isdigit()):
                raise ValueError(f"invalid tag filter: {tag!r}")
            if int(tag) >= 2 ** 63:  # cannot be a stored ID
                return {"total": 0, "messages": []}
        if not source_ids:
            return {"total": 0, "messages": []}
        limit, offset = max(1, limit), max(0, offset)
        con = self._conn()
        sources = list(dict.fromkeys(source_ids))
        archive = list(dict.fromkeys(archive_ids))
        marks = ",".join("?" for _ in sources)
        where = [f"m.source_id IN ({marks})"]
        args: list[Any] = list(sources)
        if folder == VIRTUAL_ARCHIVED:
            if not archive:
                return {"total": 0, "messages": []}
            clause, clause_args = archived_clause(archive)
            where.append(clause)
            args += clause_args
        elif folder is not None:
            pairs = [(sid, lid) for sid, lid, path in con.execute(
                f"SELECT source_id, label_id, path FROM labels WHERE source_id IN ({marks})", sources)
                if folder_key(lid, path or "") == folder]
            if not pairs:
                return {"total": 0, "messages": []}
            values = ", ".join("(?, ?)" for _ in pairs)
            where.append("EXISTS (SELECT 1 FROM message_labels ml WHERE ml.source_id = m.source_id "
                         f"AND ml.mid = m.mid AND (ml.source_id, ml.label_id) IN (VALUES {values}))")
            args += [x for pair in pairs for x in pair]
        if tag == "star":
            where.append("m.ident <> '' AND m.ident IN (SELECT ident FROM tags.stars)")
        elif tag is not None:
            where.append("m.ident <> '' AND m.ident IN (SELECT ident FROM tags.message_tags WHERE tag_id = ?)")
            args.append(int(tag))
        if len(needle) >= 3:  # as a phrase, so FTS operators and quotes in the input stay literal
            where.append("(m.source_id, m.mid) IN (SELECT source_id, mid FROM messages_fts WHERE messages_fts MATCH ?)")
            args.append('"' + needle.replace('"', '""') + '"')
        elif needle:
            like = "%" + escape_like(needle) + "%"
            where.append("(m.source_id, m.mid) IN (SELECT source_id, mid FROM messages_fts "
                         "WHERE casefold(subject) LIKE ? ESCAPE '\\' OR casefold(people) LIKE ? ESCAPE '\\')")
            args += [like, like]
        clause = " AND ".join(where)
        if dedupe:
            # rank the sources by position; the best-ranked matching copy of each group is kept (rn = 1)
            rank_marks = ", ".join("(?, ?)" for _ in sources)
            rank_args = [x for i, sid in enumerate(sources) for x in (sid, i)]
            cte = (f"WITH src_rank(source_id, r) AS (VALUES {rank_marks}), "
                   f"ranked AS (SELECT m.source_id, m.mid, m.payload, m.unread, m.ident, m.time, "
                   f"ROW_NUMBER() OVER (PARTITION BY {GROUP_KEY_SQL} ORDER BY sr.r, m.mid) AS rn "
                   f"FROM messages m JOIN src_rank sr ON sr.source_id = m.source_id WHERE {clause}) ")
            source = "ranked WHERE rn = 1"
            cols = "source_id, mid, payload, unread, ident"
            params = [*rank_args, *args]
        else:
            cte = ""
            source = f"messages m WHERE {clause}"
            cols = "m.source_id, m.mid, m.payload, m.unread, m.ident"
            params = args
        total = con.execute(f"{cte}SELECT COUNT(*) FROM {source}", params).fetchone()[0]
        rows = con.execute(
            f"{cte}SELECT {cols} FROM {source} ORDER BY time DESC, mid DESC, source_id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
        marks = self._marks([ident for *_, ident in rows])
        copies = self._copies([ident for *_, ident in rows], sources) if dedupe else {}
        archived = self._archived([ident for *_, ident in rows], archive) if archive else {}
        messages = []
        for sid, mid, payload, unread, ident in rows:
            mark = marks.get(ident, {"starred": False, "tags": []})  # ident "" (no .eml) never has marks
            messages.append(summarize(mid, json.loads(payload)) | {
                "source": sid, "unread": bool(unread), "ident": ident,
                "starred": mark["starred"], "tags": list(mark["tags"]), "copies": copies.get(ident, 1),
                "archived": [] if sid in archive else archived.get(ident, [])})
        return {"total": total, "messages": messages}

    def archived_count(self, source_ids: list[str], archive_ids: Collection[str], dedupe: bool = False) -> int:
        """Total of messages(folder=VIRTUAL_ARCHIVED) for the same sources, archive_ids and dedupe."""
        sources = list(dict.fromkeys(source_ids))
        archive = list(dict.fromkeys(archive_ids))
        if not sources or not archive:
            return 0
        marks = ",".join("?" for _ in sources)
        clause, clause_args = archived_clause(archive)
        what = f"COUNT(DISTINCT {GROUP_KEY_SQL})" if dedupe else "COUNT(*)"
        return self._conn().execute(
            f"SELECT {what} FROM messages m WHERE m.source_id IN ({marks}) AND {clause}",
            [*sources, *clause_args]).fetchone()[0]

    def idents_in(self, source_ids: Collection[str], idents: Collection[str]) -> set[str]:
        """The identities among idents that occur in one of source_ids; "" is never returned."""
        ids = [ident for ident in dict.fromkeys(idents) if ident]
        sources = list(dict.fromkeys(source_ids))
        if not ids or not sources:
            return set()
        con = self._conn()
        marks_src = ",".join("?" for _ in sources)
        found: set[str] = set()
        for i in range(0, len(ids), IN_CHUNK):
            chunk = ids[i:i + IN_CHUNK]
            marks = ",".join("?" for _ in chunk)
            found.update(ident for (ident,) in con.execute(
                f"SELECT DISTINCT ident FROM messages WHERE ident IN ({marks}) AND source_id IN ({marks_src})",
                [*chunk, *sources]))
        return found

    def _archived(self, idents: list[str], archive_ids: list[str]) -> dict[str, list[str]]:
        """Archive sources holding each identity, sorted; "" is not looked up."""
        con = self._conn()
        ids = [ident for ident in dict.fromkeys(idents) if ident]
        marks_src = ",".join("?" for _ in archive_ids)
        found: dict[str, list[str]] = {}
        for i in range(0, len(ids), IN_CHUNK):
            chunk = ids[i:i + IN_CHUNK]
            marks = ",".join("?" for _ in chunk)
            for ident, sid in con.execute(
                    f"SELECT DISTINCT ident, source_id FROM messages "
                    f"WHERE ident IN ({marks}) AND source_id IN ({marks_src})", [*chunk, *archive_ids]):
                found.setdefault(ident, []).append(sid)
        for sids in found.values():
            sids.sort()
        return found

    def _copies(self, idents: list[str], source_ids: list[str]) -> dict[str, int]:
        """Number of distinct sources among source_ids that hold each identity; "" is not looked up."""
        con = self._conn()
        ids = [ident for ident in dict.fromkeys(idents) if ident]
        marks_src = ",".join("?" for _ in source_ids)
        found: dict[str, int] = {}
        for i in range(0, len(ids), IN_CHUNK):
            chunk = ids[i:i + IN_CHUNK]
            marks = ",".join("?" for _ in chunk)
            for ident, count in con.execute(
                    f"SELECT ident, COUNT(DISTINCT source_id) FROM messages "
                    f"WHERE ident IN ({marks}) AND source_id IN ({marks_src}) GROUP BY ident",
                    [*chunk, *source_ids]):
                found[ident] = count
        return found

    def _marks(self, idents: list[str]) -> dict[str, Json]:
        """Star and tag IDs per identity; one query per table and chunk of identities."""
        con = self._conn()
        ids = [ident for ident in dict.fromkeys(idents) if ident]  # "" means no .eml: never marked
        found: dict[str, Json] = {ident: {"starred": False, "tags": []} for ident in ids}
        for i in range(0, len(ids), IN_CHUNK):
            chunk = ids[i:i + IN_CHUNK]
            marks = ",".join("?" for _ in chunk)
            for (ident,) in con.execute(f"SELECT ident FROM tags.stars WHERE ident IN ({marks})", chunk):
                found[ident]["starred"] = True
            for ident, tag_id in con.execute(
                    f"SELECT ident, tag_id FROM tags.message_tags WHERE ident IN ({marks}) ORDER BY tag_id", chunk):
                found[ident]["tags"].append(tag_id)
        return found

    def tag_counts(self, source_ids: list[str], dedupe: bool = False) -> dict[str, int]:
        """Counts per tag filter value, like messages()["total"]; "star" is always present.

        One per message copy, or one per mail (identity) with dedupe.
        """
        counts = {"star": 0}
        if not source_ids:
            return counts
        sources = list(dict.fromkeys(source_ids))
        marks = ",".join("?" for _ in sources)
        con = self._conn()
        what = "COUNT(DISTINCT m.ident)" if dedupe else "COUNT(*)"
        counts["star"] = con.execute(
            f"SELECT {what} FROM messages m JOIN tags.stars s ON s.ident = m.ident "
            f"WHERE m.source_id IN ({marks}) AND m.ident <> ''", sources).fetchone()[0]
        for tag_id, count in con.execute(
                f"SELECT mt.tag_id, {what} FROM messages m JOIN tags.message_tags mt ON mt.ident = m.ident "
                f"WHERE m.source_id IN ({marks}) AND m.ident <> '' GROUP BY mt.tag_id", sources):
            counts[str(tag_id)] = count
        return counts

    def ident(self, source_id: str, mid: str) -> str | None:
        """Mail identity for tags.db; None if the mail is not in the index, "" if its .eml is missing."""
        row = self._conn().execute(
            "SELECT ident FROM messages WHERE source_id = ? AND mid = ?", (source_id, mid)).fetchone()
        return row[0] if row else None

    def copies(self, ident: str, source_ids: list[str]) -> list[Json]:
        """Copies of one mail in the given sources, in source_ids order; folders are label names, sorted."""
        if not ident or not source_ids:
            return []
        sources = list(dict.fromkeys(source_ids))
        rank = {sid: i for i, sid in enumerate(sources)}
        marks = ",".join("?" for _ in sources)
        rows = self._conn().execute(
            "SELECT m.source_id, m.mid, l.label_id, l.name, l.path FROM messages m "
            "LEFT JOIN message_labels ml ON ml.source_id = m.source_id AND ml.mid = m.mid "
            "LEFT JOIN labels l ON l.source_id = ml.source_id AND l.label_id = ml.label_id "
            f"WHERE m.ident = ? AND m.source_id IN ({marks})", [ident, *sources]).fetchall()
        found: dict[tuple[str, str], dict[str, Json]] = {}  # copy -> folder name -> folder
        for sid, mid, label_id, name, path in rows:
            folders = found.setdefault((sid, mid), {})
            if label_id is not None:
                folders.setdefault(name or "", {"key": folder_key(label_id, path or ""), "name": name or ""})
        result = []
        for sid, mid in sorted(found, key=lambda k: (rank[k[0]], k[1])):
            names = sorted(found[(sid, mid)].values(), key=folder_sort_key)
            result.append({"source": sid, "id": mid, "folders": [f["name"] for f in names]})
        return result

    def duplicate_pairs(self, target: str, reference: str) -> list[tuple[str, str]]:
        """(target mid, reference mid) for each mail of target whose identity is in reference.

        One pair per target mid, using the smallest reference mid; sorted by target mid.
        """
        rows = self._conn().execute(
            "SELECT t.mid, MIN(r.mid) FROM messages t JOIN messages r ON r.ident = t.ident "
            "WHERE t.source_id = ? AND r.source_id = ? AND t.ident <> '' "
            "GROUP BY t.mid ORDER BY t.mid", (target, reference)).fetchall()
        return [(t_mid, r_mid) for t_mid, r_mid in rows]

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
