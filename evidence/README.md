# Evidence index

Each folder is one run. Discovery runs hold `steps.jsonl` (what the agent did and why), `run_summary.json`, `final_state.png`, and, when a person was asked something (a risky-step approval or a takeover), `handoff_log.jsonl` and `escalation_*.png`. Replay runs hold `replay_input.json`, `replay_result.json` (with the per-step locator strategy used) and a screenshot of the end state.

Replays below ran with an in-memory copy of each artifact set to `approved`, so the risky step ran unattended and is listed in `unattended_risky_steps`. The saved files in `artifacts/` are still `draft`.

## How to read these honestly

- Discovery runs 170932Z to 195003Z were run headless with the risky-step approval answered automatically (`yes y` piped to the terminal), not clicked by a person. Their `handoff_log.jsonl` approvals are therefore scripted.
- The replay runs use an in-memory copy of each artifact set to `approved`; the saved files are still `draft`.
- Two runs are staged on purpose and say so below: the replay escalation, and the stuck-detection dead end whose final answer was piped in.
- File paths inside the JSON were made repo-relative (they originally held an absolute path under a home directory). Screenshots show only the mock bank's fictional members and data.

## Discovery (real Claude runs against the mock bank)

| Run | Shows |
|---|---|
| `discovery_20260930T170932Z` | fetch-account-balance recorded |
| `discovery_20260930T170943Z` | update-member-address recorded |
| `discovery_20260930T194922Z` | open-member-subaccount recorded (the dropdown's choices are saved as the input's `allowed_values`). Its `handoff_log.jsonl` is the approval of the risky Confirm click |
| `discovery_20260930T194941Z` | create-auto-loan-for recorded |
| `discovery_20260930T195003Z` | transfer-funds recorded |
| `discovery_20261001T000022Z` | **stuck detection and escalation with the real agent**: a locked member (10002) is a dead end for opening a sub-account. The agent wandered between Accounts, Sub-Accounts and Search, the screen watcher warned at step 12 ("come back to this same screen 3 times"), the agent called `give_up` at step 13, and a person ended the run (the answer was piped in on stdin, not typed live). Outcome `give_up`; `handoff_log.jsonl` has the `stuck` request and `human_terminated` |
| `discovery_20260930T163455Z` | human in the loop: four takeovers (three by clicking in the window, one by the pause file), each ended with Restart, then the run completed. It shows detection, context and handback (`handoff_log.jsonl`, `escalation_*.png`); the resume-with-captured-steps path is covered by tests, not by a recording |

## Replay

| Run | Result | Shows |
|---|---|---|
| `replay_20260930T195342Z_*_fetch-account-balance` | success | read-only capability, outputs returned |
| `replay_20260930T195344Z_*_update-member-address` | success | free-text input, success text checked |
| `replay_20260930T195347Z_*_open-member-subaccount` | success | write capability, generated account id returned |
| `replay_20260930T195350Z_*_create-auto-loan-for` | success | write capability, typed numeric inputs |
| `replay_20260930T195354Z_*_transfer-funds` | success | two-member transfer, confirmation number returned |
| `replay_20260930T195358Z_*_fetch-account-balance` | business_outcome | unknown member 99999, declared once for the app |
| `replay_20260930T195400Z_*_transfer-funds` | business_outcome | same unknown member on a different capability |
| `replay_20260930T195402Z_*_transfer-funds` | business_outcome | insufficient funds |
| `replay_20260930T195405Z_*_transfer-funds` | business_outcome | account locked (compliance hold) |
| `replay_20260930T195408Z_*_transfer-funds` | success, recovered | fault after the write: the run id found in the history proves it committed, so no retry |
| `replay_20260930T195417Z_*_transfer-funds` | success, 2 attempts | fault before the write: nothing registered, one whole-transaction retry, exactly one transfer |
| `replay_20260930T195430Z_*_transfer-funds` | hard_failure | a recorded target no longer on the page: step, candidates tried, and what the page showed |
| `replay_20260930T235359Z_*_open-member-subaccount` | success after a human asked for help | **replay escalation, run by a person in a visible window** (`scripts/demo_replay_escalation.py`). The capability's declared recovery for the session-renewal page was removed in memory to stage it, so replay failed step 5, paused and asked. The person clicked Continue and Resume; replay retried that same step once and finished. `handoff_log.jsonl` holds the request (page text, DevTools link) and the outcome `human_completed` with `captured_actions: []` (replay never learns from a human); `escalation_0.png` is the page it was stuck on. The link in the log is for a browser that has since closed. |
