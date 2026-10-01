"""Human takeover: detecting it, the banners, and capturing live actions as replayable steps. No
LLM. Runs on inert pages, not the mock app, to avoid racing real navigation."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.request

import pytest
from playwright.sync_api import sync_playwright
from websockets.sync.client import connect as ws_connect

from agent.executor import Executor
from agent.loop import DiscoveryResult
from artifact.recorder import record_artifact
from artifact.schema import ActionType, ArtifactStatus, TargetApp
from guardrails.allowlist import Allowlist
from handoff.gesture import GestureController
from handoff.remote import devtools_link, free_loopback_port, launch_args
from handoff.session import HandoffSession, InterventionRequest, SessionState, _CliOperator
from replay.executor import replay_artifact
from tests.conftest import member


def _blank_page(pw):
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("about:blank")
    return browser, page


def _click_center(page, selector: str) -> None:
    """A real mouse click (down, up, click) at the element's position. element.click() fires only
    `click`, so it would miss listener bugs on mousedown."""
    box = page.locator(selector).bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


# -- detecting a human taking over ---------------------------------------------------------


def test_only_a_human_click_triggers_takeover_detection_and_it_survives_navigation():
    with sync_playwright() as pw:
        browser, page = _blank_page(pw)
        ctrl = GestureController(page)
        signals = []
        ctrl.arm_human_detection(lambda: signals.append(1))

        ctrl.mark_active(True)  # the automation's own action
        page.mouse.click(50, 50)
        ctrl.mark_active(False)
        page.wait_for_timeout(200)
        assert signals == []

        page.mouse.click(60, 60)  # nobody marked this one: a human
        page.wait_for_timeout(200)
        assert len(signals) == 1

        page.goto("data:text/html,<html><body>blank</body></html>")  # listener is re-armed after navigation
        page.mouse.click(20, 20)
        page.wait_for_timeout(200)
        assert len(signals) == 2
        browser.close()


def test_the_banners_never_count_as_a_human_taking_over_the_page():
    """Regression: a real run escalated 22 times in 25 seconds because clicking
    'Resume Automation' is itself a mousedown, and the listener did not exclude
    clicks on the automation's own banners."""
    with sync_playwright() as pw:
        browser, page = _blank_page(pw)
        ctrl = GestureController(page)
        signals = []
        ctrl.arm_human_detection(lambda: signals.append(1))

        page.evaluate(
            """
            (() => {
              const bar = document.createElement('div');
              bar.id = '__pw_pause_banner';
              bar.className = '__pw_ui';
              bar.innerHTML = '<input id="__pw_note"><button id="__pw_resume">Resume</button>';
              document.body.appendChild(bar);
            })();
            """
        )
        _click_center(page, "#__pw_resume")
        ctrl.show_confirm_banner("a risky action")
        _click_center(page, "#__pw_approve")
        page.wait_for_timeout(300)

        assert ctrl.poll_confirm_result() is True
        assert signals == []
        browser.close()


# -- the pause/resume and approve/deny banners -----------------------------------------------


def test_the_banners_return_the_humans_choice_and_the_cli_operator_resolves_through_them(monkeypatch):
    with sync_playwright() as pw:
        browser, page = _blank_page(pw)
        ctrl = GestureController(page)

        # pause banner: the resume click comes from the page's own JS (a poller), since Playwright's sync API is not thread-safe
        page.evaluate(
            """
            (function poll() {
              const btn = document.getElementById('__pw_resume');
              if (btn) { document.getElementById('__pw_note').value = 'test note'; btn.click(); }
              else { setTimeout(poll, 50); }
            })();
            """
        )
        assert ctrl.pause_and_wait_for_resume(poll_interval_s=0.1, timeout_s=10) == ("test note", True, False)
        assert page.locator("#__pw_pause_banner").count() == 0

        # confirm banner

        assert ctrl.poll_confirm_result() is None
        ctrl.show_confirm_banner('click on button "Confirm Transfer"')
        assert "Confirm Transfer" in page.locator("#__pw_confirm_banner").inner_text()
        page.click("#__pw_deny")
        page.wait_for_timeout(100)
        assert ctrl.poll_confirm_result() is False

        ctrl._confirm_result = None
        ctrl.show_confirm_banner("a risky action")
        page.click("#__pw_approve")
        page.wait_for_timeout(100)
        assert ctrl.poll_confirm_result() is True
        ctrl.hide_confirm_banner()
        assert page.locator("#__pw_confirm_banner").count() == 0

        # the operator races the terminal prompt against the banner; here the banner wins
        page.evaluate(
            "(function poll() { const b = document.getElementById('__pw_approve'); if (b) { b.click(); } else { setTimeout(poll, 50); } })();"
        )
        approved, note = _CliOperator(gesture=ctrl).confirm(
            InterventionRequest(reason="risky_action_confirm", goal_or_capability="test", message="click Confirm")
        )
        assert approved is True and "banner" in note

        # ...and the terminal can win instead, with no helper thread left reading the keyboard afterwards
        threads_before = threading.active_count()
        read_end, write_end = os.pipe()
        os.write(write_end, b"y\n")
        monkeypatch.setattr(sys, "stdin", os.fdopen(read_end))
        approved, note = _CliOperator(gesture=ctrl).confirm(
            InterventionRequest(reason="risky_action_confirm", goal_or_capability="test", message="click Confirm")
        )
        assert approved is True and "terminal" in note
        assert threading.active_count() == threads_before
        browser.close()


