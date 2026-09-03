"""Checkout-specific Docker Compose project-name derivation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.compose import compose_command, compose_project_name


def test_compose_project_name_is_deterministic_valid_and_checkout_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived names are valid, stable, and differ for different checkout roots."""
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    root = Path("/tmp/tunstrap-one")
    name = compose_project_name(root, "integration")

    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name)
    assert name == compose_project_name(root, "integration")
    assert name != compose_project_name(Path("/tmp/tunstrap-two"), "integration")


def test_compose_project_name_uses_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """COMPOSE_PROJECT_NAME overrides the checkout-derived name."""
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "manual-project")

    assert compose_project_name(Path("/tmp/tunstrap-one"), "integration") == "manual-project"


def test_compose_command_passes_the_derived_name_to_every_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose commands put the checkout-specific project name before ``-f``."""
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    root = Path("/tmp/tunstrap-one")
    compose_file = root / "tests" / "integration" / "docker-compose.yml"

    assert compose_command(compose_file, root, "integration", "up", "-d") == [
        "docker",
        "compose",
        "-p",
        compose_project_name(root, "integration"),
        "-f",
        str(compose_file),
        "up",
        "-d",
    ]
