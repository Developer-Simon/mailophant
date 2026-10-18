"""Remembered library folder in the user config file."""

from __future__ import annotations

import json
import os

from mailviewer.cleanup import remove_quiet

def default_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "mail-backup-viewer", "config.json")


def _config_value(config_path: str, key: str) -> str | None:
    try:
        with open(config_path, encoding="utf-8") as f:
            value = json.load(f).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def load_saved(config_path: str) -> str | None:
    return _config_value(config_path, "library")


def load_language(config_path: str) -> str | None:
    return _config_value(config_path, "language")


def load_legacy(config_path: str) -> str | None:
    """Export dir of the viewer before libraries existed; offered once, then added as a source."""
    return _config_value(config_path, "export_dir")


def update_config(config_path: str, updates: dict[str, str | None]) -> None:
    """Set or (value None) remove keys of the config file; unknown keys are kept. The file is
    replaced atomically, and removed when nothing is left in it."""
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key, value in updates.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    if not data:
        remove_quiet(config_path)
        return
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    tmp = f"{config_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, config_path)


def save_saved(config_path: str, library_dir: str | None) -> None:
    """Persist the library folder; None forgets it."""
    update_config(config_path, {"library": library_dir})
