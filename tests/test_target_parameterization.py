"""Fix 1: a recorded locator/frame-chain string can embed the concrete value
of a declared input (e.g. an iframe src built from a member id) just as
easily as a step's fill/select/navigate value can -- artifact/recorder.py
already templates the latter (_templatize); this proves the former is now
templated too (_templatize_target/_templatize_candidate), and that
replay/locators.py's resolve_target/resolve_frame_chain interpolate the
placeholder back with whatever value THIS replay was actually given
(the new `params` argument) -- without both halves, a templated locator
would simply never resolve on replay at all.

Pure tests use fully generic, non-banking fixtures (a "widget"/"panel" shape)
specifically to prove the mechanism isn't tied to member_id or iframes in any
way. Live tests (real Playwright) use the real mock app's real iframe --
the exact shape the bug was originally found in -- for an end-to-end proof.
"""

from __future__ import annotations

import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from agent.executor import StepLog
from agent.loop import DiscoveryResult
from artifact.recorder import _templatize_candidate, _templatize_target, record_artifact
from artifact.schema import LocatorCandidate, LocatorStrategy, Target, TargetApp
from replay.executor import replay_artifact
from replay.locators import resolve_frame_chain, resolve_target

BASE = "http://127.0.0.1:8000"


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


# -- pure: _templatize_candidate / _templatize_target, fully generic fixtures --


def test_input_value_embedded_in_a_frame_selector_becomes_parameterized():
    candidate = LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": 'iframe[src*="/widget/42/panel"]'})
    templated = _templatize_candidate(candidate, {"widget_id": "42"})
    assert templated.value["css"] == 'iframe[src*="/widget/{widget_id}/panel"]'


def test_templatize_target_applies_to_frame_chain_entries_not_just_top_level_candidates():
    target = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": "$99.00"})],
        frame_chain=[[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": 'iframe[src*="/widget/42/panel"]'})]],
    )
    templated = _templatize_target(target, {"widget_id": "42"})
    assert templated.frame_chain[0][0].value["css"] == 'iframe[src*="/widget/{widget_id}/panel"]'
    assert templated.candidates[0].value["text"] == "$99.00"  # unrelated value untouched


def test_unrelated_numeric_and_text_values_are_not_modified():
    # "$99.00" and the bbox-derived coordinates share no substring with the declared
    # param's value ("42") -- nothing here should be rewritten.
    candidate = LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 100, "y": 42})
    templated = _templatize_candidate(candidate, {"widget_id": "42"})
    assert templated.value == {"x": 100, "y": 42}, "numeric fields must never be treated as templatable strings"

    text_candidate = LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": "Order Total: $99.00"})
    templated_text = _templatize_candidate(text_candidate, {"widget_id": "42"})
    assert templated_text.value["text"] == "Order Total: $99.00"


def test_generic_to_an_arbitrary_parameter_name_and_value_not_member_id():
    # Deliberately not "member_id", not a banking shape, not an iframe -- a css_path
    # for an ordinary element, keyed by a param this mechanism has never seen before.
    candidate = LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "table > tr:nth-of-type(7) > td[data-order='ORD-7788']"})
    templated = _templatize_candidate(candidate, {"order_reference": "ORD-7788"})
    assert templated.value["css"] == "table > tr:nth-of-type(7) > td[data-order='{order_reference}']"


def test_templatize_target_handles_none():
    assert _templatize_target(None, {"widget_id": "42"}) is None


def test_record_artifact_parameterizes_a_frame_chain_css_candidate():
    """End-to-end through the real recorder: a hand-built read_text StepLog
    whose Target's frame_chain embeds the declared member_id, exactly the
    shape agent/perception.py produces for the real balance iframe.
    """
    frame_css = 'iframe[src*="/member/10001/balance-frame"]'
    target = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": "$1204.09"})],
        frame_chain=[[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": frame_css})]],
    )
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after=f"{BASE}/member/10001", ok=True),
        StepLog(index=1, action="read_text", rationale="read savings balance", element_name="$1204.09", target=target, ok=True),
    ]
    result = DiscoveryResult(
        run_id="t", success=True, outcome="done", summary="done", outputs={"savings_balance": "$1204.09"},
        steps=steps, goal="g", target_url=BASE, started_at="t0", ended_at="t1", evidence_dir="/tmp/x",
    )
    target_app = TargetApp(app_id="a", base_url=BASE, entry_path="/")
    artifact = record_artifact(result, "cap", "desc", {"member_id": "10001"}, target_app)

    read_step = next(s for s in artifact.steps if s.action.value == "read_text")
    assert read_step.target.frame_chain[0][0].value["css"] == 'iframe[src*="/member/{member_id}/balance-frame"]'


