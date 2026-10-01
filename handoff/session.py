"""Human-in-the-loop escalation and control transfer. HandoffSession.state says who may act on the
page, and the executor refuses LLM actions unless the agent is allowed. Gates never teach."""

from __future__ import annotations

import json
import logging
import re
import select
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from playwright.sync_api import Page

from guardrails.redact import redact_obj, redact_text, redact_value
from handoff.remote import devtools_link

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    LLM_CONTROL = "llm_control"
    HUMAN_CONTROL = "human_control"


EscalationReason = Literal[
    "stuck", "risky_action_confirm", "replay_hard_failure", "human_requested", "human_gesture",
    "commit_verification_inconclusive", "commit_retry_exhausted", "commit_duplicate_suspected",
]

# Reasons where the human is teaching discovery something, so their actions get captured. Other
# reasons are gates or notifications.
DISCOVERY_HITL_REASONS = frozenset({"stuck", "human_gesture", "human_requested"})


@dataclass
class InterventionRequest:
    reason: EscalationReason
    goal_or_capability: str
    message: str
    step_index: Optional[int] = None
    current_url: str = ""
    screenshot_path: Optional[str] = None
    remote_link: str = ""
    """A DevTools link to this run's live tab (see handoff/remote.py), for a person who cannot see the window.
    Empty when remote debugging is off."""
    page_summary: str = ""
    """The start of the visible text on the page at the moment of escalation. A legacy app often renders a
    review or result page from a POST to the same address, so the URL alone can look like the page before it."""
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
    """The reason and message for the on-page banner, led by the goal or capability, so a human can
    judge whether the action matches what was asked."""
    return f"Goal: {request.goal_or_capability}\n\n[{request.reason}] {request.message}"


class HandoffSession:
    """One per live Playwright page. Owns the state machine and the evidence trail."""

    def __init__(self, page: Page, run_id: str, evidence_dir: Path, operator=None, remote_debug_port: Optional[int] = None):
        self.page = page
        self.remote_debug_port = remote_debug_port
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
        """Same state machine and evidence as escalate(), for a human already acting in the page.
        Waits on the page banner, with the same Resume, Restart and End Run choices."""
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
        request.remote_link = devtools_link(self.page, self.remote_debug_port)
        try:
            text = re.sub(r"\s+", " ", self.page.inner_text("body", timeout=1500)).strip()
            request.page_summary = redact_text(text[:160])
        except Exception:
            pass

    def _append_log(self, record: HandoffRecord) -> None:
        log_path = self.evidence_dir / "handoff_log.jsonl"
        with open(log_path, "a") as f:
            f.write(
                json.dumps(
                    redact_obj(
                        {
                            "request": vars(record.request),
                            "outcome": record.outcome,
                            "human_note": record.human_note,
                            "resumed_at": record.resumed_at,
                            "resume": record.resume,
                            "captured_actions": [_redact_action(a) for a in record.captured_actions],
                        }
                    )
                )
                + "\n"
            )


def _redact_action(action: dict) -> dict:
    """A captured human action, with its typed value redacted by the field's name as well as by pattern."""
    name = (action.get("descriptor") or {}).get("name", "")
    value = action.get("value")
    return {**action, "value": redact_value(name, value) if value is not None else None}


class _TerminalPrompt:
    """Terminal questions answered without a thread. A blocked input() in a spare thread cannot be cancelled,
    so an abandoned one kept reading the keyboard and could stall the next prompt (and the banner with it).
    Here the one thread that drives the browser polls stdin between browser events, so nothing is left behind."""

    def __init__(self, *questions: str):
        self.questions, self.answers = list(questions), []
        self._ask()

    def _ask(self) -> None:
        if len(self.answers) < len(self.questions):
            print(self.questions[len(self.answers)], end="", flush=True)

    @staticmethod
    def _ready() -> bool:
        try:
            return bool(select.select([sys.stdin], [], [], 0)[0])
        except (ValueError, OSError):  # no usable terminal (captured, closed): never ready
            return False

    def poll(self) -> bool:
        """Reads any waiting line. True once every question has an answer."""
        while len(self.answers) < len(self.questions) and self._ready():
            line = sys.stdin.readline()
            if line == "":  # end of input: nobody is typing
                break
            self.answers.append(line.strip())
            self._ask()
        return len(self.answers) == len(self.questions)


