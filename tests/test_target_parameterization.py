"""Record a read_text step through a member-specific iframe, then replay for a different member and
read the right frame. Needs recorder templating and locator interpolation."""

import re
import urllib.request

from playwright.sync_api import sync_playwright

from agent.executor import StepLog
from agent.loop import DiscoveryResult
from artifact.recorder import record_artifact
from artifact.schema import LocatorCandidate, LocatorStrategy, Target, TargetApp
from replay.executor import replay_artifact
from replay.locators import resolve_frame_chain, resolve_target


def test_a_recorded_iframe_read_replays_against_a_different_members_frame(base_url):
    live = urllib.request.urlopen(base_url + "/member/20001/balance-frame", timeout=2).read().decode()
    expected = re.search(r"Current Savings Balance:</td><td><b>(\$[\d.]+)</b>", live).group(1)

    target = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "table > tbody > tr > td:nth-of-type(2) > b"})],
        frame_chain=[[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": 'iframe[src*="/member/10001/balance-frame"]'})]],
    )
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after=f"{base_url}/member/10001", ok=True),
        StepLog(index=1, action="read_text", rationale="read savings balance", element_name="$1204.09", target=target, ok=True),
    ]
    result = DiscoveryResult(
        run_id="t", success=True, outcome="done", summary="done", outputs={"savings_balance": "$1204.09"},
        steps=steps, goal="g", target_url=base_url, started_at="t0", ended_at="t1", evidence_dir="/tmp/x",
    )
    artifact = record_artifact(
        result, "iframe-param-demo", "desc", {"member_id": "10001"}, TargetApp(app_id="cu-servicing-console", base_url=base_url, entry_path="/")
    )
    assert artifact.steps[-1].target.frame_chain[0][0].value["css"] == 'iframe[src*="/member/{member_id}/balance-frame"]'  # templated, not literal

    replayed = replay_artifact(artifact, {"member_id": "20001"}, headless=True)

    assert replayed.kind == "success"
    assert replayed.outputs["savings_balance"] == expected  # 20001's live value: not 10001's, not empty

    # callers that pass no params at all behave exactly as before the fix
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<button id="btn">Click me</button>')
        plain = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#btn"})])
        assert resolve_target(page, plain).locator is not None
        assert resolve_frame_chain(page, []) is page
        browser.close()