# -- pure: resolve_target/resolve_frame_chain default (params=None) preserves prior behavior --


def test_resolve_target_and_resolve_frame_chain_still_work_with_no_params_argument():
    """Requirement 5: existing callers (and existing tests) that never pass
    `params` at all must behave exactly as before this fix.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<button id="btn">Click me</button>')
        target = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#btn"})])
        resolved = resolve_target(page, target)  # no params -- must not raise
        assert resolved.locator is not None
        assert resolve_frame_chain(page, []) is page  # empty frame_chain, no params -- unchanged
        browser.close()


# -- live: real mock app, the exact real iframe shape the bug was found in --


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_replay_substitutes_a_different_input_value_into_a_templated_frame_chain():
    """The literal scenario from the bug report: a frame_chain css recorded
    against member 10001's iframe must resolve member 20001's iframe when
    replayed with member_id=20001 -- proven by actually reading a real,
    member-specific value (the live savings balance) out of the CORRECT frame.
    """
    savings_live = urllib.request.urlopen(BASE + "/member/20001/balance-frame", timeout=2).read().decode()
    import re

    savings_value = re.search(r"Current Savings Balance:</td><td><b>(\$[\d.]+)</b>", savings_live).group(1)

    templated_target = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "table > tbody > tr > td:nth-of-type(2) > b"})],
        frame_chain=[[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": 'iframe[src*="/member/{member_id}/balance-frame"]'})]],
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE + "/member/20001", wait_until="load")

        scope = resolve_frame_chain(page, templated_target.frame_chain, {"member_id": "20001"})
        resolved = resolve_target(scope, templated_target, {"member_id": "20001"})
        assert resolved.locator is not None
        assert resolved.locator.inner_text() == savings_value

        browser.close()


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_full_record_then_replay_round_trip_reads_the_correct_iframe_for_a_different_member():
    """The complete pipeline: record an artifact whose read_text step crosses
    a member-specific iframe (recorded against 10001), then replay it for a
    DIFFERENT member (20001) and confirm the extracted savings_balance
    matches 20001's real, live value -- not 10001's, not empty.
    """
    savings_20001 = urllib.request.urlopen(BASE + "/member/20001/balance-frame", timeout=2).read().decode()
    import re

    expected = re.search(r"Current Savings Balance:</td><td><b>(\$[\d.]+)</b>", savings_20001).group(1)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE + "/member/10001", wait_until="load")
        frame_css_at_record_time = 'iframe[src*="/member/10001/balance-frame"]'
        target = Target(
            candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "table > tbody > tr > td:nth-of-type(2) > b"})],
            frame_chain=[[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": frame_css_at_record_time})]],
        )
        browser.close()

    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after=f"{BASE}/member/10001", ok=True),
        StepLog(index=1, action="read_text", rationale="read savings balance", element_name="$1204.09", target=target, ok=True),
    ]
    result = DiscoveryResult(
        run_id="t", success=True, outcome="done", summary="done", outputs={"savings_balance": "$1204.09"},
        steps=steps, goal="g", target_url=BASE, started_at="t0", ended_at="t1", evidence_dir="/tmp/x",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/")
    artifact = record_artifact(result, "iframe-param-demo", "desc", {"member_id": "10001"}, target_app)

    # the recorded artifact must be templated, not still literally "10001"
    assert artifact.steps[-1].target.frame_chain[0][0].value["css"] == 'iframe[src*="/member/{member_id}/balance-frame"]'

    replay_result = replay_artifact(artifact, {"member_id": "20001"}, headless=True)

    assert replay_result.kind == "success"
    assert replay_result.outputs["savings_balance"] == expected
