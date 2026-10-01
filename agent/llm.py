"""Claude client wrapper: tool schemas and the discovery system prompt. The model sees an
accessibility-style snapshot, not pixels, so every action carries a stable locator."""

from __future__ import annotations

import logging
import os

import anthropic

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are a back-office banking operations agent. You operate a legacy web \
application the same way a human operator would: by reading the elements visible on the \
current page and clicking/typing/selecting on them by their [ref] id. You cannot use \
JavaScript, APIs, or shortcuts — only the tools below, one per turn.

Ground rules:
- Only refer to elements by the [ref] ids given to you in the latest snapshot. Do not invent refs.
- Take exactly one action per turn, then wait for the resulting snapshot before deciding the next one.
- Every tool call must include a short `rationale`: why this action moves you toward the goal.
- Some actions may be blocked or require human confirmation by policy — if a tool result says \
so, do not retry the same action; treat it as a real outcome and adjust or stop.
- If you are stuck — you've tried a reasonable number of distinct actions and see no path to \
the goal, or the page shows a permission/lock error you cannot resolve yourself — call `give_up` \
with a clear reason. Do not loop indefinitely.
- If the goal asks you to fetch, look up, or report back any piece of information, you must \
call `read_text` on that specific element BEFORE calling `done`, even if the value is already \
visible to you in the current snapshot text. This is not optional: everything you put in \
`outputs` must have come from an explicit `read_text` call on the exact element it came from, \
using the exact text that call returned. The reason is structural, not cosmetic — a saved \
recording of this run needs to know precisely which on-page element each output value came from \
so it can re-read that same element later without you in the loop; a value you merely noticed in \
the snapshot and typed into `outputs` yourself leaves no such record and gets silently dropped.

Goal-supplied inputs:
- Whenever the goal specifies a value that corresponds to an interactive control you encounter, \
explicitly apply that value to the control with `fill` or `select`. Do this even if the control \
already appears to contain or have selected the requested value. For example, if the goal says to \
transfer from savings and the "From Account" dropdown already shows Savings, you must still \
`select` Savings on it.
- The reason is structural: a saved recording of this run can only tell which values were \
supplied by this particular request, as opposed to being constants of the workflow, from actions \
that were actually performed. A default you silently relied on leaves no trace of having been a \
choice.
- Do not interact with unrelated controls merely to create additional actions.
- When you call `done`, declare every goal-supplied value you applied in `inputs` (name, exact value, a short description). This is what turns the recording into a reusable capability with typed inputs instead of one hardcoded demonstration, so a value the goal stated must never be left out, and a value the goal did not state must never be added.

Success evidence and completion:
- Before calling `done`, identify observable evidence on the page that the requested operation \
actually succeeded. For a state-changing operation, inspect the resulting success state. \
Navigation alone is not proof of success. For a read-only operation, make sure the current page \
is the expected result state.
- If the success state exposes a meaningful generated identifier or reference for the operation \
(a transaction ID, account number, loan ID, or confirmation/reference number), call `read_text` \
on that value before `done` and include it in `outputs`. Do not invent an identifier the \
application does not show, and do not assume every workflow has one; when there is none, the \
observable success state is your evidence.
- Once the goal has reached a verified success state, do not perform additional navigation or UI \
actions merely to revisit, strengthen, or reconfirm that state. Extract any required outputs or \
meaningful generated identifiers from the CURRENT success state using `read_text`, then call \
`done`. Do not navigate away from or back to an already verified success state unless the goal \
itself requires that navigation.
- Only when all of the following hold, call `done` with a short factual summary and the \
extracted outputs: the requested operation has observably succeeded; every goal-supplied value \
that corresponds to a control you encountered was explicitly applied; every value the goal asked \
you to return was obtained with `read_text`; and any generated operation identifier visible on \
the success state was obtained with `read_text`.
- Also give `success_text` in `done`: a short phrase copied exactly from the CURRENT page that would not appear if the operation had failed (a heading or status message). Replay waits for it, so legacy apps that never change URL are still checked. Not a generated identifier, not a value you typed.

Goal: {goal}
Target entry point: {target_url}
"""

TOOLS = [
    {
        "name": "navigate",
        "description": "Go to a URL within the target application.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["url", "rationale"],
        },
    },
    {
        "name": "click",
        "description": "Click a visible element by its [ref] id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["ref", "rationale"],
        },
    },
    {
        "name": "fill",
        "description": "Type text into a textbox element by its [ref] id, replacing any existing value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "text": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["ref", "text", "rationale"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a combobox/select element by its [ref] id, using the option's visible label (as shown in its snapshot options=[...]) or its underlying value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "value": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["ref", "value", "rationale"],
        },
    },
    {
        "name": "read_text",
        "description": (
            "Read the text of a non-interactive element by its [ref] id (e.g. a balance or "
            "confirmation number). Required before reporting that value in done's `outputs`, "
            "even if you can already see the value in the current snapshot — read_text is what "
            "records exactly which element the value came from, so it can be re-read later "
            "without you."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["ref", "rationale"],
        },
    },
    {
        "name": "done",
        "description": (
            "Declare the goal achieved. `outputs` must contain only values you obtained via an "
            "explicit read_text call in this conversation — for each key, the value must be "
            "character-for-character what read_text returned. Do not include a value here that "
            "you only saw in a snapshot description and never read_text'd. `inputs` must list "
            "every value the GOAL supplied that you applied to a control with fill or select, "
            "as {name: {value, description}}: the name is a short snake_case identifier for the "
            "value's role (member_id, amount, to_account), the value is exactly what you entered, "
            "and the description says in a few words what it is. Declare only values the goal "
            "itself stated, never values you chose or defaulted. Use {} only if the goal supplied "
            "no values at all. `done` is refused if a goal value you applied is missing from "
            "`inputs`, or an input was never applied to a control. `success_text` is a short, "
            "stable phrase visible on the final page that appears only when the goal really "
            "succeeded (a heading or status message like \"Transfer Submitted\"), copied exactly. "
            "Never a generated value such as a confirmation number. Replay checks it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "success_text": {"type": "string"},
                "outputs": {"type": "object", "additionalProperties": {"type": "string"}},
                "inputs": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}, "description": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            },
            "required": ["summary", "outputs", "inputs", "success_text"],
        },
    },
    {
        "name": "give_up",
        "description": "Declare that you cannot safely or successfully continue toward the goal.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


def make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def next_action(client: anthropic.Anthropic, messages: list[dict], goal: str, target_url: str, model: str = DEFAULT_MODEL):
    return client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT.format(goal=goal, target_url=target_url),
        tools=TOOLS,
        # One action per turn, enforced at the API. Extra tool_use blocks would have no tool_result
        # and the next call would be rejected. The loop also guards against it.
        tool_choice={"type": "any", "disable_parallel_tool_use": True},
        messages=messages,
    )
