"""Risk classification. Clicks matching an irreversible verb are `confirm`, navigation off the
allowlist is `blocked`, the rest is `safe`. Patterns name the commit action, not just the domain."""

from __future__ import annotations

import re

_RISKY_CLICK_PATTERNS = [
    r"\bconfirm\b",           # covers every actual commit button in this app:
                               # "Confirm & Open Account", "Confirm Transfer"
    r"\bdelete\b",
    r"\bclose account\b",
    r"\bwithdraw funds\b",
]
_RISKY_RE = re.compile("|".join(_RISKY_CLICK_PATTERNS), re.IGNORECASE)


def classify_click(element_name: str) -> str:
    """Returns 'confirm' for clicks that look irreversible/state-changing, else 'safe'."""
    if _RISKY_RE.search(element_name or ""):
        return "confirm"
    return "safe"


def classify_action(action: str, element_name: str | None, url_allowed: bool) -> str:
    """Top-level policy decision for one proposed action.

    Returns one of "safe", "confirm", "blocked".
    """
    if action == "navigate" and not url_allowed:
        return "blocked"
    if action == "click":
        return classify_click(element_name or "")
    return "safe"
