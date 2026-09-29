"""Human-in-the-loop escalation and control transfer.

Design: the automation runs in a headed browser, on purpose. That means
handing control to a human does not require a co-browsing/screen-sharing
console (explicitly out of scope); the same OS-level browser window
Playwright has been driving is already visible and clickable. What this
module makes real is the control-transfer model around that window:

  LLM_CONTROL, escalate() -> HUMAN_CONTROL, human resumes -> LLM_CONTROL

HandoffSession.state is the single source of truth for who is allowed to
act on the page. agent/executor.py's _dispatch refuses to issue an
LLM-driven action unless is_agent_allowed() is true, so this is an enforced
invariant, not a convention.

Two categories of escalation, kept structurally distinct:

- Discovery-time ("stuck", "human_gesture", "human_requested"): a human can
  take over, teach the agent something by acting directly on the page, and
  what they did gets captured as a normal, replayable Step
  (source="human_intervention") before control returns to the LLM.
- Everything else ("risky_action_confirm", "replay_hard_failure",
  "commit_verification_inconclusive"): a yes/no gate or an
  operational-recovery notification. Neither teaches anything; replay must
  never come out of an escalation with a modified artifact.

The operator surface here is intentionally a bare CLI prompt plus an
on-page banner (handoff/gesture.py), not a built console. What's real: the
pause, the context handed over, the same live page, the resume signal, the
captured actions, and a logged record of both.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from playwright.sync_api import Page

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    LLM_CONTROL = "llm_control"
    HUMAN_CONTROL = "human_control"


EscalationReason = Literal[
    "stuck", "risky_action_confirm", "replay_hard_failure", "human_requested", "human_gesture",
    "commit_verification_inconclusive",
]

# Escalation reasons where a human taking over is teaching the discovery run
# something; their actions get captured as replayable steps. The other
# reasons are a gate or an operational notification, never a teaching moment.
DISCOVERY_HITL_REASONS = frozenset({"stuck", "human_gesture", "human_requested"})


@dataclass
class InterventionRequest:
    reason: EscalationReason
    goal_or_capability: str
    message: str
    step_index: Optional[int] = None
    current_url: str = ""
    screenshot_path: Optional[str] = None
    context_snapshot: Optional[str] = None
    """The discovery agent's own view of the page at the moment it escalated,
    the same text the model itself was reasoning over. Populated for 'stuck'
    escalations so the intervention request carries why it concluded it was
    stuck, not just that it did."""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class HandoffRecord:
    request: InterventionRequest
    outcome: Literal["approved", "denied", "human_completed", "human_terminated", "human_restarted"]
    human_note: str
    resumed_at: str
    resume: bool = True
    """Whether the caller should let the agent try again. False means the
    human judged this unrecoverable and the run should end here rather than
    loop back. Always True for 'risky_action_confirm' (only approve/deny)."""
    restart: bool = False
    """Only meaningful for a take_control escalation: the human wants the
    whole run started over (same session, same run_id) rather than resumed
    from here or ended. Takes priority over `resume` when true. Never set
    for 'risky_action_confirm' (a single-action approve/deny has nothing to
    restart) or for any replay-side reason (replay has no loop to restart)."""
    captured_actions: list[dict] = field(default_factory=list)
    """Structured {action, descriptor, value} entries captured while the
    human had control, in order. Empty for risky_action_confirm/
    replay_hard_failure, see DISCOVERY_HITL_REASONS."""


def _banner_context(request: InterventionRequest) -> str:
    """Same reason/message already printed to the terminal, formatted for
    the on-page banner: a real end user watching the browser never sees a
    terminal, so this must be visible on the page itself. Leads with the goal
    (or capability) this escalation is in service of, so a human approving or
    taking over can actually judge whether the action matches what was asked,
    not just read the action in isolation."""
    return f"Goal: {request.goal_or_capability}\n\n[{request.reason}] {request.message}"


class HandoffSession:
    """One per live Playwright page. Owns the state machine and the evidence trail."""

    def __init__(self, page: Page, run_id: str, evidence_dir: Path, operator=None):
        self.page = page
        self.run_id = run_id
        self.evidence_dir = evidence_dir
        self.state = SessionState.LLM_CONTROL
        self.records: list[HandoffRecord] = []
        # operator is injectable so tests / non-interactive runs can supply a
        # scripted response instead of blocking on real stdin.
        self._operator = operator or _CliOperator()

    def is_agent_allowed(self) -> bool:
        return self.state is SessionState.LLM_CONTROL

    def escalate_via_gesture(self, request: InterventionRequest, controller) -> HandoffRecord:
        """Same state machine and evidence trail as escalate(), but for a
        human who has already taken control by clicking/typing in the live
        page directly. Waits on the page's own banner instead of a CLI
        prompt, with the same Resume/Restart/End Run choices escalate()'s
        take_control path offers, since by the time this fires the human is
        already acting in the browser and may decide this run needs to
        restart or stop entirely, not just resume."""
        self._save_context(request)
        logger.warning("escalation raised reason=%s run_id=%s", request.reason, self.run_id)
        self.state = SessionState.HUMAN_CONTROL
        controller.start_capturing()
        note, resume, restart = controller.pause_and_wait_for_resume(context=_banner_context(request), show_cancel=True)
        captured = controller.stop_capturing()
        if not resume and not restart:
            # Ending the run entirely: whatever was captured en route isn't being
            # taught to anything, same rule escalate()'s take_control path follows.
            captured = []
        if restart:
            outcome = "human_restarted"
        else:
            outcome = "human_completed" if resume else "human_terminated"
        record = HandoffRecord(
            request=request,
            outcome=outcome,
            human_note=note,
            resumed_at=datetime.now(timezone.utc).isoformat(),
            resume=resume,
            restart=restart,
            captured_actions=captured,
        )
        self.records.append(record)
        self.state = SessionState.LLM_CONTROL
        self._append_log(record)
        logger.info("escalation resolved reason=%s run_id=%s outcome=%s", request.reason, self.run_id, record.outcome)
        return record

    def escalate(self, request: InterventionRequest) -> HandoffRecord:
        self._save_context(request)
        logger.warning("escalation raised reason=%s run_id=%s", request.reason, self.run_id)

        if request.reason == "risky_action_confirm":
            approved, note = self._operator.confirm(request)
            record = HandoffRecord(
                request=request,
                outcome="approved" if approved else "denied",
                human_note=note,
                resumed_at=datetime.now(timezone.utc).isoformat(),
            )
        else:
            self.state = SessionState.HUMAN_CONTROL
            note, resume, restart, captured = self._operator.take_control(request)
            if request.reason not in DISCOVERY_HITL_REASONS:
                # replay_hard_failure and friends: operational recovery only, never
                # teaching. Drop anything captured rather than trust every call site
                # to remember not to use it.
                captured = []
            if restart:
                outcome = "human_restarted"
            else:
                outcome = "human_completed" if resume else "human_terminated"
            record = HandoffRecord(
                request=request,
                outcome=outcome,
                human_note=note,
                resumed_at=datetime.now(timezone.utc).isoformat(),
                resume=resume,
                restart=restart,
                captured_actions=captured,
            )

        self.records.append(record)
        self.state = SessionState.LLM_CONTROL
        self._append_log(record)
        logger.info("escalation resolved reason=%s run_id=%s outcome=%s", request.reason, self.run_id, record.outcome)
        return record

    def _save_context(self, request: InterventionRequest) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        shot = self.evidence_dir / f"escalation_{len(self.records)}.png"
        try:
            self.page.screenshot(path=str(shot))
            request.screenshot_path = str(shot)
        except Exception:
            pass
        request.current_url = self.page.url

    def _append_log(self, record: HandoffRecord) -> None:
        log_path = self.evidence_dir / "handoff_log.jsonl"
        with open(log_path, "a") as f:
            f.write(
                json.dumps(
                    {
                        "request": vars(record.request),
                        "outcome": record.outcome,
                        "human_note": record.human_note,
                        "resumed_at": record.resumed_at,
                        "resume": record.resume,
                        "captured_actions": record.captured_actions,
                    }
                )
                + "\n"
            )


class _CliOperator:
    """Bare/mock operator surface: a blocking terminal prompt, with an
    on-page banner racing it whenever a GestureController is available.
    Intentionally minimal; what it exercises for real is the control-transfer
    model, not a polished UI.
    """

    def __init__(self, gesture=None, capture: bool = True):
        self.gesture = gesture
        # replay passes capture=False: this operator's on-page banner is used there
        # purely to notify a real end user, never to arm the capture listener. Keeps
        # handoff/gesture.py's capture mechanism reachable only from discovery,
        # regardless of what operator replay constructs.
        self.capture = capture

    def confirm(self, request: InterventionRequest) -> tuple[bool, str]:
        print("\n=== INTERVENTION REQUESTED (confirmation) ===")
        print(f"Goal:    {request.goal_or_capability}")
        print(f"Reason:  {request.reason}")
        print(f"Context: {request.message}")
        print(f"URL:     {request.current_url}")

        if self.gesture is None:
            ans = input("Approve this action? [y/N]: ").strip().lower()
            return ans == "y", f"operator answered {ans!r}"

        # Race two channels: a background thread blocked on stdin (touches no
        # Playwright API, a second thread calling into Playwright's sync API
        # breaks it), and the page's own Approve/Deny banner, polled from this
        # (the only) thread allowed to drive the browser. Whichever resolves first
        # wins; the thread is a daemon so an abandoned prompt cannot block exit.
        cli_result: dict = {}

        def _read_stdin():
            try:
                ans = input("Approve this action? [y/N] (or use the on-page banner): ").strip().lower()
                cli_result["approved"] = ans == "y"
                cli_result["note"] = f"operator answered {ans!r} (terminal)"
            except Exception:
                pass  # stdin unavailable/closed, or the banner already won and this got abandoned

        thread = threading.Thread(target=_read_stdin, daemon=True)
        thread.start()

        self.gesture.show_confirm_banner(_banner_context(request))
        while "approved" not in cli_result and self.gesture.poll_confirm_result() is None:
            self.gesture.pump(0.2)

        self.gesture.hide_confirm_banner()
        if "approved" in cli_result:
            return cli_result["approved"], cli_result["note"]
        approved = self.gesture.poll_confirm_result()
        return approved, f"operator {'approved' if approved else 'denied'} via on-page banner"

    def take_control(self, request: InterventionRequest) -> tuple[str, bool, bool, list[dict]]:
        print("\n=== INTERVENTION REQUESTED (take control) ===")
        print(f"Goal:    {request.goal_or_capability}")
        print(f"Reason:  {request.reason}")
        print(f"Context: {request.message}")
        print(f"URL:     {request.current_url}")
        if request.context_snapshot:
            print(f"What the agent saw:\n{request.context_snapshot}")
        print("The automation has paused. The browser window is now yours.")
        print("Interact with it directly, then describe what you did below.")

        if self.gesture is None:
            note = input("What did you do? (one line, for the record): ").strip()
            ans = input("Let the agent try again, restart the run from the beginning, or end it? [y/r/N]: ").strip().lower()
            if ans == "r":
                return note or "(no note provided)", False, True, []
            return note or "(no note provided)", ans == "y", False, []

        # Same race pattern as confirm(). Structured action capture is armed for
        # the whole window regardless of which channel answers, except on replay's
        # path (self.capture=False), where it is never armed at all, by construction.
        if self.capture:
            self.gesture.start_capturing()
        # show_cancel=True: unlike escalate_via_gesture's call, this has a real
        # decision to make, so the banner needs Restart/End Run alongside Resume,
        # not just Resume.
        self.gesture.show_pause_banner(_banner_context(request), show_cancel=True)

        cli_result: dict = {}

        def _read_stdin():
            try:
                note = input("What did you do? (one line, for the record, or use the on-page banner): ").strip()
                ans = input("Let the agent try again, restart the run from the beginning, or end it? [y/r/N]: ").strip().lower()
                cli_result["note"] = note or "(no note provided)"
                cli_result["restart"] = ans == "r"
                cli_result["resume"] = False if cli_result["restart"] else ans == "y"
            except Exception:
                pass

        thread = threading.Thread(target=_read_stdin, daemon=True)
        thread.start()

        while "resume" not in cli_result and self.gesture.poll_resume_result() is None:
            self.gesture.pump(0.2)

        captured = self.gesture.stop_capturing() if self.capture else []
        self.gesture.hide_pause_banner()

        if "resume" in cli_result:
            return cli_result["note"], cli_result["resume"], cli_result["restart"], captured
        note = self.gesture.poll_resume_result()
        restart = self.gesture.poll_restart_requested()
        # Resume -> True, End Run -> False, Restart -> resume forced False (restart
        # takes priority, checked by the caller): the banner's buttons are real
        # choices now, not "clicking anything means continue".
        resume = False if restart else bool(self.gesture.poll_resume_decision())
        return note, resume, restart, captured