def _where_to_look(request: InterventionRequest, headless: bool, port: Optional[int]) -> list[str]:
    """Where the person should look, for a window on this computer and for a DevTools link. An app address
    cannot reopen a POST-rendered page and is not a link to the session, so it is only a last resort."""
    page = f"Page:    {request.page_summary}"
    tunnel = f"(from another machine, tunnel to port {port} first: ssh -L {port}:127.0.0.1:{port} <this-host>)" if port else ""
    lines: list[str] = []
    if not headless:
        lines += [
            "Look at the automated Chromium window (the test browser) on this computer: it is waiting for you,",
            "and the banner on its page has the details and the buttons.",
        ]
    elif request.remote_link:
        lines += ["This run has no visible window (headless)."]
    if request.remote_link:
        lines += [
            "To see and operate the live page remotely, open this link in a browser on this computer:" if headless
            else "No window in front of you? Open this link in a browser on this computer to see and operate the same page:",
            f"  {request.remote_link}",
            f"  {tunnel}",
            "  The banner on that page has the buttons; you can also answer here.",
        ]
    if headless and not request.remote_link:
        lines += [f"URL:     {request.current_url}", "No browser window is open and remote debugging is off (headless run): answer here."]
    return lines + [page]


class _CliOperator:
    """A bare operator surface: a blocking terminal prompt, raced by an on-page banner when a
    GestureController exists. It exercises control transfer, not a polished UI."""

    def __init__(self, gesture=None, capture: bool = True, headless: bool = False, remote_debug_port: Optional[int] = None):
        self.gesture = gesture
        self.headless = headless
        self.remote_debug_port = remote_debug_port
        # Replay passes capture=False, so its banner only notifies the end user and never arms
        # capture. That keeps capture reachable from discovery only.
        self.capture = capture

    def _print_where(self, request: InterventionRequest) -> None:
        for line in _where_to_look(request, headless=self.headless, port=self.remote_debug_port):
            print(line)

    def confirm(self, request: InterventionRequest) -> tuple[bool, str]:
        print("\n=== INTERVENTION REQUESTED (confirmation) ===")
        print(f"Goal:    {request.goal_or_capability}")
        print(f"Reason:  {request.reason}")
        print(f"Context: {request.message}")
        self._print_where(request)

        if self.gesture is None:
            ans = input("Approve this action? [y/N]: ").strip().lower()
            return ans == "y", f"operator answered {ans!r}"

        # Race two channels, both polled from this one thread: the terminal and the page banner. First answer wins.
        terminal = _TerminalPrompt("Approve this action? [y/N] (or use the on-page banner): ")
        self.gesture.show_confirm_banner(_banner_context(request))
        while self.gesture.poll_confirm_result() is None and not terminal.poll():
            self.gesture.pump(0.2)

        self.gesture.hide_confirm_banner()
        if self.gesture.poll_confirm_result() is None:
            answer = terminal.answers[0].lower()
            return answer == "y", f"operator answered {answer!r} (terminal)"
        approved = self.gesture.poll_confirm_result()
        return approved, f"operator {'approved' if approved else 'denied'} via on-page banner"

    def take_control(self, request: InterventionRequest) -> tuple[str, bool, bool, list[dict]]:
        print("\n=== INTERVENTION REQUESTED (take control) ===")
        print(f"Goal:    {request.goal_or_capability}")
        print(f"Reason:  {request.reason}")
        print(f"Context: {request.message}")
        self._print_where(request)
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

        # Same race as confirm(). Action capture is armed the whole window, except on replay
        # (capture=False), where it never is.
        if self.capture:
            self.gesture.start_capturing()
        # show_cancel=True: unlike escalate_via_gesture's call, this has a real
        # decision to make, so the banner needs Restart/End Run alongside Resume,
        # not just Resume.
        self.gesture.show_pause_banner(_banner_context(request), show_cancel=True)

        terminal = _TerminalPrompt(
            "What did you do? (one line, for the record, or use the on-page banner): ",
            "Let the agent try again, restart the run from the beginning, or end it? [y/r/N]: ",
        )
        while self.gesture.poll_resume_result() is None and not terminal.poll():
            self.gesture.pump(0.2)

        captured = self.gesture.stop_capturing() if self.capture else []
        self.gesture.hide_pause_banner()

        if self.gesture.poll_resume_result() is None:
            note, ans = terminal.answers[0] or "(no note provided)", terminal.answers[1].lower()
            restart = ans == "r"
            return note, False if restart else ans == "y", restart, captured
        note = self.gesture.poll_resume_result()
        restart = self.gesture.poll_restart_requested()
        # Resume gives True, End Run gives False, and Restart forces resume False (the caller checks
        # restart first). The buttons are real choices now.
        resume = False if restart else bool(self.gesture.poll_resume_decision())
        return note, resume, restart, captured
