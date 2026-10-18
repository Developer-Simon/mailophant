"""Plan, brake and run the automatic archive of mails that vanished from the source.

Exports are flat directories: labels.json plus <mid>.eml and <mid>.metadata.json per mail.
Unchanged files are hardlinked between exports, so identities are cached per inode.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Any, Callable

import transfer
from store import META_SUFFIX, mail_ident

MAX_VANISHED_RATIO = 0.2
MAX_VANISHED_COUNT = 200
EXCLUDED_LABELS = frozenset({"1", "3", "4", "8"})
INBOX_NAME = "Unsortiert"


@dataclass
class Candidate:
    ident: str
    export_dir: str
    mid: str


@dataclass
class Plan:
    candidates: list[Candidate]
    vanished: int
    excluded: int
    reference_total: int
    unreadable: int


def plan_vanished(old_dirs: list[str], new_dir: str) -> Plan:
    """old_dirs oldest first; a mail counts with the labels of the newest old export holding it.

    unreadable counts only the old exports: a mail there that cannot be read is neither archived nor
    comparable, so the caller must not delete those exports. The new export is not counted.
    """
    cache: dict[tuple[int, int], str] = {}
    latest: dict[str, tuple[str, str, list[str]]] = {}
    unreadable = 0
    reference_total = 0
    for d in old_dirs:
        entries, bad = _scan(d, cache)
        unreadable += bad
        for ident, (mid, labels) in entries.items():
            latest[ident] = (d, mid, labels)
        reference_total = len(entries)

    new_entries, _ = _scan(new_dir, cache)

    candidates: list[Candidate] = []
    excluded = 0
    vanished = [ident for ident in latest if ident not in new_entries]
    for ident in vanished:
        d, mid, labels = latest[ident]
        if EXCLUDED_LABELS & set(labels):
            excluded += 1
        else:
            candidates.append(Candidate(ident, d, mid))
    return Plan(candidates, len(vanished), excluded, reference_total, unreadable)


def _scan(export_dir: str, cache: dict[tuple[int, int], str]) -> tuple[dict[str, tuple[str, list[str]]], int]:
    """Return {ident: (mid, label_ids)} and the number of unreadable mails."""
    entries: dict[str, tuple[str, list[str]]] = {}
    unreadable = 0
    for name in sorted(os.listdir(export_dir)):
        if not name.endswith(META_SUFFIX):
            continue
        mid = name[: -len(META_SUFFIX)]
        try:
            labels = _label_ids(os.path.join(export_dir, name))
            eml = os.path.join(export_dir, mid + ".eml")
            st = os.stat(eml)
        except (OSError, ValueError, KeyError, TypeError):
            unreadable += 1
            continue
        key = (st.st_dev, st.st_ino)
        if key not in cache:
            cache[key] = mail_ident(eml)
        if not cache[key]:
            unreadable += 1
            continue
        entries[cache[key]] = (mid, labels)
    return entries, unreadable


def _label_ids(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        payload: Any = json.load(f)["Payload"]
    if not isinstance(payload, dict):
        raise ValueError("Payload is not an object")
    labels = payload.get("LabelIDs")
    return [str(x) for x in labels] if isinstance(labels, list) else []


def should_brake(plan: Plan) -> bool:
    return plan.vanished > MAX_VANISHED_COUNT or plan.vanished > MAX_VANISHED_RATIO * plan.reference_total


def archive_candidates(candidates: list[Candidate], dest_dir: str, folder_id: str | None,
                       log: Callable[[str], None], progress: Callable[[int, int], None] | None = None
                       ) -> transfer.TransferResult:
    items = [transfer.Item(c.export_dir, c.mid, c.ident, "copy") for c in candidates]
    return transfer.transfer(items, dest_dir, folder_id, log, progress)


def delete_exports(old_dirs: list[str], active_dir: str, log: Callable[[str], None]) -> int:
    active = os.path.realpath(active_dir)
    deleted = 0
    for d in old_dirs:
        if os.path.islink(d):
            log(f"{d}: Symlink, übersprungen")
            continue
        if not os.path.basename(os.path.normpath(d)).startswith("mail_"):
            log(f"{d}: kein Exportordner, übersprungen")
            continue
        if not os.path.isfile(os.path.join(d, "labels.json")):
            log(f"{d}: ohne labels.json, übersprungen")
            continue
        if os.path.realpath(d) == active:
            log(f"{d}: aktiver Export, übersprungen")
            continue
        try:
            shutil.rmtree(d)
        except OSError as exc:
            log(f"{d}: Löschen fehlgeschlagen: {exc}")
            break
        deleted += 1
    return deleted
