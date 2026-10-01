"""Checks that a value really appears in the goal text. Whole tokens only, ignoring case, "$" and
thousands separators, so "0" is not grounded by 10001."""

from __future__ import annotations

import difflib
import re

_LEFT = r"(?<![a-z0-9.])"
_RIGHT = r"(?![a-z0-9])"


def normalize(value: object) -> str:
    text = str(value).lower().replace("$", "")
    return re.sub(r"(?<=\d),(?=\d)", "", text).strip()


def same_value(a: object, b: object) -> bool:
    """Equal after normalization, or numerically equal ("6.0" and "6")."""
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return True
    try:
        return float(na) == float(nb)
    except ValueError:
        return False


def is_grounded(value: object, text: str) -> bool:
    v = normalize(value)
    return bool(v) and re.search(_LEFT + re.escape(v) + _RIGHT, normalize(text)) is not None


def templatize_goal(goal: str, values_by_name: dict[str, str]) -> str:
    """Replaces each input's value in the goal with {name}, one occurrence per input, so the
    template describes the capability, not one demo."""
    out = goal
    for name, value in values_by_name.items():
        v = str(value).strip()
        variants = [v]
        if re.fullmatch(r"\d+", v):
            variants.append(f"{int(v):,}")  # "12500" also appears in a goal as "12,500"
        for variant in variants:
            pattern = re.compile(_LEFT + re.escape(variant) + _RIGHT, re.IGNORECASE)
            replaced, n = pattern.subn("{" + name + "}", out, count=1)
            if n:
                out = replaced
                break
    return out


def count_occurrences(value: object, text: str) -> int:
    """How many times the value appears in the text as a whole token (see is_grounded)."""
    v = normalize(value)
    if not v:
        return 0
    return len(re.findall(_LEFT + re.escape(v) + _RIGHT, normalize(text)))


def first_index(value: object, text: str) -> int:
    """Where the value first appears in the (normalized) text as a whole token, or -1."""
    v = normalize(value)
    m = re.search(_LEFT + re.escape(v) + _RIGHT, normalize(text)) if v else None
    return m.start() if m else -1


def match_option(value: object, options: list[str], cutoff: float = 0.8) -> tuple[str | None, str]:
    """Maps a value onto a dropdown's choices. Returns (option, "exact"), (option, "near") when exactly one
    choice is a close misspelling of the value, or (None, "none"). Case and spacing never matter."""
    wanted = normalize(value)
    by_norm = {normalize(o): o for o in options}
    if wanted in by_norm:
        return by_norm[wanted], "exact"
    close = difflib.get_close_matches(wanted, list(by_norm), n=2, cutoff=cutoff)
    if len(close) == 1:
        return by_norm[close[0]], "near"
    return None, "none"
