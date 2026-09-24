# Computer-Use Automation System

A small, real implementation of: an LLM discovers how to complete a goal against a live
(deliberately legacy-styled) web app, the successful run is recorded as a typed, versioned
capability artifact, and that artifact replays deterministically — with no LLM in the loop —
handling the errors and business outcomes that legitimately occur at runtime. See
[REPORT.md](REPORT.md) for the full design write-up (architecture, schema, determinism,
heterogeneity, escalation, safety, cuts).

## What's here

- `mock_app/` — the target: a deliberately hostile "credit union servicing console"
  (table layout, no ids/test-ids, an iframe, real business-error branches). The home page (`/`)
  has a persistent top tab bar — Accounts, Transfers, Loans, Cards — each starting with its own
  member lookup; Loans/Cards are placeholder servicing areas (present for navigational
  completeness, not built out), Accounts and Transfers are fully real: member lookup, opening a
  sub-account, viewing transaction history, editing contact info, and transferring funds between
  members. See REPORT.md §2/§4.
- `agent/` — the discovery loop: perception (accessibility-style snapshot, frame-aware),
  the Claude tool-calling loop, and the guardrail-enforcing executor.
- `artifact/` — the capability schema, the recorder (discovery run → artifact), and storage.
- `replay/` — the deterministic replay engine: locator fallback resolution, business-outcome /
  recoverable-condition detection, checkpoint verification.
- `guardrails/` — allowlist, risk policy, redaction.
- `handoff/` — the human-escalation session state machine.
- `cli.py` — the two commands you actually run: `run` (discovery) and `replay`.
- `evidence/` — logs + screenshots from a real discovery run and real replay runs.
- `artifacts/` — saved capability artifacts (JSON).

## Setup

Requires Python 3.11+ and an Anthropic API key.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env   # then put your ANTHROPIC_API_KEY in .env
```

Start the mock target app (leave this running in its own terminal):

```bash
source .venv/bin/activate
uvicorn mock_app.app:app --port 8000
```

## Demo path

With the mock app running at `http://127.0.0.1:8000`:

**1. Discovery** — a real, headed browser, LLM-driven run that opens a new sub-account for an
existing member and reaches the confirmation screen:

```bash
python cli.py run \
  --goal "Open a new savings sub-account for member 10001 with a $100 initial deposit and reach the confirmation screen." \
  --target-url http://127.0.0.1:8000/ \
  --capability-id open-member-subaccount \
  --description "Opens a new sub-account for an existing member and reaches the confirmation screen." \
  --param member_id=10001 --param account_type=savings --param initial_deposit=100
```

This writes a full step log + screenshots to `evidence/discovery_<timestamp>/`, and on success
saves `artifacts/open-member-subaccount@1.0.0.json`.

**2. Replay** — the same capability, deterministically, no LLM:

```bash
python cli.py replay \
  --artifact-path artifacts/open-member-subaccount@1.0.0.json \
  --param member_id=10001 --param account_type=savings --param initial_deposit=100
```

**3. Replay hitting a business outcome** (not a crash — member 99999 doesn't exist):

```bash
python cli.py replay \
  --artifact-path artifacts/open-member-subaccount@1.0.0.json \
  --param member_id=99999 --param account_type=savings --param initial_deposit=100
```

Every replay run writes its result (`success` / `business_outcome` / `hard_failure`), a
screenshot, and the strategy log (which locator candidate actually resolved each step) to
`evidence/replay_<timestamp>_<capability>/`.

**4. Human escalation demo** — force a risky-action confirmation prompt in the terminal:
the "Confirm & Open Account" click in the discovery run above is classified `confirm` by policy
(see `guardrails/policy.py`) and will pause and ask you to approve it right there in the terminal
before continuing — that's the handoff mechanism, exercised for real, not mocked.

**5. A second capability — Transfer Funds.** This is the flagship risky/irreversible action (it
moves money between two members), and its "Confirm Transfer" step is guardrail-gated the same
way. Discovery:

```bash
python cli.py run \
  --goal "Transfer $50 from member 10001's checking account to member 20001's checking account and reach the confirmation screen." \
  --target-url http://127.0.0.1:8000/ \
  --capability-id transfer-funds \
  --description "Transfers funds from one member's account to another member's account." \
  --param member_id=10001 --param from_account=checking --param to_member_id=20001 --param to_account=checking --param amount=50
```

Then replay it, including against a nonexistent recipient (`--param to_member_id=88888`) or an
amount larger than the source balance (`--param amount=999999`) to see the
`recipient_not_found` / `insufficient_funds` business outcomes reported cleanly instead of a crash:

```bash
python cli.py replay \
  --artifact-path artifacts/transfer-funds@1.0.0.json \
  --param member_id=10001 --param from_account=checking --param to_member_id=88888 --param to_account=checking --param amount=50
```

## Running without live services

`replay --artifact-path ... --param ...` needs only the mock app running, no LLM/API key.
The full test suite also runs without an API key (it exercises the replay engine directly, plus
pure-logic tests for guardrails/parameterization):

```bash
python -m playwright install chromium  # if not already installed
uvicorn mock_app.app:app --port 8000 &
python -m pytest tests/ -v
```

## Notes

- The mock member data (search `10001`, `10002`, `20001`, or a nonexistent id like `99999`) is
  defined in `mock_app/data.py` — it deliberately encodes a happy path, a locked/permission-denied
  account, a session-interstitial account, and a not-found case, to exercise the business-outcome
  and recoverable-condition paths in the replay engine.
- Beyond the two recorded capabilities (open a sub-account, transfer funds), the mock app also
  has a transaction-history view and a contact-info edit flow, for a more realistic surface —
  those aren't recorded as artifacts in this submission, but the same discovery/replay path works
  against them unchanged.
- Discovery defaults to a **headed** browser (`--headless` to change) so you can watch the agent work.
