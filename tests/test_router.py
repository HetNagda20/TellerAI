"""Router: catalog, a model proposes, a validator decides. The model is scripted here, plus one live
test that skips without Ollama. The pool is a fixed set of artifact copies."""

from __future__ import annotations

import json
import shutil
import urllib.request

import pytest

import artifact.store as store
from router.catalog import build_catalog
from router.proposer import OllamaProposer, ProposerUnavailable
from router.router import route
from router.validator import validate_proposal

T = "http://127.0.0.1:8000"
_POOL = [
    "transfer-funds@1.0.0", "open-member-subaccount@1.0.0", "create-auto-loan-for@1.0.0",
    "fetch-account-balance@1.0.0", "update-member-address@1.0.0",
]
TRANSFER_GOAL = "Transfer $100 from Member 10001 checking to Member 10002 checking."
TRANSFER_ARGS = {"from_member_id": "10001", "from_account_type": "checking", "to_member_id": "10002", "to_account_type": "checking", "amount": "100"}


@pytest.fixture(autouse=True)
def _fixed_artifact_pool(tmp_path, monkeypatch):
    for name in _POOL:
        shutil.copy(store.ARTIFACTS_DIR / f"{name}.json", tmp_path / f"{name}.json")
    monkeypatch.setattr(store, "ARTIFACTS_DIR", tmp_path)


def proposal(capability, args, needed=None, whole=True):
    return {"needed_capabilities": [capability] if needed is None else needed, "capability_id": capability, "args": args, "covers_whole_request": whole}


NONE = {"needed_capabilities": [], "capability_id": "none", "args": {}, "covers_whole_request": False}


class Scripted:
    """A stand-in model that returns scripted answers in order (the last repeats) and records the
    catalog order it was shown."""

    def __init__(self, *answers):
        self.answers, self.shown = list(answers), []

    def propose(self, goal, entries):
        self.shown.append([e.capability_id for e in entries])
        return self.answers[min(len(self.shown), len(self.answers)) - 1]


class Unreachable:
    def propose(self, goal, entries):
        raise ProposerUnavailable("the router model at http://x could not be reached")


