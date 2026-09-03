"""Docker Compose commands isolated by checkout and test tier."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def compose_project_name(repo_root: Path, tier: str) -> str:
    """Return an override or a valid, deterministic name unique to ``repo_root``."""
    if override := os.environ.get("COMPOSE_PROJECT_NAME"):
        return override
    digest = hashlib.sha256(os.fsencode(str(repo_root.resolve()))).hexdigest()[:12]
    return f"tunstrap-{tier}-{digest}"


def compose_command(compose_file: Path, repo_root: Path, tier: str, *args: str) -> list[str]:
    """Build a Compose command with its checkout-specific project name."""
    return [
        "docker",
        "compose",
        "-p",
        compose_project_name(repo_root, tier),
        "-f",
        str(compose_file),
        *args,
    ]
