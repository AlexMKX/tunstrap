"""End-to-end: daemon shuts itself down when no forward sees traffic.

Validates: with daemon.auto_stop_idle_seconds set, the daemon process
exits on its own after the configured idle window passes with no client
connections. The identity lockfile is cleaned up.
Code: tunstrap/_worker.py::_idle_watchdog,
      tunstrap/activity.py::ActivityTracker.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import tunstrap_start

pytestmark = pytest.mark.integration


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_idle_auto_stop_kills_daemon(
    ssh_test_cluster: dict[str, Any],
) -> None:
    """Daemon with auto_stop_idle_seconds=3 exits within ~5s of start."""
    payload = {
        "nodes": {
            "edge": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["bastion_port"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"echo": "target-1:80"},
            }
        },
        "daemon": {
            "auto_stop_idle_seconds": 3,
        },
    }

    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    pid = body["pid"]
    session_dir = body["session_dir"]

    # While the daemon runs, its session.lock exists and is flock-held.
    lock_path = Path(session_dir) / "session.lock"
    assert lock_path.exists(), f"session.lock should exist while running: {lock_path}"

    # Daemon must exit within ~5s. We never connect to its forward.
    msg = f"daemon pid={pid} still alive after 8s; expected exit by ~3s"
    assert _wait_until_dead(pid, timeout=8.0), msg

    # session.lock cleaned up by graceful shutdown.
    assert not lock_path.exists(), f"stale session.lock {lock_path}"


def test_active_connection_prevents_auto_stop(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[str],
) -> None:
    """A long-lived TCP connection keeps the daemon alive past idle window."""
    payload = {
        "nodes": {
            "edge": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["bastion_port"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"echo": "target-1:80"},
            }
        },
        "daemon": {
            "auto_stop_idle_seconds": 3,
        },
    }

    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    pid = body["pid"]
    local_port = body["connections"]["edge"]["ports"]["echo"]

    # Open a TCP connection through the forward and hold it open.
    sock = socket.create_connection(("127.0.0.1", local_port), timeout=5)
    try:
        # Wait longer than auto_stop_idle_seconds; daemon must stay alive
        # because our socket counts as an active connection.
        time.sleep(5.0)
        assert _process_alive(pid), "daemon should still be alive while a connection is open"
    finally:
        sock.close()

    # After we close, wait for the idle window to elapse + small buffer.
    msg = f"daemon pid={pid} still alive after socket close + 8s"
    assert _wait_until_dead(pid, timeout=8.0), msg

    # Append to started_daemons so the session cleanup doesn't complain.
    started_daemons.append(body["session_dir"])
