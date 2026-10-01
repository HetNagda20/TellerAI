"""Locator behavior on real Playwright pages: reading text without a false success, and telling
apart controls that share role, name and text."""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

import replay.executor as replay_executor
from agent.perception import snapshot
from artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    LocatorCandidate,
    LocatorStrategy,
    Step,
    Target,
    TargetApp,
)
from guardrails.allowlist import Allowlist
from handoff.gesture import GestureController
from replay.executor import replay_artifact
from replay.locators import ResolutionError, resolve_target, resolve_text

_INERT_ALLOWLIST = Allowlist(allowed_domains=["127.0.0.1"], allowed_path_prefixes=["/"], allowed_actions=["click", "fill", "select", "navigate", "read_text"])

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


def _css_target(*csss: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": c}) for c in csss])


def _coordinates() -> LocatorCandidate:
    return LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 5, "y": 5})


def _read_step(target: Target) -> Step:
    return Step(index=0, action=ActionType.READ_TEXT, description="read", target=target, extract_as="value")


def test_reading_text_only_succeeds_when_real_text_was_obtained():
    """Before this rule, a read that fell back to coordinates returned success with empty text, so a
    wrong-page transfer reported success with a missing output."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        page.set_content('<div id="balance">$642.44</div><div id="empty"></div><div id="blank">   \n\t </div><div id="real">$9800.15</div><div class="widget-42">$50.00</div>')
        assert resolve_text(page, _css_target("#balance")) == ("$642.44", "css_path")
        assert resolve_text(page, _css_target("#empty", "#real")) == ("$9800.15", "css_path")  # an empty candidate falls through
        assert resolve_text(page, _css_target(".widget-{widget_id}"), {"widget_id": "42"})[0] == "$50.00"  # params interpolate
        for unreadable in (_css_target("#empty"), _css_target("#blank"), Target(candidates=[_coordinates()]), Target(candidates=[*_css_target("#empty", "#blank").candidates, _coordinates()])):
            with pytest.raises(ResolutionError):
                resolve_text(page, unreadable)

        # the same rules through the real replay call path
        ok = replay_executor._run_step(page, _read_step(_css_target("#balance")), {}, _INERT_ALLOWLIST, [], None)
        assert (ok.ok, ok.observed) == (True, "$642.44")
        log: list = []
        empty = replay_executor._run_step(page, _read_step(_css_target("#empty")), {}, _INERT_ALLOWLIST, log, None)
        assert empty.ok is False and "No locator candidate produced meaningful" in empty.observed
        assert log == []  # reported through the same failure machinery as any resolution failure
        stale = Target(
            candidates=[
                LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "text", "name": "$1204.09"}),
                LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#does-not-exist-anywhere"}),
                LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 208, "y": 19}),
            ]
        )
        assert replay_executor._run_step(page, _read_step(stale), {}, _INERT_ALLOWLIST, [], None).ok is False
        browser.close()


def test_a_replay_whose_output_cannot_be_read_is_a_hard_failure_not_a_success_with_a_missing_output(base_url):
    unreadable = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#nonexistent"}), _coordinates()])
    artifact = Artifact(
        capability_id="read-text-failure-demo",
        version="1.0.0",
        description="test",
        goal_template="n/a",
        target_app=TargetApp(app_id="a", base_url=base_url, entry_path="/"),
        inputs=[],
        outputs=[],
        steps=[
            Step(index=0, action=ActionType.NAVIGATE, description="go", value_template=f"{base_url}/accounts"),
            Step(index=1, action=ActionType.READ_TEXT, description="read something that will not be there", target=unreadable, extract_as="checking_balance"),
        ],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/accounts"),
        created_from_run_id="hand_built_for_tests",
    )

    result = replay_artifact(artifact, {}, headless=True)

    assert result.kind == "hard_failure" and result.outputs == {}
    assert result.failure.step_index == 1


def test_two_controls_with_identical_role_name_and_text_stay_distinctly_addressable():
    """Two same-shaped selects on a legacy form. Capture infers each label from its row, and replay
    prefers a candidate that matches exactly one element."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_TWO_IDENTICAL_SELECTS_HTML)

        selects = [el for el in snapshot(page).elements if el.role == "combobox"]
        assert {el.name for el in selects} == {"From Account:", "To Account:"}
        assert all(el.name_source == "inferred_label" for el in selects)

        ctrl = GestureController(page)
        ctrl.start_capturing()
        page.locator("select").nth(0).select_option("savings")
        page.locator("select").nth(1).select_option("checking")
        assert {c["descriptor"]["name"] for c in ctrl.stop_capturing()} == {"From Account:", "To Account:"}

        def ambiguous_then_unique(css: str) -> Target:  # a role/name candidate matching BOTH selects, ranked above a css_path matching one
            return Target(
                candidates=[
                    LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "combobox", "name": ""}),
                    LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css}),
                ]
            )

        assert resolve_target(page, ambiguous_then_unique("tr:nth-of-type(1) select")).strategy == "css_path"
        assert resolve_target(page, ambiguous_then_unique("tr:nth-of-type(2) select")).strategy == "css_path"

        only_ambiguous = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "combobox", "name": ""})])
        resolved = resolve_target(page, only_ambiguous)  # nothing unique: the first match wins, as before
        assert resolved.strategy == "role_name" and resolved.locator is not None

        # A field's current value is user data, not its name. Name it by its row label, or old
        # values end up baked into locators.
        page.set_content(
            '<table><tr><td>Mailing Address:</td><td><input type="text" value="142 Willow St, Springfield"></td></tr>'
            '<tr><td colspan="2"><input type="submit" value="Save Changes"></td></tr></table>'
        )
        named = {e.role: (e.name, e.name_source) for e in snapshot(page).elements}
        assert named["textbox"] == ("Mailing Address:", "inferred_label")
        assert named["button"] == ("Save Changes", "value")
        ctrl2_page = browser.new_page()
        ctrl2_page.set_content(
            '<table><tr><td>Mailing Address:</td><td><input type="text" value="142 Willow St, Springfield"></td></tr></table>'
        )
        ctrl2 = GestureController(ctrl2_page)
        ctrl2.start_capturing()
        ctrl2_page.locator("input").fill("555 W Washington")
        ctrl2_page.keyboard.press("Tab")
        captured = ctrl2.stop_capturing()
        assert [c["descriptor"]["name"] for c in captured if c["action"] == "fill"] == ["Mailing Address:"]
        browser.close()
