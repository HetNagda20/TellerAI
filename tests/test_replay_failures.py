"""Replay failure semantics: bounded transient-timeout retry (requirement 4),
structured target-resolution failure (requirement 5), and the replay HITL
boundary, operational recovery only, never teaching (requirement 6).

Deliberately no LLM anywhere in this file, including in what's being tested:
replay must never call one to repair a failure. Where a real timeout is
needed, tests use a minimal page (page.set_content) rather than the mock
app, so the retry timing is under the test's control, not coupled to how
fast any particular route happens to respond.
"""

from __future__ import annotations

import urllib.request

import pytest
from playwright.sync_api import sync_playwright

import replay.executor as replay_executor_mod
from artifact.annotations import annotations_for
from artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    ParamType,
    Step,
    Target,
    TargetApp,
)
from guardrails.allowlist import Allowlist
from handoff.session import InterventionRequest
from replay.executor import replay_artifact
from replay.outcomes import StrategyLogEntry

BASE = "http://127.0.0.1:8000"
_INERT_ALLOWLIST = Allowlist(allowed_domains=["127.0.0.1"], allowed_path_prefixes=["/"], allowed_actions=["click", "fill", "select", "navigate", "read_text"])


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


def _click_step(css: str) -> Step:
    return Step(
        index=0,
        action=ActionType.CLICK,
        description="click a target for retry testing",
        target=Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css})]),
    )


# -- requirement 4: bounded transient-timeout retry ---------------------------


def test_exhausted_transient_timeout_produces_structured_hard_failure(monkeypatch):
    monkeypatch.setattr(replay_executor_mod, "_ACTION_TIMEOUT_MS", 300)
    monkeypatch.setattr(replay_executor_mod, "_TRANSIENT_RETRY_WAIT_MS", 50)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<html><body><button id="hidden-btn" style="display:none">Hi</button></body></html>')
        outcome = replay_executor_mod._run_step(page, _click_step("#hidden-btn"), {}, _INERT_ALLOWLIST, [], None)
        browser.close()

    assert outcome.ok is False
    assert "Exhausted" in outcome.message
    assert "timeout" in outcome.message.lower()


def test_transient_timeout_recovers_if_condition_clears_before_retry_exhausted(monkeypatch):
    # Proves the bounded retry actually retries (not just "fails twice, still fails"):
    # the element is hidden long enough to time out attempt 1, but becomes visible
    # partway through attempt 2's window.
    monkeypatch.setattr(replay_executor_mod, "_ACTION_TIMEOUT_MS", 400)
    monkeypatch.setattr(replay_executor_mod, "_TRANSIENT_RETRY_WAIT_MS", 50)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<html><body><button id="btn" style="display:none">Hi</button>'
            '<script>setTimeout(() => { document.getElementById("btn").style.display = "block"; }, 420);</script>'
            "</body></html>"
        )
        outcome = replay_executor_mod._run_step(page, _click_step("#btn"), {}, _INERT_ALLOWLIST, [], None)
        browser.close()

    assert outcome.ok is True


# -- requirement 5: artifact/target resolution failure -------------------------


def test_unresolvable_target_produces_structured_hard_failure_without_retry():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<html><body><p>nothing clickable here</p></body></html>")
        outcome = replay_executor_mod._run_step(page, _click_step("#does-not-exist-anywhere"), {}, _INERT_ALLOWLIST, [], None)
        browser.close()

    assert outcome.ok is False
    assert "No locator candidate resolved" in outcome.message
    assert "Exhausted" not in outcome.message  # confirms this took the no-retry path, not the timeout one


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_full_replay_reports_structured_failure_evidence_for_target_mismatch():
    """End-to-end: an artifact whose one step's locator strategies are all
    wrong (simulating drift, the recorded target no longer matches this
    page) produces a hard_failure carrying step index, action, expected
    candidates tried, and observed page state, not a crash, not an LLM call.
    """
    bogus_target = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "#totally-not-a-real-selector"})])
    artifact = Artifact(
        capability_id="broken-target-demo",
        version="1.0.0",
        description="Deliberately broken target for failure-evidence testing.",
        goal_template="n/a",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"),
        inputs=[],
        outputs=[],
        steps=[
            Step(index=0, action=ActionType.NAVIGATE, description="go to accounts", value_template=f"{BASE}/accounts"),
            Step(index=1, action=ActionType.CLICK, description="click something that no longer exists", target=bogus_target),
        ],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/accounts"),
        created_from_run_id="hand_built_for_tests",
    )
    result = replay_artifact(artifact, {}, headless=True)

    assert result.kind == "hard_failure"
    assert result.failure is not None
    assert result.failure.step_index == 1
    assert result.failure.action == "click"
    assert "#totally-not-a-real-selector" in result.failure.expected or "candidate" in result.failure.expected
    assert result.version == "1.0.0"


# -- requirement 6: replay HITL is operational recovery only, never teaching ---


