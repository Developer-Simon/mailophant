"""Language files shared with the browser and Electron, and structured server messages."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

I18N_DIR = Path(__file__).resolve().parent.parent / "static" / "i18n"
PLACEHOLDER = re.compile(r"\{(\w+)(?::(\w+))?\}")


def _load(name: str, default):
    try:
        with open(I18N_DIR / name, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def languages() -> list[dict[str, str]]:
    data = _load("languages.json", [])
    return [l for l in data if isinstance(l, dict) and isinstance(l.get("code"), str)] or [
        {"code": "en", "name": "English"}]


def _codes() -> list[str]:
    return [l["code"] for l in languages()]


@cache
def _messages(lang: str) -> dict:
    data = _load(f"{lang}.json", {})
    return data if isinstance(data, dict) else {}


def messages(lang: str) -> dict[str, str | dict[str, str]]:
    return _messages(lang) if lang in _codes() else {}


def format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@dataclass(frozen=True)
class Msg:
    code: str | None
    params: Mapping[str, Any] = field(default_factory=dict)
    raw_text: str | None = None

    @property
    def text(self) -> str:
        if self.code is None:
            return self.raw_text or ""
        value = messages("en").get(self.code)
        return self.code if value is None else render(value, self.params)

    def to_json(self) -> dict:
        if self.code is None:
            return {"text": self.raw_text or ""}
        return {"text": self.text, "code": self.code,
                "params": {k: v.to_json() if isinstance(v, Msg) else v for k, v in self.params.items()}}


def render(value: str | dict[str, str], params: Mapping[str, Any]) -> str:
    if isinstance(value, dict):
        count = params.get("count")
        form = "one" if count == 1 else "other"
        value = value.get(form, value.get("other", ""))

    def sub(m: re.Match) -> str:
        name, fmt = m.group(1), m.group(2)
        if name not in params:
            return m.group(0)
        v = params[name]
        if isinstance(v, Msg):
            return v.text
        if fmt == "size" and isinstance(v, (int, float)):
            return format_size(int(v))
        if isinstance(v, int) and not isinstance(v, bool):
            return f"{v:,}"
        return str(v)

    return PLACEHOLDER.sub(sub, value)


def msg(code: str, **params) -> Msg:
    return Msg(code, params)


def raw(line: str) -> Msg:
    return Msg(None, {}, line)


class UserError(ValueError):
    def __init__(self, code: str, **params):
        self.msg = Msg(code, params)
        super().__init__(self.msg.text)


def detail(exc: BaseException) -> Msg:
    m = getattr(exc, "msg", None)
    if isinstance(m, Msg):
        return m
    return raw(str(exc) or type(exc).__name__)


def error_body(m: Msg) -> dict:
    body: dict[str, Any] = {"error": m.text}
    if m.code:
        body["code"] = m.code
        body["params"] = m.to_json()["params"]
    return body


def resolve_language(setting, accept_language: str | None) -> str:
    codes = _codes()
    if isinstance(setting, str) and setting in codes:
        return setting
    best, best_q = None, 0.0
    for part in (accept_language or "").split(","):
        pieces = part.strip().split(";")
        tag = pieces[0].strip().lower()
        if not tag or tag == "*":
            continue
        q = 1.0
        for p in pieces[1:]:
            p = p.strip()
            if p.lower().startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        base = tag.split("-")[0]
        if q > best_q:
            best, best_q = base, q
    return best if best in codes else "en"


def boot(setting, accept_language: str | None) -> dict:
    lang = resolve_language(setting, accept_language)
    known = isinstance(setting, str) and setting in _codes()
    return {"setting": setting if known else "system", "lang": lang, "languages": languages(),
            "messages": messages(lang), "fallback": messages("en")}
