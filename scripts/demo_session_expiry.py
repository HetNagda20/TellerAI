"""Live slow-motion demo: the app's session expires during a replay, and replay signs back in. No API key needed.

  python scripts/demo_session_expiry.py 1   it expires BEFORE the confirm click: sign in, re-run the steps, one transfer
  python scripts/demo_session_expiry.py 2   it expires ON the confirm click (the write never happens): the check signs in,
                                            finds nothing, retries the whole transaction once, one transfer
  python scripts/demo_session_expiry.py 3   it expires AFTER the write went through: the check signs in, finds this
                                            attempt's run id, and does NOT retry, one transfer

Replay signs in with CONSOLE_USERNAME and CONSOLE_PASSWORD from its environment. This script defaults them to the
mock bank's demo login. Needs the mock bank running (uvicorn mock_app.app:app --port 8000). The saved artifact stays a
draft: an approved copy is used in memory so the only thing you watch is the sign-in.
"""
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import replay.executor as replay_executor  # noqa: E402
from artifact.schema import ArtifactStatus  # noqa: E402
from artifact.store import load_path  # noqa: E402

BASE = "http://127.0.0.1:8000"
SLOW_MO_MS = int(os.environ.get("DEMO_SLOW_MO_MS", "700"))
PARAMS = {"from_member_id": "10001", "from_account_type": "Checking", "to_member_id": "20001", "to_account_type": "Checking", "amount": "7"}
SCENARIOS = {
    "1": ("session_expires_at_review", "SCENARIO 1: the session expires before the confirm click. Nothing was written yet."),
    "2": ("session_expires_at_confirm", "SCENARIO 2: the session expires on the confirm click. The write never happens."),
    "3": ("session_expires_after_confirm", "SCENARIO 3: the session expires after the write went through. The confirmation page never appears."),
}


def get(path: str) -> str:
    return urllib.request.urlopen(BASE + path, timeout=5).read().decode()


def transfers() -> int:
    """How many automation transfers the sender's history shows."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", get("/member/10001/transactions"), re.S)
    return sum(1 for tr in rows if "Transfer to 20001" in tr and ("replay" in tr or "discovery" in tr))


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        sys.exit(__doc__)
    try:
        get("/")
    except Exception:
        sys.exit(f"The mock bank is not running at {BASE}. Start it first:\n  uvicorn mock_app.app:app --port 8000")
    os.environ.setdefault("CONSOLE_USERNAME", "teller")
    os.environ.setdefault("CONSOLE_PASSWORD", "teller-demo-pass")
    replay_executor.EVIDENCE_ROOT = Path(tempfile.mkdtemp(prefix="session_expiry_demo_"))

    scenario, narration = SCENARIOS[sys.argv[1]]
    artifact = load_path(ROOT / "artifacts" / "transfer-funds@1.0.0.json").model_copy(update={"status": ArtifactStatus.APPROVED})
    print("=" * 100 + f"\n{narration}\n" + "=" * 100)
    before = transfers()
    print(f"transfers in the sender's history before: {before}")

    urllib.request.urlopen(urllib.request.Request(f"{BASE}/__test__/arm_failure", data=f"scenario={scenario}".encode(), method="POST"), timeout=3)
    print(f"expiry armed on the app: {scenario}\nreplaying transfer-funds ($7, 10001 -> 20001), slow motion; watch for the sign-in page...\n")

    result = replay_executor.replay_artifact(artifact, PARAMS, headless=False, slow_mo_ms=SLOW_MO_MS)

    print("\n--- RESULT")
    print(f"    kind                       = {result.kind}")
    print(f"    signed back in (times)     = {result.session_reauths}")
    print(f"    attempts (commit_attempts) = {result.commit_attempts}")
    print(f"    recovered via run-id check = {result.recovered_via_commit_verification}")
    if result.failure:
        print(f"    failure                    = step {result.failure.step_index}: {result.failure.message[:140]}")
    after = transfers()
    print(f"\ntransfers in the sender's history after: {after}  (this run added {after - before}; the answer should be exactly 1)")


main()
