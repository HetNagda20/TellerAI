"""Command-line entrypoint.

    python cli.py run --goal "..." --target-url http://127.0.0.1:8000/ \\
        --capability-id open-member-subaccount --description "..." \\
        --param member_id=10001 --param account_type=savings --param initial_deposit=100

    python cli.py replay --artifact-path artifacts/open-member-subaccount@1.0.0.json \\
        --param member_id=10001 --param account_type=savings --param initial_deposit=100
"""

from __future__ import annotations

import json

import typer
from dotenv import load_dotenv

load_dotenv()

from agent.loop import run_discovery
from artifact.annotations import annotations_for
from artifact.recorder import record_artifact
from artifact.schema import TargetApp
from artifact.store import load_path, save
from replay.executor import replay_artifact

app = typer.Typer(add_completion=False)


def _parse_params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"Expected name=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


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
):
    declared_params = _parse_params(param)
    result = run_discovery(goal=goal, target_url=target_url, max_steps=max_steps, headless=headless)

    typer.echo(f"\nDiscovery outcome: {result.outcome} (success={result.success})")
    typer.echo(f"Summary: {result.summary}")
    typer.echo(f"Evidence: {result.evidence_dir}")

    if not result.success:
        raise typer.Exit(code=1)

    target_app = TargetApp(app_id=app_id, base_url=target_url.rstrip("/"), entry_path="/")
    artifact = record_artifact(result, capability_id, description, declared_params, target_app)
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
