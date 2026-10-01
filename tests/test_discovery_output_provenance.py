"""Checks the completion boundary (agent/loop.py::_unproven_outputs). A claimed output is proven
only if read_text read it, or it echoes a value the run typed or selected."""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from agent.executor import Executor, StepLog
from agent.loop import _StuckWatch, _check_declared_inputs, _success_text_error, _unproven_outputs
from guardrails.allowlist import Allowlist
from handoff.session import HandoffSession
from artifact.schema import LocatorCandidate, LocatorStrategy, Target
from mock_app.data import MEMBERS


def _fill(index: int, value: str, ok: bool = True) -> StepLog:
    return StepLog(index=index, action="fill", rationale="fill", element_name="a field", value=value, ok=ok)


def _select(index: int, value: str) -> StepLog:
    return StepLog(index=index, action="select", rationale="select", element_name="a dropdown", value=value, ok=True)


def _read_text(index: int, element_name: str) -> StepLog:
    return StepLog(index=index, action="read_text", rationale="read", element_name=element_name, ok=True)


def test_outputs_and_declared_inputs_are_each_proven_by_what_the_run_actually_did():
    assert _unproven_outputs([], {}) == []  # nothing claimed, nothing to prove
    assert _unproven_outputs([_fill(0, "10001")], {"member_id": "10001", "checking_balance": "$662.44"}) == ["checking_balance"]
    assert _unproven_outputs([_select(0, "savings")], {"account_type": "savings"}) == []  # echo of a select
    assert _unproven_outputs([_read_text(0, "$662.44")], {"checking_balance": "$662.44"}) == []  # a real read

    # each output is judged independently: one echo, one read, one with nothing behind it
    mixed = [_fill(0, "10001"), _read_text(1, "$662.44")]
    assert _unproven_outputs(mixed, {"member_id": "10001", "checking_balance": "$662.44", "savings_balance": "$1204.09"}) == ["savings_balance"]

    # a value typed by a fill that FAILED is not an echo of anything
    assert _unproven_outputs([_fill(0, "10001", ok=False)], {"member_id": "10001"}) == ["member_id"]

    # ---- inputs: the twin rule. An input is proven by a fill/select that applied it AND by the goal saying it.
    def applied(index, value, css, name="a field", redact=False, ok=True):
        t = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css})])
        log = StepLog(index=index, action="fill", rationale="fill", element_name=name, value="[REDACTED]" if redact else value, target=t, ok=ok)
        return log, (index, value)

    def check(goal, declared, *steps):
        logs, raw = zip(*steps)
        return _check_declared_inputs(list(logs), dict(raw), goal, {n: (v, "") for n, v in declared.items()})

    goal = "Update address of member 10001 to 555 W Washington blvd chicago"
    member, address = applied(0, "10001", "#a", "Member"), applied(1, "555 W Washington blvd chicago", "#b", "Mailing Address:")
    both = {"member_id": "10001", "address": "555 W Washington blvd chicago"}

    assert check(goal, both, member, address) == (None, {0: "member_id", 1: "address"})
    assert "did not declare it" in check(goal, {"member_id": "10001"}, member, address)[0]  # a goal value about to be hardcoded
    assert "does not appear in the goal" in check(goal, {"member_id": "99999"}, member)[0]
    assert "never applied" in check(goal, {"member_id": "10001", "address": both["address"]}, member)[0]
    assert "snake_case" in check(goal, {"Member ID": "10001"}, member)[0]

    # a value the agent chose itself (not in the goal) is not a goal input and needs no declaration
    assert check(goal, {"member_id": "10001"}, member, applied(1, "Cash Deposit", "#c"))[0] is None

    # one control's value supports one input: two inputs sharing a value need two controls
    same = "Transfer 5 from checking to checking"
    src, dst = applied(0, "checking", "#from"), applied(1, "checking", "#to")
    assert check(same, {"from_account_type": "checking", "to_account_type": "checking"}, src, dst) == (None, {0: "from_account_type", 1: "to_account_type"})
    assert "never applied" in check(same, {"from_account_type": "checking", "to_account_type": "checking"}, src)[0]

    # a control refilled with the same value is one binding, and a value redacted on its StepLog still proves out
    refill = check(goal, {"member_id": "10001"}, member, applied(1, "10001", "#a", "Member"))
    assert refill == (None, {0: "member_id", 1: "member_id"})
    assert check("Verify SSN 123-45-6789", {"ssn": "123-45-6789"}, applied(0, "123-45-6789", "#s", "SSN", redact=True)) == (None, {0: "ssn"})


