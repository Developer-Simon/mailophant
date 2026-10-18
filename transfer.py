"""Copy, move or refile mails into an archive.

An origin is deleted only after its complete copy exists in the destination.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from typing import Any, Callable

import store
from archive import atomic_write_json

ALL_MAIL = "5"


@dataclass
class Item:
    src_dir: str
    mid: str
    ident: str
    mode: str  # "copy": origin read-only, "move": origin is another archive, "refile": origin is the destination


@dataclass
class TransferResult:
    copied: int = 0
    reused: int = 0
    refiled: int = 0
    deleted_origin: int = 0
    skipped: int = 0
    failed: int = 0


def dest_mid(ident: str) -> str:
    return "o" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:40]


def transfer(items: list[Item], dest_dir: str, folder_id: str | None,
             log: Callable[[str], None], progress: Callable[[int, int], None] | None = None) -> TransferResult:
    result = TransferResult()
    labels = [folder_id, ALL_MAIL] if folder_id and folder_id != ALL_MAIL else [ALL_MAIL]
    for i, item in enumerate(items, 1):
        try:
            _one(item, dest_dir, labels, result, log)
        except (OSError, ValueError) as exc:
            result.failed += 1
            log(f"{item.src_dir} {item.mid}: fehlgeschlagen: {exc}")
        if progress:
            progress(i, len(items))
    return result


def _one(item: Item, dest_dir: str, labels: list[str], result: TransferResult, log: Callable[[str], None]) -> None:
    if item.mode not in ("copy", "move", "refile"):
        raise ValueError(f"unbekannter Modus {item.mode!r}")
    if not item.ident:
        result.skipped += 1
        log(f"{item.src_dir} {item.mid}: ohne Kennung übersprungen")
        return
    if item.mode == "refile":
        _set_labels(os.path.join(dest_dir, item.mid + store.META_SUFFIX), labels)
        result.refiled += 1
        return

    mid = dest_mid(item.ident)
    dest_eml = os.path.join(dest_dir, mid + ".eml")
    dest_meta = os.path.join(dest_dir, mid + store.META_SUFFIX)
    if os.path.exists(dest_meta):
        _set_labels(dest_meta, labels)
        result.reused += 1
    else:
        _copy_mail(item, dest_dir, mid, dest_eml, dest_meta, labels)
        result.copied += 1

    if item.mode == "move" and not _is_origin_in_dest(item, dest_eml, dest_meta):
        if os.path.isfile(dest_eml) and os.path.isfile(dest_meta):
            _delete_origin(item, result)


def _copy_mail(item: Item, dest_dir: str, mid: str, dest_eml: str, dest_meta: str, labels: list[str]) -> None:
    src_eml = os.path.join(item.src_dir, item.mid + ".eml")
    meta = _read_payload(os.path.join(item.src_dir, item.mid + store.META_SUFFIX))
    tmp = os.path.join(dest_dir, "." + mid + ".eml.tmp")
    try:
        _remove(tmp)
        try:
            os.link(src_eml, tmp)
        except OSError:
            shutil.copyfile(src_eml, tmp)
        os.replace(tmp, dest_eml)
    finally:
        _remove(tmp)
    meta["LabelIDs"] = labels
    atomic_write_json(dest_meta, {"Version": 1, "Payload": meta})


def _set_labels(path: str, labels: list[str]) -> None:
    meta = _read_payload(path)
    meta["LabelIDs"] = labels
    atomic_write_json(path, {"Version": 1, "Payload": meta})


def _delete_origin(item: Item, result: TransferResult) -> None:
    for name in (item.mid + store.META_SUFFIX, item.mid + ".eml"):
        os.remove(os.path.join(item.src_dir, name))
    result.deleted_origin += 1


def _is_origin_in_dest(item: Item, dest_eml: str, dest_meta: str) -> bool:
    """Origin files that are the destination files themselves must never be deleted."""
    origin = {os.path.realpath(os.path.join(item.src_dir, item.mid + x)) for x in (".eml", store.META_SUFFIX)}
    return bool(origin & {os.path.realpath(dest_eml), os.path.realpath(dest_meta)})


def _read_payload(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    try:
        payload = doc["Payload"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{path}: kein Payload") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: Payload ist kein Objekt")
    return payload


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
