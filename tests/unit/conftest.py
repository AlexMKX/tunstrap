"""Unit-test helpers (shared ``make_node`` factory, ``SSH_AUTH_SOCK`` guard)."""

from __future__ import annotations

import shutil
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_ssh_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete ``SSH_AUTH_SOCK`` for every unit test; opt in with ``setenv``.

    ``InputSchema._validate_auth`` (tunstrap/schemas.py) accepts a node with
    no ``ssh_pkey``/``ssh_password`` only when ambient ``SSH_AUTH_SOCK`` is
    set -- correct production behaviour that must not change. The hazard is
    on the test side: a test that neither sets nor unsets the variable passes
    on any host with an ssh-agent (dev workstations, macOS runners) and fails
    on hosts without one (ubuntu runners). That asymmetry kept ubuntu unit CI
    red for ~20 days while every agent-bearing host stayed green (#35; the
    six affected tests were point-fixed in 085da79). Making "absent" the
    tier-wide default turns the hidden dependency into a failure on every
    host, not just agent-less ones.

    A test that legitimately needs an agent opts in explicitly with
    ``monkeypatch.setenv("SSH_AUTH_SOCK", ...)`` -- the existing convention;
    see tests/unit/test_schemas.py, tests/unit/test_cli_input.py,
    tests/unit/test_ssh_transport.py, and tests/unit/test_cli_runner.py.
    Deliberately scoped to the unit tier: this conftest is not loaded by the
    integration or e2e tiers, whose sshd containers never see the host's
    agent socket.
    """
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)


def make_node(**overrides: Any) -> dict[str, Any]:
    """Minimal valid NodeInput payload for tests."""
    base: dict[str, Any] = {
        "host": "node1.example.net",
        "user": "ubuntu",
        "ssh_password": "p",
        "remote_targets": {"p": "127.0.0.1:6443"},
    }
    base.update(overrides)
    return base


def cleaning_teardown(
    session_dir: str, grace_seconds: int, *, minted_root: str | None = None
) -> None:
    """Stand-in for ``cli._teardown_run`` that still removes a run-minted root.

    ``run`` mints its own session directory before spawning when the caller
    supplies no ``--session-dir``, and owns removing it. A stub that ignored
    ``minted_root`` would leak one temp directory per test. The ``None``
    default keeps this usable before that parameter exists (Task 4.1), and
    means a caller-supplied ``--session-dir`` is never removed — matching the
    real ``_teardown_run``, which also never touches a caller's directory.
    """
    del session_dir, grace_seconds  # signature parity with cli._teardown_run
    if minted_root is not None:
        shutil.rmtree(minted_root, ignore_errors=True)