def test_the_validator_accepts_only_a_proposal_the_request_supports():
    catalog = {e.capability_id: e for e in build_catalog(T)}

    ok = validate_proposal(TRANSFER_GOAL, proposal("transfer-funds", TRANSFER_ARGS), catalog)
    assert ok.ok and ok.capability_id == "transfer-funds" and ok.params["amount"] == "100"

    # every rejection is a reason a person can read, and every one means discovery
    rejections = [
        (NONE, TRANSFER_GOAL, "no saved capability"),
        (proposal("wire-money", TRANSFER_ARGS, needed=["wire-money"]), TRANSFER_GOAL, "not a saved capability"),
        (proposal("transfer-funds", TRANSFER_ARGS, needed=["transfer-funds", "open-member-subaccount"]), TRANSFER_GOAL, "needs"),  # a compound request
        (proposal("transfer-funds", TRANSFER_ARGS, whole=False), TRANSFER_GOAL, "whole request"),
        (proposal("transfer-funds", {k: v for k, v in TRANSFER_ARGS.items() if k != "amount"}), TRANSFER_GOAL, "not stated"),
        (proposal("transfer-funds", {**TRANSFER_ARGS, "memo": "rent"}), TRANSFER_GOAL, "invented"),
        (proposal("transfer-funds", {**TRANSFER_ARGS, "amount": "null"}), TRANSFER_GOAL, "not stated"),
        (proposal("transfer-funds", {**TRANSFER_ARGS, "amount": "a lot"}), TRANSFER_GOAL, "not a number"),
        (proposal("transfer-funds", {**TRANSFER_ARGS, "amount": "30"}), TRANSFER_GOAL, "does not appear"),  # made up: the request never says 30
        (proposal("transfer-funds", {**TRANSFER_ARGS, "amount": "0"}), TRANSFER_GOAL, "does not appear"),  # "0" is not grounded by member id 10001
        # the destination account was never named; it must not borrow the source's "checking"
        (proposal("transfer-funds", TRANSFER_ARGS), "Transfer $100 from member 10001's checking to member 10002.", "fewer times"),
    ]
    for bad, goal, reason in rejections:
        verdict = validate_proposal(goal, bad, catalog)
        assert not verdict.ok and reason in verdict.reason, (reason, verdict.reason)

    # a dropdown input carries the page's own choices: spelling is mapped onto them, a typo is read but flagged
    loan_args = {"member_id": "20001", "loan_amount": "12500", "loan_purpose": "auto", "interest_rate": "6.25"}
    loan_goal = "Create a $12,500 auto loan for member 20001 at 6.25% interest."
    mapped = validate_proposal(loan_goal, proposal("create-auto-loan-for", loan_args), catalog)
    assert mapped.ok and mapped.params["loan_purpose"] == "Auto" and mapped.notes == []  # "auto" becomes the page's "Auto"
    typo_goal = "Create a $12,500 autoo loan for member 20001 at 6.25% interest."
    for said in ("autoo", "auto"):  # the model may copy the typo or quietly correct it: both are read as "Auto", both flagged
        typo = validate_proposal(typo_goal, proposal("create-auto-loan-for", {**loan_args, "loan_purpose": said}), catalog)
        assert typo.ok and typo.params["loan_purpose"] == "Auto" and typo.notes == ["read 'autoo' as 'Auto' for loan_purpose"], said
    for bad_choice, goal in [("Gold", "Create a $12,500 Gold loan for member 20001 at 6.25% interest."), ("Home", "Create a $12,500 loan for member 20001 at 6.25% interest.")]:
        verdict = validate_proposal(goal, proposal("create-auto-loan-for", {**loan_args, "loan_purpose": bad_choice}), catalog)
        assert not verdict.ok and ("not one of the choices" in verdict.reason or "does not appear" in verdict.reason), bad_choice

    # "its" points back at the member already named, so one mention of the id may serve both member arguments
    same_member = {**TRANSFER_ARGS, "from_member_id": "12345", "to_member_id": "12345", "from_account_type": "savings", "amount": "100"}
    ok_its = validate_proposal("Transfer $100 from member 12345 savings to its checking.", proposal("transfer-funds", same_member), catalog)
    assert ok_its.ok and ok_its.params["from_member_id"] == ok_its.params["to_member_id"] == "12345"
    # ...but only after the mention, only for identifiers, and only with such a word
    for goal, args in [
        ("Transfer $100 from member 12345 savings to checking.", same_member),  # nothing says the destination is the same member
        ("Its owner asked: transfer $100 from member 12345 savings to checking.", same_member),  # the pronoun comes before the id
        ("Transfer $100 from member 10001's checking to member 10002, same type.", TRANSFER_ARGS),  # a repeated word is still borrowing
    ]:
        verdict = validate_proposal(goal, proposal("transfer-funds", args), catalog)
        assert not verdict.ok and "fewer times" in verdict.reason, goal


def test_route_replays_only_what_the_model_proposes_and_the_validator_accepts():
    accepted = Scripted(proposal("transfer-funds", TRANSFER_ARGS))
    decision = route(TRANSFER_GOAL, T, proposer=accepted)
    assert (decision.action, decision.capability_id, decision.risky, decision.status) == ("replay", "transfer-funds", True, "draft")
    assert decision.params["from_member_id"] == "10001" and decision.artifact_path.name == "transfer-funds@1.0.0.json"
    assert len(accepted.shown) == 2 and accepted.shown[1] == list(reversed(accepted.shown[0]))  # asked twice, order reversed

    no_match = Scripted(NONE)
    assert route("Please water the office plants.", T, proposer=no_match).action == "discover" and len(no_match.shown) == 1

    made_up = route(TRANSFER_GOAL, T, proposer=Scripted(proposal("transfer-funds", {**TRANSFER_ARGS, "amount": "30"})))
    assert made_up.action == "discover" and "does not appear" in made_up.reason

    unstable = route(TRANSFER_GOAL, T, proposer=Scripted(proposal("transfer-funds", TRANSFER_ARGS), NONE))
    assert unstable.action == "discover" and "different order" in unstable.reason


def test_route_fails_toward_discovery_when_there_is_nothing_to_route_to_or_no_model():
    down = route(TRANSFER_GOAL, T, proposer=Unreachable())
    assert down.action == "discover" and "could not be reached" in down.reason

    real_but_off = route(TRANSFER_GOAL, T, proposer=OllamaProposer(url="http://127.0.0.1:1", timeout_s=2))
    assert real_but_off.action == "discover" and "could not be reached" in real_but_off.reason

    nothing_recorded = Scripted(proposal("transfer-funds", TRANSFER_ARGS))
    elsewhere = route(TRANSFER_GOAL, "http://another-bank.example:9000", proposer=nothing_recorded)
    assert elsewhere.action == "discover" and nothing_recorded.shown == []  # no capability for that target: the model is not even asked


