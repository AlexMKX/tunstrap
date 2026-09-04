"""Multi-port local forwarding.

Validates: a node with two entries pointing at the same remote port
produces two distinct local listeners.
Code: tunstrap/ssh.py::open_local_forwards
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import tunstrap_start

pytestmark = pytest.mark.integration


def test_two_forwards_to_same_remote_port(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[str],
) -> None:
    """Two forwards to the same remote port yield distinct local ports."""
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["bastion_port"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {
                    "p1": "127.0.0.1:2222",
                    "p2": "127.0.0.1:2222",
                },
            }
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0
    body = outcome["json"]
    node_out = body["connections"]["a"]
    entries = node_out["ports"]
    assert len(entries) == 2
    assert entries["p1"] != entries["p2"]
    assert node_out["fetch_files"] == {}
    started_daemons.append(body["session_dir"])
