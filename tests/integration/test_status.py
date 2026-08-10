"""tunstrap status against a real daemon.

Validates: alive-then-dead status transitions keyed off --session-dir
through the real CLI binary.
Code: tunstrap/cli.py::status_command
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from tests.integration.conftest import tunstrap_start

pytestmark = pytest.mark.integration


def test_status_alive_then_dead(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[str],
) -> None:
    """status flips from alive to dead (stale session) keyed off --session-dir."""
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"p": "127.0.0.1:6443"},
            }
        }
    }
    outcome = tunstrap_start(payload)
    body = outcome["json"]
    session_dir = body["session_dir"]

    alive = subprocess.run(
        ["tunstrap", "status", "--session-dir", session_dir],
        capture_output=True,
        text=True,
    )
    assert json.loads(alive.stdout)["alive"] is True

    subprocess.run(
        ["tunstrap", "stop", "--session-dir", session_dir],
        capture_output=True,
    )

    # Stale/dead session: the daemon is gone, so status reports not alive.
    dead = subprocess.run(
        ["tunstrap", "status", "--session-dir", session_dir],
        capture_output=True,
        text=True,
    )
    assert json.loads(dead.stdout)["alive"] is False