def test_the_catalog_is_rebuilt_from_the_saved_artifacts_and_picks_the_highest_semantic_version(tmp_path):
    entries = {e.capability_id: e for e in build_catalog(T)}
    assert set(entries) == {"transfer-funds", "open-member-subaccount", "create-auto-loan-for", "fetch-account-balance", "update-member-address"}
    transfer = entries["transfer-funds"]
    assert transfer.risky and transfer.status == "draft" and "{amount}" in transfer.example_request
    assert {i.name for i in transfer.inputs} == set(TRANSFER_ARGS) and all(i.description for i in transfer.inputs)
    assert not entries["fetch-account-balance"].risky

    # 1.10.0 outranks 1.9.0 (a filename sort gets that wrong), and a hand-made pre-release name is not a candidate
    base = json.loads((tmp_path / "create-auto-loan-for@1.0.0.json").read_text())
    for version in ("1.9.0", "1.10.0", "2.0.0-draft"):
        (tmp_path / f"create-auto-loan-for@{version}.json").write_text(json.dumps({**base, "version": version}))
    assert {e.capability_id: e.version for e in build_catalog(T)}["create-auto-loan-for"] == "1.10.0"

    assert build_catalog("http://another-bank.example:9000") == []  # recorded against a different deployment

    # the same catalog, with no model in the path: listed by `capabilities`, run by name with typed args
    from typer.testing import CliRunner

    from cli import app

    runner = CliRunner()
    listing = runner.invoke(app, ["capabilities"])
    assert listing.exit_code == 0 and "transfer-funds@1.0.0" in listing.output and "--param amount=" in listing.output
    missing = runner.invoke(app, ["invoke", "--capability", "transfer-funds", "--param", "amount=5"])
    assert missing.exit_code == 2 and "missing --param from_member_id" in missing.output
    bad = runner.invoke(app, ["invoke", "--capability", "fetch-account-balance", "--param", "member_id=abc"])
    assert bad.exit_code == 2 and "must be a number" in bad.output
    assert runner.invoke(app, ["invoke", "--capability", "wire-money"]).exit_code == 2
    wrong_choice = runner.invoke(app, ["invoke", "--capability", "create-auto-loan-for", "--param", "member_id=20001", "--param", "loan_amount=500",
                                       "--param", "loan_purpose=Gold", "--param", "interest_rate=5"])
    assert wrong_choice.exit_code == 2 and "is not one of" in wrong_choice.output and "Home Improvement" in wrong_choice.output
    assert "(one of: Auto, Home Improvement" in listing.output


def _live_model_available() -> bool:
    try:
        tags = json.loads(urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1).read())
        return any(m.get("name", "").startswith("llama3.1:8b") for m in tags.get("models", []))
    except Exception:
        return False


@pytest.mark.skipif(not _live_model_available(), reason="the local router model (llama3.1:8b via Ollama) is not running")
def test_the_real_model_routes_the_goals_that_used_to_break_the_heuristic_router():
    """Goals the old router got wrong: wrong values, a phone number matched to a transfer, a missing
    amount. Temperature 0 and a fixed seed keep it repeatable."""
    replayed = route(TRANSFER_GOAL, T)
    assert (replayed.action, replayed.capability_id) == ("replay", "transfer-funds")
    assert {k: v.lower() for k, v in replayed.params.items()} == {k: v.lower() for k, v in TRANSFER_ARGS.items()}

    loan = route("Create a $12,500 auto loan for member 20001 at 6.25% interest.", T)
    assert (loan.action, loan.capability_id) == ("replay", "create-auto-loan-for")
    assert (loan.params["member_id"], loan.params["loan_amount"], loan.params["interest_rate"]) == ("20001", "12500", "6.25")  # not swapped

    its = route("Move $100 to 12345 savings from its checking.", T)  # one mention of the member, pointed back at by "its"
    assert (its.action, its.params["from_member_id"], its.params["to_member_id"]) == ("replay", "12345", "12345")
    assert (its.params["from_account_type"].lower(), its.params["to_account_type"].lower()) == ("checking", "savings")

    for goal in (
        "Update the phone number for member 10001 to 312-555-0199.",  # a different field than the address capability names
        "Transfer money from Member 10001 checking to Member 10002 checking.",  # no amount stated
        "Transfer $50 from member 10001's savings to member 20001's savings, then open a checking sub-account for member 20001 with $25.",
    ):
        assert route(goal, T).action == "discover", goal
