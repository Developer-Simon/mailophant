"""Plan and run the cleanup of duplicate mails in one source.

Works on files only. The caller passes (target_mid, reference_mid) pairs, e.g. from
Store.duplicate_pairs. The reference directory is only read. The target changes only by
replacing a copy with a hardlink to the reference, or by deleting the copy.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from store import META_SUFFIX

ACTIONS = ("link", "delete")
CROSS_DEVICE_REASON = "Ziel und Referenz liegen auf verschiedenen Dateisystemen, Hardlinks sind nicht möglich."


@dataclass
class CleanupPlan:
    count: int  # pairs whose reference .eml exists, including already linked ones
    already_linked: int
    missing_reference: int
    bytes_link: int
    bytes_delete: int
    link_possible: bool
    link_reason: str | None


@dataclass
class CleanupResult:
    linked: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    freed: int = 0


def is_regular(path: str) -> bool:
    return os.path.isfile(path) and not os.path.islink(path)


def remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def check_dirs(target_dir: str, reference_dir: str) -> tuple[bool, str | None]:
    """Return (link_possible, reason). Raises ValueError for the same directory."""
    if os.path.realpath(target_dir) == os.path.realpath(reference_dir):
        raise ValueError("Ziel und Referenz sind derselbe Ordner.")
    if os.stat(target_dir).st_dev != os.stat(reference_dir).st_dev:
        return False, CROSS_DEVICE_REASON
    return True, None


def freed_if_unlinked(path: str) -> int:
    """Bytes that removing path would free: its size if it is a regular file with one link."""
    if not is_regular(path):
        return 0
    st = os.stat(path)
    return st.st_size if st.st_nlink == 1 else 0


def plan_cleanup(pairs: list[tuple[str, str]], target_dir: str, reference_dir: str) -> CleanupPlan:
    link_possible, link_reason = check_dirs(target_dir, reference_dir)
    count = already_linked = missing_reference = bytes_link = bytes_delete = 0
    for mid, rmid in pairs:
        ref = os.path.join(reference_dir, rmid + ".eml")
        if not is_regular(ref):
            missing_reference += 1
            continue
        count += 1
        tgt = os.path.join(target_dir, mid + ".eml")
        if not is_regular(tgt):
            continue
        eml_freed = freed_if_unlinked(tgt)
        if os.path.samefile(tgt, ref):
            already_linked += 1
        else:
            bytes_link += eml_freed
        bytes_delete += eml_freed + freed_if_unlinked(os.path.join(target_dir, mid + META_SUFFIX))
    return CleanupPlan(
        count=count,
        already_linked=already_linked,
        missing_reference=missing_reference,
        bytes_link=bytes_link,
        bytes_delete=bytes_delete,
        link_possible=link_possible,
        link_reason=link_reason,
    )


def replace_with_link(tgt: str, ref: str) -> int:
    """Replace tgt with a hardlink to ref. Returns the bytes freed."""
    freed = freed_if_unlinked(tgt)
    tmp = os.path.join(os.path.dirname(tgt), "." + os.path.basename(tgt) + ".link-tmp")
    try:
        os.link(ref, tmp)
        os.replace(tmp, tgt)  # atomic: tgt is never missing
    finally:
        if os.path.lexists(tmp):
            remove_quiet(tmp)
    return freed


def delete_copy(tgt: str, meta: str, log: Callable[[str], None]) -> int:
    """Delete the metadata first, so no metadata file is left without its .eml."""
    freed = freed_if_unlinked(tgt) + freed_if_unlinked(meta)
    if os.path.lexists(meta):
        os.remove(meta)
        log(f"delete {meta}")
    os.remove(tgt)
    log(f"delete {tgt}")
    return freed


def run_cleanup(
    pairs: list[tuple[str, str]],
    target_dir: str,
    reference_dir: str,
    action: str,
    log: Callable[[str], None],
) -> CleanupResult:
    if action not in ACTIONS:
        raise ValueError(f"unknown cleanup action: {action}")
    link_possible, link_reason = check_dirs(target_dir, reference_dir)
    if action == "link" and not link_possible:
        raise ValueError(link_reason)

    result = CleanupResult()
    for mid, rmid in pairs:
        tgt = os.path.join(target_dir, mid + ".eml")
        ref = os.path.join(reference_dir, rmid + ".eml")
        meta = os.path.join(target_dir, mid + META_SUFFIX)
        try:
            # Checked right before each change: the reference must be a regular file
            # and not the target itself.
            if not is_regular(ref):
                result.skipped += 1
                log(f"skip {tgt}: reference missing")
            elif not is_regular(tgt):
                result.skipped += 1
                log(f"skip {tgt}: copy missing")
            elif os.path.samefile(tgt, ref):
                result.skipped += 1
                log(f"skip {tgt}: already the same file as the reference")
            elif action == "link":
                result.freed += replace_with_link(tgt, ref)
                result.linked += 1
                log(f"link {tgt} -> {ref}")
            else:
                result.freed += delete_copy(tgt, meta, log)
                result.deleted += 1
        except OSError as e:
            result.failed += 1
            log(f"error {tgt}: {e}")
    return result
