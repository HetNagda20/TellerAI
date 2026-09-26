"""Converts a successful discovery run's step log into a saved Artifact.

Parameterization is declared, not inferred by magic: whoever kicks off the
discovery run states which concrete values stand in for which named
parameters (e.g. member_id=10001). The recorder then finds every step whose
literal value matches a declared parameter and replaces it with a
`{param_name}` template — so the artifact is reusable with different inputs,
not just a transcript of one specific run.

For a click/fill/select step's value specifically, matching is keyed on
*(value, control identity)*, not value alone — see `_bind_value_template`.
Two different controls can legitimately demonstrate the identical value
(e.g. a transfer's source and destination account both set to "Checking");
matching by value alone would collapse both onto whichever declared param
happens to be checked first, silently leaving the other undeclared. Control
identity comes from each step's own `css_path` locator candidate, which is
already positionally distinct between different controls even when their
higher-ranked role/name candidates happen to collide (see
agent/perception.py and replay/locators.py for that separate, related
problem). The same control revisited later in the run reuses its existing
binding, so a param that legitimately appears in several steps still works
exactly as before.

Checkpoints are deliberately never built from dynamically-generated values
observed during discovery (e.g. an auto-generated confirmation number) —
replay will produce a *different* value each time, so asserting on the
recorded one would make every replay fail. The final checkpoint instead
asserts on the stable URL path reached, with any parameter values in it
templated out the same way step values are.

The artifact's declared `inputs`/`outputs` are only what the recorded steps
actually consume/produce — not everything the operator happened to pass on
the CLI, or everything the model happened to mention when it called `done`.
Found the hard way: a real run recorded `account_type` as a required input
(because it was passed via --param) even though the agent never touched that
control — its default already matched the goal, so no step ever templated
`{account_type}` in. The declared contract would have silently lied about
being able to control account type on replay. Same idea for outputs: the
model's `done` call echoed several input values back as "outputs" with
nothing that actually read them off the page; keeping those in the schema
would promise a caller data replay can't reproduce.

A step's `source` ("llm" or "human_intervention", see StepLog/Step) passes
through unchanged below — this recorder has no separate code path for
human-taught steps. They arrive in `result.steps` shaped identically to any
LLM-driven step (same Target-building, same ok/risk/value fields — see
agent/executor.py's record_human_action), at whatever position in the
sequence they actually happened, so ordering, templating, and output-
matching all just work without knowing or caring who performed the step.

Sensitive data: fill/select *values* are already redacted upstream, at
StepLog-creation time (agent/executor.py calls guardrails.redact.redact_value
before a value ever reaches self.steps). What isn't covered by that is a
step's own free-text `description` (the LLM's rationale, which can echo an
observed value back verbatim) and a step's `target` locator candidates —
concretely, a `read_text` step's ROLE_NAME/TEXT candidates are built from
whatever text is actually on the page (see agent/perception.py), which is
exactly the value being read; if that happens to be sensitive, it would
otherwise be baked permanently into this *reusable, versioned* artifact as a
literal locator string. `_scrub_target` reuses the existing
guardrails.redact primitives (no new patterns, no new subsystem) to drop
such a candidate outright rather than replacing it with a fake "[REDACTED]"
string that could never resolve on replay — css_path/coordinates candidates
are always generated alongside role_name/text (see
PerceivedElement.candidates), so dropping a sensitive one never leaves a
step unresolvable.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional
from urllib.parse import urlparse

from agent.loop import DiscoveryResult
from artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    OutputField,
    ParamType,
    Step,
    Target,
    TargetApp,
)
from guardrails.redact import field_is_sensitive, redact_text

_RISK_ORDER = {"safe": 0, "confirm": 1, "blocked": 2}


def _looks_sensitive(text: str) -> bool:
    """Reuses the existing guardrails.redact primitives: either the text itself
    is shaped like PII/a secret (SSN, card number, JWT, ...), or it reads like
    the *name* of a sensitive field (rare for locator text, cheap to also
    catch). No new patterns are introduced here.
    """
    if not text:
        return False
    return field_is_sensitive(text) or redact_text(text) != text


def _scrub_target(target: Optional[Target]) -> Optional[Target]:
    """Drops any ROLE_NAME/TEXT locator candidate whose value looks sensitive,
    rather than replacing it with a redacted placeholder string that could
    never resolve on replay. css_path/coordinates candidates never embed page
    text, so they're always left untouched — a step never loses the ability
    to resolve just because one of its higher-ranked candidates was dropped.
    """
    if target is None:
        return None
    safe: list[LocatorCandidate] = []
    for c in target.candidates:
        if c.strategy in (LocatorStrategy.ROLE_NAME, LocatorStrategy.TEXT):
            text = c.value.get("name") or c.value.get("text") or ""
            if _looks_sensitive(text):
                continue
        safe.append(c)
    return Target(candidates=safe, frame_chain=target.frame_chain)


def _infer_type(value: str) -> ParamType:
    try:
        float(value)
        return ParamType.NUMBER
    except ValueError:
        return ParamType.STRING


def _templatize(text: str, declared_params: dict[str, str]) -> str:
    out = text
    for name, value in declared_params.items():
        if value and value in out:
            out = out.replace(value, "{" + name + "}")
    return out


def _target_signature(target: Optional[Target]) -> Optional[str]:
    """A stable-enough per-control key for disambiguating which declared
    param a step's value belongs to, independent of the value itself. Uses
    the css_path candidate: generic to any legacy app, and already
    positionally distinct between different controls even when their
    higher-ranked role/name candidates happen to collide.
    """
    if target is None:
        return None
    for c in target.candidates:
        if c.strategy == LocatorStrategy.CSS_PATH:
            return c.value.get("css")
    return None


def _bind_value_template(
    value: str,
    target: Optional[Target],
    declared_params: dict[str, str],
    unclaimed: dict[str, "deque[str]"],
    bound_controls: dict[str, str],
) -> str:
    """Templates one click/fill/select step's literal value, assigning
    declared params by (value, control identity) rather than value alone.

    `unclaimed` starts as every declared (name, value) pair, grouped by
    value, in declaration order; each control identity claims at most one
    name. `bound_controls` remembers which control already claimed which
    name, so the same control revisited later in the run (e.g. re-confirmed
    after a refresh) reuses its own binding instead of consuming a second
    declared param — this is what preserves the existing "same param appears
    in multiple steps" behavior while fixing the identical-value case.
    """
    sig = _target_signature(target)
    if sig is not None and sig in bound_controls:
        return "{" + bound_controls[sig] + "}"

    queue = unclaimed.get(value)
    if queue:
        name = queue.popleft()
        if sig is not None:
            bound_controls[sig] = name
        return "{" + name + "}"

    # No still-unclaimed declared param has this exact value (either none ever
    # did, or every param sharing it was already claimed by other controls) —
    # fall back to the original flat substitution rather than leaving the
    # literal untouched, matching prior behavior for the non-colliding case.
    return _templatize(value, declared_params)


def record_artifact(
    result: DiscoveryResult,
    capability_id: str,
    description: str,
    declared_params: dict[str, str],
    target_app: TargetApp,
    version: str = "1.0.0",
) -> Artifact:
    if not result.success:
        raise ValueError(f"Cannot record an artifact from a non-successful run (outcome={result.outcome!r}).")

    # Only steps that actually executed, and that are meaningful to replay:
    # every successful mutating/navigating action, plus read_text steps that
    # produced one of the run's declared outputs (informational reads the
    # agent made for its own reasoning are discovery noise, not capability).
    raw_outputs = result.outputs
    steps: list[Step] = []
    output_fields: list[OutputField] = []
    matched_output_names: set[str] = set()

    # Per-control param bindings for click/fill/select values (see
    # _bind_value_template) — built once per run, consumed in step order.
    unclaimed: dict[str, "deque[str]"] = defaultdict(deque)
    for name, value in declared_params.items():
        if value:
            unclaimed[value].append(name)
    bound_controls: dict[str, str] = {}

    for s in result.steps:
        if not s.ok:
            continue

        if s.action == "read_text":
            match = next(
                (name for name, val in raw_outputs.items() if name not in matched_output_names and val == s.element_name),
                None,
            )
            if match is None:
                continue  # informational-only read; not part of the reusable capability
            matched_output_names.add(match)
            step = Step(
                index=len(steps),
                action=ActionType.READ_TEXT,
                description=redact_text(s.rationale),
                target=_scrub_target(s.target),
                extract_as=match,
                risk=s.risk,
                source=s.source,
            )
            output_fields.append(
                OutputField(name=match, type=_infer_type(raw_outputs[match]), description=f"Value read for {match}.", source_step=step.index)
            )
            steps.append(step)
            continue

        if s.action == "navigate":
            steps.append(
                Step(
                    index=len(steps),
                    action=ActionType.NAVIGATE,
                    description=redact_text(s.rationale),
                    value_template=_templatize(s.url_after or s.url_before, declared_params),
                    risk=s.risk,
                    source=s.source,
                )
            )
            continue

        if s.action in ("click", "fill", "select"):
            value_template = None
            if s.value is not None:
                value_template = _bind_value_template(s.value, s.target, declared_params, unclaimed, bound_controls)
            steps.append(
                Step(
                    index=len(steps),
                    action=ActionType(s.action),
                    description=redact_text(s.rationale),
                    target=_scrub_target(s.target),
                    value_template=value_template,
                    risk=s.risk,
                    source=s.source,
                )
            )

    # A declared output the model mentioned but never actually read off the page
    # (no matching read_text step) isn't something replay can reproduce — drop it
    # rather than promise a caller data that will never be populated.

    final_url = next((s.url_after for s in reversed(result.steps) if s.url_after), result.target_url)
    final_path = urlparse(final_url).path
    final_checkpoint = Checkpoint(kind=CheckpointKind.URL_CONTAINS, value=_templatize(final_path, declared_params))

    overall_risk = max((s.risk for s in steps), key=lambda r: _RISK_ORDER.get(r, 0), default="safe")

    # A declared param the operator passed but that no step ever templated in
    # (e.g. a form control the agent left at its already-correct default) isn't
    # something replay can actually control — dropping it keeps the contract honest.
    used_param_names = {
        name
        for name in declared_params
        if any(step.value_template and "{" + name + "}" in step.value_template for step in steps)
    }
    inputs = [
        InputParam(name=name, type=_infer_type(declared_params[name]), required=True, description=f"Value for {name}.", example=declared_params[name])
        for name in declared_params
        if name in used_param_names
    ]

    return Artifact(
        capability_id=capability_id,
        version=version,
        description=description,
        goal_template=result.goal,
        target_app=target_app,
        inputs=inputs,
        outputs=output_fields,
        steps=steps,
        final_checkpoint=final_checkpoint,
        overall_risk=overall_risk,
        created_from_run_id=result.run_id,
    )
