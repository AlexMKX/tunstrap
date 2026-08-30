"""CLI top-level parsing.

Validates: help, version, and unknown-subcommand behaviour of the
tunstrap CLI dispatcher.
Code: tunstrap/cli.py
"""

from __future__ import annotations

from importlib.metadata import version

import pytest
from click.testing import CliRunner

from tunstrap.cli import main

pytestmark = pytest.mark.unit


def test_help_exits_zero() -> None:
    """Print top-level help and exit 0."""
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "tunstrap" in result.output


def test_version_flag() -> None:
    """Print the exact version the package metadata resolves, not the fallback.

    Compares the flag's output against ``importlib.metadata.version("tunstrap")``
    — the same source the lazy ``--version`` callback reads through the package
    ``__init__``'s PEP 562 ``__getattr__``. This accepts ANY PEP 440 version
    setuptools-scm/hatch-vcs derives: a 3-component release from a tagged clone
    (``0.0.5.dev72+…``), a 2-component release from a tagless CI shallow clone
    (``0.1.dev1+g6e691637c``), ``rcN``, ``+local`` — without a regex that guesses
    at the release-segment width and breaks on the CI shape.

    Rejects the not-installed fallback by construction: when the package is not
    installed, ``version()`` raises ``PackageNotFoundError`` (which is exactly
    when the flag would emit ``0.0.0+unknown``), failing this test rather than
    passing on the placeholder. The ``"unknown"`` check makes that intent
    legible at a glance.
    """
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    expected = version("tunstrap")
    # Local message vars: hoisted per the pattern established across the test
    # suite when black and ruff format disagreed on the multi-line
    # ``assert cond, (msg)`` form (black is gone, #34; the pattern stays).
    mismatch_msg = (
        f"--version output does not match the resolved metadata version: {result.output!r}"
    )
    assert result.output.strip() == f"tunstrap, version {expected}", mismatch_msg
    fallback_msg = f"--version fell back to the not-installed placeholder: {result.output!r}"
    assert "unknown" not in result.output, fallback_msg


def test_unknown_subcommand_exits_64() -> None:
    """Reject an unknown subcommand with exit code 64 (usage error)."""
    result = CliRunner().invoke(main, ["does-not-exist"])
    assert result.exit_code == 64