class _RecoveryOnlyOperator:
    """Simulates a human doing something real to fix an operational problem
    (dismissing an interstitial) without that action ever being captured or
    fed back into the artifact, replay has no GestureController, no capture
    mechanism at all, so there is nothing here that could teach it even if
    this tried to.

    `take_control_calls`/`urls_seen` exist purely so tests can prove exactly
    how many times this operator was actually invoked, and what page it saw
    at the time, not part of the operator protocol itself (HandoffSession
    never reads them).
    """

    def __init__(self, page, resume=True):
        self.page = page
        self.resume = resume
        self.take_control_calls = 0
        self.urls_seen: list[str] = []

    def confirm(self, request: InterventionRequest):
        return True, "n/a"

    def take_control(self, request: InterventionRequest):
        self.take_control_calls += 1
        self.urls_seen.append(self.page.url)
        if self.resume:
            self.page.get_by_role("button", name="Continue").click()
            # Same wait the real declared-recovery path uses (replay/executor.py's
            # _execute_recovery) before the retried step tries to resolve anything,
            # without it, the retry can race the interstitial's own navigation.
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        return "dismissed the interstitial manually", self.resume, False, []  # no captured_actions, ever


def _subaccount_artifact_without_declared_interstitial_recovery() -> Artifact:
    """Same shape as the real open-member-subaccount capability, but with
    recoverable_conditions deliberately left empty, so member 20001's known
    session-renewal interstitial is NOT auto-handled, and hits the form step
    as a genuine hard_failure instead. That's what escalate_on_failure exists
    for: an operational problem the artifact itself doesn't know how to
    recover from automatically.
    """
    member_id_box = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "input[name=member_id]"})])
    search_button = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": "Search"})])
    open_subaccount_link = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "link", "name": "Open a New Sub-Account for this Member"})])
    deposit_box = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "input[name=initial_deposit]"})])

    steps = [
        Step(index=0, action=ActionType.NAVIGATE, description="go to accounts", value_template=f"{BASE}/accounts"),
        Step(index=1, action=ActionType.FILL, description="enter member id", target=member_id_box, value_template="{member_id}"),
        Step(index=2, action=ActionType.CLICK, description="search", target=search_button),
        Step(index=3, action=ActionType.CLICK, description="open new sub-account form", target=open_subaccount_link),
        # This step expects the deposit form. For member 20001 the interstitial shows
        # instead, so this step's target genuinely won't resolve on the first attempt.
        Step(index=4, action=ActionType.FILL, description="enter initial deposit", target=deposit_box, value_template="{initial_deposit}"),
    ]
    return Artifact(
        capability_id="open-member-subaccount",
        version="test",
        description="test artifact without declared interstitial recovery",
        goal_template="n/a",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"),
        inputs=[
            InputParam(name="member_id", type=ParamType.STRING, description="member id"),
            InputParam(name="initial_deposit", type=ParamType.NUMBER, description="deposit"),
        ],
        outputs=[],
        steps=steps,
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/new-subaccount"),
        business_outcomes=[],
        recoverable_conditions=[],  # deliberately empty, see docstring
        created_from_run_id="hand_built_for_tests",
    )


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_escalate_on_failure_bounded_operational_retry_succeeds():
    """The fix for the previously-dead `record.resume`: a human reporting they
    fixed something operational gets exactly one more attempt at the SAME
    step, on the SAME page, no LLM, no new step in the artifact.

    Previously skipped: the old version of this test built its own separate
    Page and monkeypatched sync_playwright/HandoffSession.__init__ to smuggle
    an operator bound to THAT page into replay_artifact(), which still calls
    browser.new_page() internally, so the operator's clicks landed on a page
    nobody was actually replaying against, and the retry always timed out.
    This version uses replay_artifact()'s operator_factory seam instead: the
    factory is called with the exact live Page replay itself creates, so the
    operator can act on it directly, no monkeypatching of Playwright/browser
    construction anywhere in this test.
    """
    artifact = _subaccount_artifact_without_declared_interstitial_recovery()
    before = artifact.model_dump_json()

    operators: list[_RecoveryOnlyOperator] = []

    def _make_operator(page):
        op = _RecoveryOnlyOperator(page, resume=True)
        operators.append(op)
        return op

    result = replay_artifact(
        artifact,
        {"member_id": "20001", "initial_deposit": "50"},
        headless=True,
        escalate_on_failure=True,
        operator_factory=_make_operator,
    )

    # G: replay succeeded once the human dismissed the interstitial.
    assert result.kind == "success"
    assert result.steps_executed == len(artifact.steps) == 5

    # E: the human's action happened on the SAME live session. This is not just
    # asserted structurally (the factory was handed replay's own Page) but
    # proven causally: if take_control() had acted on a different page, the
    # interstitial would still be showing on replay's real page and the
    # retried step would fail exactly as it did before this seam existed
    # (see the skip reason this test used to carry), instead it succeeded.
    assert len(operators) == 1
    operator = operators[0]
    assert operator.take_control_calls == 1, "expected exactly one operational-recovery escalation"
    assert len(operator.urls_seen) == 1
    assert "/new-subaccount" in operator.urls_seen[0], (
        "operator must have been looking at the real in-flight replay page "
        "(the sub-account form flow), not a blank or unrelated one"
    )

    # F: the SAME failed step (index 4) was retried exactly once, the first
    # attempt fails at target resolution (interstitial showing) before ever
    # appending to strategy_log, so exactly one successful resolution of step 4
    # in the log means exactly one (successful) retry happened, not zero, not more.
    step_4_entries = [e for e in result.strategy_log if e.step_index == 4]
    assert len(step_4_entries) == 1, f"expected exactly one successful resolution of step 4, got {len(step_4_entries)}"
    assert [e.step_index for e in result.strategy_log] == [0, 1, 2, 3, 4], "every step ran exactly once in order"

    # H: the artifact was never touched, operational recovery only, never learning/repair.
    assert artifact.model_dump_json() == before
    assert artifact.recoverable_conditions == []


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_escalate_on_failure_declining_resume_ends_in_hard_failure_no_retry():
    artifact = _subaccount_artifact_without_declared_interstitial_recovery()
    # no escalate_on_failure at all: the simplest, most common case, a failure
    # is just a failure, reported once, no human involved.
    result = replay_artifact(artifact, {"member_id": "20001", "initial_deposit": "50"}, headless=True, escalate_on_failure=False)

    assert result.kind == "hard_failure"
    assert result.failure.step_index == 4


