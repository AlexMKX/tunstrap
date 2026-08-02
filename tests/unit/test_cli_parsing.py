"""CLI top-level parsing.

Validates: help, version, and unknown-subcommand behaviour of the
tunstrap CLI dispatcher.
Code: tunstrap/cli.py
"""

from __future__ import annotations

import re

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
    """Print the resolved package version, not the not-installed fallback.

    The lazy ``--version`` callback resolves ``__version__`` via the package
    ``__init__``'s PEP 562 ``__getattr__``. Under a real install that yields a
    setuptools-scm version like ``0.0.5.dev72+…``; the not-installed fallback is
    ``0.0.0+unknown``. Asserting only ``"tunstrap" in output`` passes either —
    including the fallback, which would hide a regression to a broken
    distribution lookup. Pin a real-version shape and reject the fallback.
    """
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    shape_msg = f"--version output does not look like a resolved version: {result.output!r}"
    assert re.match(r"tunstrap, version \d+\.\d+\.\d", result.output), shape_msg
    fb_msg = f"--version fell back to the not-installed placeholder: {result.output!r}"
    assert "unknown" not in result.output, fb_msg


def test_unknown_subcommand_exits_64() -> None:
    """Reject an unknown subcommand with exit code 64 (usage error)."""
    result = CliRunner().invoke(main, ["does-not-exist"])
    assert result.exit_code == 64
