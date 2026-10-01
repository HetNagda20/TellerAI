"""The router's view of saved capabilities: one entry each, rebuilt from artifacts/ on every call so
nothing goes stale. An entry has a description, an example request, and inputs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import artifact.store as store
from artifact.schema import Artifact

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass
class CatalogInput:
    name: str
    type: str  # "number" | "string"
    description: str
    example: str
    required: bool = True
    allowed_values: Optional[list[str]] = None  # a dropdown's choices, when the input fills one


@dataclass
class CatalogEntry:
    capability_id: str
    version: str
    path: Path
    status: str  # "draft" | "approved"
    risky: bool  # has an irreversible (risk="confirm") step
    description: str
    example_request: str  # the goal template, with {placeholders} where the inputs go
    inputs: list[CatalogInput] = field(default_factory=list)


def _semver(version: str) -> Optional[tuple[int, int, int]]:
    m = _SEMVER.match(version)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def target_compatible(target_url: str, artifact: Artifact) -> bool:
    """True if the capability was recorded against the same deployment as the caller's target: same
    host and port as TargetApp.base_url."""
    requested = urlparse(target_url)
    recorded = urlparse(artifact.target_app.base_url)
    if requested.hostname and recorded.hostname:
        return (requested.hostname, requested.port) == (recorded.hostname, recorded.port)
    return target_url.rstrip("/") == artifact.target_app.base_url.rstrip("/")


def _latest_path(capability_id: str) -> Optional[Path]:
    """The highest semantic version on disk. Names that are not plain X.Y.Z (a hand-made "0.0.1-manual-test")
    are not candidates, and 1.10.0 correctly outranks 1.9.0, which a filename sort gets wrong."""
    best: Optional[tuple[tuple[int, int, int], Path]] = None
    for path in Path(store.ARTIFACTS_DIR).glob(f"{capability_id}@*.json"):
        version = _semver(path.stem.split("@", 1)[1])
        if version is not None and (best is None or version > best[0]):
            best = (version, path)
    return best[1] if best else None


def build_catalog(target_url: str) -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    for capability_id in store.list_capability_ids():
        path = _latest_path(capability_id)
        if path is None:
            continue
        artifact = store.load_path(path)
        if not target_compatible(target_url, artifact):
            continue
        entries.append(
            CatalogEntry(
                capability_id=artifact.capability_id,
                version=artifact.version,
                path=path,
                status=artifact.status.value,
                risky=any(step.risk == "confirm" for step in artifact.steps),
                description=artifact.description,
                example_request=artifact.goal_template,
                inputs=[
                    CatalogInput(i.name, i.type.value, i.description or "", i.example or "", i.required, i.allowed_values)
                    for i in artifact.inputs
                ],
            )
        )
    return sorted(entries, key=lambda e: e.capability_id)
