"""Command-line entrypoint. `task` is the public Task AI interface (goal and target). `run` and
`replay` are internal paths for recording or replaying a named capability."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import typer
from dotenv import load_dotenv

load_dotenv()

from logging_config import configure_logging

configure_logging()

from agent.loop import run_discovery
from artifact.annotations import annotations_for, check_annotation_placeholders
from artifact.recorder import record_artifact
from artifact.schema import ArtifactStatus, TargetApp
from artifact.store import load_path, save
from replay.executor import replay_artifact
from artifact.grounding import match_option
from router.catalog import build_catalog
from router.router import route

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False)

_SLUG_STOPWORDS = {
    "a", "an", "the", "to", "from", "and", "of", "then", "please", "exactly",
    "reach", "confirmation", "screen", "member", "account",
}


def _parse_params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"Expected name=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _attach_annotations(artifact, capability_id: str) -> None:
    """Attaches reviewer annotations, and refuses to save an artifact whose annotations name an
    input it lacks, which would leave literal braces in a replay URL."""
    business_outcomes, recoverable, commit_verification = annotations_for(capability_id)
    artifact.business_outcomes = business_outcomes
    artifact.recoverable_conditions = recoverable
    artifact.commit_verification = commit_verification
    missing = check_annotation_placeholders(artifact)
    if missing:
        typer.echo(
            f"NOT SAVED: the annotations for {capability_id!r} use {missing}, which are not inputs of this "
            f"artifact (inputs: {[i.name for i in artifact.inputs]}). Update artifact/annotations.py to the "
            "artifact's actual input names and record again.",
            err=True,
        )
        raise typer.Exit(code=1)


def _slugify(goal: str) -> str:
    """Makes a best-effort capability_id from a goal when the operator gave no name."""
    words = [w for w in re.findall(r"[a-zA-Z]+", goal.lower()) if w not in _SLUG_STOPWORDS]
    return "-".join(words[:4]) or "task"


@app.command()
def task(
    goal: str = typer.Option(..., help="Natural-language goal. The Task AI entry point's ONLY required input besides target."),
    target: str = typer.Option("http://127.0.0.1:8000/", help="Target application entry point (URL)."),
    app_id: str = typer.Option("cu-servicing-console", help="Stable id of the target vendor app (used only if a new capability must be discovered)."),
    max_steps: int = typer.Option(30),
    headless: bool = typer.Option(False, help="Run a discovery fallback headless (default: headed, so you can watch)."),
    escalate_on_failure: bool = typer.Option(False, help="Route a replay hard failure to a human via the handoff session."),
    yes: bool = typer.Option(False, help="Skip the confirmation shown before an APPROVED capability with an irreversible step runs unattended."),
):
    """The single public Task AI entry point: goal + target, nothing else.
    router.route() decides replay vs. discovery; this command carries out
    whichever it picks."""
    logger.info("task goal=%r target=%s", goal, target)
    decision = route(goal, target)
    typer.echo(f"Router decision: {decision.action}, {decision.reason}")
    if decision.action == "discover" and "could not be reached" in decision.reason:
        typer.echo("Tip: to run a saved capability without the router model, use `cli.py capabilities` and `cli.py invoke`.")

    if decision.action == "replay":
        if decision.risky and decision.status == "approved" and not yes:
            # An approved capability runs its risky step with nobody asked. The router picked it
            # from free text, so show the interpretation and let a person catch a wrong match.
            typer.echo(f"About to run '{decision.capability_id}' unattended, including an irreversible step, with:")
            for name, value in decision.params.items():
                typer.echo(f"  {name} = {value}")
            typer.confirm("Is that what you meant?", abort=True)
        artifact = load_path(decision.artifact_path)
        result = replay_artifact(artifact, decision.params, headless=headless, escalate_on_failure=escalate_on_failure)
        typer.echo(json.dumps(result.model_dump(), indent=2, default=str))
        if result.kind == "hard_failure":
            raise typer.Exit(code=1)
        return

    # Nothing saved covers this goal, so fall through to a real discovery run, the same way `run`
    # below does.
    result = run_discovery(goal=goal, target_url=target, max_steps=max_steps, headless=headless)
    logger.info("discovery outcome=%s success=%s", result.outcome, result.success)
    typer.echo(f"\nDiscovery outcome: {result.outcome} (success={result.success})")
    typer.echo(f"Summary: {result.summary}")
    typer.echo(f"Evidence: {result.evidence_dir}")
    if not result.success:
        raise typer.Exit(code=1)

    capability_id = _slugify(goal)
    target_app = TargetApp(app_id=app_id, base_url=target.rstrip("/"), entry_path="/")
    artifact = record_artifact(result, capability_id, "Auto-recorded from a Task AI goal.", {}, target_app)
    # the description carries the goal TEMPLATE, never the raw goal: the demonstration's values (and any
    # sensitive one) do not belong in a reusable capability's description or in the router's vocabulary
    artifact.description = f"Auto-recorded from Task AI goal: {artifact.goal_template}"
    _attach_annotations(artifact, capability_id)
    path = save(artifact)
    typer.echo(
        f"No existing capability matched, recorded a new one at {path} "
        f"(capability_id={capability_id!r}, auto-generated)."
    )
    if artifact.inputs:
        typer.echo("Typed inputs taken from the goal: " + ", ".join(f"{i.name} ({i.type.value})" for i in artifact.inputs))
    else:
        typer.echo(
            "WARNING: the goal supplied no values the agent applied to a control, so this capability has no "
            "typed inputs and replays one fixed demonstration. Do not rely on it for other values."
        )


@app.command()
def run(
    goal: str = typer.Option(..., help="Natural-language goal for the discovery run."),
    target_url: str = typer.Option("http://127.0.0.1:8000/", help="Entry point of the target app."),
    capability_id: str = typer.Option(..., help="Slug to save the resulting artifact under."),
    description: str = typer.Option(..., help="Human-readable description of the capability."),
    param: list[str] = typer.Option([], help="Declared input param as name=value; repeatable."),
    app_id: str = typer.Option("cu-servicing-console", help="Stable id of the target vendor app."),
    max_steps: int = typer.Option(30),
    headless: bool = typer.Option(False, help="Run the discovery browser headless (default: headed, so you can watch)."),
    version: str = typer.Option("1.0.0", help="Artifact semver to save under; bump this rather than overwriting an existing capability_id@version.json."),
):
    logger.info("run capability_id=%s goal=%r", capability_id, goal)
    declared_params = _parse_params(param)
    result = run_discovery(goal=goal, target_url=target_url, max_steps=max_steps, headless=headless)

    logger.info("discovery outcome=%s success=%s", result.outcome, result.success)
    typer.echo(f"\nDiscovery outcome: {result.outcome} (success={result.success})")
    typer.echo(f"Summary: {result.summary}")
    typer.echo(f"Evidence: {result.evidence_dir}")

    if not result.success:
        raise typer.Exit(code=1)

    target_app = TargetApp(app_id=app_id, base_url=target_url.rstrip("/"), entry_path="/")
    artifact = record_artifact(result, capability_id, description, declared_params, target_app, version=version)
    _attach_annotations(artifact, capability_id)

    path = save(artifact)
    typer.echo(f"Saved artifact: {path}")


@app.command()
def replay(
    artifact_path: str = typer.Option(..., help="Path to a saved artifact JSON file."),
    param: list[str] = typer.Option([], help="Input param as name=value; repeatable."),
    headless: bool = typer.Option(True),
    escalate_on_failure: bool = typer.Option(False, help="Route hard failures to a human via the handoff session."),
    slow_mo_ms: int = typer.Option(0, help="Delay every browser operation by this many ms, so a headed run is easy to watch (e.g. 700)."),
):
    logger.info("replay artifact_path=%s", artifact_path)
    artifact = load_path(artifact_path)
    params = _parse_params(param)
    result = replay_artifact(artifact, params, headless=headless, escalate_on_failure=escalate_on_failure, slow_mo_ms=slow_mo_ms)
    typer.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if result.kind == "hard_failure":
        raise typer.Exit(code=1)


@app.command()
def capabilities(
    target: str = typer.Option("http://127.0.0.1:8000/", help="Only capabilities recorded against this target are listed."),
):
    """Lists the saved capabilities (the catalog) with their typed inputs. No model needed."""
    entries = build_catalog(target)
    if not entries:
        typer.echo("No saved capabilities for this target.")
        return
    for e in entries:
        flags = f"{e.status}" + (", has an irreversible step" if e.risky else "")
        typer.echo(f"{e.capability_id}@{e.version} ({flags})\n  {e.description}")
        for i in e.inputs:
            choices = f" (one of: {', '.join(i.allowed_values)})" if i.allowed_values else ""
            typer.echo(f"    --param {i.name}=...   {i.type}, e.g. {i.example}: {i.description}{choices}")


@app.command()
def invoke(
    capability: str = typer.Option(..., help="Capability id from `capabilities`, e.g. transfer-funds. The latest version runs."),
    param: list[str] = typer.Option([], help="Input param as name=value; repeatable. Every declared input is required."),
    target: str = typer.Option("http://127.0.0.1:8000/", help="Target the capability was recorded against."),
    headless: bool = typer.Option(True),
    escalate_on_failure: bool = typer.Option(False, help="Route hard failures to a human via the handoff session."),
    slow_mo_ms: int = typer.Option(0, help="Delay every browser operation by this many ms."),
    yes: bool = typer.Option(False, help="Skip the confirmation before an APPROVED capability with an irreversible step runs."),
):
    """Runs a saved capability by name with typed args: the catalog interface, with no model in the path."""
    entry = next((e for e in build_catalog(target) if e.capability_id == capability), None)
    if entry is None:
        known = ", ".join(e.capability_id for e in build_catalog(target)) or "none"
        typer.echo(f"No saved capability {capability!r} for this target. Known: {known}.", err=True)
        raise typer.Exit(code=2)
    params = _parse_params(param)
    declared = {i.name: i for i in entry.inputs}
    problems = [f"missing --param {n}=..." for n in declared if n not in params]
    problems += [f"unknown input {n!r}" for n in params if n not in declared]
    problems += [
        f"{n} must be a number, got {v!r}"
        for n, v in params.items()
        if n in declared and declared[n].type == "number" and not re.fullmatch(r"-?[\d,]*\.?\d+", v.replace("$", "").replace("%", "").strip())
    ]
    for n, v in list(params.items()):
        choices = declared[n].allowed_values if n in declared else None
        if choices:
            option, kind = match_option(v, choices)
            if option is None:
                problems.append(f"{n} = {v!r} is not one of {choices}")
            else:
                if kind == "near":
                    typer.echo(f"Reading {n} = {v!r} as {option!r}.")
                params[n] = option  # the page's own spelling
    if problems:
        typer.echo(f"Cannot run {capability}: " + "; ".join(problems) + ". See `capabilities`.", err=True)
        raise typer.Exit(code=2)
    if entry.risky and entry.status == "approved" and not yes:
        typer.echo(f"About to run '{capability}' unattended, including an irreversible step, with:")
        for name, value in params.items():
            typer.echo(f"  {name} = {value}")
        typer.confirm("Go ahead?", abort=True)
    logger.info("invoke capability=%s version=%s", capability, entry.version)
    result = replay_artifact(load_path(entry.path), params, headless=headless, escalate_on_failure=escalate_on_failure, slow_mo_ms=slow_mo_ms)
    typer.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if result.kind == "hard_failure":
        raise typer.Exit(code=1)


@app.command()
def approve(
    artifact_path: str = typer.Option(..., help="Path to a saved artifact JSON file to approve for unattended replay."),
    yes: bool = typer.Option(False, help="Skip the interactive confirmation (for scripted review workflows)."),
):
    """Moves an artifact from draft to approved. Its risky steps then run unattended, flagged in the
    result; a draft asks a human first. Read it before approving."""
    artifact = load_path(artifact_path)
    risky = [s for s in artifact.steps if s.risk == "confirm"]
    typer.echo(f"{artifact.capability_id}@{artifact.version} (status: {artifact.status.value})")
    typer.echo(f"Description: {artifact.description}")
    typer.echo(f"Inputs: {', '.join(i.name for i in artifact.inputs) or 'none'}")
    typer.echo(f"Risky/irreversible steps that will run unattended once approved: {len(risky)}")
    for s in risky:
        typer.echo(f"  step {s.index} [{s.action.value}]: {s.description}")
    if artifact.status == ArtifactStatus.APPROVED:
        typer.echo("Already approved.")
        return
    if not yes:
        typer.confirm("Approve this artifact for unattended replay?", abort=True)
    artifact.status = ArtifactStatus.APPROVED
    # Write back to the file that was reviewed, not to the canonical artifacts/ location: approving a copy
    # must never change a different file (save() keys the path on capability_id@version).
    path = Path(artifact_path)
    path.write_text(artifact.model_dump_json(indent=2))
    logger.info("artifact approved path=%s", path)
    typer.echo(f"Approved: {path}")


if __name__ == "__main__":
    app()
