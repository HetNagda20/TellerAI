"""End-to-end replay tests for the 'transfer-funds' capability against the
live mock app, mirroring tests/test_replay_integration.py. This is the
flagship risky/irreversible capability (guardrails classify its confirm
step as 'confirm'), so its business-outcome coverage (bad recipient,
insufficient funds, locked account) matters more than most.
"""

from __future__ import annotations

import urllib.request

import pytest

from artifact.annotations import annotations_for
from artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    OutputField,
    ParamType,
    Step,
    Target,
    TargetApp,
)
from replay.executor import replay_artifact

BASE = "http://127.0.0.1:8000"


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")


def _css(css: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css})])


def _role(role: str, name: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": role, "name": name})])


def _build_artifact() -> Artifact:
    steps = [
        Step(index=0, action=ActionType.NAVIGATE, description="Go to member record.", value_template="{base_url}/member/{member_id}/transfer"),
        Step(index=1, action=ActionType.SELECT, description="Choose source account.", target=_css("select[name=from_account]"), value_template="{from_account}"),
        Step(index=2, action=ActionType.FILL, description="Enter recipient member id.", target=_css("input[name=to_member_id]"), value_template="{to_member_id}"),
        Step(index=3, action=ActionType.SELECT, description="Choose destination account.", target=_css("select[name=to_account]"), value_template="{to_account}"),
        Step(index=4, action=ActionType.FILL, description="Enter transfer amount.", target=_css("input[name=amount]"), value_template="{amount}"),
        Step(index=5, action=ActionType.CLICK, description="Review the transfer.", target=_role("button", "Review Transfer"), risk="safe"),
        Step(index=6, action=ActionType.CLICK, description="Confirm the transfer.", target=_role("button", "Confirm Transfer"), risk="confirm"),
        Step(index=7, action=ActionType.READ_TEXT, description="Read confirmation number.", target=_css("td b:nth-of-type(2)"), extract_as="confirmation_number"),
    ]
    business_outcomes, recoverable, commit_verification = annotations_for("transfer-funds")
    return Artifact(
        capability_id="transfer-funds",
        version="test",
        description="Transfer funds from one member's account to another member's account.",
        goal_template="Transfer {amount} from member {member_id}'s {from_account} to member {to_member_id}'s {to_account}.",
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
        commit_verification=commit_verification,
        created_from_run_id="hand_built_for_tests",
    )


def _params(member_id="10001", to_member_id="20001", from_account="checking", to_account="checking", amount="25") -> dict[str, str]:
    return {
        "member_id": member_id,
        "from_account": from_account,
        "to_member_id": to_member_id,
        "to_account": to_account,
        "amount": amount,
        "base_url": BASE,
    }


def test_transfer_success_path():
    result = replay_artifact(_build_artifact(), _params(), headless=True)
    assert result.kind == "success"
    assert result.outputs["confirmation_number"].startswith("TXF-")


def test_transfer_recipient_not_found_is_business_outcome():
    result = replay_artifact(_build_artifact(), _params(to_member_id="88888"), headless=True)
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "recipient_not_found"


def test_transfer_insufficient_funds_is_business_outcome():
    result = replay_artifact(_build_artifact(), _params(amount="999999"), headless=True)
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "insufficient_funds"


def test_transfer_from_locked_account_is_business_outcome():
    result = replay_artifact(_build_artifact(), _params(member_id="10002"), headless=True)
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "account_locked"
