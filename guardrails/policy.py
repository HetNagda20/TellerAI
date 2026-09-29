"""Risk classification: separates safe/reversible actions from ones that need
a human to sign off before they happen.

Heuristic, on purpose: real deployments would let a reviewer annotate risk
per-step when approving an artifact (see the `status: draft -> approved`
field on Artifact, and the "Confidence & approval" stretch goal). For this
project the heuristic is transparent and auditable: any click whose target
name matches an irreversible/state-changing verb is `confirm`; navigation
outside the allowlist is `blocked`; everything else is `safe`.

Patterns are deliberately specific rather than bare domain-action verbs. An
earlier version included a standalone \btransfer\b, which, found during a
real discovery run, flagged "Review Transfer" (a safe, reversible step in a
two-step review-then-confirm flow) as needing human sign-off, not just the
actual commit button "Confirm Transfer". A bare "submit" would have the same
problem the moment any safe form uses that word for its button. The fix is
to require the pattern to name the commit action, not just its domain
("confirm ...", or a specific always-final verb+object like "delete member").
"""

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
