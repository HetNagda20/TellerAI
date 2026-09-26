"""Resolves an artifact's ranked LocatorCandidates against a live page/frame
during replay, trying strategies in the recorded order and reporting which
one actually hit — that's the drift signal a production system would use to
flag an artifact for re-review well before it goes fully dark.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from playwright.sync_api import FrameLocator, Locator, Page

from artifact.schema import LocatorCandidate, LocatorStrategy, Target

Scope = Union[Page, FrameLocator]


class ResolutionError(Exception):
    pass


@dataclass
class Resolved:
    locator: Optional[Locator]
    strategy: str
    coordinates: Optional[dict] = None


def resolve_frame_chain(page: Page, frame_chain: list[list[LocatorCandidate]]) -> Scope:
    scope: Scope = page
    for candidates in frame_chain:
        css = next((c.value.get("css") for c in candidates if c.strategy == LocatorStrategy.CSS_PATH), None)
        if not css:
            raise ResolutionError("frame_chain entry has no css candidate to resolve")
        scope = scope.frame_locator(css)
    return scope


def resolve_target(scope: Scope, target: Target) -> Resolved:
    """Tries each ranked candidate in order, but prefers whichever one first
    resolves to exactly one element over an earlier, higher-priority
    candidate that matches more than one — e.g. two same-shaped <select>
    elements can share an identical role/name while their css_path
    candidates remain positionally distinct. Falls back to the first
    candidate that matched *something* (today's original behavior, via
    `.first`) only if no candidate in the whole ranked list ever resolves
    uniquely, so the common (already-unique) case is completely unchanged.
    """
    errors = []
    first_ambiguous: Optional[Resolved] = None
    for cand in target.candidates:
        try:
            if cand.strategy == LocatorStrategy.ROLE_NAME:
                loc = scope.get_by_role(cand.value["role"], name=cand.value["name"], exact=False)
            elif cand.strategy == LocatorStrategy.TEXT:
                loc = scope.get_by_text(cand.value["text"], exact=False)
            elif cand.strategy == LocatorStrategy.CSS_PATH:
                loc = scope.locator(cand.value["css"])
            elif cand.strategy == LocatorStrategy.COORDINATES:
                continue  # coordinates are the last resort, handled after the loop
            else:
                continue
            count = loc.count()
            if count == 1:
                return Resolved(locator=loc.first, strategy=cand.strategy.value)
            if count > 1 and first_ambiguous is None:
                first_ambiguous = Resolved(locator=loc.first, strategy=cand.strategy.value)
        except Exception as e:  # noqa: BLE001 - deliberately broad: any candidate may legitimately fail
            errors.append(f"{cand.strategy.value}: {e}")
            continue

    if first_ambiguous is not None:
        return first_ambiguous

    coord = next((c for c in target.candidates if c.strategy == LocatorStrategy.COORDINATES), None)
    if coord is not None:
        return Resolved(locator=None, strategy="coordinates", coordinates=coord.value)

    raise ResolutionError(
        f"No locator candidate resolved (tried: {[c.strategy.value for c in target.candidates]}); errors={errors}"
    )
