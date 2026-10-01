"""The goal-driven observe, decide, act loop. Claude picks every action. It ends on done, give_up
(sent to a human), max steps or timeout. Human actions become steps too."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright

from agent.executor import Executor, StepLog
from agent.llm import make_client, next_action
from artifact.grounding import is_grounded, normalize, same_value
from artifact.pagetext import collapse, page_text, text_present
from artifact.schema import LocatorStrategy
from guardrails.allowlist import Allowlist
from guardrails.redact import redact_obj, redact_text, redact_value
from handoff.gesture import GestureController
from handoff.remote import free_loopback_port, launch_args, remote_debugging_enabled
from handoff.session import HandoffSession, InterventionRequest, _CliOperator

logger = logging.getLogger(__name__)

EVIDENCE_ROOT = Path(__file__).resolve().parent.parent / "evidence"
MAX_STEPS_DEFAULT = 30
TIMEOUT_S_DEFAULT = 300
STUCK_WARNING_THRESHOLD = 2  # warn from the 3rd arrival at one screen, or the 3rd identical action on it


@dataclass
class DiscoveryResult:
    run_id: str
    success: bool
    outcome: str  # "done" | "give_up" | "human_stopped" | "max_steps" | "timeout"
    summary: str
    outputs: dict[str, Any]
    steps: list[StepLog]
    goal: str
    target_url: str
    started_at: str
    ended_at: str
    evidence_dir: str
    inputs: dict[str, str] = field(default_factory=dict)
    """Goal-supplied values the agent declared at `done`, name -> RAW value, in memory only:
    the evidence writer redacts these, and the recorder persists only {name} placeholders."""
    input_descriptions: dict[str, str] = field(default_factory=dict)
    success_text: str = ""
    """The phrase the agent saw on the final page that only a successful run shows. Replay's checkpoint."""


def _screen_fingerprint(page) -> str:
    """What the screen shows: its URL plus its visible text, frames included. The URL alone cannot tell screens
    apart in an app that serves every screen from one address."""
    return hashlib.sha1(f"{page.url}|{page_text(page)}".encode()).hexdigest()[:12]


