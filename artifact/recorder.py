"""Turns a successful discovery run's step log into an Artifact. Declared inputs become {param}
templates, checkpoints avoid generated values, and sensitive locator candidates are dropped."""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

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
from artifact.grounding import templatize_goal
from guardrails.redact import REDACTED, field_is_sensitive, redact_text, redact_value

if TYPE_CHECKING:  # the type of `result` only: importing agent at runtime would pull the LLM client into artifact/
    from agent.loop import DiscoveryResult

logger = logging.getLogger(__name__)

_RISK_ORDER = {"safe": 0, "confirm": 1, "blocked": 2}


def _looks_sensitive(text: str) -> bool:
    """True if the text looks like PII or a secret, or reads like a sensitive field name. Reuses
    guardrails.redact."""
    if not text:
        return False
    return field_is_sensitive(text) or redact_text(text) != text


def _scrub_target(target: Optional[Target]) -> Optional[Target]:
    """Drops role/text locator candidates that look sensitive, since a fake redacted one could never
    resolve. css_path and coordinates stay."""
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


def _normalize_for_match(value: str) -> str:
    """Trims whitespace and lowercases, nothing fuzzier. A select logs "Checking" while the operator
    declares "checking": same input."""
    return value.strip().casefold()


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


def _templatize_candidate(candidate: LocatorCandidate, declared_params: dict[str, str]) -> LocatorCandidate:
    """Like _templatize, but for a locator candidate's string fields, since a selector or frame URL
    can embed an input's value."""
    new_value = {
        key: (_templatize(val, declared_params) if isinstance(val, str) else val)
        for key, val in candidate.value.items()
    }
    return LocatorCandidate(strategy=candidate.strategy, value=new_value, observed_at_record_time=candidate.observed_at_record_time)


def _templatize_target(target: Optional[Target], declared_params: dict[str, str]) -> Optional[Target]:
    """Templates every candidate in a Target, including every frame_chain
    entry. See replay/locators.py for the matching replay-side substitution
    back in; without both halves, a templated locator would never resolve
    on replay."""
    if target is None:
        return None
    return Target(
        candidates=[_templatize_candidate(c, declared_params) for c in target.candidates],
        frame_chain=[[_templatize_candidate(c, declared_params) for c in chain] for chain in target.frame_chain],
    )


def _target_signature(target: Optional[Target]) -> Optional[str]:
    """A stable key for one control, from its css_path candidate, so two controls with the same
    value bind to different params."""
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
    """Templates one step's value by (value, control), not value alone. Each control claims one
    declared param, and a revisited control reuses its binding."""
    sig = _target_signature(target)
    if sig is not None and sig in bound_controls:
        return "{" + bound_controls[sig] + "}"

    queue = unclaimed.get(_normalize_for_match(value))
    if queue:
        name = queue.popleft()
        if sig is not None:
            bound_controls[sig] = name
        return "{" + name + "}"

    # No still-unclaimed declared param has this exact value. Fall back to the
    # original flat substitution rather than leaving the literal untouched.
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
        logger.error("record_artifact refused: run did not succeed run_id=%s outcome=%s", result.run_id, result.outcome)
        raise ValueError(f"Cannot record an artifact from a non-successful run (outcome={result.outcome!r}).")

    # Discovery's declared inputs (already proven by agent/loop.py) plus any the operator declared
    # by hand; the operator wins on a name clash.
    declared_params = {**result.inputs, **declared_params}
    bound_names = {s.param_binding for s in result.steps if s.param_binding}

    # Keep only steps that ran and matter for replay: successful clicks, fills and navigation, plus
    # read_text steps that produced a declared output.
    raw_outputs = result.outputs
    steps: list[Step] = []
    output_fields: list[OutputField] = []
    matched_output_names: set[str] = set()

    # Per-control param bindings for click/fill/select values (see
    # _bind_value_template), built once per run, consumed in step order.
    unclaimed: dict[str, "deque[str]"] = defaultdict(deque)
    for name, value in declared_params.items():
        if value and name not in bound_names:
            unclaimed[_normalize_for_match(value)].append(name)
    bound_controls: dict[str, str] = {}
    select_choices: dict[str, list[str]] = {}  # input name -> the dropdown's visible choices

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
                target=_templatize_target(_scrub_target(s.target), declared_params),
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
            if s.param_binding:
                value_template = "{" + s.param_binding + "}"
            elif s.value is not None:
                value_template = _bind_value_template(s.value, s.target, declared_params, unclaimed, bound_controls)
            only_param = re.fullmatch(r"\{(\w+)\}", value_template or "")
            if s.action == "select" and only_param and s.options:
                select_choices[only_param.group(1)] = [redact_text(o) for o in s.options]
            steps.append(
                Step(
                    index=len(steps),
                    action=ActionType(s.action),
                    description=redact_text(s.rationale),
                    target=_templatize_target(_scrub_target(s.target), declared_params),
                    value_template=value_template,
                    risk=s.risk,
                    source=s.source,
                )
            )

    # A declared output the model mentioned but never actually read off the page
    # is not something replay can reproduce; drop it rather than promise data
    # that will never be populated.

    final_url = next((s.url_after for s in reversed(result.steps) if s.url_after), result.target_url)
    final_path = urlparse(final_url).path
    # The success phrase the agent saw is the checkpoint: legacy apps often keep one URL, so a path says
    # little. Fall back to the path only when no usable phrase was given.
    phrase = (getattr(result, "success_text", "") or "").strip()
    if phrase and not _looks_sensitive(phrase):
        final_checkpoint = Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value=_templatize(phrase, declared_params))
    else:
        final_checkpoint = Checkpoint(kind=CheckpointKind.URL_CONTAINS, value=_templatize(final_path, declared_params))

    overall_risk = max((s.risk for s in steps), key=lambda r: _RISK_ORDER.get(r, 0), default="safe")

    # A declared param the operator passed but that no step ever templated in is
    # not something replay can actually control; dropping it keeps the contract
    # honest.
    used_param_names = {
        name
        for name in declared_params
        if any(step.value_template and "{" + name + "}" in step.value_template for step in steps)
    }
    redacted_bound = {s.param_binding for s in result.steps if s.param_binding and s.value == REDACTED}

    def _example(name: str) -> str:
        # never persist a sensitive value: not by field name, and not when the step itself was redacted
        return REDACTED if name in redacted_bound else redact_value(name, declared_params[name])

    inputs = [
        InputParam(
            name=name,
            type=_infer_type(declared_params[name]),
            required=True,
            description=result.input_descriptions.get(name) or f"Value for {name}.",
            example=_example(name),
            allowed_values=select_choices.get(name),
        )
        for name in declared_params
        if name in used_param_names
    ]
    goal_template = templatize_goal(result.goal, {i.name: declared_params[i.name] for i in inputs})

    logger.info("artifact recorded capability=%s version=%s steps=%s run_id=%s", capability_id, version, len(steps), result.run_id)
    return Artifact(
        capability_id=capability_id,
        version=version,
        description=description,
        goal_template=goal_template,
        target_app=target_app,
        inputs=inputs,
        outputs=output_fields,
        steps=steps,
        final_checkpoint=final_checkpoint,
        overall_risk=overall_risk,
        created_from_run_id=result.run_id,
    )
