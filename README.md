# Teller AI - Computer-Use Automation System

A goal goes in. An LLM drives a real browser to complete it once. The run is saved as a typed, versioned
capability artifact. That artifact then replays deterministically, with no model in the loop, and reports a
success, a business outcome, or a debuggable failure. A human can take over the live session during discovery.

> The model discovers. The artifact becomes the reusable capability. Deterministic replay executes it.

The design write-up is [REPORT.md](REPORT.md). Recorded runs are indexed in [evidence/README.md](evidence/README.md).

## Repo map

| Path | What it is |
|---|---|
| `cli.py` | Entry points: `run` (discover and record), `replay` (by path), `capabilities` and `invoke` (the catalog, by name, no model), `approve` (draft to approved), `task` (plain English, needs Ollama to match). |
| `agent/` | Discovery: `perception.py` (accessibility-style snapshot of the page and its iframes), `loop.py` (the Claude loop, stopping conditions, completion checks), `executor.py` (runs each tool call through the guardrails), `llm.py` (tool schemas, prompt). |
| `artifact/` | `schema.py` (the capability contract), `recorder.py` (run to artifact), `store.py`, `annotations.py` (reviewer-declared business outcomes, recoverable conditions, commit verification), `grounding.py` and `pagetext.py` (helpers). |
| `replay/` | `executor.py` (deterministic replay), `locators.py` (ranked target resolution), `outcomes.py` (the result types). No model. |
| `router/` | `catalog.py` (capabilities built from `artifacts/`), `proposer.py` (local Llama via Ollama), `validator.py` (the deterministic gate), `router.py`. Optional. |
| `guardrails/` | `allowlist.py` with `config/allowlist.json`, `policy.py` (risk), `redact.py`. Used by discovery and replay. |
| `handoff/` | `session.py` (control-transfer state machine), `gesture.py` (detects and captures a human's live actions), `remote.py` (the loopback-only debugging port and the DevTools link). |
| `mock_app/` | The target: a legacy-style credit-union console (FastAPI, in-memory data, table layout, no test IDs, an iframe, error pages, injectable 503s, expiring sessions). |
| `artifacts/` | Five saved capabilities, all `draft`: `fetch-account-balance`, `transfer-funds`, `open-member-subaccount`, `create-auto-loan-for`, `update-member-address`. |
| `evidence/` | Logs and screenshots from real discovery and replay runs. |
| `scripts/` | Live demos: `demo_commit_recovery.py` (ambiguous-commit recovery), `demo_session_expiry.py` (replay signs back in after the session expires), `demo_human_takeover.py` (you are the human in the loop, headed or `--headless`), and `demo_replay_escalation.py` (a replay asks you for help). |
| `tests/` | The test suite. |

## Setup

Developed and tested on Python 3.13 (macOS).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env   # then put your ANTHROPIC_API_KEY in .env
```

| Setting | Needed for | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` in `.env` (git-ignored) | discovery: `run`, and `task` when it falls back to discovery | none |
| `ANTHROPIC_MODEL` | the discovery model | `claude-sonnet-5` |
| [Ollama](https://ollama.com) with `ollama pull llama3.1:8b` | only the plain-English router (`task`) | `OLLAMA_URL=http://localhost:11434`, `ROUTER_MODEL=llama3.1:8b` |
| `config/allowlist.json` | both phases: allowed hosts, path prefixes, action types | `127.0.0.1` and `localhost` |
| `CONSOLE_USERNAME`, `CONSOLE_PASSWORD` | replay signing back in if the app's session expires (the mock bank's demo login is in `.env.example`) | none |
| `DEMO_SLOW_MO_MS` | pace of the demo scripts in `scripts/` | `800` (commit recovery), `700` (session expiry), `600` (takeover and replay escalation) |
| `REMOTE_DEBUGGING` | set to `off` to keep the browser's loopback debugging port closed | on |

Ollama is optional. The core path (discover, save, replay) never uses it, and a saved capability can be run by
name with `capabilities` and `invoke` with no model at all.

## Demo path

The commands run against the mock bank at `http://127.0.0.1:8000`. `run` and `task` (including the replay `task` triggers) open a visible browser
by default; `replay` and `invoke` are headless unless given `--no-headless`. The repo already contains five recorded artifacts, so you can start at step 4 without an
API key.

**1. Start the mock bank** in its own terminal:

```bash
uvicorn mock_app.app:app --port 8000
```

**2. Run the agent on a goal.** A real Claude-driven run. It pauses at the risky "Confirm Transfer" click and asks
for approval (on-page banner or terminal). Its typed inputs come from the goal itself, so nothing is passed with
`--param`. This overwrites `artifacts/transfer-funds@1.0.0.json`.

```bash
python cli.py run \
  --goal 'Transfer $30 from Checking for member 10001 to Checking for member 20001 and reach the transfer confirmation.' \
  --capability-id transfer-funds \
  --description "Transfer funds between two members' accounts and reach the confirmation screen."
```

**3. Approve it.** This is the reviewer step, after reading the artifact. A draft asks a human before every risky
click. An approved artifact runs them unattended and lists them in the result.

```bash
python cli.py approve --artifact-path artifacts/transfer-funds@1.0.0.json
```

**4. Replay it with different values.** No model is involved. The input names are the ones in the artifact; list
them with `python cli.py capabilities`. A replay of a draft with a risky step needs a person (run it headed and use
the banner) or it is refused before any step runs.

```bash
python cli.py replay --artifact-path artifacts/transfer-funds@1.0.0.json \
  --param from_member_id=12345 --param from_account_type=Savings \
  --param to_member_id=20001 --param to_account_type=Checking --param amount=40 \
  --no-headless --slow-mo-ms 800
```

**5. Or run it from the catalog, by name.** The latest version runs. Missing, unknown, non-numeric, or
not-a-dropdown-choice inputs are refused before a browser opens. An approved capability with an irreversible step asks you to confirm first (`--yes` skips it).

```bash
python cli.py capabilities
python cli.py invoke --capability transfer-funds \
  --param from_member_id=12345 --param from_account_type=Savings \
  --param to_member_id=20001 --param to_account_type=Checking --param amount=40
```

**6. A business outcome, not a crash.** Member 99999 does not exist, and the app answers the same way on every
screen, so every capability reports `business_outcome: member_not_found`:

```bash
python cli.py invoke --yes --capability transfer-funds \
  --param from_member_id=99999 --param from_account_type=Checking \
  --param to_member_id=20001 --param to_account_type=Checking --param amount=25
```

**7. Ambiguous commits, live and in slow motion.** Every request carries a per-attempt run ID that the app records
next to what it wrote, and replay checks that trail. A draft artifact shows the approval banner.

```bash
# the write registered but the confirmation never appeared: recovered from the run ID, NOT retried
python scripts/demo_commit_recovery.py 1

# a failure before anything was written: verified not registered, so the whole transaction is retried once
python scripts/demo_commit_recovery.py 2
```

**8. A session that expires mid-run.** The app throws the browser onto a sign-in page. Replay signs back in from
`CONSOLE_USERNAME` and `CONSOLE_PASSWORD` and still moves the money exactly once, even when the expiry lands on the
confirm click or just after the write:

```bash
python scripts/demo_session_expiry.py 1   # before the confirm click
python scripts/demo_session_expiry.py 2   # on the confirm click, the write never happens
python scripts/demo_session_expiry.py 3   # after the write went through
```

**9. Plain English (stretch goal, needs Ollama).** A local Llama proposes a capability and arguments and a
deterministic validator decides. Anything doubtful becomes a discovery. An approved capability with an irreversible
step asks you to confirm the interpreted arguments first (`--yes` skips it).

```bash
python cli.py task --goal 'Transfer $75 from member 12345 savings to member 20001 checking.'
```

**10. Take over the live session.** During a discovery run, click or type in the browser window, or run
`touch evidence/<run_id>/PAUSE_REQUESTED` from another terminal. A banner offers Resume, Restart, or End Run. Your
actions are recorded as steps in the artifact. Whenever a person is needed, the terminal also prints a DevTools link
to the live page (for a headless run it is the only way to see and operate it); open it in Chrome on this computer, or
through an SSH tunnel to the port it names.

To try it without an API key, run the scripted takeover where you are the human. The first opens a window; the second
has none and prints the link:

```bash
python scripts/demo_human_takeover.py
python scripts/demo_human_takeover.py --headless
```

A replay can ask for help too. This one removes the capability's declared recovery for a page in memory, so replay
stops, you click Continue in the window and Resume on the banner, and it retries that step once:

```bash
python scripts/demo_replay_escalation.py
```

## Tests and running without live services

- Replay needs only the mock bank (step 1): no API key, no model.
- The test suite needs no API key and no running server. It starts its own private mock bank on a free port and
  resets its state before every test, so it never touches the copy on :8000.
- One test runs the real Llama router and skips itself when Ollama or `llama3.1:8b` is not available.

```bash
python -m pytest tests -q
```

The last run here: **50 passed in 118 s**, with Ollama and `llama3.1:8b` present, so the live router test ran.

## Mock data

`mock_app/data.py` defines the members. Use `10001` (a normal member), `20001` (the new sub-account form shows a
"session being renewed" interstitial first), `12345` (a normal member), `10002` (a compliance hold: the new
sub-account form and the transfer form answer 403 "Account Locked."), or a nonexistent ID like `99999`. The app can
also expire a session onto a sign-in page (see Test logins below). State lives in memory, so restart the app to get
back to the seed data.

## Test logins

The mock bank has one sign-in, used only when a session expires. It is a demo login for the local mock app, not a secret.

| | |
|---|---|
| User name | `teller` |
| Password | `teller-demo-pass` |
| Sign-in page | `http://127.0.0.1:8000/login` |
| For replay | `CONSOLE_USERNAME=teller` and `CONSOLE_PASSWORD=teller-demo-pass` (already in `.env.example`) |

To see the sign-in page yourself: open `http://127.0.0.1:8000` in a browser, expire every open session, then click any link.

```bash
curl -X POST http://127.0.0.1:8000/__test__/expire_sessions
```

Sign in with the login above and you land back on the page you asked for. A wrong password shows "Invalid user name or password."
To watch replay do it for you, run `python scripts/demo_session_expiry.py 1`, `2`, or `3` (step 8 above).

## Known limits

See [REPORT.md](REPORT.md) §6 and §7 for the full list. The ones a reviewer is most likely to hit:

- **Allowlist scope:** it is checked on explicit navigation and action types. A click on a link is not URL-checked.
- **Risk classification** looks at button names only. The address-change capability saves through a button that is
  not flagged, so it runs without a gate even as a draft.
- **Stuck detection** is a nudge, not a measure. It warns the model from the third arrival at the same screen, or the third identical action on an unchanged screen, where a screen is its URL plus its visible text, so it works when every screen shares one URL and does not fire while a form is being filled. A screen whose text changes every time (a clock) never matches, so it never warns. In the one recorded dead-end run it warned at step 12 and the agent gave up at step 13 of 14.
- **Human handoff reach:** a headless run prints a DevTools link for local operators. Remote handoff, from a different machine, isn't built: a remote person needs an SSH tunnel to the printed port that they set up (nothing opens one), and the loopback debugging port has no authentication (`REMOTE_DEBUGGING=off` closes it).
- **Human handoff evidence:** the recorded discovery takeover ended in restarts, and the path where a human's actions
  become artifact steps is covered by tests, not by a recording. A replay escalation is recorded (`evidence/replay_20260930T235359Z_*`), but staged: the
  capability's declared recovery was removed in memory so replay would have to ask a person.
- **Router latency:** a matched plain-English request takes a few seconds, because the model is asked twice for
  consistency. `invoke` skips it.
- **Commit verification** needs an app that records a caller-supplied ID.
- **Session expiry** is handled in replay only, for a username-and-password sign-in page it was told about. Not handled: multi-factor sign-in, discovery meeting an expired session, or an expiry on a run's last, risky click.
- Multi-tenant overrides, a desktop surface, and stability scoring are designed but not built.
