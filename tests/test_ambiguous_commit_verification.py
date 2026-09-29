"""Ambiguous-commit detection: when a step fails at or after this artifact's
own risky "confirm" step, the underlying action may have already committed
server-side even though the step itself didn't complete cleanly (see
mock_app's post_commit_response_failure fault injection and the design
discussion in this session leading up to it). Never guess either way:

  - the declared CommitVerification finds clear evidence it committed
    -> report success (recovered_via_commit_verification=True), never
       invent the outputs that were never actually read
  - it finds clear evidence it did NOT commit -> ordinary hard_failure,
    unchanged from before this feature existed
  - the check itself can't complete -> escalate to a human (if
    escalate_on_failure) or report hard_failure with an explicit
    "do not retry" warning (if not) -- NEVER silently retry

No queue, no scheduler, no async retry-later infrastructure -- deliberately
out of scope (see this session's design discussion). This is entirely a
detect-and-report boundary inside the existing replay failure machinery.
"""

from __future__ import annotations

import re
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from artifact.annotations import annotations_for
from artifact.schema import (
    ArtifactStatus,
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    CommitVerification,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    OutputField,
    ParamType,
    Step,
    Target,
    TargetApp,
)
from handoff.session import InterventionRequest
from replay.executor import replay_artifact

BASE = "http://127.0.0.1:8000"


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


def _arm(scenario: str) -> None:
    urllib.request.urlopen(
        urllib.request.Request(f"{BASE}/__test__/arm_failure", data=f"scenario={scenario}".encode(), method="POST"),
        timeout=2,
    )


def _live_checking_balance(member_id: str) -> float:
    html = urllib.request.urlopen(f"{BASE}/member/{member_id}", timeout=2).read().decode()
    return float(re.search(r"Checking Balance:</td><td>\$([\-\d.]+)</td>", html).group(1))


def _css(css: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css})])


