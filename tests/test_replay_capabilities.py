"""Replays the real saved artifacts against the mock app, so a re-recording that breaks a contract
fails here. Assertions are on the app's own state."""

from __future__ import annotations

import threading

import pytest

from artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    InputParam,
    ParamType,
    Step,
    TargetApp,
)
from artifact.annotations import check_annotation_placeholders
from replay.executor import replay_artifact
from tests.conftest import ledger, load_capability, member
from mock_app import data as mock_data

TRANSFER = {"from_member_id": "10001", "from_account_type": "checking", "to_member_id": "20001", "to_account_type": "checking", "amount": "30"}


def _transfer(**overrides) -> dict[str, str]:
    return {**TRANSFER, **overrides}


def _balances(member_id: str) -> tuple[float, float]:
    m = member(member_id)
    return m["checking_balance"], m["savings_balance"]


# -- success -----------------------------------------------------------------


def test_transfer_between_two_members_debits_and_credits_and_extracts_the_confirmation(base_url):
    result = replay_artifact(load_capability("transfer-funds", "1.0.0", base_url), _transfer(), headless=True)

    assert result.kind == "success"
    assert result.outputs["confirmation_number"] == "TXF-7001"
    assert member("10001")["checking_balance"] == pytest.approx(812.44 - 30)
    assert member("20001")["checking_balance"] == pytest.approx(2210.77 + 30)
    rows = ledger("10001") + ledger("20001")
    assert [r["source"] for r in rows] == ["replay", "replay"]
    assert all("TXF-7001" in r["description"] for r in rows)


def test_transfer_with_different_accounts_and_a_new_confirmation_id_each_run(base_url):
    """Savings to checking, twice. The second confirmation number differs from the recorded one, so
    the css_path fallback must still read it."""
    artifact = load_capability("transfer-funds", "1.0.0", base_url)
    params = _transfer(from_member_id="12345", to_member_id="12345", from_account_type="savings", to_account_type="checking", amount="100")

    first = replay_artifact(artifact, params, headless=True)
    second = replay_artifact(artifact, params, headless=True)

    assert (first.kind, second.kind) == ("success", "success")
    assert (first.outputs["confirmation_number"], second.outputs["confirmation_number"]) == ("TXF-7001", "TXF-7002")
    read_step = next(s for s in second.strategy_log if s.action == "read_text")
    assert read_step.strategy_used == "css_path"
    assert _balances("12345") == (pytest.approx(8732.00 + 200), pytest.approx(1562.00 - 200))


def test_new_subaccount_is_created_with_the_default_cash_funding(base_url):
    """The recorded procedure never touches the funding-source select (it is not an
    input), so the form default, a cash deposit, applies: no member balance moves."""
    result = replay_artifact(
        load_capability("open-member-subaccount", "1.0.0", base_url),
        {"member_id": "10001", "account_type": "Savings", "initial_deposit": "100"},
        headless=True,
    )

    assert result.kind == "success"
    created = mock_data.SUBACCOUNTS["10001"]
    assert [(s["type"], s["balance"], s["source"]) for s in created] == [("savings", 100.0, "replay")]  # the select was given the label "Savings"; the app stores the option value
    assert list(result.outputs.values()) == [created[0]["id"]]  # the output name is the agent's choice
    assert _balances("10001") == (pytest.approx(812.44), pytest.approx(1204.09))


# -- business outcomes: legitimate results, not errors, and nothing moved ----------


@pytest.mark.parametrize(
    "overrides, outcome",
    [
        ({"to_member_id": "99999"}, "recipient_not_found"),
        ({"amount": "999999"}, "insufficient_funds"),
        ({"from_member_id": "10002"}, "account_locked"),
    ],
    ids=["recipient_not_found", "insufficient_funds", "account_locked"],
)
def test_transfer_business_outcome_is_reported_and_no_money_moves(base_url, overrides, outcome):
    before = (_balances("10001"), _balances("10002"), _balances("20001"))
    result = replay_artifact(load_capability("transfer-funds", "1.0.0", base_url), _transfer(**overrides), headless=True)

    assert result.kind == "business_outcome"
    assert result.business_outcome_name == outcome
    assert (_balances("10001"), _balances("10002"), _balances("20001")) == before
    assert ledger("10001") == ledger("10002") == ledger("20001") == []


