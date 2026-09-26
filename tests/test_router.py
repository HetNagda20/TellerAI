"""Router behavior: goal + target -> replay an existing capability, or fall
back to discovery. No Playwright, no LLM, no UI reasoning anywhere in this
module or the one it tests -- verified below by a source-scan test mirroring
replay/executor.py's own "no LLM anywhere in this module" check.

Requires the mock app's saved artifacts on disk (artifacts/transfer-funds@*
and artifacts/open-member-subaccount@*) -- no live mock app or browser needed,
since the router only reads already-saved artifact JSON.
"""

from __future__ import annotations

import router.router as router_mod
from router.router import route


def test_router_matches_the_documented_example_goal_and_extracts_all_typed_inputs():
    """The exact worked example from the approved corrections: a goal-only
    Task AI call must resolve to transfer-funds with every declared input
    correctly and distinctly extracted -- including from_account/to_account,
    which share the identical word "checking" in the goal text.
    """
    goal = "Transfer $100 from Member 10001 checking to Member 10002 checking."
    decision = route(goal, "http://127.0.0.1:8000")

    assert decision.action == "replay"
    assert decision.capability_id == "transfer-funds"
    assert decision.artifact_path is not None
    assert decision.params == {
        "member_id": "10001",
        "from_account": "checking",
        "to_member_id": "10002",
        "to_account": "checking",
        "amount": "100",
    }


def test_router_extracts_correctly_from_a_goal_with_no_from_to_structure():
    """Regression: a goal with no "from"/"to" clause at all (unlike the
    transfer example) used to fall back to scanning the ENTIRE sentence for
    account_type's value and grab the trailing word "screen" instead of
    "savings" -- a confident, WRONG extraction, not a safe fail-closed one.
    Caught by actually running `cli.py task` against this exact goal, which
    produced a real hard_failure (Playwright couldn't select "screen" as an
    account type). The account-qualifier heuristic in _find_in_segment fixes
    this generically, not by hardcoding "savings"/"screen" anywhere.
    """
    goal = "Open a new savings sub-account for member 10001 with an initial deposit of $100 and reach the confirmation screen."
    decision = route(goal, "http://127.0.0.1:8000")

    assert decision.action == "replay"
    assert decision.capability_id == "open-member-subaccount"
    assert decision.params == {
        "member_id": "10001",
        "account_type": "savings",
        "initial_deposit": "100",
    }


def test_router_falls_back_to_discovery_when_no_capability_matches():
    decision = route("Please water the office plants every Tuesday.", "http://127.0.0.1:8000")
    assert decision.action == "discover"
    assert decision.capability_id is None
    assert decision.params == {}


def test_router_fails_closed_to_discovery_when_a_required_input_cannot_be_extracted():
    """Matches transfer-funds by topic, but never states an amount anywhere --
    the router must never guess a value for a missing required input; it must
    fall back to discovery instead of confidently (and wrongly) replaying.
    """
    goal = "Transfer money from Member 10001 checking to Member 10002 checking."
    decision = route(goal, "http://127.0.0.1:8000")
    assert decision.action == "discover"
    assert "amount" in decision.reason


def test_router_never_imports_playwright_llm_or_discovery_machinery():
    source = open(router_mod.__file__).read()
    forbidden = (
        "playwright",
        "agent.llm",
        "agent.loop",
        "agent.executor",
        "agent.perception",
        "handoff.gesture",
        "make_client",
        "next_action",
    )
    for token in forbidden:
        assert token not in source, f"router/router.py must never reference {token!r}"