def test_a_real_discovery_style_run_proves_a_main_frame_read_and_an_iframe_read(base_url, tmp_path):
    """Look up 10001, then claim member_id, checking_balance and savings_balance. The extracted two
    are unproven until read_text reads them, in the main frame and the iframe."""
    checking, savings = f"${MEMBERS['10001']['checking_balance']:.2f}", f"${MEMBERS['10001']['savings_balance']:.2f}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        handoff = HandoffSession(page=page, run_id="test_provenance", evidence_dir=tmp_path)
        ex = Executor(page=page, allowlist=Allowlist.load(), handoff=handoff, goal="Fetch the account balance for member 10001.", evidence_dir=tmp_path)

        ex.navigate(base_url + "/accounts/view", rationale="start")
        ex.fill(next(e.ref for e in ex.current_snapshot.elements if e.role == "textbox"), "10001", rationale="enter member id")
        ex.click(next(e.ref for e in ex.current_snapshot.elements if e.role == "button" and e.name == "Search"), rationale="search")

        claimed = {"member_id": "10001", "checking_balance": checking, "savings_balance": savings}
        assert set(_unproven_outputs(ex.steps, claimed)) == {"checking_balance", "savings_balance"}

        snap = ex.current_snapshot
        ex.read_text(next(e.ref for e in snap.elements if e.role == "text" and e.name == checking and e.frame_index == 0), rationale="read checking")
        ex.read_text(next(e.ref for e in snap.elements if e.role == "text" and e.name == savings and e.frame_index != 0), rationale="read savings from the iframe")

        assert _unproven_outputs(ex.steps, claimed) == []
        assert _unproven_outputs(ex.steps, {**claimed, "bogus_field": "not on the page anywhere"}) == ["bogus_field"]
        browser.close()


    # ---- success_text: the phrase replay will wait for must be real, short and the same for every request
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<h1>Transfer Submitted</h1><p>Reference TRF-100234 for member 10001</p>")
        check_text = lambda phrase: _success_text_error(page, phrase, {"ref": "TRF-100234"}, ["10001"])  # noqa: E731
        assert check_text("Transfer Submitted") is None
        assert "not visible" in check_text("Transfer Failed")
        assert "empty" in check_text("  ")
        assert "generated output" in check_text("Reference TRF-100234")
        assert "input values" in check_text("for member 10001")
        assert "too long" in check_text("x" * 81)
        browser.close()


    # ---- the stuck nudge: from what the screen shows, not from its URL
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        screens = {name: f"<h1>{name}</h1><p>screen {name}</p>" for name in "ABCDE"}

        def show(name):
            page.set_content(screens[name])  # the URL never changes: a single-URL app

        # filling a form's fields is many actions on one screen, and is not being stuck
        watch = _StuckWatch()
        show("A")
        assert [watch.observe(page, "fill", {"ref": f"f0e{i}", "value": str(i)}) for i in range(6)] == [None] * 6

        # walking through different screens that share one URL is not being stuck either
        watch = _StuckWatch()
        warned = []
        for name in "ABCDE":
            show(name)
            warned.append(watch.observe(page, "click", {"ref": "f0e1"}))
        assert warned == [None] * 5

        # wandering back to the same screen a third time is
        watch = _StuckWatch()
        warned = []
        for i, name in enumerate("ABABAB"):
            show(name)
            warned.append(watch.observe(page, "click", {"ref": f"go-{i}"}))  # a different click each time
        assert warned[:4] == [None] * 4 and "come back to this same screen" in warned[4]

        # so is trying the same action a third time on a screen that does not change
        watch = _StuckWatch()
        show("A")
        results = [watch.observe(page, "click", {"ref": "f0e9"}) for _ in range(3)]
        assert results[:2] == [None, None] and "same action 3 times" in results[2]
        watch.reset()  # a human restart clears it
        assert watch.observe(page, "click", {"ref": "f0e9"}) is None
        browser.close()
