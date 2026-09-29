"""Per-artifact approval for risky steps on the replay path.

Policy under test: an artifact with status=approved runs its risky
(risk="confirm") steps unattended and flags them in the result; any other
artifact containing a risky step must get a live human approval before that
step, and fails closed (never executes it) when there is no approver or the
human declines. Runs the real transfer-funds@1.2.1 artifact against the mock
app and checks the app's own balances, not just the result object.
"""

import re
import urllib.request

import pytest

from artifact.schema import ArtifactStatus
from artifact.store import ARTIFACTS_DIR, load_path
from replay.executor import _needs_per_run_approval, replay_artifact

BASE = "http://127.0.0.1:8000"
ARTIFACT_PATH = ARTIFACTS_DIR / "transfer-funds@1.2.1.json"
PARAMS = {"member_id": "10001", "from_account": "checking", "to_member_id": "20001", "to_account": "checking", "amount": "5"}


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


needs_app = pytest.mark.skipif(
    not _mock_app_up() or not ARTIFACT_PATH.exists(),
    reason="mock app is not running at 127.0.0.1:8000, or transfer-funds@1.2.1 is missing",
)


def _checking_balance(member_id: str) -> float:
    html = urllib.request.urlopen(f"{BASE}/member/{member_id}").read().decode()
    return float(re.search(r"Checking Balance:.*?\$([\d,.]+)", html, re.S).group(1).replace(",", ""))


def _artifact(status: ArtifactStatus):
    return load_path(ARTIFACT_PATH).model_copy(update={"status": status})


class _ScriptedOperator:
    def __init__(self, approve: bool):
        self.approve = approve
        self.requests = []

    def confirm(self, request):
        self.requests.append(request)
        return self.approve, "scripted"


def test_needs_per_run_approval_only_for_unapproved_artifacts_with_a_risky_step():
    draft = load_path(ARTIFACT_PATH).model_copy(update={"status": ArtifactStatus.DRAFT}) if ARTIFACT_PATH.exists() else None
    if draft is None:
        pytest.skip("transfer-funds@1.2.1 is missing")
    assert any(s.risk == "confirm" for s in draft.steps)
    assert _needs_per_run_approval(draft) is True
    assert _needs_per_run_approval(draft.model_copy(update={"status": ArtifactStatus.APPROVED})) is False

    no_risk = draft.model_copy(update={"steps": [s.model_copy(update={"risk": "safe"}) for s in draft.steps]})
    assert _needs_per_run_approval(no_risk) is False


@needs_app
def test_draft_artifact_headless_with_no_approver_is_refused_before_any_step():
    before = _checking_balance("10001")
    result = replay_artifact(_artifact(ArtifactStatus.DRAFT), PARAMS, headless=True)

    assert result.kind == "hard_failure"
    assert result.steps_executed == 0
    assert result.failure.step_index == 11
    assert "draft" in result.failure.message
    assert _checking_balance("10001") == before


@needs_app
def test_draft_artifact_runs_the_risky_step_only_after_a_human_approves():
    before_from, before_to = _checking_balance("10001"), _checking_balance("20001")
    operator = _ScriptedOperator(approve=True)
    result = replay_artifact(_artifact(ArtifactStatus.DRAFT), PARAMS, headless=True, operator_factory=lambda page: operator)

    assert result.kind == "success"
    assert len(operator.requests) == 1
    assert operator.requests[0].reason == "risky_action_confirm"
    assert [(e.reason, e.outcome) for e in result.escalations] == [("risky_action_confirm", "approved")]
    assert result.unattended_risky_steps == []
    assert _checking_balance("10001") == pytest.approx(before_from - 5)
    assert _checking_balance("20001") == pytest.approx(before_to + 5)


@needs_app
def test_draft_artifact_denied_by_the_human_never_executes_the_risky_step():
    before_from, before_to = _checking_balance("10001"), _checking_balance("20001")
    operator = _ScriptedOperator(approve=False)
    result = replay_artifact(_artifact(ArtifactStatus.DRAFT), PARAMS, headless=True, operator_factory=lambda page: operator)

    assert result.kind == "hard_failure"
    assert result.failure.step_index == 11
    assert result.failure.observed == "denied"
    assert [(e.reason, e.outcome) for e in result.escalations] == [("risky_action_confirm", "denied")]
    assert result.steps_executed == 11  # every step before the gate ran; the gated one did not
    assert _checking_balance("10001") == before_from
    assert _checking_balance("20001") == before_to


@needs_app
def test_approved_artifact_runs_unattended_and_flags_the_risky_step():
    before_from, before_to = _checking_balance("10001"), _checking_balance("20001")
    result = replay_artifact(_artifact(ArtifactStatus.APPROVED), PARAMS, headless=True)

    assert result.kind == "success"
    assert result.escalations == []
    assert result.unattended_risky_steps == [11]
    assert _checking_balance("10001") == pytest.approx(before_from - 5)
    assert _checking_balance("20001") == pytest.approx(before_to + 5)
