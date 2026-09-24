"""Executes one tool call against the live page, enforcing guardrails and
routing risky actions through the human-handoff session before they happen.

This is the single chokepoint both the LLM-driven loop calls through — no
tool call reaches Playwright without passing `_check_policy` first. That's
deliberate: the allowlist and risk policy are enforced *here*, not trusted
to the LLM's judgment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Page

from agent.perception import Snapshot, resolve_locator, snapshot as take_snapshot
from artifact.schema import Target
from guardrails.allowlist import Allowlist
from guardrails.policy import classify_action
from guardrails.redact import redact_value
from handoff.session import HandoffSession, InterventionRequest


@dataclass
class StepLog:
    index: int
    action: str
    rationale: str
    ref: Optional[str] = None
    element_role: Optional[str] = None
    element_name: Optional[str] = None
    value: Optional[str] = None
    value_field_name: Optional[str] = None
    target: Optional[Target] = None
    url_before: str = ""
    url_after: str = ""
    risk: str = "safe"
    ok: bool = True
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class DeadEnd(Exception):
    """Raised when the agent explicitly gives up, or a blocked action leaves no path forward."""


class Executor:
    def __init__(
        self,
        page: Page,
        allowlist: Allowlist,
        handoff: HandoffSession,
        goal: str,
        evidence_dir: Path,
        gesture=None,
    ):
        self.page = page
        self.allowlist = allowlist
        self.handoff = handoff
        self.goal = goal
        self.evidence_dir = evidence_dir
        self.steps: list[StepLog] = []
        self._snapshot: Optional[Snapshot] = None
        # Optional GestureController (handoff/gesture.py) — marks each dispatched
        # action as "ours" so a human's own click/keypress elsewhere is detectable.
        self._gesture = gesture

    def _dispatch(self, fn):
        """Runs one real Playwright action, bracketed so gesture detection can tell
        'we did this' apart from a human doing something else on the page.
        """
        if self._gesture is None:
            return fn()
        self._gesture.mark_active(True)
        try:
            return fn()
        finally:
            self._gesture.mark_active(False)

    # -- perception -----------------------------------------------------

    def refresh_snapshot(self) -> Snapshot:
        self._snapshot = take_snapshot(self.page)
        return self._snapshot

    @property
    def current_snapshot(self) -> Snapshot:
        if self._snapshot is None:
            return self.refresh_snapshot()
        return self._snapshot

    # -- guardrail chokepoint --------------------------------------------

    def _check_policy(self, action: str, element_name: Optional[str], url: Optional[str]) -> str:
        url_ok = self.allowlist.url_allowed(url) if url else True
        if not self.allowlist.action_allowed(action):
            return "blocked"
        return classify_action(action, element_name, url_ok)

    def _gate(self, risk: str, action: str, context: str) -> tuple[bool, str]:
        """Returns (allowed, note). Blocks outright, or escalates 'confirm' risk to a human."""
        if risk == "blocked":
            return False, "blocked by allowlist policy"
        if risk == "confirm":
            record = self.handoff.escalate(
                InterventionRequest(
                    reason="risky_action_confirm",
                    goal_or_capability=self.goal,
                    message=f"About to {action}: {context}. This looks irreversible — approve?",
                    step_index=len(self.steps),
                )
            )
            return record.outcome == "approved", record.human_note
        return True, ""

    # -- tools exposed to the LLM -----------------------------------------

    def navigate(self, url: str, rationale: str) -> dict[str, Any]:
        risk = self._check_policy("navigate", None, url)
        allowed, note = self._gate(risk, "navigate", url)
        log = StepLog(index=len(self.steps), action="navigate", rationale=rationale, risk=risk)
        log.url_before = self.page.url
        if not allowed:
            log.ok, log.error = False, f"navigate blocked: {note}"
            self.steps.append(log)
            return {"ok": False, "error": log.error}
        try:
            self._dispatch(lambda: self.page.goto(url, wait_until="load"))
            log.url_after = self.page.url
            self.refresh_snapshot()
        except Exception as e:
            log.ok, log.error = False, str(e)
        self.steps.append(log)
        return {"ok": log.ok, "error": log.error, "snapshot": self.current_snapshot.to_prompt_text()}

    def _act_on_ref(self, action: str, ref: str, rationale: str, value: Optional[str] = None) -> dict[str, Any]:
        try:
            el = self.current_snapshot.get(ref)
        except KeyError as e:
            return {"ok": False, "error": str(e)}

        risk = self._check_policy(action, el.name, None)
        allowed, note = self._gate(risk, action, f'{action} on {el.role} "{el.name}"')
        log = StepLog(
            index=len(self.steps),
            action=action,
            rationale=rationale,
            ref=ref,
            element_role=el.role,
            element_name=el.name,
            value=redact_value(el.name, value) if value is not None else None,
            value_field_name=el.name if value is not None else None,
            target=el.to_target(),
            url_before=self.page.url,
            risk=risk,
        )
        if not allowed:
            log.ok, log.error = False, f"{action} blocked: {note}"
            self.steps.append(log)
            return {"ok": False, "error": log.error}

        try:
            locator = resolve_locator(self.page, el)

            def _do():
                if action == "click":
                    locator.click(timeout=5000)
                elif action == "fill":
                    locator.fill(value or "", timeout=5000)
                elif action == "select":
                    locator.select_option(value, timeout=5000)
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)

            self._dispatch(_do)
            log.url_after = self.page.url
            self.refresh_snapshot()
        except Exception as e:
            log.ok, log.error = False, str(e)

        self.steps.append(log)
        result: dict[str, Any] = {"ok": log.ok, "error": log.error}
        if log.ok:
            result["snapshot"] = self.current_snapshot.to_prompt_text()
        return result

    def click(self, ref: str, rationale: str) -> dict[str, Any]:
        return self._act_on_ref("click", ref, rationale)

    def fill(self, ref: str, text: str, rationale: str) -> dict[str, Any]:
        return self._act_on_ref("fill", ref, rationale, value=text)

    def select(self, ref: str, value: str, rationale: str) -> dict[str, Any]:
        return self._act_on_ref("select", ref, rationale, value=value)

    def read_text(self, ref: str, rationale: str) -> dict[str, Any]:
        try:
            el = self.current_snapshot.get(ref)
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        log = StepLog(
            index=len(self.steps),
            action="read_text",
            rationale=rationale,
            ref=ref,
            element_role=el.role,
            element_name=el.name,
            target=el.to_target(),
            url_before=self.page.url,
            url_after=self.page.url,
            risk="safe",
        )
        self.steps.append(log)
        return {"ok": True, "text": el.name}

    def screenshot(self, tag: str) -> Path:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / f"{tag}.png"
        self.page.screenshot(path=str(path))
        return path
