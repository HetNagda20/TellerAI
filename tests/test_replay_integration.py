"""End-to-end replay tests against the live mock app, using a hand-built
artifact that mirrors the real "open-member-subaccount" flow.

This is deliberately NOT the artifact the discovery run produces, per the
assignment, only a genuine LLM-driven run may produce that one (see
/evidence/). This test exists to validate the replay engine's mechanics
(locator fallback, frame traversal, business-outcome/recoverable-condition
detection, checkpoint verification) independently of the LLM, against every
branch the mock app exposes.

Requires the mock app running at http://127.0.0.1:8000 (see README.md).
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


def _build_artifact() -> Artifact:
    member_id_box = Target(
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "textbox", "name": "e.g. 10001"}),
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "input[name=member_id]"}),
        ]
    )
    search_button = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": "Search"})]
    )
    open_subaccount_link = Target(
        candidates=[
            LocatorCandidate(
                strategy=LocatorStrategy.ROLE_NAME,
                value={"role": "link", "name": "Open a New Sub-Account for this Member"},
            )
        ]
    )
    account_type_select = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "select[name=account_type]"})]
    )
    deposit_box = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "input[name=initial_deposit]"})]
    )
    funding_source_select = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "select[name=funding_source]"})]
    )
    review_button = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": "Review"})]
    )
    confirm_button = Target(
        candidates=[
            LocatorCandidate(
                strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": "Confirm & Open Account"}
            )
        ]
    )
    # success.html has two <b> tags in the same cell ("...successfully." and
    # the confirmation number itself), nth-of-type(2) picks the second.
    confirmation_number_cell = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "td b:nth-of-type(2)"})]
    )

    steps = [
        Step(index=0, action=ActionType.NAVIGATE, description="Go to the Accounts tab lookup page.", value_template="{base_url}/accounts"),
        Step(index=1, action=ActionType.FILL, description="Enter member id.", target=member_id_box, value_template="{member_id}"),
        Step(index=2, action=ActionType.CLICK, description="Search.", target=search_button),
        Step(index=3, action=ActionType.CLICK, description="Open new sub-account form.", target=open_subaccount_link),
        Step(index=4, action=ActionType.SELECT, description="Choose account type.", target=account_type_select, value_template="{account_type}"),
        Step(index=5, action=ActionType.FILL, description="Enter initial deposit.", target=deposit_box, value_template="{initial_deposit}"),
        Step(index=6, action=ActionType.SELECT, description="Choose funding source.", target=funding_source_select, value_template="{funding_source}"),
        Step(index=7, action=ActionType.CLICK, description="Review.", target=review_button, risk="safe"),
        Step(index=8, action=ActionType.CLICK, description="Confirm and open the account.", target=confirm_button, risk="confirm"),
        Step(index=9, action=ActionType.READ_TEXT, description="Read confirmation number.", target=confirmation_number_cell, extract_as="confirmation_number"),
    ]

    business_outcomes, recoverable, commit_verification = annotations_for("open-member-subaccount")

    return Artifact(
        capability_id="open-member-subaccount",
        version="test",
        description="Open a new sub-account for a member and reach the confirmation screen.",
        goal_template="Open a new sub-account for member {member_id} and reach the confirmation screen.",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"),
        inputs=[
            InputParam(name="member_id", type=ParamType.STRING, description="Member id."),
            InputParam(name="account_type", type=ParamType.STRING, description="savings or checking."),
            InputParam(name="initial_deposit", type=ParamType.NUMBER, description="Initial deposit amount."),
            InputParam(name="funding_source", type=ParamType.STRING, description="cash, checking, or savings."),
            InputParam(name="base_url", type=ParamType.STRING, description="Target app base URL."),
        ],
        outputs=[OutputField(name="confirmation_number", type=ParamType.STRING, description="New sub-account id.", source_step=9)],
        steps=steps,
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/new-subaccount/confirm"),
        business_outcomes=business_outcomes,
        recoverable_conditions=recoverable,
        commit_verification=commit_verification,
        created_from_run_id="hand_built_for_tests",
    )


def _params(member_id: str, account_type="savings", initial_deposit="100", funding_source="cash") -> dict[str, str]:
    return {
        "member_id": member_id,
        "account_type": account_type,
        "initial_deposit": initial_deposit,
        "funding_source": funding_source,
        "base_url": BASE,
    }


def test_replay_success_path():
    result = replay_artifact(_build_artifact(), _params("10001"), headless=True)
    assert result.kind == "success"
    assert result.outputs["confirmation_number"].startswith("SA-")


def test_replay_member_not_found_is_business_outcome():
    result = replay_artifact(_build_artifact(), _params("99999"), headless=True)
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "member_not_found"


def test_replay_locked_account_is_business_outcome():
    result = replay_artifact(_build_artifact(), _params("10002"), headless=True)
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "account_locked"


def test_replay_invalid_deposit_is_business_outcome():
    result = replay_artifact(_build_artifact(), _params("10001", initial_deposit="1"), headless=True)
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "invalid_deposit_amount"


def test_replay_session_interstitial_recovers_and_succeeds():
    result = replay_artifact(_build_artifact(), _params("20001"), headless=True)
    assert result.kind == "success"
    assert any(e.recovered_condition == "session_renewal_interstitial" for e in result.strategy_log)


def test_replay_funding_from_checking_debits_balance():
    result = replay_artifact(
        _build_artifact(), _params("10001", initial_deposit="50", funding_source="checking"), headless=True
    )
    assert result.kind == "success"


def test_replay_insufficient_funds_for_subaccount_is_business_outcome():
    result = replay_artifact(
        _build_artifact(), _params("10001", initial_deposit="999999", funding_source="checking"), headless=True
    )
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "insufficient_funds"
