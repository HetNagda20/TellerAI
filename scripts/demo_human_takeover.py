"""Interactive demo: YOU are the human in the loop. No API key needed.

A scripted stand-in for the agent starts a transfer, then says it is stuck. You take control of the same live
browser window, click the link it could not find, and press Resume. Your click becomes a step in the recorded
artifact, which is then replayed on its own with different values.

With --headless there is no window at all: the terminal prints a DevTools link instead, and you do the same
steps in that page view from any Chromium-family browser on this computer.

Needs the mock bank running (uvicorn mock_app.app:app --port 8000). Nothing is saved to artifacts/ or evidence/.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

import replay.executor as replay_executor  # noqa: E402
from agent.executor import Executor  # noqa: E402
from agent.loop import DiscoveryResult  # noqa: E402
from artifact.recorder import record_artifact  # noqa: E402
from artifact.schema import ArtifactStatus, TargetApp  # noqa: E402
from guardrails.allowlist import Allowlist  # noqa: E402
from handoff.gesture import GestureController  # noqa: E402
from handoff.remote import free_loopback_port, launch_args  # noqa: E402
from handoff.session import HandoffSession, InterventionRequest, _CliOperator  # noqa: E402

BASE = "http://127.0.0.1:8000"
SLOW_MO_MS = int(os.environ.get("DEMO_SLOW_MO_MS", "600"))
HEADLESS = "--headless" in sys.argv
GOAL = "Transfer $30 from checking for member 10001 to checking for member 20001."


def say(text: str) -> None:
    print(f"\n>>> {text}", flush=True)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="human_takeover_demo_"))
    replay_executor.EVIDENCE_ROOT = work  # keep the demo's evidence out of the repo

    with sync_playwright() as pw:
        # a window that fits a laptop screen, so nothing (like the approval banner) sits off-screen
        port = free_loopback_port()
        browser = pw.chromium.launch(
            headless=HEADLESS, slow_mo=0 if HEADLESS else SLOW_MO_MS,
            args=["--window-size=1000,760", "--window-position=40,40", *launch_args(port)],
        )
        page = browser.new_page(viewport={"width": 1000, "height": 700}) if HEADLESS else browser.new_page(no_viewport=True)
        gesture = GestureController(page)
        handoff = HandoffSession(
            page=page, run_id="human_takeover_demo", evidence_dir=work, remote_debug_port=port,
            operator=_CliOperator(gesture=gesture, headless=HEADLESS, remote_debug_port=port),
        )
        ex = Executor(page=page, allowlist=Allowlist.load(), handoff=handoff, goal=GOAL, evidence_dir=work, gesture=gesture)

        def ref(role, name=None, startswith=None):
            return next(
                e.ref for e in ex.current_snapshot.elements
                if e.role == role and (name is None or e.name == name) and (startswith is None or e.name.startswith(startswith))
            )

        say("PART 1. The agent works on its own (watch the browser).")
        ex.navigate(BASE + "/", rationale="start")
        ex.click(ref("link", "Transactions"), rationale="agent: open Transactions")
        ex.click(ref("link", "Transaction Entry"), rationale="agent: open Transaction Entry")
        ex.fill(ref("textbox"), "10001", rationale="agent: source member")
        ex.click(ref("button", "Search"), rationale="agent: look the member up")

        say("PART 2. The agent says it is stuck and hands you control.")
        where = "In the DevTools page that the link below opens (look at the left-hand page view):" if HEADLESS else "In the browser window:"
        print(f"    {where}\n"
              "      1. Click the 'Transfer Funds' link yourself.\n"
              "      2. Then click the 'Resume Automation' button on the banner at the top of the page.\n"
              "    (The terminal also asks what you did. You can ignore it and use the banner.)", flush=True)
        before = len(ex.steps)
        record = handoff.escalate(InterventionRequest(
            reason="stuck", goal_or_capability=GOAL, message="I cannot find the link that opens the transfer form. Please click it.",
        ))
        say(f"Control is back with the agent. Outcome: {record.outcome}. Your actions, as captured:")
        for act in record.captured_actions:
            print(f"    {act['action']:7} {act['descriptor']['role']} {act['descriptor']['name']!r}", flush=True)
        if not record.captured_actions:
            print("    (none captured: did you click Transfer Funds before Resume? The demo can't continue.)")
            browser.close()
            return
        for act in record.captured_actions:
            ex.record_human_action(action=act["action"], descriptor=act["descriptor"], value=act.get("value"), url=page.url)
        ex.refresh_snapshot()
        print(f"    They are now steps {before}..{len(ex.steps) - 1} in the run, marked source=human_intervention.", flush=True)

        say("PART 3. The agent carries on from where you left it.")
        ex.select(ref("combobox", "From Account:"), "checking", rationale="agent: source account")
        ex.fill(ref("textbox", "To Member ID:"), "20001", rationale="agent: destination member")
        ex.select(ref("combobox", "To Account:"), "checking", rationale="agent: destination account")
        ex.fill(ref("textbox", "Amount ($):"), "30", rationale="agent: amount")
        ex.click(ref("button", "Review Transfer"), rationale="agent: review")
        print("    The next click is irreversible, so it asks you first: click Approve on the banner.", flush=True)
        ex.click(ref("button", "Confirm Transfer"), rationale="agent: confirm")
        confirmation = ex.read_text(ref("text", startswith="TXF-"), rationale="agent: read the confirmation number")["text"]
        browser.close()

    say("PART 4. The run is recorded as an artifact.")
    result = DiscoveryResult(
        run_id="human_takeover_demo", success=True, outcome="done", summary="done", outputs={"confirmation_number": confirmation},
        steps=ex.steps, goal=GOAL, target_url=BASE + "/", started_at="t0", ended_at="t1", evidence_dir=str(work),
    )
    declared = {"member_id": "10001", "from_account": "checking", "to_member_id": "20001", "to_account": "checking", "amount": "30"}
    artifact = record_artifact(result, "human-taught-transfer", "demo", declared, TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"))
    for s in artifact.steps:
        mark = "  <-- YOU" if s.source == "human_intervention" else ""
        print(f"    {s.index:2} {s.action.value:9} {s.description[:60]}{mark}", flush=True)

    say("PART 5. Replay it with different values, with no model and no human (watch the browser).")
    replayed = replay_executor.replay_artifact(
        artifact.model_copy(update={"status": ArtifactStatus.APPROVED}),
        {"member_id": "12345", "from_account": "savings", "to_member_id": "20001", "to_account": "checking", "amount": "40"},
        headless=HEADLESS, slow_mo_ms=0 if HEADLESS else SLOW_MO_MS,
    )
    print(f"    result: {replayed.kind}  outputs: {replayed.outputs}", flush=True)
    print(f"\nDone. Your click was step {[s.index for s in artifact.steps if s.source == 'human_intervention']} of {len(artifact.steps)}.", flush=True)


if __name__ == "__main__":
    main()
