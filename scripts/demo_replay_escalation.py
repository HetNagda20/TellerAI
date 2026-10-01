"""Interactive demo: a REPLAY asks you for help, you fix it in the live window, and it carries on. No API key needed.

The saved sub-account capability is replayed for member 20001, whose form shows a "session is being renewed" page
first. We take away the capability's declared recovery for that page, so replay meets a page it was never told
about. It fails the step, pauses, and asks you. You click Continue in the window yourself, then press Resume. Replay
retries that same recorded step once and finishes.

Nothing you do during a replay is recorded as a step: replay human help is operational recovery, never teaching.

Needs the mock bank running (uvicorn mock_app.app:app --port 8000). Nothing is saved to artifacts/ or evidence/.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import replay.executor as replay_executor  # noqa: E402
from artifact.schema import ArtifactStatus  # noqa: E402
from artifact.store import load_path  # noqa: E402

SLOW_MO_MS = int(os.environ.get("DEMO_SLOW_MO_MS", "600"))
PARAMS = {"member_id": "20001", "account_type": "Savings", "initial_deposit": "50"}


def say(text: str) -> None:
    print(f"\n>>> {text}", flush=True)


def main() -> None:
    replay_executor.EVIDENCE_ROOT = Path(tempfile.mkdtemp(prefix="replay_escalation_demo_"))  # keep it out of the repo
    artifact = load_path(ROOT / "artifacts" / "open-member-subaccount@1.0.0.json")
    declared = [c.name for c in artifact.recoverable_conditions]
    artifact = artifact.model_copy(update={
        "status": ArtifactStatus.APPROVED,  # so the only person asked for is the recovery, not the risk gate
        "recoverable_conditions": [],
        "step_timeout_ms": 3000,
    })

    say("PART 1. Replay runs on its own (watch the browser).")
    print(f"    The capability normally handles {declared} itself. For this demo that is removed, so the "
          "page below is a surprise to it.", flush=True)
    print("    When it stops and asks you:\n"
          "      1. In the browser window, click the 'Continue' button on the session-renewal page.\n"
          "      2. Then click 'Resume Automation' on the banner at the top right.\n"
          "    (The terminal also asks; you can ignore it and use the banner.)", flush=True)

    result = replay_executor.replay_artifact(
        artifact, PARAMS, headless=False, escalate_on_failure=True, slow_mo_ms=SLOW_MO_MS,
    )

    say("PART 2. The result.")
    print(f"    kind:        {result.kind}", flush=True)
    print(f"    outputs:     {result.outputs}", flush=True)
    print(f"    escalations: {[(e.reason, e.outcome, e.resume) for e in result.escalations]}", flush=True)
    if result.failure:
        print(f"    failure:     step {result.failure.step_index}: {result.failure.message}", flush=True)
    print("    Your clicks were not recorded as steps: replay never learns from a human.", flush=True)


if __name__ == "__main__":
    main()