# -- a remote operator through the DevTools link --------------------------------------------


def _lan_address():
    """This machine's address on its network, or None when it has none (offline, or a hostname that does not
    resolve). Found by asking the OS which address it would use to reach another network; nothing is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            address = probe.getsockname()[0]
        return None if address.startswith("127.") or address == "0.0.0.0" else address
    except OSError:
        return None


def _cdp_click(ws, x: float, y: float) -> None:
    """A mouse click the way DevTools' page view sends it: Input.dispatchMouseEvent over the debugging port."""
    for n, kind in enumerate(("mousePressed", "mouseReleased")):
        ws.send(json.dumps({"id": n + 1, "method": "Input.dispatchMouseEvent",
                            "params": {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}}))
        ws.recv()


def test_the_devtools_link_is_loopback_only_points_at_the_live_tab_and_a_remote_operators_actions_are_captured():
    port = free_loopback_port()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=launch_args(port))
        other = browser.new_page()  # a second tab: the link must name THIS run's tab, not the first in the list
        other.goto("about:blank")
        page = browser.new_page()
        page.set_content('<input id="n" type="text" aria-label="Member"><button id="go">Go</button>')

        link = devtools_link(page, port)
        tab_id = page.context.new_cdp_session(page).send("Target.getTargetInfo")["targetInfo"]["targetId"]
        assert link == f"http://127.0.0.1:{port}/devtools/inspector.html?ws=127.0.0.1:{port}/devtools/page/{tab_id}"
        assert tab_id != other.context.new_cdp_session(other).send("Target.getTargetInfo")["targetInfo"]["targetId"]
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/devtools/inspector.html", timeout=3).status == 200  # served by the browser
        assert devtools_link(page, None) == ""  # debugging off: no link, never a dead one

        # loopback only: the port refuses a connection on this machine's own network address
        lan = _lan_address()
        if lan:
            with socket.socket() as probe:
                probe.settimeout(2)
                assert probe.connect_ex((lan, port)) != 0

        # a remote operator clicks the field, types, then clicks Go, all over the debugging port
        ctrl = GestureController(page)
        ctrl.start_capturing()
        field, button = page.locator("#n").bounding_box(), page.locator("#go").bounding_box()
        with ws_connect(f"ws://127.0.0.1:{port}/devtools/page/{tab_id}") as ws:
            _cdp_click(ws, field["x"] + 5, field["y"] + 5)
            ws.send(json.dumps({"id": 10, "method": "Input.insertText", "params": {"text": "10001"}}))
            ws.recv()
            _cdp_click(ws, button["x"] + 5, button["y"] + 5)
            page.wait_for_timeout(300)
        captured = ctrl.stop_capturing()

        assert [(a["action"], a["descriptor"]["name"], a.get("value")) for a in captured] == [("fill", "Member", "10001"), ("click", "Go", None)]
        browser.close()


# -- capturing a human's live actions as replayable steps -------------------------------------


class _ScriptedHuman:
    """A person for testing: performs real actions on the live page while capture is armed."""

    def __init__(self, gesture: GestureController, actions):
        self.gesture, self.actions = gesture, actions

    def confirm(self, request):
        return True, "auto-approved by the test operator"

    def take_control(self, request):
        self.gesture.start_capturing()
        for act in self.actions:
            act()
        return "Did it manually.", True, False, self.gesture.stop_capturing()


def test_a_stuck_escalation_hands_the_same_live_page_to_a_human_and_captures_their_actions_in_order(base_url, tmp_path):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(base_url + "/accounts/view", wait_until="load")
        gesture = GestureController(page)

        def human():
            page.get_by_role("textbox").fill("10001")
            page.get_by_role("button", name="Search").click()  # navigates to the member page
            page.get_by_role("link", name="Edit Contact Info").click()  # navigates again
            field = page.locator("input[type=text]").last
            field.fill("Suite 9")  # on a third document: still captured once the field loses focus
            field.press("Tab")

        handoff = HandoffSession(page=page, run_id="test_run", evidence_dir=tmp_path, operator=_ScriptedHuman(gesture, [human]))
        assert handoff.state is SessionState.LLM_CONTROL

        record = handoff.escalate(InterventionRequest(reason="stuck", goal_or_capability="look up member 10001", message="cannot search"))

        assert record.request.page_summary.startswith("Member Servicing Console")  # the prompt says what the page shows, not only its URL
        assert "/member/10001/edit" in page.url  # the actions landed on the automation's own page, not a new session
        assert handoff.state is SessionState.LLM_CONTROL  # control came back
        assert record.resume is True
        # every action is captured, in order, including the ones made after two navigations
        assert [(a["action"], a.get("value")) for a in record.captured_actions] == [
            ("fill", "10001"), ("click", None), ("click", None), ("fill", "Suite 9"),
        ]
        assert [record.captured_actions[i]["descriptor"]["name"] for i in (1, 2)] == ["Search", "Edit Contact Info"]
        browser.close()


