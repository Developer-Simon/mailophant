"""Library folder: library.json lists the mail sources (Proton accounts or maildirs) shown together.

Mail files are never touched here. Only library.json and icons/<id>.* are written.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

LIBRARY_FILE = "library.json"
LIBRARY_VERSION = 1
ICON_DIR = "icons"
KINDS = ("proton", "maildir", "outlook", "archive")
ARCHIVE_KINDS = ("outlook", "archive")
VIEWS = ("combined", "single")
MAX_EMOJI_LEN = 16
ID_RE = re.compile(r"^s([1-9]\d*)$")

Json = dict[str, Any]


class LibraryError(ValueError):
    """The message is shown to the user as is."""


@dataclass
class Source:
    id: str
    name: str
    kind: str
    path: str  # relative to the library if the source lies inside it, else absolute
    icon: Json = field(default_factory=lambda: {"type": "auto"})
    hidden: bool = False
    archive_target: str = ""  # id of an archive source that a proton source is archived into, "" for none


@dataclass
class Settings:
    view: str = "combined"
    proton_email: str = ""
    cli: str = ""


def _has_mail_dir(path: str) -> bool:
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return any(
        n.startswith("mail_") and os.path.isfile(os.path.join(path, n, "labels.json"))
        for n in names
    )


def detect_kind(path: str) -> str:
    """"maildir" if path holds labels.json, "proton" if it holds mail_* exports, else LibraryError."""
    if os.path.isfile(os.path.join(path, "labels.json")):
        return "maildir"
    if _has_mail_dir(path):
        return "proton"
    raise LibraryError(f"Kein Mail-Export gefunden in {path}.")


def is_archive(src: Source) -> bool:
    return src.kind in ARCHIVE_KINDS


def is_library(path: str) -> bool:
    return os.path.isfile(os.path.join(path, LIBRARY_FILE))


def _check_icon(icon: Any) -> Json:
    if not isinstance(icon, dict):
        raise LibraryError("Ungültiges Icon.")
    kind = icon.get("type")
    if kind == "auto":
        return {"type": "auto"}
    if kind == "emoji":
        value = icon.get("value")
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_EMOJI_LEN:
            raise LibraryError(f"Das Emoji muss 1 bis {MAX_EMOJI_LEN} Zeichen lang sein.")
        return {"type": "emoji", "value": value}
    if kind == "image":
        file = icon.get("file")
        if not isinstance(file, str) or not file or ".." in file or "/" in file or "\\" in file:
            raise LibraryError("Ungültiger Dateiname für das Icon.")
        return {"type": "image", "file": file}
    raise LibraryError("Unbekannter Icon-Typ.")


def _check_settings(settings: Settings) -> Settings:
    for value in asdict(settings).values():
        if not isinstance(value, str):
            raise LibraryError("Einstellungen müssen Text sein.")
    if settings.view not in VIEWS:
        raise LibraryError(f"Ansicht muss {' oder '.join(VIEWS)} sein.")
    return settings


def _source_from(item: Any) -> Source:
    if not isinstance(item, dict):
        raise ValueError("source is not an object")
    src = Source(id=item["id"], name=item["name"], kind=item["kind"], path=item["path"],
                 icon=_check_icon(item["icon"]), hidden=item["hidden"],
                 archive_target=item.get("archive_target", ""))
    if src.kind not in KINDS or not isinstance(src.id, str) or not isinstance(src.name, str) \
            or not isinstance(src.path, str) or type(src.hidden) is not bool \
            or not isinstance(src.archive_target, str):
        raise ValueError("bad source field")
    return src


class Library:
    def __init__(self, root: str, sources: list[Source], settings: Settings, next_id: int) -> None:
        self.root = os.path.abspath(root)
        self.sources = sources
        self.settings = settings
        self.next_id = next_id

    @classmethod
    def open(cls, root: str) -> Library:
        """Load library.json. Missing, corrupt or newer files raise and are never rewritten."""
        path = os.path.join(root, LIBRARY_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise LibraryError(f"Keine Bibliothek in {root}.") from None
        except (OSError, ValueError) as exc:
            raise LibraryError(f"{path} ist beschädigt und wird nicht überschrieben.") from exc
        version = data.get("version") if isinstance(data, dict) else None
        if type(version) is not int or version > LIBRARY_VERSION:
            raise LibraryError(f"{path} stammt aus einer neueren Version und wird nicht geöffnet.")
        if version != LIBRARY_VERSION:
            raise LibraryError(f"{path} hat eine unbekannte Version.")
        try:
            sources = [_source_from(item) for item in data["sources"]]
            settings = _check_settings(Settings(**data["settings"]))
            next_id = data["next_id"]
            if type(next_id) is not int or next_id < 1:
                raise ValueError("bad next_id")
            ids = [s.id for s in sources]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate ids")
            for source_id in ids:
                match = ID_RE.match(source_id)
                if not match or int(match.group(1)) >= next_id:
                    raise ValueError("bad id")
        except (KeyError, TypeError, ValueError) as exc:
            raise LibraryError(f"{path} ist beschädigt und wird nicht überschrieben.") from exc
        return cls(root, sources, settings, next_id)

    @classmethod
    def create(cls, root: str) -> Library:
        if os.path.lexists(os.path.join(root, LIBRARY_FILE)):
            raise LibraryError(f"In {root} gibt es schon eine Bibliothek.")
        if os.path.exists(root) and not os.path.isdir(root):
            raise LibraryError(f"{root} ist kein Ordner.")
        lib = cls(root, [], Settings(), next_id=1)
        lib.save()
        return lib

    def save(self, sources: list[Source] | None = None, settings: Settings | None = None,
             next_id: int | None = None) -> None:
        """Write library.json via a .tmp file and os.replace, so a crash leaves the old file.
        Writes the given candidate state (not yet adopted) or, by default, the current one."""
        os.makedirs(self.root, exist_ok=True)
        data = {
            "version": LIBRARY_VERSION,
            "next_id": self.next_id if next_id is None else next_id,
            "sources": [asdict(s) for s in (self.sources if sources is None else sources)],
            "settings": asdict(self.settings if settings is None else settings),
        }
        path = os.path.join(self.root, LIBRARY_FILE)
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def abspath(self, source: Source) -> str:
        return source.path if os.path.isabs(source.path) else os.path.join(self.root, source.path)

    def get(self, source_id: str) -> Source:
        for src in self.sources:
            if src.id == source_id:
                return src
        raise KeyError(source_id)

    def add_source(self, path: str, name: str = "", kind: str | None = None) -> Source:
        if kind is None:
            kind = detect_kind(path)
        elif kind in ARCHIVE_KINDS:
            # Outlook files and archives are kept as maildirs, so only a maildir can be added as one.
            if detect_kind(path) != "maildir":
                raise LibraryError(f"Kein Maildir-Export in {path}.")
        else:
            raise LibraryError(f"Unbekannte Quellenart: {kind}")
        real = os.path.realpath(path)
        # Realpath comparison catches symlinks and "a/../a" spellings of the same folder.
        if any(os.path.realpath(self.abspath(s)) == real for s in self.sources):
            raise LibraryError("Diese Quelle ist schon in der Bibliothek.")
        root_real = os.path.realpath(self.root)
        if os.path.commonpath([root_real, real]) == root_real:
            stored = os.path.relpath(real, root_real)
        else:
            stored = real
        default_name = os.path.basename(os.path.normpath(os.path.abspath(path)))
        src = Source(id=f"s{self.next_id}", name=name.strip() or default_name, kind=kind, path=stored)
        sources, next_id = [*self.sources, src], self.next_id + 1
        self.save(sources, next_id=next_id)
        self.sources, self.next_id = sources, next_id
        return src

    def update_source(self, source_id: str, *, name: str | None = None,
                      icon: Json | None = None, hidden: bool | None = None,
                      archive_target: str | None = None) -> Source:
        src = self.get(source_id)
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise LibraryError("Der Name darf nicht leer sein.")
        if hidden is not None and not isinstance(hidden, bool):
            raise LibraryError("Ausblenden muss an oder aus sein.")
        if archive_target is not None:
            self._check_archive_target(src, archive_target)
        new_icon = _check_icon(icon) if icon is not None else None
        changes: Json = {}
        if name is not None:
            changes["name"] = name.strip()
        if new_icon is not None:
            changes["icon"] = new_icon
        if hidden is not None:
            changes["hidden"] = hidden
        if archive_target is not None:
            changes["archive_target"] = archive_target
        updated = replace(src, **changes)
        sources = [updated if s is src else s for s in self.sources]
        self.save(sources)
        self.sources = sources
        return updated

    def _check_archive_target(self, src: Source, target: str) -> None:
        if not isinstance(target, str):
            raise LibraryError("Ungültiges Archiv-Ziel.")
        if target == "":
            return
        if src.kind != "proton":
            raise LibraryError("Nur Proton-Quellen können in ein Archiv verschoben werden.")
        try:
            archive = self.get(target)
        except KeyError:
            raise LibraryError("Das Archiv-Ziel gibt es nicht.") from None
        if not is_archive(archive):
            raise LibraryError("Das Archiv-Ziel ist kein Archiv.")

    def move_source(self, source_id: str, position: int) -> None:
        src = self.get(source_id)
        rest = [s for s in self.sources if s is not src]
        rest.insert(max(0, min(position, len(rest))), src)
        self.sources = rest  # a new list: a reader holding the old one never sees it half-moved
        self.save()

    def remove_source(self, source_id: str) -> Source:
        """Forget the source and its icon. The mail folder itself is left alone.
        Proton sources that archived into it lose that target in the same save."""
        src = self.get(source_id)
        sources = [replace(s, archive_target="") if s.archive_target == src.id else s
                   for s in self.sources if s is not src]
        self.save(sources)
        self.sources = sources
        icons = os.path.join(self.root, ICON_DIR)
        try:
            names = os.listdir(icons)
        except OSError:
            names = []
        for name in names:
            full = os.path.join(icons, name)
            if name.startswith(f"{source_id}.") and os.path.isfile(full):
                os.remove(full)
        return src

    def update_settings(self, **fields: str) -> Settings:
        unknown = set(fields) - set(asdict(self.settings))
        if unknown:
            raise LibraryError(f"Unbekannte Einstellung: {', '.join(sorted(unknown))}")
        settings = _check_settings(replace(self.settings, **fields))
        self.save(settings=settings)
        self.settings = settings
        return settings
