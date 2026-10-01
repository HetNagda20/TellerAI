"""Runs one tool call against the live page. Every call goes through the allowlist and risk policy
first, and risky ones go to a human."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Page

from agent.perception import PerceivedElement, Snapshot, resolve_locator, snapshot as take_snapshot
from artifact.schema import Target
from guardrails.allowlist import Allowlist
from guardrails.policy import classify_action
from guardrails.redact import redact_value
from handoff.session import HandoffSession, InterventionRequest

logger = logging.getLogger(__name__)


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
    options: Optional[list[str]] = None
    """For a select step: the dropdown's visible choices at the time, so the recorder can declare them
    as the input's allowed values."""
    param_binding: Optional[str] = None
    """The declared input this step's fill/select value was bound to (set by
    agent/loop.py when `done` is accepted). The recorder templates the step as
    {param_binding} directly, so a value redacted at log time (a sensitive-named
    field) can still be parameterized without the raw value ever being persisted."""
    source: str = "llm"
    """'llm' (default, a normal tool call) or 'human_intervention' (captured
    while a human had control, see Executor.record_human_action). Threaded
    through by artifact/recorder.py into Step.source unchanged; this is the
    single place both kinds of step live."""


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
        # Raw fill/select values by step index, in memory only. Never serialized. Used at `done` to
        # prove a declared input was applied and came from the goal.
        self.raw_values: dict[int, str] = {}
        self._snapshot: Optional[Snapshot] = None
        # Optional GestureController: marks each dispatched action as "ours" so a
        # human's own click/keypress elsewhere is detectable.
        self._gesture = gesture

    def _dispatch(self, fn):
        """Runs one real Playwright action, marked so gesture detection can tell it from a human's.
        Also where LLM_CONTROL is enforced."""
        if not self.handoff.is_agent_allowed():
            raise RuntimeError(
                f"Refusing to dispatch an LLM-driven action while session state is "
                f"{self.handoff.state.value!r}. Control has not been handed back yet."
            )
        if self._gesture is None:
            return fn()
        self._gesture.mark_active(True)
        try:
            return fn()
        finally:
            self._gesture.mark_active(False)

    # -- perception --------------------------------------------------------

    def refresh_snapshot(self) -> Snapshot:
        self._snapshot = take_snapshot(self.page)
        return self._snapshot

    @property
    def current_snapshot(self) -> Snapshot:
        if self._snapshot is None:
            return self.refresh_snapshot()
        return self._snapshot

    # -- guardrail chokepoint ------------------------------------------------

    def _check_policy(self, action: str, element_name: Optional[str], url: Optional[str]) -> str:
        url_ok = self.allowlist.url_allowed(url) if url else True
        if not self.allowlist.action_allowed(action):
            return "blocked"
        return classify_action(action, element_name, url_ok)

    def _gate(self, risk: str, action: str, context: str) -> tuple[bool, str]:
        """Returns (allowed, note). Blocks outright, or escalates confirm risk to a human."""
        if risk == "blocked":
            logger.warning("blocked by allowlist/policy: action=%s context=%s", action, context)
            return False, "blocked by allowlist policy"
        if risk == "confirm":
            logger.warning("escalation raised reason=risky_action_confirm action=%s", action)
            record = self.handoff.escalate(
                InterventionRequest(
                    reason="risky_action_confirm",
                    goal_or_capability=self.goal,
                    message=f"About to {action}: {context}. This looks irreversible, approve?",
                    step_index=len(self.steps),
                )
            )
            return record.outcome == "approved", record.human_note
        return True, ""

    # -- tools exposed to the LLM --------------------------------------------

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
            options=[o["label"] for o in el.options] if action == "select" and el.options else None,
        )
        if not allowed:
            log.ok, log.error = False, f"{action} blocked: {note}"
            self.steps.append(log)
            return {"ok": False, "error": log.error}
        if value is not None:
            self.raw_values[log.index] = value

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

    def record_human_action(self, action: str, descriptor: dict, value: Optional[str], url: str) -> StepLog:
        """Turns one captured human action into a StepLog (source="human_intervention"), built like
        an LLM step. Not run through _dispatch, since the human already did it."""
        el = PerceivedElement(
            ref="human",
            frame_index=0,  # main-frame only, see handoff/gesture.py's documented scope
            role=descriptor["role"],
            name=descriptor["name"],
            name_source=descriptor["name_source"],
            tag="",
            input_type="",
            css=descriptor["css"],
            bbox=descriptor["bbox"],
            interactive=True,
            options=descriptor.get("options"),
        )
        risk = classify_action(action, el.name, True)
        log = StepLog(
            index=len(self.steps),
            action=action,
            rationale="Captured from a human operator's action during intervention.",
            element_role=el.role,
            element_name=el.name,
            value=redact_value(el.name, value) if value is not None else None,
            value_field_name=el.name if value is not None else None,
            target=el.to_target(),
            url_before=url,
            url_after=url,
            risk=risk,
            ok=True,
            source="human_intervention",
            options=[o["label"] for o in el.options] if action == "select" and el.options else None,
        )
        if value is not None:
            self.raw_values[log.index] = value
        self.steps.append(log)
        return log

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
