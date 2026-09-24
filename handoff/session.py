"""Human-in-the-loop escalation and control transfer.

Design: the automation runs in a *headed* browser, on purpose. That means
"hand control to a human" doesn't require standing up a co-browsing/screen-
sharing console (explicitly out of scope) — the same OS-level browser window
Playwright has been driving is already visible and clickable. What this
module makes real is the *control-transfer model* around that window:

  AGENT_ACTIVE  -- escalate() -->  ESCALATED  -- human takes over -->  HUMAN_ACTIVE
       ^                                                                    |
       +-------------------------- resume() -------------------------------+

While a session is ESCALATED or HUMAN_ACTIVE, the executor (agent/tools.py)
and the replay engine (replay/executor.py) both refuse to issue further
automated actions against the page — enforced by state, not convention — so
"who is in control" is never ambiguous.

The operator surface here is intentionally a bare CLI prompt, not a built
console (see REPORT.md section 5 for what a real one would add). What's
real: the pause, the context handed over, the same live page, the resume
signal, and a logged record of what the human reported doing.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from playwright.sync_api import Page


class SessionState(str, Enum):
    AGENT_ACTIVE = "agent_active"
    ESCALATED = "escalated"
    HUMAN_ACTIVE = "human_active"


EscalationReason = Literal[
    "stuck", "risky_action_confirm", "replay_hard_failure", "human_requested", "human_gesture"
]


@dataclass
class InterventionRequest:
    reason: EscalationReason
    goal_or_capability: str
    message: str
    step_index: Optional[int] = None
    current_url: str = ""
    screenshot_path: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class HandoffRecord:
    request: InterventionRequest
    outcome: Literal["approved", "denied", "human_completed", "human_terminated"]
    human_note: str
    resumed_at: str
    resume: bool = True
    """Whether the caller should let the agent try again. False means the human
    judged this unrecoverable (e.g. a business fact no retry can change) and the
    run should end here rather than loop back — see agent/loop.py's give_up
    handling. Always True for 'risky_action_confirm' (there is no "try again",
    only approve/deny)."""


class HandoffSession:
    """One per live Playwright page. Owns the state machine and the evidence trail."""

    def __init__(self, page: Page, run_id: str, evidence_dir: Path, operator=None):
        self.page = page
        self.run_id = run_id
        self.evidence_dir = evidence_dir
        self.state = SessionState.AGENT_ACTIVE
        self.records: list[HandoffRecord] = []
        # `operator` is injectable so tests / non-interactive runs can supply a
        # scripted response instead of blocking on real stdin.
        self._operator = operator or _CliOperator()

    def is_agent_allowed(self) -> bool:
        return self.state is SessionState.AGENT_ACTIVE

    def escalate_via_gesture(self, request: InterventionRequest, controller) -> HandoffRecord:
        """Same state machine and evidence trail as escalate(), but for a human who
        has already taken control by clicking/typing in the live page directly —
        waits on the page's own "Resume Automation" click (handoff/gesture.py)
        instead of a CLI prompt. There is no deny/stop branch here: by the time
        this is called the human is already acting in the browser; clicking
        Resume is unambiguously "continue", and closing the run is what Ctrl+C
        is for.
        """
        self.state = SessionState.ESCALATED
        self._save_context(request)
        self.state = SessionState.HUMAN_ACTIVE
        note = controller.pause_and_wait_for_resume()
        record = HandoffRecord(
            request=request,
            outcome="human_completed",
            human_note=note,
            resumed_at=datetime.now(timezone.utc).isoformat(),
            resume=True,
        )
        self.records.append(record)
        self.state = SessionState.AGENT_ACTIVE
        self._append_log(record)
        return record

    def escalate(self, request: InterventionRequest) -> HandoffRecord:
        self.state = SessionState.ESCALATED
        self._save_context(request)

        if request.reason == "risky_action_confirm":
            approved, note = self._operator.confirm(request)
            record = HandoffRecord(
                request=request,
                outcome="approved" if approved else "denied",
                human_note=note,
                resumed_at=datetime.now(timezone.utc).isoformat(),
            )
        else:
            self.state = SessionState.HUMAN_ACTIVE
            note, resume = self._operator.take_control(request)
            record = HandoffRecord(
                request=request,
                outcome="human_completed" if resume else "human_terminated",
                human_note=note,
                resumed_at=datetime.now(timezone.utc).isoformat(),
                resume=resume,
            )

        self.records.append(record)
        self.state = SessionState.AGENT_ACTIVE
        self._append_log(record)
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
                    }
                )
                + "\n"
            )


class _CliOperator:
    """Bare/mock operator surface: a blocking terminal prompt, with an on-page
    banner (handoff/gesture.py) racing it whenever a GestureController is
    available — a human watching the browser (the whole point of running
    headed) shouldn't have to alt-tab to a terminal to see a decision is
    pending, let alone to answer it. Documented as intentionally minimal
    (see REPORT.md, Cuts) beyond that — what it exercises for real is the
    control-transfer model, not a polished UI.
    """

    def __init__(self, gesture=None):
        self.gesture = gesture

    def confirm(self, request: InterventionRequest) -> tuple[bool, str]:
        print("\n=== INTERVENTION REQUESTED (confirmation) ===")
        print(f"Reason:  {request.reason}")
        print(f"Context: {request.message}")
        print(f"URL:     {request.current_url}")

        if self.gesture is None:
            ans = input("Approve this action? [y/N]: ").strip().lower()
            return ans == "y", f"operator answered {ans!r}"

        # Race two channels: a background thread blocked on stdin (pure Python,
        # touches no Playwright API — a second thread calling into Playwright's
        # sync API breaks it, see REPORT.md), and the page's own Approve/Deny
        # banner, polled from this (the only) thread actually allowed to drive
        # the browser. Whichever resolves first wins; the thread is a daemon so
        # an abandoned terminal prompt can't block process exit.
        #
        # Known rough edge: if the banner answers first, this thread is left
        # sitting on input() forever (daemon, so harmless to shutdown, but not
        # cleaned up). If a *later* confirm() spawns a second stdin thread and
        # the human then types into the terminal, whichever thread's input()
        # call happens to be next in line consumes it — usually the new one,
        # but not guaranteed. Not fixed here: Python has no clean way to cancel
        # a blocked input() call short of closing stdin outright.
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

        self.gesture.show_confirm_banner(request.message)
        while "approved" not in cli_result and self.gesture.poll_confirm_result() is None:
            self.gesture.pump(0.2)

        self.gesture.hide_confirm_banner()
        if "approved" in cli_result:
            return cli_result["approved"], cli_result["note"]
        approved = self.gesture.poll_confirm_result()
        return approved, f"operator {'approved' if approved else 'denied'} via on-page banner"

    def take_control(self, request: InterventionRequest) -> tuple[str, bool]:
        print("\n=== INTERVENTION REQUESTED (take control) ===")
        print(f"Reason:  {request.reason}")
        print(f"Context: {request.message}")
        print(f"URL:     {request.current_url}")
        print("The automation has paused. The browser window is now yours —")
        print("interact with it directly, then describe what you did below.")
        note = input("What did you do? (one line, for the record): ").strip()
        resume = input("Let the agent try again? [y/N] (N ends the run here): ").strip().lower() == "y"
        return note or "(no note provided)", resume
