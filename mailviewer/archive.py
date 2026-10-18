"""Folder operations on an archive's labels.json.

Every write replaces the whole file (tmp file + os.replace), so hardlinked copies keep their old content.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from typing import Any, Callable

from mailviewer import store

Json = dict[str, Any]

ALL_MAIL = "5"
MAX_NAME = 100
SYSTEM_IDS = frozenset([*store.SYSTEM_FOLDER_ORDER, "1", "10"])


class ArchiveError(ValueError):
    pass


def atomic_write(path: str, data: bytes) -> None:
    folder, name = os.path.split(os.path.abspath(path))
    tmp = os.path.join(folder, "." + name + ".tmp")
    try:
        try:
            os.remove(tmp)  # a leftover tmp may be a hardlink: write a new file, never into it
        except FileNotFoundError:
            pass
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def write_empty_archive(mail_dir: str) -> None:
    all_mail = {"ID": ALL_MAIL, "Name": "All Mail", "Path": "All Mail", "Color": "", "Type": 3}
    atomic_write_json(os.path.join(mail_dir, "labels.json"), {"Version": 1, "Payload": [all_mail]})


def _new_id(path: str, taken: set[str]) -> str:
    """Path hash first; a renamed or moved folder keeps its ID, so a folder created later under the old path
    gets a counter-based ID instead of a duplicate."""
    n = 0
    while True:
        key = path if n == 0 else f"{path}#{n}"
        label_id = "f" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        if label_id not in taken:
            return label_id
        n += 1


def is_system_folder(label_id: str) -> bool:
    return label_id in SYSTEM_IDS


def create_folder(mail_dir: str, name: str, parent_id: str | None = None) -> Json:
    doc, labels = _load(mail_dir)
    name = check_name(name)
    parent_path = ""
    if parent_id is not None:
        if parent_id == ALL_MAIL:
            raise ArchiveError("Unter „All Mail“ können keine Ordner angelegt werden.")
        parent_path = _find(labels, parent_id, "Übergeordneter Ordner nicht gefunden.")["Path"]
    level = _level(labels, parent_path)
    _check_unique(level, name)
    path = parent_path + "/" + name if parent_path else name
    folder = {
        "ID": _new_id(path, {x["ID"] for x in labels}),
        "Name": name,
        "Path": path,
        "Color": "",
        "Type": 3,
        "Order": len(_ordered(level)) + 1,
    }
    labels.append(folder)
    _save(mail_dir, doc)
    return folder


def rename_folder(mail_dir: str, folder_id: str, name: str) -> None:
    doc, labels = _load(mail_dir)
    folder = _mutable(labels, folder_id)
    name = check_name(name)
    level = [x for x in _level(labels, _parent_of(folder)) if x is not folder]
    _check_unique(level, name)
    old = folder["Path"]
    new = _parent_of(folder) + "/" + name if _parent_of(folder) else name
    _move_paths(labels, old, new)
    folder["Name"] = name
    _save(mail_dir, doc)


def move_folder(mail_dir: str, folder_id: str, parent_id: str | None, index: int) -> None:
    doc, labels = _load(mail_dir)
    folder = _mutable(labels, folder_id)
    old = folder["Path"]
    new_parent = ""
    if parent_id is not None:
        if parent_id == ALL_MAIL:
            raise ArchiveError("„All Mail“ kann nicht als Ordner-Ziel verwendet werden.")
        parent = _find(labels, parent_id, "Zielordner nicht gefunden.")
        if parent["Path"] == old or parent["Path"].startswith(old + "/"):
            raise ArchiveError("Ein Ordner kann nicht in sich selbst verschoben werden.")
        new_parent = parent["Path"]
    old_level = [x for x in _level(labels, _parent_of(folder)) if x is not folder]
    new_level = [x for x in _level(labels, new_parent) if x is not folder]
    _check_unique(new_level, folder["Name"])
    _ordered(old_level)
    new_level = _ordered(new_level)
    new_level.insert(min(max(index, 0), len(new_level)), folder)
    _assign(new_level)
    _move_paths(labels, old, new_parent + "/" + folder["Name"] if new_parent else folder["Name"])
    _save(mail_dir, doc)


def delete_folder(mail_dir: str, folder_id: str, log: Callable[[str], None]) -> int:
    doc, labels = _load(mail_dir)
    folder = _mutable(labels, folder_id)
    prefix = folder["Path"] + "/"
    doomed = {x["ID"] for x in labels if x["Path"] == folder["Path"] or x["Path"].startswith(prefix)}
    rewritten = _strip_labels(mail_dir, doomed, log)
    labels[:] = [x for x in labels if x["ID"] not in doomed]
    _ordered(_level(labels, _parent_of(folder)))
    _save(mail_dir, doc)
    return rewritten


def ensure_folder(mail_dir: str, name: str) -> str:
    _, labels = _load(mail_dir)
    for x in _level(labels, ""):
        if not is_system_folder(x["ID"]) and x["Name"].casefold() == name.strip().casefold():
            return x["ID"]
    return create_folder(mail_dir, name)["ID"]


def _labels_path(mail_dir: str) -> str:
    return os.path.join(mail_dir, "labels.json")


def _load(mail_dir: str) -> tuple[Json, list[Json]]:
    """Whole labels.json document and its folder list; unreadable files are an error, never an empty archive."""
    try:
        with open(_labels_path(mail_dir), encoding="utf-8") as f:
            doc = json.load(f)
        payload = doc["Payload"]
        if not isinstance(payload, list) or not all(_valid_label(x) for x in payload):
            raise ValueError("bad payload")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArchiveError("Die Ordnerliste (labels.json) ist nicht lesbar.") from exc
    return doc, payload


def _valid_label(x: Any) -> bool:
    return (isinstance(x, dict) and isinstance(x.get("ID"), str) and x["ID"] != ""
            and isinstance(x.get("Name"), str) and isinstance(x.get("Path"), str))


def _doomed(label_id: Any, doomed: set[str]) -> bool:
    return isinstance(label_id, str) and label_id in doomed


def _save(mail_dir: str, doc: Json) -> None:
    atomic_write_json(_labels_path(mail_dir), doc)


def check_name(name: str) -> str:
    name = name.strip() if isinstance(name, str) else ""
    if (not name or len(name) > MAX_NAME or "/" in name
            or any(unicodedata.category(c) == "Cc" for c in name)):
        raise ArchiveError("Der Ordnername ist ungültig: Er darf nicht leer sein, höchstens 100 Zeichen lang sein, "
                           "kein „/“ und keine Steuerzeichen enthalten.")
    return name


def _find(labels: list[Json], folder_id: str, message: str) -> Json:
    for x in labels:
        if x["ID"] == folder_id:
            return x
    raise ArchiveError(message)


def _mutable(labels: list[Json], folder_id: str) -> Json:
    if is_system_folder(folder_id):
        raise ArchiveError("Systemordner können nicht geändert werden.")
    return _find(labels, folder_id, "Ordner nicht gefunden.")


def _parent_of(folder: Json) -> str:
    return folder["Path"].rpartition("/")[0]


def _level(labels: list[Json], parent_path: str) -> list[Json]:
    return [x for x in labels if _parent_of(x) == parent_path]


def _check_unique(level: list[Json], name: str) -> None:
    if any(x["Name"].casefold() == name.casefold() for x in level):
        raise ArchiveError("Am selben Ort gibt es bereits einen Ordner mit diesem Namen.")


def _ordered(level: list[Json]) -> list[Json]:
    """User folders of a level, sorted by Order (alphabetically if any lacks one), numbered 1..n."""
    movable = [x for x in level if not is_system_folder(x["ID"])]
    if all(type(x.get("Order")) is int for x in movable):
        movable.sort(key=lambda x: (x["Order"], x["Name"].casefold()))
    else:
        movable.sort(key=lambda x: x["Name"].casefold())
    _assign(movable)
    return movable


def _assign(level: list[Json]) -> None:
    for i, x in enumerate(level, 1):
        x["Order"] = i


def _move_paths(labels: list[Json], old: str, new: str) -> None:
    """Give a folder and all its descendants the new path prefix; IDs stay."""
    for x in labels:
        if x["Path"] == old:
            x["Path"] = new
        elif x["Path"].startswith(old + "/"):
            x["Path"] = new + x["Path"][len(old):]


def _strip_labels(mail_dir: str, doomed: set[str], log: Callable[[str], None]) -> int:
    rewritten = 0
    for name in sorted(os.listdir(mail_dir)):
        if not name.endswith(store.META_SUFFIX):
            continue
        path = os.path.join(mail_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            meta = doc["Payload"]
            if not isinstance(meta, dict):
                raise ValueError("Payload is not an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log(f"{name} übersprungen: {exc}")
            continue
        ids = meta.get("LabelIDs")
        if not isinstance(ids, list) or not any(_doomed(x, doomed) for x in ids):
            continue
        kept = [x for x in ids if not _doomed(x, doomed)]
        if ALL_MAIL not in kept:
            kept.append(ALL_MAIL)
        meta["LabelIDs"] = kept
        atomic_write_json(path, doc)
        rewritten += 1
    return rewritten
