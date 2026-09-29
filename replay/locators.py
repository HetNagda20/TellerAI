"""Resolves an artifact's ranked LocatorCandidates against a live page/frame
during replay, trying strategies in the recorded order and reporting which
one actually hit. That is the drift signal a production system would use to
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


def _fmt(value: str, params: dict[str, str]) -> str:
    """Interpolates `{param_name}` placeholders artifact/recorder.py's
    _templatize_target may have baked into a locator/frame-chain string back
    into the real replay value -- the counterpart to replay/executor.py's own
    _fmt() for step values, kept local here (not imported) since executor.py
    imports FROM this module and a reverse import would be circular. A
    literal string with no matching placeholder passes through unchanged.
    """
    try:
        return value.format(**params)
    except (KeyError, IndexError):
        return value


def resolve_frame_chain(page: Page, frame_chain: list[list[LocatorCandidate]], params: Optional[dict[str, str]] = None) -> Scope:
    params = params or {}
    scope: Scope = page
    for candidates in frame_chain:
        css = next((c.value.get("css") for c in candidates if c.strategy == LocatorStrategy.CSS_PATH), None)
        if not css:
            raise ResolutionError("frame_chain entry has no css candidate to resolve")
        scope = scope.frame_locator(_fmt(css, params))
    return scope


def resolve_target(scope: Scope, target: Target, params: Optional[dict[str, str]] = None) -> Resolved:
    """Tries each ranked candidate in order, but prefers whichever one first
    resolves to exactly one element over an earlier, higher-priority
    candidate that matches more than one. Two same-shaped <select>
    elements can share an identical role/name while their css_path
    candidates remain positionally distinct. Falls back to the first
    candidate that matched *something* (today's original behavior, via
    `.first`) only if no candidate in the whole ranked list ever resolves
    uniquely, so the common (already-unique) case is completely unchanged.

    `params` (default: none, preserving every existing caller's behavior
    exactly) interpolates any `{param_name}` placeholder a candidate's
    string may contain -- see artifact/recorder.py's _templatize_target.
    """
    params = params or {}
    errors = []
    first_ambiguous: Optional[Resolved] = None
    for cand in target.candidates:
        try:
            if cand.strategy == LocatorStrategy.ROLE_NAME:
                loc = scope.get_by_role(cand.value["role"], name=_fmt(cand.value["name"], params), exact=False)
            elif cand.strategy == LocatorStrategy.TEXT:
                loc = scope.get_by_text(_fmt(cand.value["text"], params), exact=False)
            elif cand.strategy == LocatorStrategy.CSS_PATH:
                loc = scope.locator(_fmt(cand.value["css"], params))
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


def resolve_text(scope: Scope, target: Target, params: Optional[dict[str, str]] = None, timeout_ms: int = 5000) -> tuple[str, str]:
    """READ_TEXT-specific resolution: tries each ranked candidate (skipping
    COORDINATES, which has no locator and so cannot extract anything) and
    returns the first NON-EMPTY, non-whitespace text any of them actually
    yields -- a stricter success criterion than resolve_target()'s "resolves
    to exactly one element", because a candidate can structurally resolve to
    a real element that simply has no text (or a coordinate pair with no
    element behind it at all) without that being a meaningful extraction.

    Raises ResolutionError -- the same exception replay/executor.py already
    treats as an immediate, unretried hard_failure -- if no candidate ever
    produces meaningful text. Never invents or substitutes a placeholder
    value; an empty/whitespace-only read is exactly as much a failure here
    as no locator resolving at all.
    """
    params = params or {}
    errors = []
    for cand in target.candidates:
        if cand.strategy == LocatorStrategy.COORDINATES:
            continue
        try:
            if cand.strategy == LocatorStrategy.ROLE_NAME:
                loc = scope.get_by_role(cand.value["role"], name=_fmt(cand.value["name"], params), exact=False)
            elif cand.strategy == LocatorStrategy.TEXT:
                loc = scope.get_by_text(_fmt(cand.value["text"], params), exact=False)
            elif cand.strategy == LocatorStrategy.CSS_PATH:
                loc = scope.locator(_fmt(cand.value["css"], params))
            else:
                continue
            if loc.count() < 1:
                continue
            text = loc.first.inner_text(timeout=timeout_ms)
            if text and text.strip():
                return text, cand.strategy.value
        except Exception as e:  # noqa: BLE001 - deliberately broad: any candidate may legitimately fail
            errors.append(f"{cand.strategy.value}: {e}")
            continue

    raise ResolutionError(
        f"No locator candidate produced meaningful (non-empty) text "
        f"(tried: {[c.strategy.value for c in target.candidates]}); errors={errors}"
    )
