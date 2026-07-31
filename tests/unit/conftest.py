"""Unit-test helpers (shared ``make_node`` payload factory)."""

from __future__ import annotations

import shutil
from typing import Any


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
