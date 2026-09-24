# REPORT

The mock target app covers five back-office actions a real servicing console would (member
lookup, open a sub-account, view transaction history, edit contact info, transfer funds), and two
of them — `open-member-subaccount` and `transfer-funds` — are recorded as real, discovered,
replayed capability artifacts (see `evidence/`). Recording a second, structurally different
capability (a cross-member money movement, versus a single-member account-opening form) against
the same schema and replay engine is deliberate: it's the cheapest way to show the system isn't
shaped around one flow.

## 1. Architecture

Single Python process, four layers, one direction of dependency:

```
agent/  (discovery: LLM observe → decide → act)
   |  produces
   v
artifact/  (schema, recorder, store)
   |  consumes
   v
replay/  (deterministic execution, no LLM)

guardrails/ and handoff/ are cross-cutting: both agent/executor.py (discovery) and
replay/executor.py (production replay) call through them, not around them.
```

**Why one process, not services.** Nothing here needs independent scaling or deployment yet —
discovery is a human-triggered, occasional event; replay is the hot path, but it's CPU/IO-bound
on a single browser tab, not a fan-out problem. A queue or service boundary would be premature
infrastructure (explicitly discouraged in the brief) before there's a second consumer. The
boundary that *does* matter — discovery vs. replay never sharing a code path for deciding what
to do — is enforced by structure: `replay/executor.py` has no import of `agent/llm.py`, full stop.

**Perception is accessibility-tree-first, not screenshot/coordinate-first.** `agent/perception.py`
walks the live DOM (main frame + every nested iframe) computing a simplified accessible name/role
for each interactive element and each short text leaf, and hands the LLM a flat, ref-addressable
list (`[f0e3] button "Search"`), not a screenshot. This was the single highest-leverage decision
in the project: it means every action the model takes already carries a locator strategy that
generalizes to a no-DOM environment (the same computation — role, name, structural path — applies
to a desktop app's accessibility tree, see §4), and it means the *discovery* run and the *artifact*
share one representation of "what is this element" instead of needing a translation step between
"what the model clicked" and "what gets replayed."

Elements are listed to the model in **reading order** (top-to-bottom, left-to-right within a
row-bucketed y), not DOM-query order. This surfaced a real ambiguity while building the transfer
form: the walker finds every interactive element before any text leaf, so the "from account" and
"to account" `<select>` elements (identical options, so identical accessible names) landed back to
back in the snapshot with their "From Account:"/"To Account:" labels many lines below — nothing
would have stopped the model from filling the wrong one. Sorting by position instead means a
label always immediately precedes its field, the way a person reading the table actually would.

**Guardrails are a chokepoint, not a convention.** Every tool call in `agent/executor.py` and
every step in `replay/executor.py` passes through `guardrails/policy.py` before it touches
Playwright. Nothing downstream trusts the LLM's judgment about what's safe.

**Trade-off accepted:** the recorder only converts the *successful* path of a discovery run into
steps (failed/backtracked attempts are dropped, see `artifact/recorder.py`). For a single linear
goal this produces a clean artifact; a discovery run with heavy backtracking would need a smarter
pruning pass. Documented as a known simplification, not hidden.

## 2. Artifact schema

`artifact/schema.py`. The design choice I'd defend hardest: **locator targeting is a ranked list
of independent strategies, not one selector.**

```
Target { candidates: [LocatorCandidate], frame_chain: [[LocatorCandidate]] }
LocatorCandidate.strategy ∈ { role_name, text, css_path, coordinates }
```

Replay tries `role_name` → `text` → `css_path` → `coordinates` in order and *records which one
hit* (`strategy_log` in the replay result). On a no-test-id legacy surface, no single strategy is
trustworthy everywhere; a ranked, falsifiable chain is. This is not hypothetical — building the
mock app's "Initial Deposit" field (no label, no placeholder, no aria attributes at all) surfaced
a real bug: my first cut generated a `text` candidate from a heuristic "nearest table-cell label"
guess, and `get_by_text()` on that guess matched the *label cell itself*, not the input — silently
filling the wrong element. Fixed by tracking name provenance (`name_source` in
`agent/perception.py`) and only emitting `role_name`/`text` candidates when the name is something
the browser's own accessible-name computation would actually produce; a borrowed guess falls
straight to `css_path`/`coordinates`. That fix, and the test that would have caught it
(`tests/test_replay_integration.py`), are the clearest evidence of "reasoning about robustness"
this project has.

`frame_chain` is a list of ranked candidates for each `<iframe>` to descend through before
resolving the target — needed because the mock app's balance panel is deliberately in an iframe,
and legacy apps frequently nest content this way.

One more thing a real run surfaced, in the other direction — a case where the underlying tooling
was more robust than I'd assumed. The `select` tool's description originally told the model to
supply an option's *value*, not its label, but the snapshot only ever shows labels (values are
rarely meaningful on a legacy `<select>` in the first place). A discovery run recorded
`value_template: "Transfer from Checking"` — the visible label — for a `<select>` whose real HTML
value is `"checking"`. It replayed correctly anyway: Playwright's `select_option()` matches by
value first and falls back to label. That's the right behavior for this environment, so the fix
was to correct the tool's description to match reality rather than "fix" behavior that was already
right — asserting a contract without checking what the library actually does is its own kind of
locator bug.

