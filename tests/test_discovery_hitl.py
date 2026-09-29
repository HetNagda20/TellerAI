"""Discovery-time human-in-the-loop: verifies a human's action during an
escalation is captured as a real, replayable Step (source="human_intervention"),
not a free-text note, see handoff/session.py, handoff/gesture.py, and
agent/executor.py's record_human_action.

No LLM is used here. HandoffSession's operator is a small scripted fake
standing in for a person, driving the SAME real Playwright page the
discovery loop would use, that's what these tests actually exercise: the
capture mechanism, the LLM_CONTROL/HUMAN_CONTROL state machine, and the
recorder's handling of a step sequence with mixed sources.
"""

from __future__ import annotations

import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from agent.executor import Executor
from agent.loop import DiscoveryResult
from artifact.recorder import record_artifact
from artifact.schema import ActionType, TargetApp
from guardrails.allowlist import Allowlist
from handoff.gesture import GestureController
from handoff.session import HandoffSession, InterventionRequest, SessionState
from replay.executor import replay_artifact

BASE = "http://127.0.0.1:8000"


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")


class _ScriptedHumanOperator:
    """A person, for testing purposes: when the agent escalates as stuck, this
    performs real action(s) directly on the live page (simulating physical
    input, no LLM involved) while capture is armed, then hands control back.
    """

    def __init__(self, gesture: GestureController, actions, note="Did it manually.", resume=True):
        self.gesture = gesture
        self._actions = actions  # list of zero-arg callables, each a real page action
        self.note = note
        self.resume = resume

    def confirm(self, request: InterventionRequest):
        return True, "auto-approved by test operator"

    def take_control(self, request: InterventionRequest):
        self.gesture.start_capturing()
        for act in self._actions:
            act()
        captured = self.gesture.stop_capturing()
        return self.note, self.resume, False, captured


def test_stuck_escalation_uses_same_session_and_captures_structured_action(tmp_path):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE + "/", wait_until="load")
        gesture = GestureController(page)

        clicked = {"done": False}

        def human_clicks_accounts():
            page.get_by_role("link", name="Accounts").click()
            clicked["done"] = True

        operator = _ScriptedHumanOperator(gesture, [human_clicks_accounts])
        handoff = HandoffSession(page=page, run_id="test_run", evidence_dir=tmp_path, operator=operator)

        assert handoff.state is SessionState.LLM_CONTROL

        record = handoff.escalate(
            InterventionRequest(
                reason="stuck",
                goal_or_capability="look up member 10001",
                message="Agent reported being stuck: cannot find where to search for a member.",
                context_snapshot="Current page: http://127.0.0.1:8000/\nVisible elements:\n  [f0e1] link \"Accounts\"",
            )
        )

        # B: same session, not a new browser, the click really happened on `page`.
        assert clicked["done"]
        assert "/accounts" in page.url

        # state machine returns to LLM_CONTROL after the human hands back control
        assert handoff.state is SessionState.LLM_CONTROL

        # C/D: a structured, replayable action was captured, not just a note
        assert record.resume is True
        assert len(record.captured_actions) == 1
        entry = record.captured_actions[0]
        assert entry["action"] == "click"
        assert entry["descriptor"]["role"] == "link"
        assert entry["descriptor"]["name"] == "Accounts"

        browser.close()


def test_multiple_human_actions_captured_in_order(tmp_path):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE + "/accounts", wait_until="load")
        gesture = GestureController(page)

        def fill_member_id():
            page.get_by_role("textbox").fill("10001")

        def click_search():
            page.get_by_role("button", name="Search").click()

        operator = _ScriptedHumanOperator(gesture, [fill_member_id, click_search])
        handoff = HandoffSession(page=page, run_id="test_run", evidence_dir=tmp_path, operator=operator)

        record = handoff.escalate(
            InterventionRequest(reason="stuck", goal_or_capability="test", message="stuck for test")
        )

        assert len(record.captured_actions) == 2
        assert record.captured_actions[0]["action"] == "fill"
        assert record.captured_actions[0]["value"] == "10001"
        assert record.captured_actions[1]["action"] == "click"
        assert record.captured_actions[1]["descriptor"]["name"] == "Search"
        assert "/member/10001" in page.url

        browser.close()


