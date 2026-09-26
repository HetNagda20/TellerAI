"""Generic target-disambiguation coverage: two semantically different controls
that happen to share identical role/name/text must still end up distinctly
addressable, both at capture time (agent/perception.py, handoff/gesture.py)
and at replay-resolution time (replay/locators.py). Nothing here is specific
to "From Account"/"To Account" -- the fixture is a generic legacy table-layout
form with two same-shaped <select> elements, the same shape as any app that
has more than one dropdown with an identical option set.
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from agent.perception import snapshot
from artifact.schema import LocatorCandidate, LocatorStrategy, Target
from handoff.gesture import GestureController
from replay.locators import resolve_target

_TWO_IDENTICAL_SELECTS_HTML = """
<table>
  <tr><td>From Account:</td><td>
    <select><option value="checking">Checking</option><option value="savings">Savings</option></select>
  </td></tr>
  <tr><td>To Account:</td><td>
    <select><option value="checking">Checking</option><option value="savings">Savings</option></select>
  </td></tr>
</table>
"""


def test_perception_distinguishes_selects_via_row_label():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_TWO_IDENTICAL_SELECTS_HTML)
        snap = snapshot(page)
        browser.close()

    selects = [el for el in snap.elements if el.role == "combobox"]
    assert len(selects) == 2
    names = {el.name for el in selects}
    assert names == {"From Account:", "To Account:"}, f"expected distinct row-inferred labels, got {names}"
    assert all(el.name_source == "inferred_label" for el in selects)


def test_gesture_capture_distinguishes_selects_via_row_label():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_TWO_IDENTICAL_SELECTS_HTML)
        ctrl = GestureController(page)
        ctrl.start_capturing()
        selects = page.locator("select")
        selects.nth(0).select_option("savings")
        selects.nth(1).select_option("checking")
        captured = ctrl.stop_capturing()
        browser.close()

    assert len(captured) == 2
    names = {c["descriptor"]["name"] for c in captured}
    assert names == {"From Account:", "To Account:"}, f"expected distinct row-inferred labels, got {names}"


def _ambiguous_role_target(unique_css: str) -> Target:
    """Mirrors a real recorded Target for one of two identical selects: a
    role/name candidate that matches BOTH selects, ranked above a css_path
    candidate that matches only this one.
    """
    return Target(
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "combobox", "name": ""}),
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": unique_css}),
        ]
    )


def test_resolve_target_prefers_unique_match_over_ambiguous_higher_priority_candidate():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_TWO_IDENTICAL_SELECTS_HTML)

        first = resolve_target(page, _ambiguous_role_target("tr:nth-of-type(1) select"))
        second = resolve_target(page, _ambiguous_role_target("tr:nth-of-type(2) select"))
        browser.close()

    # Both targets share the ambiguous role_name candidate (matches both selects),
    # but each resolves via its own distinct css_path -- proving they land on
    # different elements, not both on ".first" of the ambiguous role match.
    assert first.strategy == "css_path"
    assert second.strategy == "css_path"


def test_resolve_target_falls_back_to_first_ambiguous_when_nothing_resolves_uniquely():
    """Preserves the original behavior for the genuinely-irreducible case: if
    every candidate is ambiguous, the first one that matched anything wins,
    exactly as before this fix.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_TWO_IDENTICAL_SELECTS_HTML)

        target = Target(
            candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "combobox", "name": ""})]
        )
        resolved = resolve_target(page, target)
        browser.close()

    assert resolved.strategy == "role_name"
    assert resolved.locator is not None
