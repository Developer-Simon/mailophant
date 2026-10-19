#!/usr/bin/env python3
"""Read-only local web viewer for a library of mail backups (stdlib only).

Usage: python -m mailviewer [LIBRARY_DIR] [--port 8025] [--cli PATH] [--config FILE]

LIBRARY_DIR holds library.json, which lists the mail sources (Proton accounts or maildirs) shown
together. Its messages are indexed in archive.db (a cache next to library.json). Tags and stars
live in tags.db beside it, which an index rebuild never touches. Without
LIBRARY_DIR the library remembered in the config file is opened; if there is none, the page
asks for a library folder (optionally creating one or remembering the choice).
Mail files are only read, except for a cleanup of an imported Outlook source (POST /api/cleanup): its
duplicate mails become hardlinks to a reference source, or are deleted. Each run logs to
cleanup-<time>.log in the library. Archive jobs (POST /api/archives/<id>/folders, POST /api/transfer) change
the labels of archives, and a transfer moves mails between archives, deleting an origin only after its copy is complete.
Optional: .pst/.ost files can be imported as sources when the pypff module is installed.
"""

from __future__ import annotations

import argparse
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
from collections.abc import Callable
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from mailviewer import autoarchive, i18n, portal, protoncli
from mailviewer.i18n import Msg, UserError, detail, error_body, msg, raw
from mailviewer.archive import (
    ALL_MAIL, check_name, create_folder, delete_folder, ensure_folder, is_system_folder, move_folder, rename_folder,
    write_empty_archive,
)
from mailviewer.browse import browse
from mailviewer.config import default_config_path, load_language, load_legacy, load_saved, update_config
from mailviewer.exports import (
    account_dir_of, account_email, cli_env, cli_progress, cli_text, dedupe_with_previous, find_cli, is_executable, mail_dirs, newest_fetched,
    redact, resolve_mail_dir,
)
from mailviewer.mailparse import attachment_headers, attachment_name, attachment_payload, get_attachment, load_message, preview_type
from mailviewer.validation import (
    MAX_BODY, MAX_ICON, MAX_TRANSFER, MAX_TRANSFER_ITEMS, check_email, check_id, check_tag_color, check_tag_name,
    check_text, load_json_object, only_keys, parse_fetch_body, tag_color, valid_icon, opt_text, text_field,
)
from mailviewer.cleanup import ACTIONS, check_dirs, plan_cleanup, remove_quiet, run_cleanup
from mailviewer.library import ICON_DIR, Library, LibraryError, Settings, Source, detect_kind, is_archive
from mailviewer.outlook import (
    ImportReport,
    ImportUnavailable,
    convert,
    open_reader,
    install_hint,
    pypff_available,
)
from mailviewer.store import VIRTUAL_ARCHIVED, Store, addr, read_labels
from mailviewer.tags import TagStore, TagStoreError
from mailviewer.transfer import Item, transfer

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
STATIC_RE = re.compile(r"^/static/((?:css|js)/[a-z0-9-]+\.(css|js))$")
STATIC_TYPES = {"css": "text/css; charset=utf-8", "js": "text/javascript; charset=utf-8"}
MAX_LIMIT = 200
MSG_RE = re.compile(r"^/api/message/([^/]+)/([^/]+)$")
ATT_RE = re.compile(r"^/api/message/([^/]+)/([^/]+)/attachment/(\d+)$")
SOURCE_RE = re.compile(r"^/api/sources/([^/]+)(?:/(remove|icon|auto))?$")
TAG_RE = re.compile(r"^/api/tags/(\d+)(?:/(remove))?$")
MARKS_RE = re.compile(r"^/api/message/([^/]+)/([^/]+)/marks$")
ICON_RE = re.compile(r"^/api/sources/([^/]+)/icon$")
ICON_TYPES = {"image/png": "png", "image/svg+xml": "svg"}  # Content-Type -> file extension
ICON_CTYPES = {ext: ctype for ctype, ext in ICON_TYPES.items()}
# an SVG is only ever shown as an image document: no scripts, no external loads
ICON_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; sandbox"
SOURCE_FIELDS = ("name", "icon", "hidden", "position", "archive_target")
SETTINGS_FIELDS = tuple(asdict(Settings()))
POST_ROUTES = ("/api/library", "/api/fetch", "/api/sources", "/api/settings", "/api/import", "/api/language", "/api/tags",
               "/api/cleanup", "/api/archives", "/api/transfer", "/api/cli/install")
ARCHIVE_FOLDERS_RE = re.compile(r"^/api/archives/([^/]+)/folders$")
ARCHIVE_FOLDER_OPS = ("create", "rename", "move", "delete")
ARCHIVE_FOLDER_FIELDS = ("op", "name", "parent", "id", "index")
AUTO_ACTIONS = ("force", "discard")
AUTO_FIELDS = ("action",)
TRANSFER_FIELDS = ("items", "archive", "folder", "new_folder")
TRANSFER_ITEM_FIELDS = ("source", "id")
NEW_FOLDER_FIELDS = ("name", "parent")
TAG_FIELDS = ("name", "color")
MARK_FIELDS = ("starred", "tags")
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






























def free_name(parent: str, name: str) -> str:
    candidate, n = name, 1
    while os.path.exists(os.path.join(parent, candidate)):
        candidate = f"{name}_{n}"
        n += 1
    return candidate





















def tag_list(store: Store, tags: TagStore, source_ids: list[str]) -> Json:
    """The star count and every tag with its message count, for the given sources."""
    counts = store.tag_counts(source_ids, dedupe=len(source_ids) > 1)
    return {"star": counts["star"], "tags": [
        {**tag, "count": counts.get(str(tag["id"]), 0)} for tag in tags.list_tags()]}










def import_path(path: str) -> str:
    """The resolved path of an existing .pst or .ost file. Symlinks are resolved first, so the
    extension and the file test apply to the file that is actually read. ValueError otherwise."""
    real = os.path.realpath(os.path.expanduser(path))
    if not real.lower().endswith(IMPORT_EXTENSIONS) or not os.path.isfile(real):
        raise UserError("err.not_pst")
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


def import_log_lines(report: ImportReport) -> list[Msg]:
    """Counts and folder paths only: no subject, address or other mail content is logged."""
    lines = [msg("log.imported", count=report.imported), msg("log.merged", count=report.merged)]
    if report.skipped:
        lines.append(msg("log.skipped_classes", list=", ".join(f"{cls} {n}" for cls, n in report.skipped.items())))
    if report.unreadable:
        lines.append(msg("log.unreadable", count=report.unreadable))
    if report.failed_folders:
        lines.append(msg("log.folders_unreadable", list=", ".join(report.failed_folders)))
    return lines