def test_full_discovery_with_human_taught_step_produces_replayable_artifact(tmp_path):
    """The end-to-end version: LLM steps (real Executor calls) with one human-
    taught step interleaved, recorded into an artifact, replayed with no LLM.
    Mirrors the brief's own example ordering (LLM, LLM, LLM, HUMAN, LLM, ...).
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        gesture = GestureController(page)

        def human_clicks_open_subaccount():
            page.get_by_role("link", name="Open a New Sub-Account for this Member").click()

        operator = _ScriptedHumanOperator(gesture, [human_clicks_open_subaccount])
        evidence_dir = tmp_path / "evidence"
        allowlist = Allowlist.load()
        handoff = HandoffSession(page=page, run_id="test_full", evidence_dir=evidence_dir, operator=operator)
        executor = Executor(
            page=page, allowlist=allowlist, handoff=handoff,
            goal="Open a sub account for member 10001 and reach the confirmation screen.",
            evidence_dir=evidence_dir, gesture=gesture,
        )

        # step 0-3: LLM discovers its way to the member's page
        executor.navigate(BASE + "/", rationale="start")
        snap = executor.current_snapshot
        accounts_ref = next(e.ref for e in snap.elements if e.role == "link" and e.name == "Accounts")
        executor.click(accounts_ref, rationale="LLM: go to Accounts")

        snap = executor.current_snapshot
        textbox_ref = next(e.ref for e in snap.elements if e.role == "textbox")
        executor.fill(textbox_ref, "10001", rationale="LLM: enter member id")

        snap = executor.current_snapshot
        search_ref = next(e.ref for e in snap.elements if e.role == "button" and e.name == "Search")
        executor.click(search_ref, rationale="LLM: search")

        steps_before_human = len(executor.steps)

        # step 4: the LLM is stuck (simulated) and a human clicks the link it couldn't find
        record = handoff.escalate(
            InterventionRequest(
                reason="stuck",
                goal_or_capability=executor.goal,
                message="Agent reported being stuck: could not identify the sub-account link.",
                context_snapshot=executor.current_snapshot.to_prompt_text(),
            )
        )
        assert record.resume
        for entry in record.captured_actions:
            executor.record_human_action(
                action=entry["action"], descriptor=entry["descriptor"], value=entry.get("value"), url=page.url
            )
        executor.refresh_snapshot()

        assert len(executor.steps) == steps_before_human + 1
        assert executor.steps[steps_before_human].source == "human_intervention"
        assert "/new-subaccount" in page.url

        # steps 5-8: LLM resumes and finishes the flow
        snap = executor.current_snapshot
        deposit_ref = next(e.ref for e in snap.elements if e.role == "textbox")
        executor.fill(deposit_ref, "30", rationale="LLM: enter initial deposit")

        snap = executor.current_snapshot
        review_ref = next(e.ref for e in snap.elements if e.role == "button" and e.name == "Review")
        executor.click(review_ref, rationale="LLM: review")

        snap = executor.current_snapshot
        confirm_ref = next(e.ref for e in snap.elements if e.role == "button" and "Confirm" in e.name)
        executor.click(confirm_ref, rationale="LLM: confirm and open the account")  # auto-approved by operator.confirm()

        snap = executor.current_snapshot
        conf_num_ref = next(e.ref for e in snap.elements if e.role == "text" and e.name.startswith("SA-"))
        read = executor.read_text(conf_num_ref, rationale="LLM: read confirmation number")
        confirmation_number = read["text"]

        browser.close()

    # requirement F: the full sequence, human step included, becomes one artifact
    result = DiscoveryResult(
        run_id="test_full", success=True, outcome="done", summary="done",
        outputs={"confirmation_number": confirmation_number},
        steps=executor.steps,
        goal=executor.goal, target_url=BASE + "/",
        started_at="t0", ended_at="t1", evidence_dir=str(evidence_dir),
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/")
    artifact = record_artifact(result, "human-taught-subaccount", "test capability", {"member_id": "10001"}, target_app)

    human_steps = [s for s in artifact.steps if s.source == "human_intervention"]
    llm_steps = [s for s in artifact.steps if s.source == "llm"]
    assert len(human_steps) == 1
    assert human_steps[0].action == ActionType.CLICK
    assert len(llm_steps) == len(artifact.steps) - 1

    # ordering: the human step sits after "search" and before "review"
    search_idx = next(s.index for s in artifact.steps if "search" in s.description.lower())
    review_idx = next(s.index for s in artifact.steps if "review" in s.description.lower())
    assert search_idx < human_steps[0].index < review_idx

    # requirement: the resulting artifact replays deterministically, no LLM involved
    replay_result = replay_artifact(artifact, {"member_id": "10001"}, headless=True)
    assert replay_result.kind == "success"
