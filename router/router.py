"""Decides replay or discover for a goal and target. Catalog, model proposal, then the validator, asked twice with the catalog reversed. Anything doubtful means discover; it never touches a
browser."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from artifact.grounding import normalize
from router.catalog import CatalogEntry, build_catalog
from router.proposer import OllamaProposer, ProposerUnavailable
from router.validator import validate_proposal

logger = logging.getLogger(__name__)


@dataclass
class RouteDecision:
    action: Literal["replay", "discover"]
    capability_id: Optional[str] = None
    artifact_path: Optional[Path] = None
    params: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    risky: bool = False  # the chosen capability has an irreversible step
    status: Optional[str] = None  # "draft" | "approved"


def _discover(goal: str, why: str, code: str) -> RouteDecision:
    logger.info("route decision=discover reason=%s goal=%r detail=%s", code, goal, why)
    return RouteDecision(action="discover", reason=why)


def _same_answer(a, b) -> bool:
    return (a.capability_id, {k: normalize(v) for k, v in a.params.items()}) == (
        b.capability_id,
        {k: normalize(v) for k, v in b.params.items()},
    )


def route(goal: str, target_url: str, proposer=None, consistency_check: bool = True) -> RouteDecision:
    entries = build_catalog(target_url)
    if not entries:
        return _discover(goal, "no existing capability was recorded against this target", "no_target_match")
    catalog: dict[str, CatalogEntry] = {e.capability_id: e for e in entries}
    proposer = proposer or OllamaProposer()

    try:
        first = validate_proposal(goal, proposer.propose(goal, entries), catalog)
        if not first.ok:
            return _discover(goal, first.reason, "rejected")
        if consistency_check:
            second = validate_proposal(goal, proposer.propose(goal, list(reversed(entries))), catalog)
            if not second.ok or not _same_answer(first, second):
                return _discover(
                    goal,
                    "the router model did not give the same answer when the catalog was presented in a different order",
                    "unstable",
                )
    except ProposerUnavailable as e:
        return _discover(goal, f"{e}; treating this as a new capability", "model_unavailable")

    entry = catalog[first.capability_id]
    logger.info("route decision=replay capability=%s params=%s", entry.capability_id, sorted(first.params))
    return RouteDecision(
        action="replay",
        capability_id=entry.capability_id,
        artifact_path=entry.path,
        params=first.params,
        reason=f"the router model matched capability {entry.capability_id!r}; the validator accepted its arguments"
        + (f" ({'; '.join(first.notes)})" if first.notes else ""),
        risky=entry.risky,
        status=entry.status,
    )