def _redacted(exc: BaseException, secrets: list[str]) -> Msg:
    """The error text without secrets the message may echo."""
    return raw(redact(str(exc) or type(exc).__name__, secrets))


class Busy(Exception):
    """The job slot is taken: the library cannot be switched or a second job started meanwhile.
    Constructed with a message code (err.busy_index, err.busy_job, err.no_library)."""

    def __init__(self, code: str) -> None:
        self.msg = msg(code)
        super().__init__(self.msg.text)


class FetchUnavailable(Exception):
    """No export CLI is known for the source."""

    def __init__(self) -> None:
        self.msg = msg("err.fetch_unavailable")
        super().__init__(self.msg.text)


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
            self._finish("error", detail(exc))
        else:
            self._finish("done")

    def log(self, entry: Msg) -> None:
        with self._lock:
            log = self._state["log"]
            log.append(entry.to_json())
            del log[:-LOG_LINES]

    def progress(self, done: int, total: int) -> None:
        with self._lock:
            self._state["progress"] = {"done": done, "total": total}

    def percent(self, value: float) -> None:
        """Progress known only as a share, like that of the export CLI."""
        with self._lock:
            self._state["progress"] = {"done": value, "total": 100, "unit": "percent"}

    def status(self) -> Json:
        with self._lock:
            return {**self._state, "log": list(self._state["log"])}

    def running(self) -> bool:
        with self._lock:
            return self._state["state"] == "running"

    def set_result(self, result: Json) -> None:
        with self._lock:
            self._state["result"] = result

    def _finish(self, state: str, message: Msg | None = None) -> None:
        with self._lock:
            self._state.update(state=state, message=message.to_json() if message else None)


def _open_stores(lib: Library) -> tuple[Store, TagStore]:
    """Open <root>/tags.db, then <root>/archive.db, which attaches it. Then drop the index rows of
    sources that left library.json. tags.db comes first because Store expects its schema to exist."""
    try:
        tags = TagStore(os.path.join(lib.root, TAGS_DB))
    except (TagStoreError, sqlite3.Error) as exc:
        raise LibraryError("err.tag_db_open", detail=detail(exc)) from exc
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
        raise LibraryError("err.index_open", detail=detail(exc)) from exc
    return store, tags


def _has_path(lib: Library, path: str) -> bool:
    real = os.path.realpath(path)
    return any(os.path.realpath(lib.abspath(s)) == real for s in lib.sources)


