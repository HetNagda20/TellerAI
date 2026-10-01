"""Per-artifact approval on replay. An approved artifact runs risky steps unattended and flags them.
Any other needs a live approval first, and fails closed without one or when denied."""

import pytest

from artifact.schema import ArtifactStatus
from replay.executor import _needs_per_run_approval, replay_artifact
from tests.conftest import ledger, load_capability, member

PARAMS = {"from_member_id": "10001", "from_account_type": "checking", "to_member_id": "20001", "to_account_type": "checking", "amount": "5"}
RISKY_STEP = 11


class _ScriptedOperator:
    def __init__(self, approve: bool):
        self.approve = approve
        self.requests = []

    def confirm(self, request):
        self.requests.append(request)
        return self.approve, "scripted"


def _balances() -> tuple[float, float]:
    return member("10001")["checking_balance"], member("20001")["checking_balance"]


def test_draft_artifact_with_a_risky_step_and_no_approver_is_refused_before_any_step(base_url):
    draft = load_capability("transfer-funds", "1.0.0", base_url, ArtifactStatus.DRAFT)
    assert _needs_per_run_approval(draft) is True
    assert _needs_per_run_approval(draft.model_copy(update={"status": ArtifactStatus.APPROVED})) is False
    no_risk = draft.model_copy(update={"steps": [s.model_copy(update={"risk": "safe"}) for s in draft.steps]})
    assert _needs_per_run_approval(no_risk) is False
    before = _balances()

    result = replay_artifact(draft, PARAMS, headless=True)

    assert result.kind == "hard_failure"
    assert result.steps_executed == 0
    assert result.failure.step_index == RISKY_STEP and "draft" in result.failure.message
    assert _balances() == before


def test_a_draft_artifacts_risky_step_never_runs_if_denied_and_runs_only_after_a_human_approves(base_url):
    draft = load_capability("transfer-funds", "1.0.0", base_url, ArtifactStatus.DRAFT)
    before = _balances()

    denier = _ScriptedOperator(approve=False)
    denied = replay_artifact(draft, PARAMS, headless=True, operator_factory=lambda page: denier)
    assert denied.kind == "hard_failure"
    assert denied.failure.step_index == RISKY_STEP and denied.failure.observed == "denied"
    assert [(e.reason, e.outcome) for e in denied.escalations] == [("risky_action_confirm", "denied")]
    assert denied.steps_executed == RISKY_STEP  # every step before the gate ran; the gated one did not
    assert _balances() == before and ledger("10001") == []

    approver = _ScriptedOperator(approve=True)
    result = replay_artifact(draft, PARAMS, headless=True, operator_factory=lambda page: approver)
    assert result.kind == "success"
    assert [r.reason for r in approver.requests] == ["risky_action_confirm"]
    assert [(e.reason, e.outcome) for e in result.escalations] == [("risky_action_confirm", "approved")]
    assert result.unattended_risky_steps == []
    assert _balances() == (pytest.approx(812.44 - 5), pytest.approx(2210.77 + 5))


def test_approved_artifact_runs_unattended_and_flags_the_risky_step(base_url):
    result = replay_artifact(load_capability("transfer-funds", "1.0.0", base_url), PARAMS, headless=True)

    assert result.kind == "success"
    assert result.escalations == []
    assert result.unattended_risky_steps == [RISKY_STEP]
    assert _balances() == (pytest.approx(812.44 - 5), pytest.approx(2210.77 + 5))
