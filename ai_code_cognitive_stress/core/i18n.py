"""Translatable strings for the rendered surfaces (report + widget card).

Every user-facing string the renderers emit comes from a flat key → string
catalog in ``locales/<code>.json``; ``locales/en.json`` ships with the package
and is the reference catalog. To add a language, drop another ``<code>.json``
with the same keys next to it and either set ``"locale": "<code>"`` in
``config.json`` or pass ``--locale <code>`` on the CLI. Missing keys fall back
to English, so a partial translation still renders a complete report.

Catalog values are plain text except where the consumer inserts them as raw
HTML (the report's methodology/recommendation copy) — those keys keep their
HTML entities/tags in the catalog, mirroring the renderer they came from.
Placeholders use ``str.format`` syntax (``{name}``); values without parameters
are returned verbatim, so literal braces are only an issue in parameterised
strings.

Pure stdlib (``json``), no I/O beyond reading the catalog files once per
locale.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"
DEFAULT_LOCALE = "en"
_LOCALE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

# Explicit override (CLI --locale / tests). None → resolve from config.json.
_active_locale: str | None = None
_catalog_cache: dict[str, dict[str, object]] = {}


def available_locales() -> list[str]:
    """Locale codes with a catalog file on disk."""
    return sorted(p.stem for p in _LOCALES_DIR.glob("*.json"))


def set_locale(name: str | None) -> None:
    """Explicitly select the active locale (overrides config.json).

    ``None`` clears the override. An unknown-but-valid code is accepted —
    every key then falls back to English — but a malformed code is rejected
    early so a typo surfaces as an error, not a silently-English report.
    """
    global _active_locale
    if name is not None and not _LOCALE_RE.match(name):
        raise ValueError(f"invalid locale code {name!r} (expected e.g. 'en', 'pt-BR')")
    _active_locale = name


def get_locale() -> str:
    """The active locale: explicit override, else config.json, else English."""
    if _active_locale is not None:
        return _active_locale
    try:
        from .config import load_config
        return load_config().locale
    except Exception:
        return DEFAULT_LOCALE


def _catalog(locale: str) -> dict[str, object]:
    cached = _catalog_cache.get(locale)
    if cached is not None:
        return cached
    path = _LOCALES_DIR / f"{locale}.json"
    data: dict[str, object] = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    _catalog_cache[locale] = data
    return data


def _lookup(key: str) -> object | None:
    for locale in (get_locale(), DEFAULT_LOCALE):
        value = _catalog(locale).get(key)
        if value is not None:
            return value
    return None


def t(key: str, **params: object) -> str:
    """Translate ``key``, interpolating ``params`` via ``str.format``.

    Unknown key → the key itself is returned, so a missing translation is
    visible in the output instead of crashing the render.
    """
    value = _lookup(key)
    if not isinstance(value, str):
        return key
    return value.format(**params) if params else value


def tn(key: str, count: int, **params: object) -> str:
    """Plural-aware ``t``: looks up ``<key>_one`` when count == 1, else
    ``<key>_other``. ``count`` is always available as a parameter."""
    suffix = "_one" if count == 1 else "_other"
    return t(f"{key}{suffix}", count=count, **params)


# ---------------------------------------------------------------------------
# Date names — strftime's %A/%B are C-locale-dependent and unrelated to the
# report locale, so weekday/month names come from the catalog instead.

def _names(key: str, expected: int) -> list[str]:
    value = _lookup(key)
    if isinstance(value, list) and len(value) == expected:
        return [str(v) for v in value]
    # Reference catalog is guaranteed complete; reaching here means en.json
    # itself is broken — fail loudly rather than render wrong dates.
    raise KeyError(f"locale catalog missing or malformed list {key!r}")


def weekday_name(d: date, short: bool = False) -> str:
    return _names("date.weekdays_short" if short else "date.weekdays", 7)[d.weekday()]


def month_name(month: int, short: bool = False) -> str:
    return _names("date.months_short" if short else "date.months", 12)[month - 1]


def weekday_names_short() -> list[str]:
    """Mon..Sun column headers (heatmap)."""
    return _names("date.weekdays_short", 7)


def day_label(d: date) -> str:
    """Full day heading, e.g. "Friday 29 May 2026" (was strftime %A %d %B %Y)."""
    return t(
        "date.day_label",
        weekday=weekday_name(d), day=f"{d.day:02d}",
        month=month_name(d.month), year=d.year,
    )


def day_month_label(d: date) -> str:
    """Short day reference, e.g. "Fri 29 May" (was strftime %a %d %b)."""
    return t(
        "date.day_month_label",
        weekday=weekday_name(d, short=True), day=f"{d.day:02d}",
        month=month_name(d.month, short=True),
    )


def month_year_label(year: int, month: int) -> str:
    """e.g. "May 2026" (was strftime %B %Y)."""
    return t("date.month_year", month=month_name(month), year=year)
