"""Fix 2: a read_text action must only count as a successful extraction if it
actually obtains meaningful (non-empty, non-whitespace) text. Before this fix,
`_run_step_once`'s READ_TEXT branch returned `_StepOutcome(True)` unconditionally
-- including when the coordinates fallback meant there was no real locator at
all (`text = ""`), which is exactly how a transfer that landed on an unexpected
page silently reported `kind="success"` with a missing declared output instead
of a hard_failure.

Tests operate at two levels: replay.locators.resolve_text directly (the new
function), and replay.executor._run_step (the real call path replay_artifact
uses), both against real Playwright pages via page.set_content -- no mock app
needed, matching tests/test_replay_failures.py's existing style for this kind
of test.
"""

from __future__ import annotations

import urllib.request

import pytest
from playwright.sync_api import sync_playwright

import replay.executor as replay_executor_mod
from artifact.schema import ActionType, LocatorCandidate, LocatorStrategy, Step, Target
from guardrails.allowlist import Allowlist
from replay.locators import ResolutionError, resolve_text

BASE = "http://127.0.0.1:8000"
_INERT_ALLOWLIST = Allowlist(allowed_domains=["127.0.0.1"], allowed_path_prefixes=["/"], allowed_actions=["click", "fill", "select", "navigate", "read_text"])


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


def _css_target(*csss: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": c}) for c in csss])


def _read_text_step(target: Target) -> Step:
    return Step(index=0, action=ActionType.READ_TEXT, description="read", target=target, extract_as="value")


# -- resolve_text directly ------------------------------------------------------


def test_read_text_with_real_text_succeeds():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="balance">$642.44</div>')
        text, strategy = resolve_text(page, _css_target("#balance"))
        browser.close()
    assert text == "$642.44"
    assert strategy == "css_path"


def test_read_text_with_empty_element_does_not_succeed():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="balance"></div>')
        with pytest.raises(ResolutionError):
            resolve_text(page, _css_target("#balance"))
        browser.close()


def test_read_text_with_whitespace_only_element_does_not_succeed():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="balance">   \n\t  </div>')
        with pytest.raises(ResolutionError):
            resolve_text(page, _css_target("#balance"))
        browser.close()


def test_a_candidate_with_no_text_falls_through_to_a_later_candidate_with_real_text():
    # First-ranked candidate resolves to a real, empty element; second-ranked
    # candidate resolves to a different element that actually has text.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="empty"></div><div id="real">$9800.15</div>')
        text, strategy = resolve_text(page, _css_target("#empty", "#real"))
        browser.close()
    assert text == "$9800.15"
    assert strategy == "css_path"


def test_coordinates_only_candidate_can_never_produce_text_and_is_skipped():
    target = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 1, "y": 2})])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<body></body>")
        with pytest.raises(ResolutionError):
            resolve_text(page, target)
        browser.close()


def test_if_no_candidate_produces_text_resolution_fails_rather_than_returning_empty():
    # Two candidates, both real elements, both empty -- plus a coordinates
    # fallback that would previously have "succeeded" with "".
    target = Target(
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#a"}),
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#b"}),
            LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 5, "y": 5}),
        ]
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="a"></div><div id="b">   </div>')
        with pytest.raises(ResolutionError):
            resolve_text(page, target)
        browser.close()


def test_resolve_text_interpolates_params_like_resolve_target():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div class="widget-42">$50.00</div>')
        text, _ = resolve_text(page, _css_target(".widget-{widget_id}"), {"widget_id": "42"})
        browser.close()
    assert text == "$50.00"


# -- _run_step / _run_step_once: the real replay call path ----------------------


def test_run_step_read_text_succeeds_with_real_text():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="balance">$1204.09</div>')
        outcome = replay_executor_mod._run_step(page, _read_text_step(_css_target("#balance")), {}, _INERT_ALLOWLIST, [], None)
        browser.close()
    assert outcome.ok is True
    assert outcome.observed == "$1204.09"


def test_run_step_read_text_fails_when_only_candidate_yields_empty_text():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="balance"></div>')
        strategy_log = []
        outcome = replay_executor_mod._run_step(page, _read_text_step(_css_target("#balance")), {}, _INERT_ALLOWLIST, strategy_log, None)
        browser.close()
    assert outcome.ok is False
    assert "No locator candidate produced meaningful" in outcome.observed
    # requirement: this must be reported through the SAME failure machinery as any
    # other resolution failure -- no strategy_log entry, exactly like a plain
    # ResolutionError from resolve_target would produce.
    assert strategy_log == []


def test_run_step_read_text_that_would_previously_have_used_coordinates_now_fails():
    """The literal bug: an artifact recorded against a page structure that no
    longer matches (e.g. crossing an iframe that can't be entered) used to
    fall back to COORDINATES and report ok=True with empty text. Now it must
    report a hard_failure, not a false success.
    """
    target = Target(
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "text", "name": "$1204.09"}),
            LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": "$1204.09"}),
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#does-not-exist-anywhere"}),
            LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 208, "y": 19}),
        ]
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<body><p>nothing matching here</p></body>")
        outcome = replay_executor_mod._run_step(page, _read_text_step(target), {}, _INERT_ALLOWLIST, [], None)
        browser.close()
    assert outcome.ok is False, "must not silently succeed with empty text via the coordinates fallback"
    assert outcome.observed != ""


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_full_replay_reports_hard_failure_not_a_false_success_for_unreadable_output():
    """End-to-end through replay_artifact() itself: an artifact whose final
    step can't extract meaningful text must come back as kind='hard_failure',
    never kind='success' with a silently-missing output.
    """
    from artifact.schema import Artifact, Checkpoint, CheckpointKind, TargetApp
    from replay.executor import replay_artifact

    target = Target(candidates=[
        LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#nonexistent"}),
        LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 1, "y": 1}),
    ])
    artifact = Artifact(
        capability_id="read-text-failure-demo",
        version="1.0.0",
        description="test",
        goal_template="n/a",
        target_app=TargetApp(app_id="a", base_url="http://127.0.0.1:8000", entry_path="/"),
        inputs=[],
        outputs=[],
        steps=[
            Step(index=0, action=ActionType.NAVIGATE, description="go", value_template="http://127.0.0.1:8000/accounts"),
            Step(index=1, action=ActionType.READ_TEXT, description="read something that won't be there", target=target, extract_as="checking_balance"),
        ],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/accounts"),
        created_from_run_id="hand_built_for_tests",
    )
    result = replay_artifact(artifact, {}, headless=True)
    assert result.kind == "hard_failure"
    assert result.outputs == {}
    assert result.failure is not None
    assert result.failure.step_index == 1