class Viewer:
    """State shared by all handler threads: the open library with its index, the background job
    and the remembered choice. A new library replaces library and store as a whole."""

    def __init__(self, cli: str | None = None, explicit_cli: bool = False,
                 config_path: str | None = None, cli_dir: str | None = None) -> None:
        self.cli = cli
        self.explicit_cli = explicit_cli
        self.cli_dir = cli_dir or protoncli.default_install_dir()  # where the built-in installer puts the CLI
        self.config_path = config_path
        self.saved: str | None = load_saved(config_path) if config_path else None
        self.saved_error: i18n.Msg | None = None
        self.library: Library | None = None
        self.store: Store | None = None
        self.tags: TagStore | None = None
        self.job = Job()
        self.auto_paused: dict[str, int] = {}  # source id -> mails missing from it, while auto-archiving is paused
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
                raise Busy("err.busy_index")
            legacy = load_legacy(self.config_path) if add_legacy and self.config_path else None
            if legacy:  # checked before anything is written, so a failed request leaves no library.json
                try:
                    detect_kind(os.path.abspath(os.path.expanduser(legacy)))
                except LibraryError as exc:
                    raise LibraryError("err.legacy_missing", path=legacy) from exc
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
                    update_config(self.config_path, updates)
                except OSError as exc:
                    raise LibraryError("err.config_save", detail=raw(str(exc.strerror or exc))) from exc
            if remember is not None:
                self.saved = root if remember else None
                self.saved_error = None
            self.library, self.store, self.tags = lib, store, tags
            self.start_index()

    def _admin_library(self, idle: bool) -> tuple[Library, Store]:
        """Library and store for an admin change; the caller holds _lock. Busy while a job runs if idle."""
        lib, store = self.library, self.store
        if lib is None or store is None:
            raise Busy("err.no_library")
        if idle and self.job.running():
            raise Busy("err.busy_index")
        return lib, store

    def add_source(self, path: str, name: str) -> Source:
        """Add a source and index only it. Busy while a job runs, and then nothing is added."""
        with self._lock:
            lib, _ = self._admin_library(idle=True)
            src = lib.add_source(os.path.abspath(os.path.expanduser(path)), name)
            self.start_index([src.id])
            return src

    def update_source(self, source_id: str, *, name: str | None, icon: Json | None,
                      hidden: bool | None, position: int | None, archive_target: str | None = None) -> None:
        """Rename, icon, hide, archive target and reorder touch library.json only, so they may run during a job."""
        with self._lock:
            lib, _ = self._admin_library(idle=False)
            lib.update_source(source_id, name=name, icon=icon, hidden=hidden, archive_target=archive_target)
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
        """--cli if given, else the library's cli setting if it is an absolute executable path, else the
        CLI installed by Mailophant, else a CLI found near the source."""
        if self.explicit_cli and self.cli:
            return self.cli
        lib = self.library
        setting = lib.settings.cli if lib is not None else ""
        if os.path.isabs(setting) and is_executable(setting):
            return setting
        return protoncli.installed_cli(self.cli_dir) or find_cli(lib.abspath(source))

    def start_cli_install(self) -> None:
        """Download and install the Proton export CLI as the job. Raises UserError on an unsupported
        platform, Busy while another job runs (a running fetch may be using the old CLI)."""
        if protoncli.platform_key() is None:
            raise UserError("err.cli_unsupported", platform=sys.platform)

        def install(job: Job) -> None:
            path = protoncli.install(self.cli_dir, job.log, job.progress)
            job.set_result({"path": path})

        if not self.job.start("cli_install", install):
            raise Busy("err.busy_job")

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
                        job.log(msg("log.source_unreachable", source=src.name))
                        continue
                    try:  # one broken source must not stop the others
                        result = store.sync_source(src.id, self.mail_dir_for(src), job.progress)
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        job.log(msg("log.source_failed", source=src.name, detail=detail(exc)))
                        continue
                    job.log(msg("log.source_synced", source=src.name, added=result.added, updated=result.updated, removed=result.removed))
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
                raise Busy("err.no_library")
            try:
                src = lib.get(source_id)
            except KeyError as exc:
                raise UserError("err.unknown_source") from exc
            if src.kind != "proton":
                raise UserError("err.not_proton")
            cli = self.cli_for(src)
            if cli is None:
                raise FetchUnavailable()
            account = account_dir_of(lib.abspath(src))
            email = email or lib.settings.proton_email or account_email(account)
            if not email:
                raise UserError("err.missing_email")
            check_email(email)

            def fetch(job: Job) -> None:
                try:
                    self._fetch(job, store, src, cli, account, email, password, mailbox, totp)
                finally:
                    store.close()  # the job thread's connection, as in start_index

            if not self.job.start("fetch", fetch):
                raise Busy("err.busy_job")

    def start_import(self, path: str, name: str) -> None:
        """Import a PST or OST file as a new source of kind outlook, as the job. The file is only read.
        Raises ValueError (bad request), Busy (no library, or a job runs), or ImportUnavailable (no pypff).
        Checked and started under _lock, like start_fetch."""
        real = import_path(path)
        name = check_text(name, "name").strip()
        if not name:
            raise UserError("err.missing_name")
        with self._lock:
            lib, store = self.library, self.store
            if lib is None or store is None:
                raise Busy("err.no_library")
            if self.job.running():
                raise Busy("err.busy_job")
            if not pypff_available():
                raise ImportUnavailable()
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
                raise Busy("err.busy_job")

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
                raise UserError("err.no_readable_mails")
            final = os.path.join(sources, free_name(sources, import_slug(name)))
            os.rename(tmp, final)
            tmp = None  # moved; final is removed below if the source is not added
            with self._lock:
                src = lib.add_source(final, name, kind="outlook")
            added = True
            try:
                result = store.sync_source(src.id, final, job.progress)
            except Exception as exc:  # the files are complete; the next index run picks them up
                job.log(msg("log.source_failed", source=src.name, detail=detail(exc)))
            else:
                job.log(msg("log.source_synced", source=src.name, added=result.added, updated=result.updated, removed=result.removed))
            job.set_result({"source": src.id, **asdict(report)})
        finally:
            if reader is not None:
                reader.close()
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            if final is not None and not added:
                shutil.rmtree(final, ignore_errors=True)

    def create_archive(self, name: str) -> Source:
        """Create an empty archive in a free sources/<slug> and add it as an archive source, indexed at once.
        Synchronous under _lock. A failure removes the new directory and the source again."""
        with self._lock:
            lib, store = self._admin_library(idle=True)
            sources = os.path.join(lib.root, SOURCES_DIR)
            os.makedirs(sources, exist_ok=True)
            final = os.path.join(sources, free_name(sources, import_slug(name)))
            os.mkdir(final)  # free_name checked it under _lock; if this fails there is nothing of ours to remove
            src: Source | None = None
            try:
                write_empty_archive(final)
                src = lib.add_source(final, name, kind="archive")
                store.sync_source(src.id, final)
            except BaseException:
                if src is not None:
                    store.drop_source(src.id)
                    lib.remove_source(src.id)
                shutil.rmtree(final, ignore_errors=True)
                raise
            return src

    def _start_archive_job(self, prepare: Callable[[Library], tuple[list[Source], Callable[[Job], None]]]) -> None:
        """Start an archive job, checked and started under _lock like start_cleanup. prepare checks the request
        synchronously (KeyError, ValueError) and returns the sources the job changes and the job's work. Those
        sources are reindexed after the work, also after an error. Busy while another job runs."""
        with self._lock:
            lib, store = self._admin_library(idle=True)
            touched, work = prepare(lib)

            def reindex(job: Job) -> None:
                for src in touched:
                    if not self.available(src):
                        job.log(msg("log.source_unreachable", source=src.name))
                        continue
                    try:
                        result = store.sync_source(src.id, self.mail_dir_for(src), job.progress)
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        job.log(msg("log.source_failed", source=src.name, detail=detail(exc)))
                        continue
                    job.log(msg("log.source_synced", source=src.name, added=result.added, updated=result.updated, removed=result.removed))

            def archive_job(job: Job) -> None:
                try:
                    work(job)
                finally:
                    try:
                        reindex(job)
                    finally:
                        store.close()  # the job thread's connection, as in start_index

            if not self.job.start("archive", archive_job):
                raise Busy("err.busy_job")

    def start_archive_folders(self, source_id: str, op: str, name: str | None, parent: str | None,
                              folder_id: str | None, index: int | None) -> None:
        """Create, rename, move or delete a folder of an archive as the job. Every id is checked against the
        archive's labels.json before the job starts: unknown ids and system folders are ValueError (400).
        KeyError for unknown sources, Busy while a job runs."""

        def prepare(lib: Library) -> tuple[list[Source], Callable[[Job], None]]:
            src = lib.get(source_id)
            if not is_archive(src):
                raise UserError("err.source_not_archive", source=src.name)
            if not self.available(src):
                raise UserError("err.source_unreachable", source=src.name)
            mail_dir = self.mail_dir_for(src)
            labels = {x["ID"] for x in read_labels(mail_dir)}
            if parent is not None and parent not in labels:
                raise UserError("err.parent_not_found")
            if op in ("rename", "move", "delete"):
                if folder_id not in labels:
                    raise UserError("err.folder_not_found")
                if is_system_folder(folder_id):
                    raise UserError("err.system_folder_readonly")

            def work(job: Job) -> None:
                if op == "create":
                    job.set_result({"id": create_folder(mail_dir, name, parent)["ID"]})
                elif op == "rename":
                    rename_folder(mail_dir, folder_id, name)
                elif op == "move":
                    move_folder(mail_dir, folder_id, parent, len(labels) if index is None else index)
                else:
                    job.log(msg("log.folder_deleted", source=src.name, count=delete_folder(mail_dir, folder_id, job.log)))

            return [src], work

        self._start_archive_job(prepare)

    def start_transfer(self, items: list[tuple[str, str]], archive: str, folder: str | None,
                       new_folder: tuple[str, str | None] | None) -> None:
        """Copy, move or refile mails into an archive as the job, after a synchronous check. Each (source, id)
        must be indexed; the destination and the origins must be available. A new folder (name, parent) is created
        in the job before the transfer. KeyError for unknown sources (404), ValueError (400), Busy."""
        if folder is not None and new_folder is not None:
            raise UserError("err.folder_exclusive")

        def prepare(lib: Library) -> tuple[list[Source], Callable[[Job], None]]:
            store = self.store  # set: _admin_library checked it under _lock
            dest = lib.get(archive)
            if not is_archive(dest):
                raise UserError("err.source_not_archive", source=dest.name)
            if not self.available(dest):
                raise UserError("err.source_unreachable", source=dest.name)
            dest_dir = self.mail_dir_for(dest)
            labels = {x["ID"] for x in read_labels(dest_dir)}
            if folder is not None and folder not in labels:
                raise UserError("err.folder_not_found")
            if new_folder is not None:
                name, parent = new_folder
                check_name(name)
                if parent is not None and (parent not in labels or parent == ALL_MAIL):
                    raise UserError("err.parent_not_found")
            sources = {sid: lib.get(sid) for sid in dict.fromkeys(sid for sid, _ in items)}
            for src in sources.values():
                if not self.available(src):
                    raise UserError("err.source_unreachable", source=src.name)
            plan = []
            for sid, mid in dict.fromkeys(items):
                ident = store.ident(sid, mid)
                if ident is None:
                    raise UserError("err.mail_not_indexed")
                src = sources[sid]
                mode = "refile" if sid == dest.id else "move" if is_archive(src) else "copy"
                plan.append(Item(self.mail_dir_for(src), mid, ident, mode))
            touched = [dest] + [src for src in sources.values() if is_archive(src) and src.id != dest.id]

            def work(job: Job) -> None:
                folder_id = folder
                if new_folder is not None:
                    folder_id = create_folder(dest_dir, name, parent)["ID"]
                result = transfer(plan, dest_dir, folder_id, job.log, job.progress)
                job.set_result(asdict(result))
                job.log(msg("log.transfer_summary", source=dest.name, copied=result.copied, reused=result.reused,
                            refiled=result.refiled, deleted=result.deleted_origin, skipped=result.skipped,
                            failed=result.failed))

            return touched, work

        self._start_archive_job(prepare)

    def _paused_source(self, lib: Library, source_id: str) -> Source:
        """The source whose automatic archive is paused. KeyError for unknown ids, ValueError if none is pending."""
        src = lib.get(source_id)
        if src.kind != "proton" or source_id not in self.auto_paused:
            raise UserError("err.no_paused_auto")
        return src

    def start_auto(self, source_id: str, action: str) -> None:
        """Answer a paused automatic archive: discard drops the pause, force runs the held-back archive as the job.
        KeyError for unknown sources (404), ValueError without a pending pause or a usable target (400), Busy
        for force while a job runs. Checked under _lock like the other archive jobs."""
        if action == "discard":
            with self._lock:
                lib, _ = self._admin_library(idle=False)
                self.auto_paused.pop(self._paused_source(lib, source_id).id, None)
            return

        def prepare(lib: Library) -> tuple[list[Source], Callable[[Job], None]]:
            store = self.store  # set: _admin_library checked it under _lock
            src = self._paused_source(lib, source_id)
            if not src.archive_target:
                raise UserError("err.target_archive_unreachable")
            try:
                target = lib.get(src.archive_target)
            except KeyError as exc:
                raise UserError("err.target_archive_unreachable") from exc
            if not is_archive(target) or not self.available(target):
                raise UserError("err.target_archive_unreachable")
            newest = self.mail_dir_for(src)

            def work(job: Job) -> None:
                self._auto_archive(job, store, lib, src, newest, force=True)

            return [target], work

        self._start_archive_job(prepare)

    def check_cleanup(self, target: str, reference: str) -> tuple[Source, Source]:
        """Check the two sources of a cleanup. The preview and the run both use it, so they refuse the same
        pairs. KeyError for unknown ids (404), ValueError for unsuitable ones (400), Busy without a library."""
        lib = self.library
        if lib is None or self.store is None:
            raise Busy("err.no_library")
        if not target or not reference:
            raise UserError("err.cleanup_pair_missing")
        tgt, ref = lib.get(target), lib.get(reference)
        if target == reference:
            raise UserError("err.cleanup_same_source")
        if tgt.kind != "outlook":
            raise UserError("err.cleanup_outlook_only")
        if ref.hidden:
            raise UserError("err.reference_hidden", source=ref.name)
        for src in (tgt, ref):
            if not self.available(src):
                raise UserError("err.source_unreachable", source=src.name)
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
                raise UserError("err.cross_device")

            def cleanup(job: Job) -> None:
                try:
                    self._cleanup(job, store, lib, tgt, ref, tgt_dir, ref_dir, action)
                finally:
                    store.close()  # the job thread's connection, as in start_index

            if not self.job.start("cleanup", cleanup):
                raise Busy("err.busy_job")

    def _cleanup(self, job: Job, store: Store, lib: Library, tgt: Source, ref: Source,
                 tgt_dir: str, ref_dir: str, action: str) -> None:
        """Run the cleanup with its log in the library, then reindex the target. The reindex also runs
        after an error, so the index never keeps rows for files that were removed."""
        name = f"cleanup-{time.strftime('%Y%m%d-%H%M%S')}.log"
        with open(os.path.join(lib.root, name), "x", encoding="utf-8") as f:  # never overwrites a log
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    + msg("log.cleanup_header", target=tgt.name, reference=ref.name, action=action).text + "\n")

            def log(entry: Msg) -> None:
                job.log(entry)
                f.write(entry.text + "\n")

            try:
                result = run_cleanup(store.duplicate_pairs(tgt.id, ref.id), tgt_dir, ref_dir, action, log)
                job.set_result({**asdict(result), "log_file": name})
            finally:
                sync = store.sync_source(tgt.id, tgt_dir, job.progress)
                job.log(msg("log.source_synced", source=tgt.name, added=sync.added, updated=sync.updated, removed=sync.removed))

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
            job.log(msg("log.source_synced", source=src.name, added=result.added, updated=result.updated, removed=result.removed))
            job.set_result({"source": src.id, "export": os.path.basename(dest)})
            try:
                self._auto_archive(job, store, self.library, src, dest)
            except Exception as exc:  # the fetch itself stays successful, the exports stay in place
                job.log(msg("log.auto_failed", detail=_redacted(exc, secrets)))
        except Exception as exc:  # the message may echo a secret
            if isinstance(getattr(exc, "msg", None), Msg) and exc.msg.code:
                raise exc from None  # coded errors keep their code; they carry no secrets
            raise RuntimeError(_redacted(exc, secrets).text) from None
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def _auto_archive(self, job: Job, store: Store, lib: Library, src: Source, new_dir: str,
                      force: bool = False) -> None:
        """Archive the mails that vanished from src since the older exports into its archive target, then delete
        those exports. Any failure logs and returns before the deletion, so the exports stay in place."""
        if not src.archive_target:
            return
        try:
            target = lib.get(src.archive_target)
        except KeyError:
            target = None
        if target is None or not is_archive(target) or not self.available(target):
            job.log(msg("log.auto_target_unreachable"))
            return
        account = os.path.dirname(new_dir)
        real_new = os.path.realpath(new_dir)
        old = [d for d in mail_dirs(account) if os.path.realpath(d) != real_new]
        if not old:
            return
        plan = autoarchive.plan_vanished(old, new_dir)
        if autoarchive.should_brake(plan) and not force:
            self.auto_paused[src.id] = plan.vanished
            job.log(msg("log.auto_paused", count=plan.vanished))
            return
        self.auto_paused.pop(src.id, None)

        archive_ids = [s.id for s in lib.sources if is_archive(s) and self.available(s)]
        idents = [c.ident for c in plan.candidates]
        already_in = store.idents_in(archive_ids, idents)
        candidates = [c for c in plan.candidates if c.ident not in already_in]
        already = len(plan.candidates) - len(candidates)

        dest_dir = self.mail_dir_for(target)
        folder_id = ensure_folder(dest_dir, autoarchive.INBOX_NAME) if candidates else None
        result = autoarchive.archive_candidates(candidates, dest_dir, folder_id, job.log)
        if result.failed:
            job.log(msg("log.archive_failed", count=result.failed))
            return

        store.sync_source(target.id, dest_dir)
        copied = [c.ident for c in candidates]
        if store.idents_in([target.id], copied) != set(copied):
            job.log(msg("log.verify_failed"))
            return

        deleted = 0
        if plan.unreadable:
            job.log(msg("log.old_unreadable", count=plan.unreadable))
        else:
            deleted = autoarchive.delete_exports(old, new_dir, job.log)
        job.log(msg("log.archived_summary", archived=len(candidates), skipped=already + plan.excluded,
                    already=already, excluded=plan.excluded, deleted=deleted))

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
            job.log(msg("log.restore_failed", source=src.name, detail=_redacted(exc, secrets)))

    def _run_cli(self, cli: str, env: dict[str, str], secrets: list[str]) -> int:
        # CLI 1.0.7 ignores ET_DIR for backups and would prompt for the path on stdin,
        # so pass it as --dir too (a path is no secret)
        proc = subprocess.Popen([cli, "--dir", env["ET_DIR"]], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        last = None
        with proc:
            for line in proc.stdout:  # text mode splits at \r too, so each repaint arrives on its own
                text = redact(cli_text(line), secrets)
                percent = cli_progress(text)
                if percent is not None:
                    self.job.percent(percent)
                elif text and text != last:  # a step repaints its line every 500 ms
                    self.job.log(raw(text))
                    last = text
            return proc.wait()

    def _link_previous(self, previous: str | None, dest: str) -> None:
        """Hardlink unchanged mails to the previous export. Failures only cost disk space, so never raise."""
        if previous is None or not os.path.isdir(previous):
            return
        try:
            linked, saved = dedupe_with_previous(previous, dest)
        except Exception as exc:
            self.job.log(msg("log.hardlinks_failed", detail=detail(exc)))
            return
        if linked:
            self.job.log(msg("log.hardlinks_saved", count=linked, saved=saved))
        else:
            self.job.log(msg("log.hardlinks_none"))


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
            "archive": is_archive(src), "archive_target": src.archive_target,
            "auto_paused": {"missing": viewer.auto_paused[src.id]} if src.id in viewer.auto_paused else None,
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
                return self._static("index.html", "text/html; charset=utf-8")
            if url.path == "/i18n.js":
                return self._i18n_script()
            static = STATIC_RE.match(url.path)
            if static:
                return self._static(static.group(1), STATIC_TYPES[static.group(2)])
            if url.path == "/api/config":
                return self._json(200, self._config())
            if url.path == "/api/browse":
                try:
                    query = parse_qs(url.query)
                    return self._json(200, browse(_arg(query, "path") or viewer.saved or "~",
                                                  files=_arg(query, "files") == "outlook"))
                except ValueError as exc:
                    return self._json(400, error_body(detail(exc)))
            if url.path == "/api/job":
                return self._json(200, viewer.job.status())
            if url.path in LIBRARY_ROUTES or url.path.startswith("/api/message/") or ICON_RE.match(url.path):
                lib, store, tags = viewer.library, viewer.store, viewer.tags  # one snapshot, even on a switch
                if lib is None or store is None or tags is None:
                    return self._json(409, error_body(msg("err.no_library")))
                return self._library_get(lib, store, tags, url)
            self._json(404, error_body(msg("err.not_found")))

        def _library_get(self, lib: Library, store: Store, tags: TagStore, url: Any) -> None:
            query = parse_qs(url.query)
            if m := ICON_RE.match(url.path):
                return self._icon(lib, unquote(m[1]))
            if url.path == "/api/sources":
                return self._json(200, source_list(viewer, lib, store))
            if url.path == "/api/tags":
                source_ids = selected_sources(lib, query)
                if source_ids is None:
                    return self._json(400, error_body(msg("err.unknown_source")))
                return self._json(200, tag_list(store, tags, source_ids))
            if url.path == "/api/cleanup/preview":
                return self._cleanup_preview(store, query)
            if url.path in ("/api/folders", "/api/messages"):
                source_ids = selected_sources(lib, query)
                if source_ids is None:
                    return self._json(400, error_body(msg("err.unknown_source")))
                merge = len(source_ids) > 1  # copies of one mail count once only when several sources are shown
                archives = {s.id for s in lib.sources if is_archive(s)}
                if url.path == "/api/folders":
                    folders = store.folders(source_ids, dedupe=merge,
                                            include_empty=[x for x in source_ids if x in archives])
                    if any(x not in archives for x in source_ids):  # mails of these sources with a copy in an archive
                        count = store.archived_count(source_ids, archives, dedupe=merge)
                        if count > 0:
                            folders.append({"key": VIRTUAL_ARCHIVED, "name": msg("msg.virtual_archived").text, "name_code": "msg.virtual_archived", "path": "",
                                            "color": "", "count": count, "unread": 0, "virtual": True,
                                            "labels": {}, "order": None})
                    return self._json(200, folders)
                folder = _arg(query, "folder") or None
                needle = _arg(query, "q").strip().casefold()
                offset = max(0, _int(_arg(query, "offset"), 0))
                limit = min(MAX_LIMIT, max(1, _int(_arg(query, "limit"), 50)))
                tag = _arg(query, "tag") or None  # "star", a tag ID, or no filter
                try:
                    body = store.messages(source_ids, folder, needle, offset, limit, tag, dedupe=merge,
                                          archive_ids=archives)
                except ValueError:
                    return self._json(400, error_body(msg("err.invalid_tag")))
                return self._json(200, body)
            if m := ATT_RE.match(url.path):
                return self._attachment(store, unquote(m[1]), unquote(m[2]), int(m[3]),
                                        _arg(query, "inline") == "1")
            if m := MSG_RE.match(url.path):
                src, mid = unquote(m[1]), unquote(m[2])
                hit = store.message(src, mid)  # the id must be indexed, so no path escapes a mail dir
                if hit is None:
                    return self._json(404, error_body(msg("err.unknown_message")))
                ident = store.ident(src, mid) or ""  # "" if the .eml is missing: no marks then
                mark = tags.marks([ident])[ident]
                names = {s.id: s.name for s in lib.sources}
                copies = [c | {"name": names[c["source"]]}
                          for c in store.copies(ident, selected_sources(lib, {}) or [])]
                return self._json(200, load_message(hit[0], mid, hit[1]) | {
                    "starred": mark["starred"], "tags": mark["tags"], "markable": ident != "",
                    "copies": copies})
            self._json(404, error_body(msg("err.not_found")))

        def _cleanup_preview(self, store: Store, query: dict[str, list[str]]) -> None:
            """What a cleanup would do, from the index and the files. Changes nothing."""
            try:
                tgt, ref = viewer.check_cleanup(_arg(query, "target"), _arg(query, "reference"))
                plan = plan_cleanup(store.duplicate_pairs(tgt.id, ref.id),
                                    viewer.mail_dir_for(tgt), viewer.mail_dir_for(ref))
            except Busy as exc:
                return self._json(409, error_body(detail(exc)))
            except KeyError:
                return self._json(404, error_body(msg("err.unknown_source")))
            except (ValueError, OSError) as exc:
                return self._json(400, error_body(detail(exc)))
            self._json(200, asdict(plan) | {"target": tgt.id, "reference": ref.id,
                                             "link_reason": plan.link_reason.to_json() if plan.link_reason else None})

        def _attachment(self, store: Store, src: str, mid: str, n: int, inline: bool) -> None:
            hit = store.message(src, mid)
            part = get_attachment(hit[0], mid, n) if hit is not None else None
            if part is None:
                return self._json(404, error_body(msg("err.no_attachment")))
            ctype = part.get_content_type()
            if inline:
                ctype = preview_type(ctype) or ctype
            disposition, extra = attachment_headers(ctype, attachment_name(part, n), inline)
            self._send(200, attachment_payload(part), ctype, (("Content-Disposition", disposition),) + extra)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            m = SOURCE_RE.match(path)
            t = TAG_RE.match(path)
            mk = MARKS_RE.match(path)
            af = ARCHIVE_FOLDERS_RE.match(path)
            limit = MAX_BODY
            if m is not None and m[2] == "icon":
                limit = MAX_ICON
            elif path == "/api/transfer":
                limit = MAX_TRANSFER
            raw = self._read_body(limit)  # read first, so refused requests leave no unread body behind
            port = self.server.server_port
            if self.headers.get("Host") not in _hosts(port):
                return self._json(403, {"error": "host not allowed"})
            if self.headers.get("Origin") not in _origins(port):  # a missing Origin is refused too
                return self._json(403, {"error": "origin not allowed"})
            if m is None and t is None and mk is None and af is None and path not in POST_ROUTES:
                return self._json(404, error_body(msg("err.not_found")))
            if len(raw) > limit:
                return self._json(400, error_body(msg("err.body_too_large")))
            if t is not None:
                if t[2] == "remove":
                    return self._tags_admin(lambda tags: tags.delete_tag(int(t[1])))
                return self._tag_update(int(t[1]), raw)
            if mk is not None:
                return self._marks_update(unquote(mk[1]), unquote(mk[2]), raw)
            if af is not None:
                return self._archive_folders(unquote(af[1]), raw)
            if m is None:
                if path == "/api/tags":
                    return self._tag_create(raw)
                if path == "/api/fetch":
                    return self._fetch(raw)
                if path == "/api/library":
                    return self._set_library(raw)
                if path == "/api/language":
                    return self._set_language(raw)
                if path == "/api/sources":
                    return self._add_source(raw)
                if path == "/api/import":
                    return self._import(raw)
                if path == "/api/cleanup":
                    return self._cleanup(raw)
                if path == "/api/archives":
                    return self._create_archive(raw)
                if path == "/api/transfer":
                    return self._transfer(raw)
                if path == "/api/cli/install":
                    return self._cli_install()
                return self._settings(raw)
            source_id = unquote(m[1])
            if m[2] == "icon":
                return self._set_icon(source_id, raw)
            if m[2] == "auto":
                return self._auto(source_id, raw)
            if m[2] == "remove":
                return self._remove_source(source_id)
            self._update_source(source_id, raw)

        def _admin(self, change: Callable[[], None], reply: Callable[[], Any]) -> None:
            """Run an admin change, then answer with reply(). Busy is 409, unknown ids 404, bad input 400."""
            try:
                change()
                body = reply()
            except Busy as exc:
                return self._json(409, error_body(detail(exc)))
            except KeyError:
                return self._json(404, error_body(msg("err.unknown_source")))
            except sqlite3.Error as exc:
                return self._json(500, error_body(msg("err.index", detail=detail(exc))))
            except (ValueError, OSError) as exc:  # LibraryError included
                return self._json(400, error_body(detail(exc)))
            self._json(200, body)

        def _tags_admin(self, change: Callable[[TagStore], Any]) -> None:
            """Run a tag change without Viewer._lock and without a job check, then answer with the tag
            list of all visible sources. KeyError is 404, ValueError 400, sqlite3 errors 500; no library is 409."""
            lib, store, tags = viewer.library, viewer.store, viewer.tags  # one snapshot for the whole request
            if lib is None or store is None or tags is None:
                return self._json(409, error_body(msg("err.no_library")))
            try:
                change(tags)
                reply = tag_list(store, tags, selected_sources(lib, {}))
            except KeyError:
                return self._json(404, error_body(msg("err.unknown_tag")))
            except sqlite3.Error as exc:
                return self._json(500, error_body(msg("err.index", detail=detail(exc))))
            except ValueError as exc:
                return self._json(400, error_body(detail(exc)))
            self._json(200, reply)

        def _tag_create(self, raw: bytes) -> None:
            def change(tags: TagStore) -> None:
                body = load_json_object(raw)
                only_keys(body, TAG_FIELDS)
                name = check_tag_name(text_field(body, "name"))
                tags.create_tag(name, tag_color(body.get("color"), name))
            self._tags_admin(change)

        def _tag_update(self, tag_id: int, raw: bytes) -> None:
            def change(tags: TagStore) -> None:
                body = load_json_object(raw)
                only_keys(body, TAG_FIELDS)
                name = check_tag_name(text_field(body, "name")) if "name" in body else None
                color = check_tag_color(body["color"]) if "color" in body else None
                tags.update_tag(tag_id, name, color)
            self._tags_admin(change)

        def _marks_update(self, src: str, mid: str, raw: bytes) -> None:
            """Set a mail's star and tags by its identity. Like the tag routes: no Viewer._lock, no job check."""
            lib, store, tags = viewer.library, viewer.store, viewer.tags  # one snapshot for the whole request
            if lib is None or store is None or tags is None:
                return self._json(409, error_body(msg("err.no_library")))
            try:
                body = load_json_object(raw)
                only_keys(body, MARK_FIELDS)
                starred = body.get("starred")
                if "starred" in body and not isinstance(starred, bool):
                    raise UserError("err.starred_bool")
                tag_ids = body.get("tags")
                if "tags" in body and not (isinstance(tag_ids, list) and all(type(x) is int for x in tag_ids)):
                    raise UserError("err.tags_list")
                ident = store.ident(src, mid)  # None for unknown messages and unknown sources
                if ident is None:
                    return self._json(404, error_body(msg("err.unknown_message")))
                if ident == "":
                    raise UserError("err.mail_no_eml")
                reply = tags.set_marks(ident, starred, tag_ids)
            except KeyError:
                return self._json(404, error_body(msg("err.unknown_tag")))
            except sqlite3.Error as exc:
                return self._json(500, error_body(msg("err.index", detail=detail(exc))))
            except ValueError as exc:
                return self._json(400, error_body(detail(exc)))
            self._json(200, reply)

        def _sources_reply(self) -> list[Json]:
            lib, store = viewer.library, viewer.store
            if lib is None or store is None:
                raise Busy("err.no_library")
            return source_list(viewer, lib, store)

        def _add_source(self, raw: bytes) -> None:
            def change() -> None:
                body = load_json_object(raw)
                path = body.get("path")
                if not isinstance(path, str) or not path.strip():
                    raise UserError("err.path_required")
                viewer.add_source(path.strip(), check_text(text_field(body, "name"), "name"))
            self._admin(change, self._sources_reply)

        def _update_source(self, source_id: str, raw: bytes) -> None:
            def change() -> None:
                body = load_json_object(raw)
                only_keys(body, SOURCE_FIELDS)
                icon = body.get("icon")
                if isinstance(icon, dict) and icon.get("type") == "image":
                    raise UserError("err.icon_via_upload")
                position = body.get("position")
                if "position" in body and type(position) is not int:
                    raise UserError("err.position_int")
                viewer.update_source(source_id, name=body.get("name"), icon=icon, hidden=body.get("hidden"),
                                     position=position, archive_target=body.get("archive_target"))
            self._admin(change, self._sources_reply)

        def _remove_source(self, source_id: str) -> None:
            self._admin(lambda: viewer.remove_source(source_id), self._sources_reply)

        def _create_archive(self, raw: bytes) -> None:
            created: list[Source] = []

            def change() -> None:
                body = load_json_object(raw)
                only_keys(body, ("name",))
                name = check_text(text_field(body, "name"), "name").strip()
                if not name:
                    raise UserError("err.name_empty")
                created.append(viewer.create_archive(name))

            self._admin(change, lambda: next(s for s in self._sources_reply() if s["id"] == created[0].id))

        def _archive_folders(self, source_id: str, raw: bytes) -> None:
            def change() -> None:
                body = load_json_object(raw)
                only_keys(body, ARCHIVE_FOLDER_FIELDS)
                op = body.get("op")
                if op not in ARCHIVE_FOLDER_OPS:
                    raise UserError("err.folder_action")
                name = opt_text(body, "name")
                parent = opt_text(body, "parent")
                folder_id = opt_text(body, "id")
                index = body.get("index")
                if index is not None and type(index) is not int:
                    raise UserError("err.index_int")
                if op in ("create", "rename") and not (name or "").strip():
                    raise UserError("err.folder_name_missing")
                if op != "create" and folder_id is None:
                    raise UserError("err.folder_id_missing")
                viewer.start_archive_folders(source_id, op, name, parent, folder_id, index)
            self._admin(change, dict)

        def _transfer(self, raw: bytes) -> None:
            def change() -> None:
                body = load_json_object(raw)
                only_keys(body, TRANSFER_FIELDS)
                items = body.get("items")
                if not isinstance(items, list) or not 1 <= len(items) <= MAX_TRANSFER_ITEMS:
                    raise UserError("err.transfer_count", max=MAX_TRANSFER_ITEMS)
                pairs = []
                for item in items:
                    if not isinstance(item, dict):
                        raise UserError("err.transfer_item")
                    only_keys(item, TRANSFER_ITEM_FIELDS)
                    pairs.append((check_id(text_field(item, "source"), "source"), check_id(text_field(item, "id"), "id")))
                new_folder = body.get("new_folder")
                if new_folder is not None:
                    if not isinstance(new_folder, dict):
                        raise UserError("err.new_folder_object")
                    only_keys(new_folder, NEW_FOLDER_FIELDS)
                    new_folder = (text_field(new_folder, "name"), opt_text(new_folder, "parent"))
                viewer.start_transfer(pairs, check_id(text_field(body, "archive"), "archive"),
                                      opt_text(body, "folder"), new_folder)
            self._admin(change, dict)

        def _auto(self, source_id: str, raw: bytes) -> None:
            def change() -> None:
                body = load_json_object(raw)
                only_keys(body, AUTO_FIELDS)
                action = body.get("action")
                if action not in AUTO_ACTIONS:
                    raise UserError("err.auto_action")
                viewer.start_auto(source_id, action)
            self._admin(change, dict)

        def _set_icon(self, source_id: str, raw: bytes) -> None:
            ext = ICON_TYPES.get(self.headers.get("Content-Type") or "")
            if ext is None:
                return self._json(400, error_body(msg("err.icon_type")))
            if not valid_icon(ext, raw):
                return self._json(400, error_body(msg("err.invalid_image")))
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
                return self._json(404, error_body(msg("err.unknown_source")))
            if src.icon.get("type") != "image":
                return self._json(404, error_body(msg("err.no_icon")))
            ctype = {f"{src.id}.{ext}": ct for ext, ct in ICON_CTYPES.items()}.get(src.icon.get("file"))
            if ctype is None:
                return self._json(404, error_body(msg("err.no_icon")))
            icons = os.path.realpath(os.path.join(lib.root, ICON_DIR))
            path = os.path.realpath(os.path.join(icons, src.icon["file"]))
            if os.path.dirname(path) != icons or not os.path.isfile(path):  # e.g. a symlink out of icons/
                return self._json(404, error_body(msg("err.no_icon")))
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, error_body(msg("err.no_icon")))
            self._send(200, body, ctype, (("Content-Security-Policy", ICON_CSP),))

        def _fetch(self, raw: bytes) -> None:
            if viewer.library is None:
                return self._json(409, error_body(msg("err.no_library")))
            try:
                source, email, password, mailbox, totp = parse_fetch_body(raw)
                viewer.start_fetch(source, email, password, mailbox, totp)
            except Busy as exc:
                return self._json(409, error_body(detail(exc)))
            except FetchUnavailable as exc:
                return self._json(404, error_body(detail(exc)))
            except ValueError as exc:  # LibraryError included
                return self._json(400, error_body(detail(exc)))
            self._json(202, {"state": "running"})

        def _cli_install(self) -> None:
            try:
                viewer.start_cli_install()
            except Busy as exc:
                return self._json(409, error_body(detail(exc)))
            except ValueError as exc:
                return self._json(400, error_body(detail(exc)))
            self._json(202, {"state": "running"})

        def _import(self, raw: bytes) -> None:
            try:
                body = load_json_object(raw)
                only_keys(body, IMPORT_FIELDS)
                path = text_field(body, "path").strip()
                if not path:
                    raise UserError("err.path_required")
                viewer.start_import(path, text_field(body, "name"))
            except Busy as exc:
                return self._json(409, error_body(detail(exc)))
            except ImportUnavailable:
                return self._json(503, error_body(install_hint()))
            except (ValueError, OSError) as exc:  # LibraryError included
                return self._json(400, error_body(detail(exc)))
            self._json(200, {"started": True})

        def _cleanup(self, raw: bytes) -> None:
            try:
                body = load_json_object(raw)
                only_keys(body, CLEANUP_FIELDS)
                target, reference = text_field(body, "target"), text_field(body, "reference")
                if body.get("action") not in ACTIONS:
                    raise UserError("err.cleanup_action")
                viewer.start_cleanup(target, reference, body["action"])
            except Busy as exc:
                return self._json(409, error_body(detail(exc)))
            except KeyError:
                return self._json(404, error_body(msg("err.unknown_source")))
            except (ValueError, OSError) as exc:
                return self._json(400, error_body(detail(exc)))
            self._json(202, {"state": "running"})

        def _boot(self) -> dict:
            return i18n.boot(load_language(viewer.config_path), self.headers.get("Accept-Language"))

        def _i18n_script(self) -> None:
            body = "window.I18N_BOOT = " + json.dumps(self._boot(), ensure_ascii=False).replace("</", "<\\/") + ";\n"
            self._send(200, body.encode("utf-8"), "text/javascript; charset=utf-8", (("Cache-Control", "no-store"),))

        def _set_language(self, raw: bytes) -> None:
            try:
                language = json.loads(raw.decode("utf-8")).get("language")
            except (ValueError, RecursionError, AttributeError):
                language = None
            codes = [l["code"] for l in i18n.languages()]
            if not isinstance(language, str) or (language != "system" and language not in codes):
                return self._json(400, error_body(i18n.msg("err.invalid_language")))
            try:
                update_config(viewer.config_path, {"language": None if language == "system" else language})
            except OSError as exc:
                return self._json(500, error_body(i18n.msg("err.config_write", detail=i18n.detail(exc))))
            self._json(200, self._boot())

        def _set_library(self, raw: bytes) -> None:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):  # invalid UTF-8 and nesting too deep included
                return self._json(400, error_body(msg("err.invalid_json")))
            try:
                path = data["path"]
                if not isinstance(path, str) or not path.strip():
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, error_body(msg("err.path_required")))
            try:
                # an absent "remember" leaves the saved choice alone
                remember = bool(data["remember"]) if "remember" in data else None
                viewer.open_library(path.strip(), create=bool(data.get("create")),
                                    remember=remember,
                                    add_legacy=bool(data.get("add_legacy")))
            except Busy:
                return self._json(409, error_body(msg("err.busy_index")))
            except (ValueError, OSError) as exc:
                return self._json(400, error_body(detail(exc)))
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
                "library_shown": portal.host_path(lib.root) if lib is not None else None,
                "saved": viewer.saved,
                "saved_error": viewer.saved_error.to_json() if viewer.saved_error else None,
                "legacy": load_legacy(viewer.config_path) if viewer.config_path else None,
                "cli_override": viewer.cli if viewer.explicit_cli else None,  # --cli wins over the setting
                "cli_installed": protoncli.installed_info(viewer.cli_dir),
                "cli_installable": protoncli.platform_key() is not None,
                "home": os.path.expanduser("~"),
                "settings": asdict(lib.settings) if lib is not None else None,
                "outlook_import": pypff_available(),
                "install_hint": install_hint().to_json(),
            }

        def _static(self, rel: str, ctype: str) -> None:
            try:
                with open(os.path.join(static_dir, rel), "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, {"error": f"{rel} not found"})
            self._send(200, body, ctype)

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


def make_server(library_dir: str | None = None, port: int = 0, static_dir: str = STATIC_DIR,
                cli: str | None = None, config_path: str | None = None,
                cli_dir: str | None = None) -> ThreadingHTTPServer:
    """Bind 127.0.0.1:port. cli=None auto-detects the export CLI per source (see Viewer.cli_for).

    An explicit library_dir must hold a library (LibraryError otherwise). Without it the library
    saved in config_path is opened; if that fails or none is saved, the viewer starts empty and
    the page asks for a library folder."""
    config_path = config_path or default_config_path()
    explicit_cli = cli is not None
    cli_path = os.path.abspath(cli) if cli is not None else None
    if cli_path is not None and not is_executable(cli_path):
        cli_path = None  # fetching is simply unavailable
    viewer = Viewer(cli_path, explicit_cli, config_path, cli_dir)
    if library_dir is not None:
        viewer.open_library(library_dir)
    elif viewer.saved:
        try:
            viewer.open_library(viewer.saved)
        except (ValueError, OSError) as exc:
            viewer.saved_error = msg("err.saved_library", path=viewer.saved, detail=detail(exc))
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