def _role(role: str, name: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": role, "name": name})])


def _transfer_artifact(confirm_target: Target | None = None, navigate_template: str | None = None) -> Artifact:
    """Hand-built, transfer-funds-shaped artifact for exercising the
    ambiguous-commit mechanism directly -- independent of what any one real
    discovery run happens to produce, same philosophy as the existing
    tests/test_replay_transfer.py fixture.
    """
    business_outcomes, recoverable, cv = annotations_for("transfer-funds")
    assert cv is not None, "transfer-funds must declare a commit_verification for these tests to mean anything"
    if navigate_template is not None:
        cv = CommitVerification(navigate_template=navigate_template, detect=cv.detect, description=cv.description)

    steps = [
        Step(index=0, action=ActionType.NAVIGATE, description="Go to member record.", value_template="{base_url}/member/{member_id}/transfer"),
        Step(index=1, action=ActionType.SELECT, description="Choose source account.", target=_css("select[name=from_account]"), value_template="{from_account}"),
        Step(index=2, action=ActionType.FILL, description="Enter recipient member id.", target=_css("input[name=to_member_id]"), value_template="{to_member_id}"),
        Step(index=3, action=ActionType.SELECT, description="Choose destination account.", target=_css("select[name=to_account]"), value_template="{to_account}"),
        Step(index=4, action=ActionType.FILL, description="Enter transfer amount.", target=_css("input[name=amount]"), value_template="{amount}"),
        Step(index=5, action=ActionType.CLICK, description="Review the transfer.", target=_role("button", "Review Transfer"), risk="safe"),
        Step(index=6, action=ActionType.CLICK, description="Confirm the transfer.", target=confirm_target or _role("button", "Confirm Transfer"), risk="confirm"),
        Step(index=7, action=ActionType.READ_TEXT, description="Read confirmation number.", target=_css("td b:nth-of-type(2)"), extract_as="confirmation_number"),
    ]
    return Artifact(
        capability_id="transfer-funds",
        version="test",
        description="Transfer funds from one member's account to another member's account.",
        goal_template="n/a",
        # These tests exercise commit verification, not the risky-step approval gate
        # (tests/test_replay_risk_gate.py), so the fixture is a reviewer-approved artifact.
        status=ArtifactStatus.APPROVED,
        target_app=TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"),
        inputs=[
            InputParam(name="member_id", type=ParamType.STRING, description="Source member id."),
            InputParam(name="from_account", type=ParamType.STRING, description="checking or savings."),
            InputParam(name="to_member_id", type=ParamType.STRING, description="Destination member id."),
            InputParam(name="to_account", type=ParamType.STRING, description="checking or savings."),
            InputParam(name="amount", type=ParamType.NUMBER, description="Transfer amount."),
            InputParam(name="base_url", type=ParamType.STRING, description="Target app base URL."),
        ],
        outputs=[OutputField(name="confirmation_number", type=ParamType.STRING, description="Transfer confirmation id.", source_step=7)],
        steps=steps,
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/transfer/confirm"),
        business_outcomes=business_outcomes,
        recoverable_conditions=recoverable,
        commit_verification=cv,
        created_from_run_id="hand_built_for_tests",
    )


def _params(member_id="10001", to_member_id="20001", amount="5") -> dict[str, str]:
    return {"member_id": member_id, "from_account": "checking", "to_member_id": to_member_id, "to_account": "checking", "amount": amount, "base_url": BASE}


class _ScriptedAmbiguousCommitOperator:
    def __init__(self, resume: bool):
        self.resume = resume
        self.requests: list[InterventionRequest] = []

    def confirm(self, request: InterventionRequest):
        return True, "n/a"

    def take_control(self, request: InterventionRequest):
        self.requests.append(request)
        note = "confirmed committed" if self.resume else "confirmed not committed"
        return note, self.resume, False, []


# -- annotations: the declared contract itself ---------------------------------


def test_transfer_funds_declares_a_commit_verification_keyed_on_recipient():
    _, _, cv = annotations_for("transfer-funds")
    assert cv is not None
    assert "{member_id}" in cv.navigate_template
    assert "{to_member_id}" in cv.detect.value


def test_open_member_subaccount_does_not_declare_one():
    # No reliable generic signal exists for a server-generated new account id --
    # better none than a misleading one (see artifact/annotations.py's own comment).
    _, _, cv = annotations_for("open-member-subaccount")
    assert cv is None


# -- live: the three real outcomes ----------------------------------------------


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_post_confirm_failure_recovers_as_success_when_verification_finds_evidence_it_committed():
    from_before = _live_checking_balance("10001")
    to_before = _live_checking_balance("20001")

    _arm("post_commit_response_failure")
    artifact = _transfer_artifact()
    result = replay_artifact(artifact, _params(amount="5"), headless=True)

    assert result.kind == "success"
    assert result.recovered_via_commit_verification is True
    assert "confirmation_number" not in result.outputs, "never invent an output that was never actually read"

    from_after = _live_checking_balance("10001")
    to_after = _live_checking_balance("20001")
    assert from_after == pytest.approx(from_before - 5.0)
    assert to_after == pytest.approx(to_before + 5.0)


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_post_confirm_failure_stays_a_hard_failure_when_verification_finds_no_evidence():
    # The confirm step's own target is broken, so the click never dispatches at
    # all -- nothing is ever submitted, so a real check of transaction history
    # correctly finds nothing. Recipient 10002 deliberately chosen distinct from
    # the other tests' 20001: other tests in this file (and earlier real usage
    # this session) genuinely DO transfer to 20001, so reusing it here would let
    # a real, unrelated prior transaction falsely satisfy this "no evidence"
    # check -- the exact kind of imprecision annotations.py's own docstring
    # already flags about this signal.
    from_before = _live_checking_balance("10001")

    artifact = _transfer_artifact(confirm_target=_css("#does-not-exist-anywhere"))
    result = replay_artifact(artifact, _params(to_member_id="10002", amount="5"), headless=True)

    assert result.kind == "hard_failure"
    assert result.recovered_via_commit_verification is False
    assert result.failure.step_index == 6

    assert _live_checking_balance("10001") == pytest.approx(from_before), "nothing should have moved"


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_inconclusive_verification_escalates_and_recovers_when_a_human_confirms_it_committed():
    _arm("post_commit_response_failure")
    artifact = _transfer_artifact(navigate_template="http://127.0.0.1:1/")  # connection refused -> check raises
    operators: list[_ScriptedAmbiguousCommitOperator] = []

    def factory(page):
        op = _ScriptedAmbiguousCommitOperator(resume=True)
        operators.append(op)
        return op

    result = replay_artifact(artifact, _params(amount="5"), headless=True, escalate_on_failure=True, operator_factory=factory)

    assert result.kind == "success"
    assert result.recovered_via_commit_verification is True
    assert len(operators) == 1
    # Two escalations happen in order: the pre-existing bounded operational
    # retry gate fires first ("did you fix something, try again?") -- this
    # operator says yes to everything, so it retries the SAME failed step once,
    # which fails again against the same unchanged page, THEN the new
    # ambiguous-commit gate fires because the check itself couldn't complete.
    reasons = [r.reason for r in operators[0].requests]
    assert reasons == ["replay_hard_failure", "commit_verification_inconclusive"]
    assert "confirm" in operators[0].requests[-1].message.lower()


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_inconclusive_verification_escalates_and_reports_failure_when_a_human_confirms_it_did_not_commit():
    _arm("post_commit_response_failure")
    artifact = _transfer_artifact(navigate_template="http://127.0.0.1:1/")

    def factory(page):
        return _ScriptedAmbiguousCommitOperator(resume=False)

    result = replay_artifact(artifact, _params(amount="5"), headless=True, escalate_on_failure=True, operator_factory=factory)

    assert result.kind == "hard_failure"
    assert result.recovered_via_commit_verification is False
    assert "human confirmed" in result.failure.message.lower()


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_inconclusive_verification_without_escalate_on_failure_never_retries_and_warns_explicitly():
    # No human available at all (escalate_on_failure defaults False) -- must NEVER
    # fall back to guessing or retrying; must report a loud, explicit warning instead.
    _arm("post_commit_response_failure")
    artifact = _transfer_artifact(navigate_template="http://127.0.0.1:1/")

    result = replay_artifact(artifact, _params(amount="5"), headless=True)

    assert result.kind == "hard_failure"
    assert result.recovered_via_commit_verification is False
    assert "do not retry" in result.failure.message.lower()


# -- scope guards: only triggers where it should ---------------------------------


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_commit_verification_never_runs_for_a_failure_before_the_confirm_step():
    # Break an early, pre-commit step instead of the confirm step -- the ambiguous-
    # commit machinery must not even engage, since nothing risky was ever attempted.
    artifact = _transfer_artifact()
    artifact.steps[1].target = _css("#does-not-exist-anywhere-early")

    result = replay_artifact(artifact, _params(amount="5"), headless=True)

    assert result.kind == "hard_failure"
    assert result.recovered_via_commit_verification is False
    assert result.failure.step_index == 1


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_no_declared_commit_verification_falls_through_to_plain_hard_failure_unchanged():
    # Same shape as before this feature existed for any capability (like
    # open-member-subaccount today) that simply doesn't declare one.
    from_before = _live_checking_balance("10001")

    _arm("post_commit_response_failure")
    artifact = _transfer_artifact()
    artifact.commit_verification = None

    result = replay_artifact(artifact, _params(amount="5"), headless=True)

    assert result.kind == "hard_failure"
    assert result.recovered_via_commit_verification is False

    # the underlying write still happened for real (the injection commits before
    # rendering its error page) -- this test's point is that REPLAY doesn't know
    # that when nothing declares a way to check, exactly the pre-existing gap.
    assert _live_checking_balance("10001") == pytest.approx(from_before - 5.0)
