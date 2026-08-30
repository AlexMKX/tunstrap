"""tunstrap stop against a real daemon.

Validates: stop terminates an alive daemon, refuses on wrong identity,
and reports not-found on a non-existent PID.
Code: tunstrap/cli.py::stop_command
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import tunstrap_start

pytestmark = pytest.mark.integration


def _start(ssh_test_cluster: dict[str, Any]) -> dict[str, Any]:
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
    assert outcome["returncode"] == 0
    assert outcome["json"] is not None
    return outcome["json"]


def test_stop_alive_daemon(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[str],
) -> None:
    """stop on a live daemon kills the process and reports stopped=True."""
    body = _start(ssh_test_cluster)
    started_daemons.append(body["session_dir"])
    session_dir = body["session_dir"]
    stop = subprocess.run(
        ["tunstrap", "stop", "--session-dir", session_dir],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    assert json.loads(stop.stdout)["stopped"] is True
    # The daemon process must no longer exist.
    try:
        os.kill(body["pid"], 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert alive is False


def test_stop_foreign_session_dir(
    ssh_test_cluster: dict[str, Any], started_daemons: list[str]
) -> None:
    """stop against a foreign session dir (no session.lock) refuses.

    We craft a session dir whose tunnel-data/ records the real PID, but the
    dir has no session.lock (the live daemon's lock lives in its own dir).
    verify_session sees the process is alive but no lock exists here, so it
    returns not_found. The CLI reports reason "not found"; the real daemon
    keeps running and is cleaned up via its own session dir.
    """
    body = _start(ssh_test_cluster)
    # Build a fake session dir: real PID, but no session.lock here.
    with tempfile.TemporaryDirectory(prefix="tunstrap-stop-test-") as fake_session:
        data_dir = Path(fake_session) / "tunnel-data"
        data_dir.mkdir(mode=0o700)
        (data_dir / "daemon.pid").write_text(f"{body['pid']}\n")

        stop = subprocess.run(
            ["tunstrap", "stop", "--session-dir", fake_session],
            capture_output=True,
            text=True,
        )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert payload["reason"] == "not found"
    # Clean up the real daemon.
    started_daemons.append(body["session_dir"])
    subprocess.run(
        ["tunstrap", "stop", "--session-dir", body["session_dir"]],
        capture_output=True,
    )


def test_stop_already_dead() -> None:
    """stop on a non-existent PID returns reason='not found'."""
    with tempfile.TemporaryDirectory(prefix="tunstrap-stop-test-") as fake_session:
        data_dir = Path(fake_session) / "tunnel-data"
        data_dir.mkdir(mode=0o700)
        (data_dir / "daemon.pid").write_text(f"{2**31 - 1}\n")

        stop = subprocess.run(
            ["tunstrap", "stop", "--session-dir", fake_session],
            capture_output=True,
            text=True,
        )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert payload["reason"] == "not found"