class _StuckWatch:
    """Warns the model that it is going in circles, from what the screen shows and not from its URL.

    Two signals, each from the third occurrence on: arriving at the same screen again after being somewhere else
    (wandering among pages), and repeating the same action on a screen that has not changed (a click that does
    nothing). Acting on one screen many times, like filling a form's fields, is neither: those are different
    actions and arrive at nothing new.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.arrivals: Counter[str] = Counter()
        self.repeats: Counter[tuple] = Counter()
        self.last: Optional[str] = None

    def observe(self, page, tool: str, args: dict) -> Optional[str]:
        try:
            screen = _screen_fingerprint(page)
        except Exception:  # mid-navigation: no reading, no warning
            return None
        if screen != self.last:
            self.arrivals[screen] += 1
            self.last = screen
        action = (screen, tool, json.dumps(args, sort_keys=True, default=str))
        self.repeats[action] += 1
        if self.repeats[action] > STUCK_WARNING_THRESHOLD:
            return (
                f"You have now tried this same action {self.repeats[action]} times on a screen that has not changed. "
                "If there is no actionable path from here toward the goal, stop and call give_up now."
            )
        if self.arrivals[screen] > STUCK_WARNING_THRESHOLD:
            return (
                f"You have now come back to this same screen {self.arrivals[screen]} times this run. If there is no "
                "actionable path from here toward the goal, stop exploring alternatives and call give_up now."
            )
        return None


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("discovery_%Y%m%dT%H%M%SZ")


def _success_text_error(page, success_text: str, outputs: dict[str, Any], input_values) -> str | None:
    """Why a claimed success phrase can't be trusted, or None. It must be short, really on the page now, and
    constant across requests: no output value, no input value, no long generated-looking number."""
    phrase = collapse(success_text or "")
    if not phrase:
        return "success_text is empty"
    if len(phrase) > 80:
        return "success_text is too long; give a short phrase"
    if not text_present(page, phrase):
        return f"success_text {phrase!r} is not visible on the current page"
    if any(collapse(str(v)) and collapse(str(v)) in phrase for v in outputs.values()):
        return "success_text contains a generated output value, which changes on every run"
    lowered = phrase.lower()
    if any(len(collapse(str(v))) >= 3 and collapse(str(v)).lower() in lowered for v in input_values):
        return "success_text contains one of this request's input values, so it would fail for any other request"
    if re.search(r"\d{5,}", phrase):
        return "success_text contains a long number that looks generated; use the surrounding words"
    return None


def _unproven_outputs(steps: list[StepLog], outputs: dict[str, Any]) -> list[str]:
    """Which claimed `done` outputs have no proof. An output must echo a value the agent filled in,
    or match exactly what a read_text step returned."""
    echoed_values = {s.value for s in steps if s.action in ("fill", "select") and s.ok and s.value is not None}
    read_values = {s.element_name for s in steps if s.action == "read_text" and s.ok and s.element_name is not None}
    return [key for key, value in outputs.items() if value not in echoed_values and value not in read_values]


_INPUT_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


def _declared_inputs(raw: Any) -> dict[str, tuple[str, str]]:
    """The `inputs` argument of `done`, coerced to name -> (value, description). Tolerates a bare
    string value in place of {"value", "description"}."""
    declared: dict[str, tuple[str, str]] = {}
    for name, entry in (raw or {}).items():
        if isinstance(entry, dict):
            declared[str(name)] = (str(entry.get("value", "")), str(entry.get("description", "")))
        else:
            declared[str(name)] = (str(entry), "")
    return declared


def _control_key(step: StepLog) -> str:
    if step.target is not None:
        for c in step.target.candidates:
            if c.strategy == LocatorStrategy.CSS_PATH:
                return str(c.value.get("css", f"#{step.index}"))
    return f"#{step.index}"


def _check_declared_inputs(
    steps: list[StepLog], raw_values: dict[int, str], goal: str, declared: dict[str, tuple[str, str]]
) -> tuple[Optional[str], dict[int, str]]:
    """Proves declared inputs: each must appear in the goal and match a value the run applied, and
    no applied goal value may go undeclared. Returns (error or None, bindings)."""
    by_index = {s.index: s for s in steps}
    applied = [
        (idx, raw)
        for idx, raw in sorted(raw_values.items())
        if idx in by_index and by_index[idx].ok and by_index[idx].action in ("fill", "select")
    ]
    # A control re-filled with the same value is one binding; its LAST value is its final state.
    groups: dict[tuple[str, str], list[int]] = {}
    last_value: dict[str, str] = {}
    for idx, raw in applied:
        control = _control_key(by_index[idx])
        groups.setdefault((control, normalize(raw)), []).append(idx)
        last_value[control] = normalize(raw)

    errors: list[str] = []
    claimed: dict[tuple[str, str], str] = {}
    for name, (value, _description) in declared.items():
        if not _INPUT_NAME_RE.fullmatch(name):
            errors.append(f"input name {name!r} must be a snake_case identifier such as member_id")
            continue
        if not is_grounded(value, goal):
            errors.append(f"input {name}={value!r} does not appear in the goal text; declare only values the goal itself supplied")
            continue
        match = next((k for k, idxs in groups.items() if k not in claimed and same_value(raw_values[idxs[0]], value)), None)
        if match is None:
            errors.append(f"input {name}={value!r} was never applied to any control by a fill or select (each control's value supports one input)")
            continue
        claimed[match] = name

    for (control, norm_value), idxs in groups.items():
        if (control, norm_value) in claimed or last_value[control] != norm_value:
            continue  # declared, or overwritten later on the same control
        raw = raw_values[idxs[0]]
        if is_grounded(raw, goal):
            label = by_index[idxs[0]].element_name or "a field"
            errors.append(f"you applied {raw!r}, a value from the goal, to {label!r} but did not declare it in `inputs`")

    if errors:
        return "; ".join(errors), {}
    return None, {idx: claimed[key] for key, idxs in groups.items() if key in claimed for idx in idxs}


def _record_captured_actions(executor: Executor, captured: list[dict]) -> str:
    """Turns each action a human took during an intervention into a StepLog, in order. Returns a
    short summary for the model's next message."""
    if not captured:
        return ""
    lines = []
    for entry in captured:
        log = executor.record_human_action(
            action=entry["action"],
            descriptor=entry["descriptor"],
            value=entry.get("value"),
            url=executor.page.url,
        )
        detail = f" = {log.value!r}" if log.value else ""
        lines.append(f"  - [human] {log.action} on {log.element_role} \"{log.element_name}\"{detail}")
    return "\nRecorded as replayable step(s):\n" + "\n".join(lines)


