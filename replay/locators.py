"""Resolves an artifact's ranked locator candidates against a live page in recorded order, and
reports which one hit. That is the drift signal."""

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
    """Fills {param} placeholders in a locator string with replay values. Local to avoid a circular
    import with executor.py."""
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
    """Tries candidates in order, but prefers the first that resolves to exactly one element. Falls
    back to the first partial match only if none is ever unique."""
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
    """For read_text: returns the first non-empty text any candidate yields, skipping coordinates.
    Raises ResolutionError if none does, never inventing a value."""
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
