"""The goal-driven observe -> decide -> act loop.

Runs a real, headed Playwright browser against the target. Every decision
comes from Claude, grounded in the accessibility-style snapshot (see
perception.py) — never from hardcoded logic. The loop stops when the model
calls `done` (goal met), `give_up` (routed to human escalation as a "stuck"
event), or a hard stopping condition (max steps / wall-clock timeout) is hit.

Human escalation (give_up, a proactive gesture, or a file-touch request) is
learning, not just a pause: whatever the human does on the live page while
they have control is captured as real Step-shaped entries
(source="human_intervention", see agent/executor.py's record_human_action)
and merged into the same `executor.steps` list the LLM's own actions land
in, in the order everything actually happened. The artifact recorder
(artifact/recorder.py) doesn't need to know or care which steps came from
which source to build a correct, replayable sequence — see
_record_captured_actions below for where that merge happens.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from agent.executor import Executor, StepLog
from agent.llm import make_client, next_action
from guardrails.allowlist import Allowlist
from guardrails.redact import redact_text
from handoff.gesture import GestureController
from handoff.session import HandoffSession, InterventionRequest, _CliOperator

EVIDENCE_ROOT = Path(__file__).resolve().parent.parent / "evidence"
MAX_STEPS_DEFAULT = 20
TIMEOUT_S_DEFAULT = 300
REPEAT_VISIT_WARNING_THRESHOLD = 2  # nudge on the 3rd visit to the same URL this run


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


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("discovery_%Y%m%dT%H%M%SZ")


def _record_captured_actions(executor: Executor, captured: list[dict]) -> str:
    """Converts every {action, descriptor, value} a human performed during an
    intervention into a real StepLog (source="human_intervention"), in order,
    via Executor.record_human_action — the same StepLog list the recorder later
    turns into artifact Steps, so a human-taught action is not a side note, it's
    part of the sequence (requirements C/D/F). Returns a short summary string to
    fold into the feedback message the LLM sees next, so it knows what changed
    on the page and why, not just that something did.
    """
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

    # A human doesn't have to wait for the model to hit a confirm gate or call give_up —
    # touching this file from another terminal, at any point, pauses the run before its
    # *next* step and hands control over, exactly like any other escalation (see the
    # "human_requested" branch in the loop below). Checked once per step, not mid-step:
    # cleanly interrupting a step already in flight (mid network call or mid click) would
    # need real async cancellation, not worth it for a bare CLI operator surface.
    pause_flag_path = evidence_dir / "PAUSE_REQUESTED"

    allowlist = Allowlist.load()
    client = make_client()

    started_at = datetime.now(timezone.utc).isoformat()
    outcome, summary, outputs = "max_steps", "", {}

    print(f"Run ID: {run_id}")
    print("To take control, either:")
    print(f"  - touch {pause_flag_path}   (from another terminal), or")
    print("  - just click or type directly in the browser window — it's detected automatically")
    print("    and a 'Resume Automation' button appears on the page when you're done.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=150 if not headless else 0)
        page = browser.new_page()
        # Tags every request this run makes so the target app (and anyone watching its
        # logs or data) can tell an LLM-driven discovery action apart from a deterministic
        # replay action or an organic manual request — see replay/executor.py for the
        # replay-side counterpart, and mock_app/app.py for how the server surfaces it.
        page.set_extra_http_headers({"X-Automation-Source": "discovery", "X-Run-Id": run_id})
        gesture = GestureController(page) if not headless else None
        # Passing gesture through lets the risky-action confirm gate show an
        # on-page Approve/Deny banner (handoff/gesture.py), not just a terminal
        # prompt — see _CliOperator.confirm().
        handoff = HandoffSession(
            page=page, run_id=run_id, evidence_dir=evidence_dir, operator=_CliOperator(gesture=gesture)
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
        url_visit_counts: Counter[str] = Counter()
        try:
            while step_n < max_steps:
                if time.monotonic() > deadline:
                    outcome, summary = "timeout", "Wall-clock timeout exceeded."
                    break

                if pause_flag_path.exists():
                    pause_flag_path.unlink()
                    record = handoff.escalate(
                        InterventionRequest(
                            reason="human_requested",
                            goal_or_capability=goal,
                            message="A human operator requested control before the next step.",
                            step_index=step_n,
                        )
                    )
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
                    record = handoff.escalate_via_gesture(
                        InterventionRequest(
                            reason="human_gesture",
                            goal_or_capability=goal,
                            message="A human operator clicked/typed directly in the browser.",
                            step_index=step_n,
                        ),
                        gesture,
                    )
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

                # disable_parallel_tool_use (agent/llm.py) should make this always length 1, but
                # every tool_use in an assistant turn requires a matching tool_result in the very
                # next message regardless — so if the model ever *does* emit more than one, every
                # extra one past the first gets an explicit "not executed" result rather than
                # being silently dropped (which previously left the conversation history invalid
                # and broke the *next* API call, not this one — caught during a real run).
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
                    outcome, summary = "done", args.get("summary", "")
                    outputs = args.get("outputs", {})
                    result_blocks.insert(
                        0, {"type": "tool_result", "tool_use_id": primary.id, "content": json.dumps({"ok": True, "final": True})}
                    )
                    messages.append({"role": "user", "content": result_blocks})
                    break

                if name == "give_up":
                    reason = args.get("reason", "no reason given")
                    # Structured intervention context (requirement A): not just "stuck",
                    # but the goal, the reason in the agent's own words, and the exact
                    # page-state text it was reasoning over when it decided to escalate —
                    # the same thing a human debugging "why did it think it was stuck"
                    # would want, and richer than a screenshot alone.
                    record = handoff.escalate(
                        InterventionRequest(
                            reason="stuck",
                            goal_or_capability=goal,
                            message=f"Agent reported being stuck: {reason}",
                            step_index=step_n,
                            context_snapshot=executor.current_snapshot.to_prompt_text(),
                        )
                    )
                    if not record.resume:
                        # The human judged this unrecoverable — end the run here rather than
                        # loop back into another give_up. Without this, a genuinely unfixable
                        # business fact (e.g. a recipient that will never exist) causes the
                        # agent to re-escalate the same conclusion every turn until max_steps,
                        # burning the full step budget on repeated API calls for nothing new.
                        # Caught for real during a discovery run before this check existed.
                        _record_captured_actions(executor, record.captured_actions)
                        outcome, summary = "give_up", f"{reason} (human confirmed: {record.human_note})"
                        result_blocks.insert(
                            0, {"type": "tool_result", "tool_use_id": primary.id, "content": json.dumps({"ok": True, "final": True})}
                        )
                        messages.append({"role": "user", "content": result_blocks})
                        break
                    # human fixed something recoverable — record what they did as real,
                    # replayable steps (requirement C/D), then give the agent the resulting
                    # state and let it continue from there (requirement E).
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

                    # Structural nudge, not just a prompt instruction: every action's *own*
                    # tool result already tells the model whether it worked — nothing tells it
                    # "you've been here before." Found for real: against a structurally dead end
                    # (a locked account with no unlock path), the model never called give_up —
                    # it just kept trying other tabs/features for the full step budget, since
                    # every individual click still technically succeeded. Counting revisits to
                    # the same URL and attaching a direct nudge to the result once a page repeats
                    # doesn't override the model's judgment (it can still choose to try something
                    # else), but stops relying solely on it to notice a loop on its own.
                    current_url = page.url
                    url_visit_counts[current_url] += 1
                    if url_visit_counts[current_url] > REPEAT_VISIT_WARNING_THRESHOLD and isinstance(result, dict):
                        result["repeat_visit_warning"] = (
                            f"You have now reached this exact page ({current_url}) "
                            f"{url_visit_counts[current_url]} times this run. If there is no "
                            "actionable path from here toward the goal, stop exploring "
                            "alternatives and call give_up now."
                        )

                result_blocks.insert(
                    0, {"type": "tool_result", "tool_use_id": primary.id, "content": json.dumps(result, default=str)}
                )
                messages.append({"role": "user", "content": result_blocks})
            else:
                outcome, summary = "max_steps", f"Stopped after {max_steps} steps without calling done."

            executor.screenshot("final_state")
        finally:
            browser.close()

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
        )
        _write_evidence(result)
        return result


def _write_evidence(result: DiscoveryResult) -> None:
    evidence_dir = Path(result.evidence_dir)

    steps_path = evidence_dir / "steps.jsonl"
    with open(steps_path, "w") as f:
        for s in result.steps:
            d = asdict(s)
            if d.get("target") is not None:
                d["target"] = json.loads(s.target.model_dump_json()) if s.target else None
            f.write(json.dumps(d, default=str) + "\n")

    summary_path = evidence_dir / "run_summary.json"
    summary = {k: v for k, v in asdict(result).items() if k != "steps"}
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