def _restart_from_scratch(executor: Executor, goal: str, target_url: str) -> list[dict]:
    """Starts over in the same session and run_id: clears steps and conversation and returns to the
    entry point. The deadline is not reset."""
    executor.steps.clear()
    executor.raw_values.clear()
    nav = executor.navigate(target_url, rationale="Restart requested by a human operator.")
    return [
        {
            "role": "user",
            "content": f"Goal: {goal}\n\n{executor.current_snapshot.to_prompt_text() if nav['ok'] else 'Failed to load target: ' + str(nav.get('error'))}",
        }
    ]


def run_discovery(
    goal: str,
    target_url: str,
    max_steps: int = MAX_STEPS_DEFAULT,
    timeout_s: int = TIMEOUT_S_DEFAULT,
    headless: bool = False,
) -> DiscoveryResult:
    run_id = _run_id()
    evidence_dir = EVIDENCE_ROOT / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    logger.info("discovery start run_id=%s goal=%r target=%s", run_id, goal, target_url)

    # Touching this file from another terminal pauses the run before the next step and hands control
    # to a human. Checked once per step, not mid-step.
    pause_flag_path = evidence_dir / "PAUSE_REQUESTED"

    allowlist = Allowlist.load()
    client = make_client()

    started_at = datetime.now(timezone.utc).isoformat()
    outcome, summary, outputs = "max_steps", "", {}
    claimed_inputs: dict[str, str] = {}
    input_descriptions: dict[str, str] = {}
    success_text = ""

    print(f"Run ID: {run_id}")
    print("To take control, either:")
    print(f"  - touch {pause_flag_path}   (from another terminal), or")
    print("  - just click or type directly in the browser window, it's detected automatically")
    print("    and a 'Resume Automation' button appears on the page when you're done.")

    with sync_playwright() as pw:
        # A loopback-only debugging port, one per run (see handoff/remote.py): it lets a person who cannot see
        # the window, e.g. a headless run, open the live page in DevTools when the run needs them.
        remote_port = free_loopback_port() if remote_debugging_enabled() else None
        browser = pw.chromium.launch(
            headless=headless, slow_mo=150 if not headless else 0, args=launch_args(remote_port) if remote_port else []
        )
        page = browser.new_page()
        # Tags every request from this run so the app can tell discovery from replay. See
        # replay/executor.py for the other side.
        page.set_extra_http_headers({"X-Automation-Source": "discovery", "X-Run-Id": run_id})
        # Headless runs get the controller too when the debugging port is open: the banner and the capture of a
        # person's actions then work through the DevTools link exactly as in a visible window.
        gesture = GestureController(page) if (not headless or remote_port) else None
        # Passing gesture through lets the risky-action confirm gate show an
        # on-page Approve/Deny banner, not just a terminal prompt.
        handoff = HandoffSession(
            page=page, run_id=run_id, evidence_dir=evidence_dir, remote_debug_port=remote_port,
            operator=_CliOperator(gesture=gesture, headless=headless, remote_debug_port=remote_port),
        )
        human_wants_control = {"flag": False}
        if gesture is not None:
            gesture.arm_human_detection(lambda: human_wants_control.__setitem__("flag", True))
        executor = Executor(
            page=page, allowlist=allowlist, handoff=handoff, goal=goal, evidence_dir=evidence_dir, gesture=gesture
        )

        nav = executor.navigate(target_url, rationale="Start at the target entry point.")
        messages: list[dict] = [
            {
                "role": "user",
                "content": f"Goal: {goal}\n\n{executor.current_snapshot.to_prompt_text() if nav['ok'] else 'Failed to load target: ' + str(nav.get('error'))}",
            }
        ]

        deadline = time.monotonic() + timeout_s
        step_n = 0
        stuck_watch = _StuckWatch()
        try:
            while step_n < max_steps:
                if time.monotonic() > deadline:
                    outcome, summary = "timeout", "Wall-clock timeout exceeded."
                    break

                if pause_flag_path.exists():
                    pause_flag_path.unlink()
                    logger.warning("escalation raised reason=human_requested run_id=%s step=%s", run_id, step_n)
                    record = handoff.escalate(
                        InterventionRequest(
                            reason="human_requested",
                            goal_or_capability=goal,
                            message="A human operator requested control before the next step.",
                            step_index=step_n,
                        )
                    )
                    if record.restart:
                        logger.info("discovery restarted by human run_id=%s step=%s", run_id, step_n)
                        messages = _restart_from_scratch(executor, goal, target_url)
                        step_n = 0
                        stuck_watch.reset()
                        continue
                    if not record.resume:
                        outcome, summary = "human_stopped", f"Human operator ended the run: {record.human_note}"
                        break
                    captured_summary = _record_captured_actions(executor, record.captured_actions)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"A human operator took control and made changes, then handed control "
                                f"back. What they did: {record.human_note}{captured_summary}\n\n"
                                f"{executor.refresh_snapshot().to_prompt_text()}"
                            ),
                        }
                    )
                    continue

                if human_wants_control["flag"]:
                    human_wants_control["flag"] = False
                    logger.warning("escalation raised reason=human_gesture run_id=%s step=%s", run_id, step_n)
                    record = handoff.escalate_via_gesture(
                        InterventionRequest(
                            reason="human_gesture",
                            goal_or_capability=goal,
                            message="A human operator clicked/typed directly in the browser.",
                            step_index=step_n,
                        ),
                        gesture,
                    )
                    if record.restart:
                        logger.info("discovery restarted by human run_id=%s step=%s", run_id, step_n)
                        messages = _restart_from_scratch(executor, goal, target_url)
                        step_n = 0
                        stuck_watch.reset()
                        continue
                    if not record.resume:
                        outcome, summary = "human_stopped", f"Human operator ended the run: {record.human_note}"
                        break
                    captured_summary = _record_captured_actions(executor, record.captured_actions)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"A human operator took control directly in the browser and made changes, "
                                f"then clicked Resume Automation. What they said they did: "
                                f"{record.human_note}{captured_summary}\n\n{executor.refresh_snapshot().to_prompt_text()}"
                            ),
                        }
                    )
                    continue

                response = next_action(client, messages, goal, target_url)
                tool_uses = [b for b in response.content if b.type == "tool_use"]

                assistant_content = [b.model_dump() for b in response.content]
                messages.append({"role": "assistant", "content": assistant_content})

                if not tool_uses:
                    # Model didn't call a tool despite tool_choice=any; nudge and continue.
                    messages.append({"role": "user", "content": "Please call exactly one tool."})
                    continue

                # Parallel tool use is disabled, so this should be one. Any extra tool_use still
                # needs a tool_result, so it gets a 'not executed' reply.
                primary, extra_tool_uses = tool_uses[0], tool_uses[1:]
                result_blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps({"ok": False, "error": "Not executed: only one tool call is processed per turn."}),
                    }
                    for tu in extra_tool_uses
                ]

                name = primary.name
                args = primary.input
                step_n += 1

                if name == "done":
                    claimed_outputs = args.get("outputs", {})
                    unproven = _unproven_outputs(executor.steps, claimed_outputs)
                    if unproven:
                        # Check at the finish line: if a claimed value was never proven, don't
                        # finish. Tell the model what's missing so it can read_text and try done
                        # again.
                        result_blocks.insert(
                            0,
                            {
                                "type": "tool_result",
                                "tool_use_id": primary.id,
                                "content": json.dumps(
                                    {
                                        "ok": False,
                                        "error": (
                                            f"done rejected: output(s) {unproven} have no deterministic "
                                            "provenance. Each output must either be a value you already "
                                            "entered into the page yourself, or come from the exact text "
                                            "returned by a read_text call on the specific element it came "
                                            "from. Call read_text on the element(s) these value(s) actually "
                                            "come from, then call done again."
                                        ),
                                    }
                                ),
                            },
                        )
                        messages.append({"role": "user", "content": result_blocks})
                        continue

                    declared = _declared_inputs(args.get("inputs"))
                    input_error, bindings = _check_declared_inputs(executor.steps, executor.raw_values, goal, declared)
                    if input_error:
                        # Same completion-boundary rule as outputs: a goal value that is not
                        # declared as an input would be hardcoded into the reusable artifact.
                        result_blocks.insert(
                            0,
                            {
                                "type": "tool_result",
                                "tool_use_id": primary.id,
                                "content": json.dumps(
                                    {
                                        "ok": False,
                                        "error": (
                                            f"done rejected: {input_error}. `inputs` must list every value the goal "
                                            "supplied that you applied to a control with fill or select, as "
                                            "{name: {value, description}} with the value exactly as you entered it, "
                                            "and nothing the goal did not say. Fix `inputs` and call done again."
                                        ),
                                    }
                                ),
                            },
                        )
                        messages.append({"role": "user", "content": result_blocks})
                        continue
                    text_error = _success_text_error(
                        page, args.get("success_text", ""), claimed_outputs, [v for v, _d in declared.values()]
                    )
                    if text_error:
                        # The checkpoint replay will wait for has to be real, so check it now.
                        result_blocks.insert(
                            0,
                            {
                                "type": "tool_result",
                                "tool_use_id": primary.id,
                                "content": json.dumps(
                                    {
                                        "ok": False,
                                        "error": (
                                            f"done rejected: {text_error}. Give `success_text`: a short phrase copied "
                                            "exactly from the current page that appears only when the operation "
                                            "succeeded, then call done again."
                                        ),
                                    }
                                ),
                            },
                        )
                        messages.append({"role": "user", "content": result_blocks})
                        continue
                    success_text = collapse(args.get("success_text", ""))
                    for step in executor.steps:
                        if step.index in bindings:
                            step.param_binding = bindings[step.index]
                    claimed_inputs = {name: value for name, (value, _d) in declared.items()}
                    input_descriptions = {name: d for name, (_v, d) in declared.items() if d}

                    outcome, summary = "done", args.get("summary", "")
                    outputs = claimed_outputs
                    result_blocks.insert(
                        0, {"type": "tool_result", "tool_use_id": primary.id, "content": json.dumps({"ok": True, "final": True})}
                    )
                    messages.append({"role": "user", "content": result_blocks})
                    break

                if name == "give_up":
                    reason = args.get("reason", "no reason given")
                    logger.warning("escalation raised reason=stuck run_id=%s step=%s detail=%r", run_id, step_n, reason)
                    # Structured intervention context: the goal, the reason in the agent's
                    # own words, and the exact page-state text it was reasoning over when it
                    # decided to escalate.
                    record = handoff.escalate(
                        InterventionRequest(
                            reason="stuck",
                            goal_or_capability=goal,
                            message=f"Agent reported being stuck: {reason}",
                            step_index=step_n,
                            context_snapshot=executor.current_snapshot.to_prompt_text(),
                        )
                    )
                    if record.restart:
                        # Restart wins over resume or end. The whole conversation is dropped,
                        # including this give_up, so no tool_use is left without a result.
                        logger.info("discovery restarted by human run_id=%s step=%s", run_id, step_n)
                        messages = _restart_from_scratch(executor, goal, target_url)
                        step_n = 0
                        stuck_watch.reset()
                        continue
                    if not record.resume:
                        # A human called it unrecoverable, so end here. Otherwise the agent re-
                        # escalates the same conclusion every turn until max_steps.
                        _record_captured_actions(executor, record.captured_actions)
                        outcome, summary = "give_up", f"{reason} (human confirmed: {record.human_note})"
                        result_blocks.insert(
                            0, {"type": "tool_result", "tool_use_id": primary.id, "content": json.dumps({"ok": True, "final": True})}
                        )
                        messages.append({"role": "user", "content": result_blocks})
                        break
                    # Human fixed something recoverable: record what they did as real,
                    # replayable steps, then give the agent the resulting state and let it
                    # continue from there.
                    captured_summary = _record_captured_actions(executor, record.captured_actions)
                    result = {
                        "ok": True,
                        "human_intervened": True,
                        "human_note": record.human_note + captured_summary,
                        "snapshot": executor.refresh_snapshot().to_prompt_text(),
                    }
                else:
                    method = getattr(executor, name, None)
                    if method is None:
                        result = {"ok": False, "error": f"unknown tool {name}"}
                    else:
                        result = method(**args)

                    # A nudge for dead ends. Each click succeeds, so the model never notices it's going in
                    # circles; telling it, without overriding its judgment, is what this does.
                    warning = stuck_watch.observe(page, name, args) if isinstance(result, dict) else None
                    if warning:
                        logger.warning("stuck nudge run_id=%s step=%s: %s", run_id, step_n, warning)
                        result["repeat_visit_warning"] = warning

                result_blocks.insert(
                    0, {"type": "tool_result", "tool_use_id": primary.id, "content": json.dumps(result, default=str)}
                )
                messages.append({"role": "user", "content": result_blocks})
            else:
                outcome, summary = "max_steps", f"Stopped after {max_steps} steps without calling done."

            executor.screenshot("final_state")
        finally:
            browser.close()

        if outcome == "done":
            logger.info("discovery success run_id=%s steps=%s", run_id, step_n)
        else:
            logger.error("discovery did not complete run_id=%s outcome=%s summary=%r", run_id, outcome, summary)

        result = DiscoveryResult(
            run_id=run_id,
            success=(outcome == "done"),
            outcome=outcome,
            summary=redact_text(summary),
            outputs={k: redact_text(str(v)) for k, v in outputs.items()},
            steps=executor.steps,
            goal=goal,
            target_url=target_url,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc).isoformat(),
            evidence_dir=str(evidence_dir),
            inputs=claimed_inputs,
            input_descriptions=input_descriptions,
            success_text=success_text,
        )
        _write_evidence(result)
        return result


def _redact_for_evidence(obj: Any) -> Any:
    """Recursive text redaction for evidence, audit-only and never
    re-resolved, so a plain substring substitution is safe here. Covers what
    StepLog-creation-time redaction does not: rationale text and a step's
    raw target."""
    return redact_obj(obj)


def _write_evidence(result: DiscoveryResult) -> None:
    evidence_dir = Path(result.evidence_dir)

    steps_path = evidence_dir / "steps.jsonl"
    with open(steps_path, "w") as f:
        for s in result.steps:
            d = asdict(s)
            if d.get("target") is not None:
                d["target"] = json.loads(s.target.model_dump_json()) if s.target else None
            d = _redact_for_evidence(d)
            f.write(json.dumps(d, default=str) + "\n")

    summary_path = evidence_dir / "run_summary.json"
    summary = {k: v for k, v in asdict(result).items() if k != "steps"}
    summary["inputs"] = {name: redact_value(name, value) for name, value in result.inputs.items()}
    summary_path.write_text(json.dumps(_redact_for_evidence(summary), indent=2, default=str))
