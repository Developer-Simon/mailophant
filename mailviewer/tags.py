"""Tags and stars for mails, keyed by mail identity (see store.mail_ident).

Kept in its own DB (tags.db) and never wiped: unlike the archive index, this data is user input.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any
from mailviewer.i18n import UserError

SCHEMA_VERSION = 1

Json = dict[str, Any]


class TagStoreError(Exception):
    pass


def create_schema(con: sqlite3.Connection, schema: str = "main") -> None:
    con.execute(f"CREATE TABLE IF NOT EXISTS {schema}.tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "name_key TEXT NOT NULL UNIQUE, color TEXT NOT NULL)")
    con.execute(f"CREATE TABLE IF NOT EXISTS {schema}.message_tags (ident TEXT NOT NULL, tag_id INTEGER NOT NULL, "
                "PRIMARY KEY (ident, tag_id))")
    con.execute(f"CREATE TABLE IF NOT EXISTS {schema}.stars (ident TEXT PRIMARY KEY)")


class TagStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()  # one writer at a time; readers stay lock-free (WAL)
        con = self._conn()
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not con.execute("SELECT 1 FROM sqlite_master").fetchone():
                create_schema(con)
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                con.commit()
            elif version != SCHEMA_VERSION:
                raise TagStoreError(f"tags.db has schema version {version}, expected {SCHEMA_VERSION}")
        except BaseException:
            self.close()
            raise

    def _conn(self) -> sqlite3.Connection:
        """Each thread gets its own connection, opened lazily."""
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = sqlite3.connect(self.db_path, timeout=30)
            con.execute("PRAGMA journal_mode = WAL")
        return con

    def close(self) -> None:
        """Closes the calling thread's connection only; other threads' connections are not reachable."""
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    def list_tags(self) -> list[Json]:
        return [{"id": tag_id, "name": name, "color": color} for tag_id, name, color in self._conn().execute(
            "SELECT id, name, color FROM tags ORDER BY name_key")]

    def create_tag(self, name: str, color: str) -> Json:
        con = self._conn()
        with self._write_lock:
            try:
                if con.execute("SELECT 1 FROM tags WHERE name_key = ?", (name.casefold(),)).fetchone():
                    raise UserError("err.tag_exists")
                cur = con.execute("INSERT INTO tags (name, name_key, color) VALUES (?, ?, ?)",
                                  (name, name.casefold(), color))
                con.commit()
                return {"id": cur.lastrowid, "name": name, "color": color}
            except BaseException:
                con.rollback()
                raise

    def update_tag(self, tag_id: int, name: str | None, color: str | None) -> Json:
        con = self._conn()
        with self._write_lock:
            try:
                row = con.execute("SELECT name, color FROM tags WHERE id = ?", (tag_id,)).fetchone()
                if row is None:
                    raise KeyError(tag_id)
                new_name = row[0] if name is None else name
                new_color = row[1] if color is None else color
                if name is not None and con.execute(
                        "SELECT 1 FROM tags WHERE name_key = ? AND id != ?", (name.casefold(), tag_id)).fetchone():
                    raise UserError("err.tag_exists")
                con.execute("UPDATE tags SET name = ?, name_key = ?, color = ? WHERE id = ?",
                            (new_name, new_name.casefold(), new_color, tag_id))
                con.commit()
                return {"id": tag_id, "name": new_name, "color": new_color}
            except BaseException:
                con.rollback()
                raise

    def delete_tag(self, tag_id: int) -> None:
        con = self._conn()
        with self._write_lock:
            try:
                if con.execute("DELETE FROM tags WHERE id = ?", (tag_id,)).rowcount == 0:
                    raise KeyError(tag_id)
                con.execute("DELETE FROM message_tags WHERE tag_id = ?", (tag_id,))
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def set_marks(self, ident: str, starred: bool | None, tag_ids: list[int] | None) -> Json:
        if not ident:
            raise UserError("err.identity_missing")
        con = self._conn()
        with self._write_lock:
            try:
                if tag_ids is not None:
                    wanted = sorted(set(tag_ids))
                    if wanted:
                        marks = ",".join("?" for _ in wanted)
                        found = {row[0] for row in con.execute(
                            f"SELECT id FROM tags WHERE id IN ({marks})", wanted)}
                        if found != set(wanted):
                            raise UserError("err.unknown_tag")
                    con.execute("DELETE FROM message_tags WHERE ident = ?", (ident,))
                    con.executemany("INSERT INTO message_tags (ident, tag_id) VALUES (?, ?)",
                                    [(ident, tag_id) for tag_id in wanted])
                if starred is True:
                    con.execute("INSERT OR IGNORE INTO stars (ident) VALUES (?)", (ident,))
                elif starred is False:
                    con.execute("DELETE FROM stars WHERE ident = ?", (ident,))
                con.commit()
            except BaseException:
                con.rollback()
                raise
        return self.marks([ident])[ident]

    def marks(self, idents: list[str]) -> dict[str, Json]:
        con = self._conn()
        ids = list(dict.fromkeys(idents))
        result = {ident: {"starred": False, "tags": []} for ident in ids}
        if not ids:
            return result
        marks = ",".join("?" for _ in ids)
        for (ident,) in con.execute(f"SELECT ident FROM stars WHERE ident IN ({marks})", ids):
            result[ident]["starred"] = True
        for ident, tag_id in con.execute(
                f"SELECT ident, tag_id FROM message_tags WHERE ident IN ({marks}) ORDER BY tag_id", ids):
            result[ident]["tags"].append(tag_id)
        return result