# -- architectural boundary checks --------------------------------------------


def test_replay_module_never_imports_llm_machinery():
    source = open(replay_executor_mod.__file__).read()
    for forbidden in ("anthropic", "agent.llm", "agent.loop", "make_client", "next_action"):
        assert forbidden not in source, f"replay/executor.py must never reference {forbidden!r}"


# -- input-param contract: replay must reject what it can't honor -------------
#
# Regression coverage for a real bug: replaying open-member-subaccount@1.0.0
# with --param initial_deposit=100 silently accepted and ignored that param
# (the artifact never declared it, and no step ever templated it in), so the
# deposit box was always filled with a hardcoded literal regardless of what
# was passed. The CLI looked like it rejected $100 as an invalid amount; it
# had actually never submitted $100 at all. See PROJECT_STATE.md. Both checks
# below run before run_id/evidence_dir/browser setup in replay_artifact(), so
# these need no mock app and no Playwright browser.


def _param_contract_artifact(inputs: list[InputParam]) -> Artifact:
    return Artifact(
        capability_id="param-contract-demo",
        version="1.0.0",
        description="Minimal artifact for input-param contract tests.",
        goal_template="n/a",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"),
        inputs=inputs,
        outputs=[],
        steps=[],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/"),
        created_from_run_id="hand_built_for_tests",
    )


def test_replay_rejects_missing_required_param():
    artifact = _param_contract_artifact([InputParam(name="member_id", type=ParamType.STRING, description="member id")])
    with pytest.raises(ValueError, match="Missing required params"):
        replay_artifact(artifact, {}, headless=True)


def test_replay_rejects_unknown_param_not_declared_by_artifact():
    artifact = _param_contract_artifact([InputParam(name="member_id", type=ParamType.STRING, description="member id")])
    with pytest.raises(ValueError, match="Unknown params"):
        replay_artifact(artifact, {"member_id": "10001", "initial_deposit": "100"}, headless=True)


def test_replay_param_validation_does_not_mutate_the_artifact():
    artifact = _param_contract_artifact([InputParam(name="member_id", type=ParamType.STRING, description="member id")])
    before = artifact.model_dump_json()

    with pytest.raises(ValueError):
        replay_artifact(artifact, {"member_id": "10001", "bogus": "x"}, headless=True)
    with pytest.raises(ValueError):
        replay_artifact(artifact, {}, headless=True)

    assert artifact.model_dump_json() == before


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_replay_never_mutates_the_artifact_object():
    business_outcomes, recoverable, commit_verification = annotations_for("open-member-subaccount")
    member_id_box = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "input[name=member_id]"})])
    search_button = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": "Search"})])
    artifact = Artifact(
        capability_id="open-member-subaccount",
        version="test",
        description="mutation-check artifact",
        goal_template="n/a",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"),
        inputs=[InputParam(name="member_id", type=ParamType.STRING, description="member id")],
        outputs=[],
        steps=[
            Step(index=0, action=ActionType.NAVIGATE, description="go to accounts", value_template=f"{BASE}/accounts"),
            Step(index=1, action=ActionType.FILL, description="enter member id", target=member_id_box, value_template="{member_id}"),
            Step(index=2, action=ActionType.CLICK, description="search", target=search_button),
        ],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/member/{member_id}"),
        business_outcomes=business_outcomes,
        recoverable_conditions=recoverable,
        commit_verification=commit_verification,
        created_from_run_id="hand_built_for_tests",
    )
    before = artifact.model_dump_json()

    result = replay_artifact(artifact, {"member_id": "10001"}, headless=True)

    assert result.kind == "success"
    assert artifact.model_dump_json() == before