def test_scripted_discovery_with_a_human_taught_step_records_an_artifact_that_replays_without_the_model(base_url, tmp_path):
    """LLM-style calls with one human-taught step in the middle, recorded and replayed with new
    values, no LLM. The human step keeps its provenance and position."""
    goal = "Transfer $30 from checking for member 10001 to checking for member 20001."
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        gesture = GestureController(page)
        operator = _ScriptedHuman(gesture, [lambda: page.get_by_role("link", name="Transfer Funds").click()])
        handoff = HandoffSession(page=page, run_id="test_full", evidence_dir=tmp_path, operator=operator)
        ex = Executor(page=page, allowlist=Allowlist.load(), handoff=handoff, goal=goal, evidence_dir=tmp_path, gesture=gesture)

        def ref(role, name=None, startswith=None):
            return next(
                e.ref for e in ex.current_snapshot.elements
                if e.role == role and (name is None or e.name == name) and (startswith is None or e.name.startswith(startswith))
            )

        ex.navigate(base_url + "/", rationale="start")
        ex.click(ref("link", "Transactions"), rationale="LLM: transactions")
        ex.click(ref("link", "Transaction Entry"), rationale="LLM: entry")
        ex.fill(ref("textbox"), "10001", rationale="LLM: source member")
        ex.click(ref("button", "Search"), rationale="LLM: search")

        before_human = len(ex.steps)
        record = handoff.escalate(InterventionRequest(reason="stuck", goal_or_capability=goal, message="cannot find the transfer link"))
        for entry in record.captured_actions:
            ex.record_human_action(action=entry["action"], descriptor=entry["descriptor"], value=entry.get("value"), url=page.url)
        ex.refresh_snapshot()
        assert ex.steps[before_human].source == "human_intervention"
        assert "/transfer" in page.url

        ex.select(ref("combobox", "From Account:"), "checking", rationale="LLM: source account")
        ex.fill(ref("textbox", "To Member ID:"), "20001", rationale="LLM: destination member")
        ex.select(ref("combobox", "To Account:"), "checking", rationale="LLM: destination account")
        ex.fill(ref("textbox", "Amount ($):"), "30", rationale="LLM: amount")
        ex.click(ref("button", "Review Transfer"), rationale="LLM: review")
        ex.click(ref("button", "Confirm Transfer"), rationale="LLM: confirm")  # gated; the operator approves
        confirmation = ex.read_text(ref("text", startswith="TXF-"), rationale="LLM: read the confirmation number")["text"]
        browser.close()

    result = DiscoveryResult(
        run_id="test_full", success=True, outcome="done", summary="done", outputs={"confirmation_number": confirmation},
        steps=ex.steps, goal=goal, target_url=base_url + "/", started_at="t0", ended_at="t1", evidence_dir=str(tmp_path),
    )
    declared = {"member_id": "10001", "from_account_type": "checking", "to_member_id": "20001", "to_account_type": "checking", "amount": "30"}
    artifact = record_artifact(
        result, "human-taught-transfer", "test capability", declared,
        TargetApp(app_id="cu-servicing-console", base_url=base_url, entry_path="/"),
    )

    human = [s for s in artifact.steps if s.source == "human_intervention"]
    assert len(human) == 1 and human[0].action == ActionType.CLICK
    assert next(s.index for s in artifact.steps if "search" in s.description.lower()) < human[0].index
    assert human[0].index < next(s.index for s in artifact.steps if "review" in s.description.lower())
    assert {i.name for i in artifact.inputs} == set(declared)

    replayed = replay_artifact(
        artifact.model_copy(update={"status": ArtifactStatus.APPROVED}),
        {"member_id": "12345", "from_account_type": "savings", "to_member_id": "20001", "to_account_type": "checking", "amount": "40"},
        headless=True,
    )
    assert replayed.kind == "success", replayed.failure  # a failure says which step and what the page showed
    assert replayed.outputs["confirmation_number"] != confirmation
    assert member("12345")["savings_balance"] == pytest.approx(1562.00 - 40)
