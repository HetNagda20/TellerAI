"""Flat-file artifact storage. One JSON file per (capability_id, version)."""

import logging
from pathlib import Path

from artifact.schema import Artifact

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"


def save(artifact: Artifact) -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / f"{artifact.capability_id}@{artifact.version}.json"
    path.write_text(artifact.model_dump_json(indent=2))
    logger.info("artifact saved path=%s", path)
    return path


def load(capability_id: str, version: str) -> Artifact:
    path = ARTIFACTS_DIR / f"{capability_id}@{version}.json"
    return Artifact.model_validate_json(path.read_text())


def load_path(path: str | Path) -> Artifact:
    return Artifact.model_validate_json(Path(path).read_text())


def latest_version_path(capability_id: str) -> Path | None:
    candidates = sorted(ARTIFACTS_DIR.glob(f"{capability_id}@*.json"))
    return candidates[-1] if candidates else None


def list_capability_ids() -> list[str]:
    """Every distinct capability_id with at least one saved version on disk,
    used by the router to know what it can possibly match a goal against,
    without hardcoding any capability name."""
    return sorted({p.name.rsplit("@", 1)[0] for p in ARTIFACTS_DIR.glob("*@*.json")})
