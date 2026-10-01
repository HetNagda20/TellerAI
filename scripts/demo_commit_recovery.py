"""Live slow-motion demo of ambiguous-commit recovery on the mock bank at :8000. Run with 1 (503
after the write) or 2 (503 before it). DEMO_SLOW_MO_MS sets the pace."""
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from artifact.store import load_path  # noqa: E402
from replay.executor import replay_artifact  # noqa: E402

BASE = "http://127.0.0.1:8000"
SLOW_MO_MS = int(os.environ.get("DEMO_SLOW_MO_MS", "800"))
ARTIFACT = ROOT / "artifacts" / "transfer-funds@1.0.0.json"
PARAMS = {"from_member_id": "10001", "from_account_type": "checking", "to_member_id": "20001", "to_account_type": "checking", "amount": "25"}

SCENARIOS = {
    "1": ("post_commit_response_failure",
          "SCENARIO 1: the fault hits AFTER the transfer is written. The confirmation screen never appears, but the transfer WAS recorded."),
    "2": ("pre_commit_response_failure",
          "SCENARIO 2: the fault hits BEFORE the transfer is written. The confirmation screen never appears and NOTHING was recorded."),
}


def get(path):
    return urllib.request.urlopen(BASE + path, timeout=5).read().decode()


def checking(member_id):
    return float(re.search(r"Checking Balance:.*?\$([\d,.\-]+)", get(f"/member/{member_id}"), re.S).group(1).replace(",", ""))


def automation_rows(member_id):
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", get(f"/member/{member_id}/transactions"), re.S):
        cells = [re.sub(r"<[^>]*>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) == 6 and cells[5] != "seed" and re.match(r"\d{4}-", cells[0]):
            rows.append(f"{cells[1]} | {cells[2]} {cells[3]} | {cells[5]}")
    return rows


def snapshot(label):
    print(f"\n--- {label}")
    print(f"    member 10001 checking = {checking('10001'):.2f}    member 20001 checking = {checking('20001'):.2f}")
    for member_id in ("10001", "20001"):
        for row in automation_rows(member_id):
            print(f"    ledger {member_id}: {row}")


def show_final_history():
    """Replay closes its browser when it ends, so open the sender's history for a few seconds: the Source column
    is the run-id trail the check reads."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=SLOW_MO_MS)
        page = browser.new_page()
        page.goto(f"{BASE}/member/10001/transactions")
        time.sleep(9)
        browser.close()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        sys.exit(__doc__)
    try:
        get("/")
    except Exception:
        sys.exit(f"The mock bank is not running at {BASE}. Start it first:\n  uvicorn mock_app.app:app --port 8000")
    if not ARTIFACT.exists():
        sys.exit(f"{ARTIFACT} is missing. Record it first (see the README's discovery command).")

    scenario, narration = SCENARIOS[sys.argv[1]]
    artifact = load_path(ARTIFACT)
    print("=" * 100 + f"\n{narration}\n" + "=" * 100)
    print(f"artifact status: {artifact.status.value} (a draft asks for approval before every confirm click: use the banner)")
    snapshot("BEFORE")

    urllib.request.urlopen(urllib.request.Request(f"{BASE}/__test__/arm_failure", data=f"scenario={scenario}".encode(), method="POST"), timeout=3)
    print(f"\nfault armed on the app: {scenario}\nreplaying transfer-funds ($25, 10001 -> 20001), slow motion...\n")

    result = replay_artifact(artifact, PARAMS, headless=False, escalate_on_failure=True, slow_mo_ms=SLOW_MO_MS)

    print("\n--- RESULT")
    print(f"    kind                       = {result.kind}")
    print(f"    attempts (commit_attempts) = {result.commit_attempts}")
    print(f"    recovered via run-id check = {result.recovered_via_commit_verification}")
    print(f"    outputs                    = {result.outputs}")
    print(f"    escalations                = {[(e.reason, e.outcome) for e in result.escalations]}")
    if result.failure:
        print(f"    failure                    = step {result.failure.step_index}: {result.failure.message[:120]}")
    snapshot("AFTER")
    print("\nopening the sender's history so you can see the Source column (the run-id trail)...")
    show_final_history()


main()