**Inputs/outputs are typed and named independently of the step list** — `InputParam`/`OutputField`
— so a calling agent sees a function contract (`open-member-subaccount(member_id, account_type,
initial_deposit) -> {confirmation_number}`), not a transcript. Concrete values never get baked
into steps: fill/select/navigate values are `str.format` templates (`"{member_id}"`), substituted
at replay time. One direct consequence for safety: **an artifact cannot contain PII by
construction** — only the parameter *names* are recorded, never the values a given discovery run
happened to use.

**`business_outcomes` and `recoverable_conditions` are first-class, declarative artifact fields**
(`BusinessOutcomeSignature`, `RecoverableCondition`), not code. Each is a `Checkpoint` (a detection
condition) plus, for recoverable conditions, one bounded recovery action. A discovery run only
ever walks the happy path — it cannot discover "what does a locked account look like." These are
added by a reviewer who knows the target app (`artifact/annotations.py` stands in for that review
step) before an artifact is trusted for unattended replay. Making this a schema field rather than
an `if` statement in the replay engine is what makes the artifact "reviewable": a non-engineer can
read the JSON and see exactly which error branches this capability knows about.

`status: draft | approved` and `overall_risk` are on the artifact for the same reason — the schema
anticipates a review gate even though this project doesn't build the workflow around it (§7).

## 3. Determinism & error handling

Replay (`replay/executor.py`) never calls the LLM. Before each step it checks, in order:

1. **Recoverable condition** — if the current page matches a declared `detect` checkpoint, run
   the one declared recovery action (e.g. click "Continue" on a known session-renewal
   interstitial), then proceed into the step as normal. One attempt per occurrence, never a retry
   loop.
2. **Business outcome** — if the current page matches a declared signature (member not found,
   account locked, deposit below minimum), stop immediately and return `kind="business_outcome"`
   with a name and description. This is a legitimate result, returned to the caller, not raised as
   an error.
3. **The step itself** — resolve the target via the locator fallback chain, act, and on any
   unhandled exception (timeout, detached element, navigation failure) or a failed inline/final
   checkpoint, return `kind="hard_failure"` with the step index, action, what was expected, and
   what was actually observed (`FailureDetail`).

**Checkpoints never assert on values generated during discovery.** The final checkpoint for
`open-member-subaccount` is `url_contains: /new-subaccount/confirm`, not a match on the specific
confirmation number that discovery happened to see — that number is auto-generated and will
differ on every replay. This mattered enough to call out explicitly because it's an easy, subtle
way to make an artifact spuriously flaky.

**Verified, not asserted:** `tests/test_replay_integration.py` runs the real replay engine against
the real mock app for all three business outcomes, the recoverable-condition path, and the happy
path — five passing tests, no LLM required. `evidence/` additionally holds output from a genuine
discovery run and genuine replay runs (see README §Demo path).

