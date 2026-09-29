"""Router behavior: goal + target -> replay an existing capability, or fall
back to discovery. No Playwright, no LLM, no UI reasoning anywhere in this
module or the one it tests -- verified below by a source-scan test mirroring
replay/executor.py's own "no LLM anywhere in this module" check.

Requires the saved artifacts on disk under artifacts/ -- no live mock app or
browser needed, since the router only reads already-saved artifact JSON. This
was already true before the false-positive-matching fix below and remains a
known test-environment dependency: results reflect whatever capabilities
happen to exist in artifacts/ at test-run time, banking ones and the
unrelated flight-booking/practice-login ones alike.

False-positive-match regression coverage (required scenarios A-D):
  A. test_router_does_not_replay_an_unrelated_workflow_that_shares_incidental_words
  B. test_router_matches_the_documented_example_goal_and_extracts_all_typed_inputs
     (existing transfer-funds goal still replays)
  C. test_router_falls_back_to_discovery_when_no_capability_matches
     (unrelated goal against the banking target still discovers)
  D. all five pre-existing tests below continue passing unchanged
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


def test_router_does_not_replay_an_unrelated_workflow_that_shares_incidental_words():
    """Regression: a goal for a totally different workflow (updating contact
    info) used to be confidently routed to `transfer-funds` replay, purely
    because both goals share generic/boilerplate wording ("member", "the",
    "to", "and", "reach the confirmation screen/state") -- none of
    transfer-funds' actual distinguishing vocabulary ("transfer", "funds",
    "checking", "amount") appears anywhere in this goal. Sharing a parameter
    name like member_id must not be read as workflow compatibility either.
    Fixed by weighting vocabulary overlap by rarity across the candidate
    pool (_weighted_overlap_score) plus a separate, explicit
    _workflow_compatible gate requiring the match to share at least one
    token that's actually distinctive to that capability -- not a keyword
    exclusion list, and nothing banking-specific.
    """
    goal = "Update the phone number for member 10001 to 312-555-0199 and reach the confirmation state."
    decision = route(goal, "http://127.0.0.1:8000")

    assert decision.action == "discover"
    assert decision.capability_id is None
    assert decision.params == {}


def test_router_does_not_treat_a_capability_recorded_against_an_unrelated_target_as_compatible():
    """Target/application compatibility: this repo's artifacts/ also holds
    capabilities discovered against a completely different application
    (a flight-booking practice site, https://www.qapractice.com/...) from an
    earlier, unrelated exploration. A goal that would otherwise resemble one
    of those workflows must never be replayed against the banking target --
    and, symmetrically, the banking-flavored goal here must not accidentally
    match a flight-booking capability just because target filtering removed
    it from contention. Uses TargetApp.base_url (artifact/schema.py) as the
    existing target-identity representation, not a new one: same host as the
    requested target_url is compatible, matching the schema's own intent
    that reuse is scoped to a deployment, while app_id stays free to be
    reused across tenants (this router has no app_id input to key off of).
    """
    decision = route(
        "Log into the practice account using the demo credentials and reach the successful login state.",
        "http://127.0.0.1:8000",
    )
    assert decision.action == "discover"
    assert decision.capability_id is None


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