def test_an_unknown_member_is_a_business_outcome_for_every_capability_not_just_annotated_ones(base_url):
    """Member 99999 is 'not found' on every screen, so it is declared once for the app and applies
    to every capability, with the source recorded."""
    unknown = {
        "fetch-account-balance": ("1.0.0", {"member_id": "99999"}),
        "create-auto-loan-for": ("1.0.0", {"member_id": "99999", "loan_amount": "8000", "loan_purpose": "Personal", "interest_rate": "5.5"}),
        "open-member-subaccount": ("1.0.0", {"member_id": "99999", "account_type": "Savings", "initial_deposit": "100"}),
        "transfer-funds": ("1.0.0", _transfer(from_member_id="99999")),  # the SOURCE member is unknown
    }
    for capability, (version, params) in unknown.items():
        result = replay_artifact(load_capability(capability, version, base_url), params, headless=True)
        assert (result.kind, result.business_outcome_name) == ("business_outcome", "member_not_found"), capability
        assert result.business_outcome_source == "app", capability  # none of them declares it itself

    too_small = replay_artifact(
        load_capability("open-member-subaccount", "1.0.0", base_url),
        {"member_id": "10001", "account_type": "Savings", "initial_deposit": "10"},
        headless=True,
    )
    assert (too_small.kind, too_small.business_outcome_name, too_small.business_outcome_source) == ("business_outcome", "invalid_deposit_amount", "capability")
    assert mock_data.SUBACCOUNTS["10001"] == [] and mock_data.LOANS["12345"] == [] and ledger("10001") == []


# -- the artifact's contract ---------------------------------------------------


def test_saved_artifacts_declare_the_contract_replay_relies_on(base_url):
    a = load_capability("transfer-funds", "1.0.0", base_url)
    selects = [s for s in a.steps if s.action == ActionType.SELECT]

    assert {i.name for i in a.inputs} == {"from_member_id", "from_account_type", "to_member_id", "to_account_type", "amount"}
    assert [s.value_template for s in selects] == ["{from_account_type}", "{to_account_type}"]  # templated, not literal
    assert [o.name for o in a.outputs] == ["confirmation_number"]
    # success is what the operator sees, the same text for every request: no placeholder, no generated value
    for name in ("transfer-funds", "open-member-subaccount", "create-auto-loan-for", "update-member-address", "fetch-account-balance"):
        checkpoint = load_capability(name, "1.0.0", base_url).final_checkpoint
        assert checkpoint.kind.value == "text_contains" and "{" not in checkpoint.value, name
    assert [s.index for s in a.steps if s.risk == "confirm"] == [11]
    assert a.commit_verification is not None and a.commit_verification.detect.value == "{run_id}"  # the run id trail
    assert "{primary_id}" in a.commit_verification.navigate_template  # not an input name the agent chose
    assert {b.name for b in a.business_outcomes} == {"recipient_not_found"}  # its own; the rest come from the app catalog

    # the sub-account capability
    a = load_capability("open-member-subaccount", "1.0.0", base_url)
    templates = {s.value_template for s in a.steps if s.value_template and s.action != ActionType.NAVIGATE}

    assert {i.name for i in a.inputs} == {"member_id", "account_type", "initial_deposit"}
    assert {"{member_id}", "{account_type}", "{initial_deposit}"} <= templates
    assert a.commit_verification is not None and a.commit_verification.detect.value == "{run_id}"
    assert "session_renewal_interstitial" in {r.name for r in a.recoverable_conditions}

    # every {placeholder} an annotation uses is a real input of that artifact (the agent chooses input names)
    for name, version in [("transfer-funds", "1.0.0"), ("open-member-subaccount", "1.0.0"), ("create-auto-loan-for", "1.0.0"), ("fetch-account-balance", "1.0.0")]:
        assert check_annotation_placeholders(load_capability(name, version, base_url)) == [], name


# -- replay's own guarantees -----------------------------------------------------


def test_param_validation_rejects_bad_input_and_never_mutates_the_artifact(base_url):
    artifact = load_capability("transfer-funds", "1.0.0", base_url)
    before = artifact.model_dump_json()

    with pytest.raises(ValueError, match="Missing required params"):
        replay_artifact(artifact, {k: v for k, v in TRANSFER.items() if k != "amount"}, headless=True)
    with pytest.raises(ValueError, match="Unknown params"):
        replay_artifact(artifact, _transfer(base_url="http://elsewhere"), headless=True)

    assert artifact.model_dump_json() == before
    assert ledger("10001") == []


def test_concurrent_replays_of_one_artifact_are_isolated(base_url):
    """Two replay_artifact() calls sharing the same read-only Artifact object get
    distinct run ids and evidence dirs and leave the artifact untouched."""
    shared = Artifact(
        capability_id="member-lookup-concurrency-demo",
        version="1.0.0",
        description="Minimal artifact for concurrency isolation testing.",
        goal_template="Look up member {member_id}.",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=base_url, entry_path="/"),
        inputs=[InputParam(name="member_id", type=ParamType.STRING, description="member id")],
        outputs=[],
        steps=[Step(index=0, action=ActionType.NAVIGATE, description="go", value_template=f"{base_url}/member/{{member_id}}")],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/member/{member_id}"),
        created_from_run_id="hand_built_for_tests",
    )
    before = shared.model_dump_json()
    results, errors = {}, []

    def _run(key: str, member_id: str) -> None:
        try:
            results[key] = replay_artifact(shared, {"member_id": member_id}, headless=True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_run, args=("a", "10001")), threading.Thread(target=_run, args=("b", "20001"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors
    a, b = results["a"], results["b"]
    assert (a.kind, b.kind) == ("success", "success")
    assert a.run_id != b.run_id and a.evidence_dir != b.evidence_dir
    assert shared.model_dump_json() == before