UI drift (the brief's secondary concern here, since this environment is explicitly stable-but-
error-prone) is handled the same way as any other resolution failure: if every locator candidate
in a step fails, that's a `hard_failure` naming exactly which step and which candidates were
tried — enough to tell a drifted-selector failure apart from a genuine runtime error at a glance,
and the `strategy_log`'s per-step "which candidate actually hit" is designed to be watched over
time as an early drift signal, ahead of a full failure.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is exactly the boundary already in the code:
`agent/perception.py` (build a `Target` from whatever the current surface is) and
`replay/locators.py` (resolve a `Target` back into an action). Nothing above that boundary
— the artifact schema, the recorder, the replay control flow — knows it's looking at a browser.
For a legacy web app (frames, tables, no test ids), this project's mock app already exercises the
hard case. For a desktop app, the same `LocatorStrategy` vocabulary maps directly: `role_name`
onto the OS accessibility tree (Win32 UIA / macOS AX — the glossary is right that these exist and
are usually more stable than raw view hierarchies), `css_path` onto a structural analog (e.g. a
control's index path in its window), `coordinates` staying exactly as-is. `frame_chain` generalizes
to "which window/pane to descend into." The `Target`/`Checkpoint`/`Step` schema would not need to
change shape; only `agent/perception.py` and `replay/locators.py` would need a second
implementation behind the same interface.

**Multi-tenant reuse.** `TargetApp.app_id` (the vendor product) is already separated from
`tenant_id`/`base_url` (the specific deployment) in the schema, precisely so an artifact recorded
against one tenant's instance is addressable independently of *which* tenant it's replayed
against. The mechanism I'd build next (a stretch goal I didn't implement, see §7): a **base
artifact per `app_id`**, with **per-tenant override records** — same `capability_id`, keyed by
`(app_id, tenant_id)` — that only need to specify the `Target`s that differ (a rebranded button
label, a moved field) rather than a full re-recording. Route resolution ("canonicalize
`/item/12345` to `/item/:id`") is the same idea applied to `value_template`/checkpoint URLs, and
is listed as a stretch goal for a reason: it only pays off once there's a second tenant to
validate against, and I'd rather ship the override mechanism cleanly than half-build
canonicalization against a single tenant with nothing to generalize from.

**Drift detection across tenants:** the `strategy_log` (which candidate resolved each step) is
the raw signal. If tenant B's replay of an artifact recorded on tenant A starts consistently
falling through `role_name`→`css_path` on one step, that step is exactly where a per-tenant
override belongs — the artifact tells you where it's straining before it breaks outright.

## 5. Escalation & handoff

Escalation isn't only agent-initiated. Four triggers route through the same mechanism
(`handoff/session.py`): the agent reporting it's stuck (`give_up`), a risky/irreversible action
needing sign-off (`guardrails/policy.py` classifying a click as `confirm` — e.g. "Confirm & Open
Account"), a hard replay failure (`--escalate-on-failure`), and — because a human shouldn't have
to wait for the model to ask — a human requesting control at any point of their own choosing.
That last one is a plain file check (`agent/loop.py`): touching `PAUSE_REQUESTED` in the run's
evidence directory from another terminal pauses the run before its *next* step, whether or not
the model was about to do anything noteworthy. All four reach the exact same state machine and
the exact same take-control/resume-or-stop decision — "who's allowed to interrupt" isn't a special
case bolted onto escalation, it's a fourth way into the same door.

**A human can grab control without the model's permission, by just clicking.** The file-touch
signal above still requires declaring intent through a side channel before acting. A more direct
version (`handoff/gesture.py`) detects a real click or keypress in the live browser itself and
pauses before the next step — no separate command needed. This can't work by inspecting the DOM
event: a Playwright-dispatched click and a real human click are both `isTrusted: true`, by
design, since that's what lets automation exercise the same code paths a real user's input would.
What's detectable instead is *when the automation itself isn't currently mid-action* — a flag
toggled around every dispatched click/fill/select (`agent/executor.py`), checked by an injected
listener before it reports input back to Python. Handing control back is a button injected onto
the page itself ("Resume Automation"), not another terminal prompt.

The risky-action confirm gate got the same treatment, prompted directly by using it: the
"Approve this action?" prompt only ever showed up in the terminal, invisible to someone watching
the browser — the whole point of running headed. `_CliOperator.confirm()` now shows an on-page
Approve/Deny banner and races it against the terminal prompt (a background thread blocked on
`input()`, touching no Playwright API — a second thread calling into Playwright's sync API breaks
it, learned the hard way while chasing the settle-delay red herring below), and answers with
whichever channel resolves first. The abandoned side (usually the terminal thread, since clicking
is faster than alt-tabbing) is a documented, accepted rough edge — Python has no clean way to
cancel a blocked `input()` call.

Getting the test for this right took a wrong turn worth recording. The first version was flaky —
roughly half the runs failed to detect an "untracked" click — and the first diagnosis reached for
was a CDP-level race between the flag-setting call and the input-dispatch call, different
protocol domains with independent latency. A settle delay appeared to fix it. It didn't: the real
cause was the test's own click coordinates landing on a real link in the mock app's compact
layout, triggering an actual navigation that occasionally destroyed the page's JS context before
the detection signal's round-trip to Python completed — a test-design bug, not a mechanism bug.
Switching the test to inert targets (`about:blank`) fixed it outright; the settle delay stayed in
as cheap, plausible insurance, but the comment claiming it was "confirmed" necessary was itself
wrong and got corrected in place rather than deleted. Worth keeping visible: the first plausible-
sounding explanation for a flaky test isn't automatically the right one.

**The clearest bug in this whole project only showed up under real usage, not testing.** Every
automated check above passed. Then an actual person ran a real discovery session, clicked "Resume
Automation," and the pause immediately came right back — 22 times in under 25 seconds by the time
it showed up in `handoff_log.jsonl`. Cause: clicking Resume is itself a `mousedown`, and the
listener didn't know to ignore clicks on its own banner — so resuming control re-triggered "human
wants control" against itself, forever. The existing test suite hadn't caught this because its
simulated resume-click used JS's `element.click()`, which — per spec — fires only a `click` event
and never `mousedown`; it physically could not have exercised this path. The fix
(`handoff/gesture.py`) excludes events whose target is inside `#__pw_pause_banner`, and the new
regression test dispatches a *real* mouse click at the button's actual coordinates instead, which
was confirmed to fail without the fix and pass with it. The lesson generalizes past this one bug:
a synthetic `.click()` and a dispatched mouse click are not interchangeable test doubles for
anything that cares about the event sequence, and no amount of unit/integration coverage
substitutes for a real person actually using the thing — which is the whole premise of why this
system's discovery run has to be genuine in the first place, not just this one feature of it.

**The control-transfer model is a small state machine** — `AGENT_ACTIVE → ESCALATED →
HUMAN_ACTIVE → AGENT_ACTIVE` — gating a single Playwright `Page`. The reason the same *live*
session is what gets handed over, not a fresh one: discovery runs **headed** by default. The
browser window Playwright has been driving is already visible and clickable; escalating doesn't
spin up a co-browsing console, it stops the automation from issuing further commands and prints
context (goal, step, URL, a screenshot saved to `evidence/`) to the terminal. A human acts
directly in that same window, reports what they did in one line, and resumes — the state machine
is what makes "who's in control" unambiguous rather than a matter of convention, and it's what a
real operator console would sit on top of.

**Resume is the human's decision, not automatic — found by actually running it.** The first
version of this always fed the human's note back to the model and let it retry. A live discovery
run against a goal with a nonexistent recipient (`transfer $200 from member 10001 to member
12345`) exposed why that's wrong: the model escalated as stuck, a human confirmed member 12345
genuinely doesn't exist, and the model — now told exactly that — escalated again with the same
conclusion, then again, fifteen times, until the step budget ran out. Nothing about the situation
was fixable by looping; there was nothing for the human to *do*. `HandoffRecord.resume` now makes
this an explicit choice on every "stuck" escalation (`handoff/session.py`,
`agent/loop.py`'s `give_up` handling): the human either fixes something and says try again, or
confirms it's unrecoverable and the run ends there with a `give_up` outcome instead of a spurious
`max_steps`. This matches the brief's own framing more literally than the first version did — "let
them take control... so the run can resume **or complete**" — resume was never supposed to be the
only option.

**"Call give_up when stuck" isn't reliable as a prompt instruction alone.** Tested against a
structurally dead end (a locked member — a 403 with nothing but a "back" link, no unlock path
anywhere in the UI), the model never called `give_up` at all: every individual click still
"succeeded" from its perspective, so it just kept trying other tabs and features — sub-account,
transfer, edit, even the same member searched from a different tab — for the full 20-step budget,
never concluding the goal itself was blocked. The fix is structural, not another sentence in the
prompt: `agent/loop.py` now counts revisits to the same URL within a run and attaches a direct
nudge to the tool result once a page repeats ("you have reached this page N times; if there's no
path forward, call give_up now") rather than trusting the model to notice a loop on its own. Same
goal, same locked member, after the fix: `give_up` on step 9 instead of `max_steps` on step 20 —
and the resulting escalation exercised the full cycle for real: human says try again, agent
re-confirms it's still locked and gives up a second time, human then ends the run. This is the
same instinct behind guardrails being a chokepoint rather than a convention (§6) applied to the
model's own judgment about when it's stuck.

**What's mocked, deliberately:** the operator surface is a blocking CLI prompt
(`handoff/session.py::_CliOperator`), not a graphical console (explicitly out of scope per the
brief) — and the human's action log is a self-reported one-liner, not an automatically captured
diff of what changed. A real system would want CDP-level action recording here; the interface
(`HandoffSession._operator`) is already swappable for exactly that.

## 6. Safety

Three layers, `guardrails/`:

- **`allowlist.py`** — an explicit, externally-editable domain/path/action-type allowlist
  (`config/allowlist.json`). Enforced in both the discovery executor and the replay engine, at
  the same call site as the risk check — an action that isn't in the allowlist is refused before
  it reaches Playwright, not caught after the fact.
- **`policy.py`** — classifies every click as `safe` or `confirm` (currently a transparent
  keyword heuristic on the target's name: confirm/delete/close account/withdraw funds). This is
  intentionally legible rather than clever: a reviewer can read the pattern list and know exactly
  what triggers a human gate. The natural evolution (§7) is per-step risk annotated by a human
  reviewer at artifact-approval time, using this heuristic as the draft default. The pattern list
  is narrower than it started: a bare `\btransfer\b` looked reasonable on paper but, in a real
  discovery run, flagged "Review Transfer" — a safe, reversible step in a two-step review-then-
  confirm flow — as needing human sign-off, producing an unplanned confirmation prompt with no
  real decision behind it. Fixed by requiring patterns to name the *commit* action ("confirm ...")
  rather than the domain verb; `tests/test_guardrails.py` now pins both directions (the real
  commit button still gates, the review step doesn't).
- **`redact.py`** — two layers: field-name matching (password/SSN/card/etc. redacted regardless
  of content) and pattern matching (SSN-shaped, card-shaped, JWT-shaped strings scrubbed even out
  of free text like an LLM rationale). Applied to the discovery run log before it's written to
  `evidence/`. As noted in §2, the *artifact* itself is parameterized by construction and never
  contains literal values at all — redaction's real job is the run log, which does capture what a
  specific run actually did.

**Limits, stated plainly:** the risk classifier is a heuristic over element names, not a semantic
understanding of consequence — a mislabeled irreversible action ("Proceed" instead of "Confirm")
would not be caught today. Redaction is pattern-based and will miss anything that doesn't match a
known shape. Neither is a substitute for a human reviewer approving an artifact before it's
trusted with unattended replay in production — which is exactly why `status: draft/approved`
exists on the schema even though the approval workflow itself isn't built here.

## 7. Cuts

Built for real: the full discovery loop against a live, deliberately hostile surface; the
artifact schema with ranked locators and frame traversal; deterministic replay with a genuine
three-way outcome split, verified against all three declared business outcomes and the one
recoverable condition; allowlist + risk-gated guardrails; redaction; and a real (not TODO'd)
human control-transfer state machine.

Cut, on purpose:
- **Graphical operator console.** Explicitly out of scope in the brief; a CLI prompt exercises
  the real mechanism underneath.
- **Automatic capture of what a human did during handoff.** Self-reported today; the swap point
  for CDP-based action recording is isolated (`HandoffSession._operator`).
- **Multi-tenant override records and route canonicalization.** Schema fields (`app_id` vs.
  `tenant_id`) anticipate this; the override mechanism itself isn't built, per the brief's
  explicit "don't build scaling infra prematurely."
- **Recorder pruning of backtracked discovery paths.** Only successful steps are kept; a
  heavily-corrected discovery run would need smarter reconciliation than "keep what worked."
- **Approval workflow around `status: draft/approved`.** The field exists; nothing gates replay
  on it yet — every artifact is replayable as soon as it's saved.
- **Confidence scoring / multi-run stability signal.** Would be straightforward to add on top of
  `strategy_log` (candidate-resolution drift across N replays is already the right raw signal);
  not built to keep depth on the load-bearing pieces instead.

Next, with more time: the multi-tenant override mechanism first (it's the one with the clearest
path from the current schema and the most direct line to the environment described in the brief),
then the approval gate, then CDP-based handoff action capture.
