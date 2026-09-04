"""Cross-host forward through an SSH bastion into an isolated docker network.

Validates: a tunnel into sshd-bastion can forward to http-target-1 / http-target-2
which are unreachable from the host directly. Each target serves a unique
identity string over HTTP; the test verifies that the forward delivered the
request to the correct container.
Code: tunstrap/ssh.py::open_local_forwards (RemoteTarget host:port path).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any

import pytest

from tests.integration.conftest import tunstrap_start

pytestmark = pytest.mark.integration


def _http_get(local_port: int, timeout: float = 5.0) -> bytes:
    """Issue HTTP GET to 127.0.0.1:local_port and return the response body."""
    url = f"http://127.0.0.1:{local_port}/"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def test_forward_into_internal_network_identifies_target(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[str],
) -> None:
    """Each handle reaches its named target; HTTP body proves the route."""
    payload = {
        "nodes": {
            "edge": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["bastion_port"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {
                    "echo1": "target-1:80",
                    "echo2": "target-2:80",
                },
            }
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    ports = body["connections"]["edge"]["ports"]
    assert set(ports.keys()) == {"echo1", "echo2"}

    assert _http_get(ports["echo1"]) == b"target-1"
    assert _http_get(ports["echo2"]) == b"target-2"

    started_daemons.append(body["session_dir"])


def test_multiple_sequential_requests_through_same_forward(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[str],
) -> None:
    """A single forward serves multiple sequential HTTP requests."""
    payload = {
        "nodes": {
            "edge": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["bastion_port"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"echo": "target-1:80"},
            }
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    local_port = body["connections"]["edge"]["ports"]["echo"]

    # Make five sequential requests; each should return the same identity.
    for _ in range(5):
        assert _http_get(local_port) == b"target-1"

    started_daemons.append(body["session_dir"])


def test_required_unreachable_target_fails_start(
    ssh_test_cluster: dict[str, Any],
) -> None:
    """A required node whose far end is dead fails `start` (issue #51).

    The startup probe used to validate only the local listener accept,
    which the kernel completes regardless of the remote target; sshd
    answers a channel-open failure per connection, so `start` reported
    success for a forward that could never carry data. For `required:
    true` nodes the far end is now probed once per target at start, and
    an unreachable target is reported with the existing required-tunnel
    vocabulary (RequiredTunnelFailure, exit code 2).
    """
    payload = {
        "nodes": {
            "edge": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["bastion_port"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"dead": "127.0.0.1:1"},
            }
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 2, outcome["stderr"]
    body = outcome["json"]
    assert body["error"] == "RequiredTunnelFailure"
    failed = body["details"]["failed"]
    assert [entry["node"] for entry in failed] == ["edge"]
    assert "unreachable" in failed[0]["error"]
    assert "127.0.0.1:1" in failed[0]["error"]


def test_optional_unreachable_target_keeps_todays_behaviour(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[str],
) -> None:
    """`start` succeeds for an optional node with an unreachable target.

    Optional nodes keep exactly today's contract: no far-end round trip,
    no new failure mode. SSH local forwards bind lazily, and the failure
    surfaces at first-byte time when the SSH server returns
    CHANNEL_OPEN_FAILURE. The caller observes this as a connection reset
    or empty response on the HTTP layer.
    """
    payload = {
        "nodes": {
            "edge": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["bastion_port"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"dead": "127.0.0.1:1"},
                "required": False,
            }
        }
    }
    outcome = tunstrap_start(payload)
    # Start succeeds because no far-end probe runs for optional nodes.
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    local_port = body["connections"]["edge"]["ports"]["dead"]

    # HTTP request through the tunnel must fail — pick any of the expected
    # error types from urllib/socket. We accept Exception broadly because the
    # exact type depends on whether asyncssh closes the channel cleanly (EOF
    # → empty response, RemoteDisconnected) or with RST (ConnectionResetError).
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        _http_get(local_port, timeout=10.0)

    started_daemons.append(body["session_dir"])
