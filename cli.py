"""Command-line entrypoint.

The single, public Task AI entry point (assignment section 3.1's contract:
goal + target only, nothing else required):

    python cli.py task --goal "Transfer $100 from Member 10001 checking to Member 10002 checking." \\
        --target http://127.0.0.1:8000

The router (router/router.py) decides whether an existing capability already
covers this goal (-> deterministic replay, with typed inputs it extracted
from the goal text itself) or a new discovery run is needed. No
--capability-id, no --param values, at this entry point.

`run` and `replay` below remain as internal/advanced execution paths — not
the product interface — for anyone who wants to record a capability under an
explicit name with explicit declared parameters, or replay one with typed
params directly:

    python cli.py run --goal "..." --target-url http://127.0.0.1:8000/ \\
        --capability-id open-member-subaccount --description "..." \\
        --param member_id=10001 --param account_type=savings --param initial_deposit=100

    python cli.py replay --artifact-path artifacts/open-member-subaccount@1.0.0.json \\
        --param member_id=10001 --param account_type=savings --param initial_deposit=100
"""

from __future__ import annotations

import json
import re

import typer
from dotenv import load_dotenv

load_dotenv()

from agent.loop import run_discovery
from artifact.annotations import annotations_for
from artifact.recorder import record_artifact
from artifact.schema import TargetApp
from artifact.store import load_path, save
from replay.executor import replay_artifact
from router.router import route

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


def _slugify(goal: str) -> str:
    """Best-effort capability_id for a brand-new capability discovered
    straight from a Task AI goal with no operator-chosen name. Deliberately
    simple: the Task AI entry point takes no --param values either, so an
    auto-recorded capability from this path has no declared typed inputs —
    it's a working, replayable transcript of this exact goal, not yet a
    properly parameterized reusable capability. Use `run` with explicit
    --param flags to record one of those under a chosen name.
    """
    words = [w for w in re.findall(r"[a-zA-Z]+", goal.lower()) if w not in _SLUG_STOPWORDS]
    return "-".join(words[:4]) or "task"


@app.command()
def task(
    goal: str = typer.Option(..., help="Natural-language goal. The Task AI entry point's ONLY required input besides target."),
    target: str = typer.Option("http://127.0.0.1:8000/", help="Target application entry point (URL)."),
    app_id: str = typer.Option("cu-servicing-console", help="Stable id of the target vendor app (used only if a new capability must be discovered)."),
    max_steps: int = typer.Option(20),
    headless: bool = typer.Option(False, help="Run a discovery fallback headless (default: headed, so you can watch)."),
    escalate_on_failure: bool = typer.Option(False, help="Route a replay hard failure to a human via the handoff session."),
):
    """The single public Task AI entry point: goal + target, nothing else.
    router.route() decides replay vs. discovery; this command just carries
    out whichever it picks.
    """
    decision = route(goal, target)
    typer.echo(f"Router decision: {decision.action} — {decision.reason}")

    if decision.action == "replay":
        artifact = load_path(decision.artifact_path)
        result = replay_artifact(artifact, decision.params, headless=headless, escalate_on_failure=escalate_on_failure)
        typer.echo(json.dumps(result.model_dump(), indent=2, default=str))
        if result.kind == "hard_failure":
            raise typer.Exit(code=1)
        return

    # decision.action == "discover": no existing capability covered this goal
    # closely enough (or couldn't be confidently parameterized from the goal
    # text alone) — fall through to a real discovery run, same underlying
    # mechanism as `run` below, just without an operator-chosen capability_id
    # or declared --param values (the Task AI entry point takes neither).
    result = run_discovery(goal=goal, target_url=target, max_steps=max_steps, headless=headless)
    typer.echo(f"\nDiscovery outcome: {result.outcome} (success={result.success})")
    typer.echo(f"Summary: {result.summary}")
    typer.echo(f"Evidence: {result.evidence_dir}")
    if not result.success:
        raise typer.Exit(code=1)

    capability_id = _slugify(goal)
    target_app = TargetApp(app_id=app_id, base_url=target.rstrip("/"), entry_path="/")
    artifact = record_artifact(result, capability_id, f"Auto-recorded from Task AI goal: {goal}", {}, target_app)
    business_outcomes, recoverable = annotations_for(capability_id)
    artifact.business_outcomes = business_outcomes
    artifact.recoverable_conditions = recoverable
    path = save(artifact)
    typer.echo(
        f"No existing capability matched — recorded a new one at {path} "
        f"(capability_id={capability_id!r}, auto-generated). It has no declared "
        f"typed inputs: the Task AI entry point doesn't take --param values, so "
        f"nothing was demonstrated as parameterizable this run. Use "
        f"`python cli.py run --capability-id {capability_id} --param ...` to "
        f"record a properly reusable, parameterized version of this capability."
    )


@app.command()
def run(
    goal: str = typer.Option(..., help="Natural-language goal for the discovery run."),
    target_url: str = typer.Option("http://127.0.0.1:8000/", help="Entry point of the target app."),
    capability_id: str = typer.Option(..., help="Slug to save the resulting artifact under."),
    description: str = typer.Option(..., help="Human-readable description of the capability."),
    param: list[str] = typer.Option([], help="Declared input param as name=value; repeatable."),
    app_id: str = typer.Option("cu-servicing-console", help="Stable id of the target vendor app."),
    max_steps: int = typer.Option(20),
    headless: bool = typer.Option(False, help="Run the discovery browser headless (default: headed, so you can watch)."),
    version: str = typer.Option("1.0.0", help="Artifact semver to save under; bump this rather than overwriting an existing capability_id@version.json."),
):
    declared_params = _parse_params(param)
    result = run_discovery(goal=goal, target_url=target_url, max_steps=max_steps, headless=headless)

    typer.echo(f"\nDiscovery outcome: {result.outcome} (success={result.success})")
    typer.echo(f"Summary: {result.summary}")
    typer.echo(f"Evidence: {result.evidence_dir}")

    if not result.success:
        raise typer.Exit(code=1)

    target_app = TargetApp(app_id=app_id, base_url=target_url.rstrip("/"), entry_path="/")
    artifact = record_artifact(result, capability_id, description, declared_params, target_app, version=version)
    business_outcomes, recoverable = annotations_for(capability_id)
    artifact.business_outcomes = business_outcomes
    artifact.recoverable_conditions = recoverable

    path = save(artifact)
    typer.echo(f"Saved artifact: {path}")


@app.command()
def replay(
    artifact_path: str = typer.Option(..., help="Path to a saved artifact JSON file."),
    param: list[str] = typer.Option([], help="Input param as name=value; repeatable."),
    headless: bool = typer.Option(True),
    escalate_on_failure: bool = typer.Option(False, help="Route hard failures to a human via the handoff session."),
):
    artifact = load_path(artifact_path)
    params = _parse_params(param)
    result = replay_artifact(artifact, params, headless=headless, escalate_on_failure=escalate_on_failure)
    typer.echo(json.dumps(result.model_dump(), indent=2, default=str))
    if result.kind == "hard_failure":
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
