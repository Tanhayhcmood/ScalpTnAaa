"""Shared utility helpers."""

import os
import re


_GOLD_BASES = ("XAUUSD", "GOLD")
_SYMBOL_ALIAS_VALUE = re.compile(r"[A-Z0-9][A-Z0-9._#+-]*\Z")
_BROKER_SUFFIXES = tuple(
    sorted(
        {
            "_MICRO", ".MICRO", "-MICRO", "MICRO",
            "_CENTS", ".CENTS", "-CENTS", "CENTS",
            "_CENT", ".CENT", "-CENT", "CENT",
            "_PRO", ".PRO", "-PRO",
            "_ECN", ".ECN", "-ECN",
            "_RAW", ".RAW", "-RAW",
            "_STD", ".STD", "-STD",
            "_I", ".I", "-I",
            "_M", ".M", "-M",
            "_C", ".C", "-C",
            "_B", ".B", "-B", ".A", "_A", "-A",
            "B",
            "M", "C", "#", "+", "_", ".",
        },
        key=len,
        reverse=True,
    )
)


def _is_gold_alias_source(source: str) -> bool:
    """Accept gold-rooted aliases without allowing other instruments as gold."""
    for base in _GOLD_BASES:
        if source == base:
            return True
        if source.startswith(base):
            suffix = source[len(base):]
            if suffix and (
                suffix[0] in "_.-#+MC"
                or suffix.startswith(("MICRO", "CENT"))
            ):
                return True
    return False


def _parse_symbol_aliases(raw: str) -> dict[str, str]:
    """Parse safe, gold-rooted SYMBOL_ALIASES entries; ignore malformed ones."""
    aliases = {}
    if not isinstance(raw, str):
        return aliases

    for entry in raw.split(","):
        if "=" not in entry:
            continue
        source, target = (part.strip().upper() for part in entry.split("=", 1))
        if (
            not source
            or not target
            or not _SYMBOL_ALIAS_VALUE.fullmatch(source)
            or not _SYMBOL_ALIAS_VALUE.fullmatch(target)
            or not _is_gold_alias_source(source)
        ):
            continue
        aliases[source] = target
    return aliases


def canonical_symbol(raw: str, *, aliases: str | None = None) -> str:
    """Canonicalize gold aliases without changing the broker-facing symbol.

    The transformation has no cache. ``aliases`` can be supplied directly for
    deterministic callers/tests; otherwise ``SYMBOL_ALIASES`` is read from the
    environment and malformed entries are ignored.
    """
    try:
        symbol = str(raw or "").strip().upper()
    except Exception:
        return ""
    if not symbol:
        return ""

    alias_source = os.getenv("SYMBOL_ALIASES", "") if aliases is None else aliases
    override = _parse_symbol_aliases(alias_source).get(symbol)
    if override is not None:
        return override

    candidate = symbol
    while True:
        for suffix in _BROKER_SUFFIXES:
            if len(candidate) > len(suffix) and candidate.endswith(suffix):
                candidate = candidate[:-len(suffix)]
                break
        else:
            break

    return "XAUUSD" if candidate in _GOLD_BASES else symbol
