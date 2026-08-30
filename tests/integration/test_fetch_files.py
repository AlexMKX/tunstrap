"""Fetch-files end-to-end against dockerized sshd.

Validates: tunstrap start fetch_files happy path, required vs
optional file errors, EFBIG cap, and the NodeOutput shape over the
real CLI process.
Code: tunstrap/manager.py, tunstrap/fetcher.py
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import tunstrap_start

pytestmark = pytest.mark.integration


def _node_with_fetch(
    host: str,
    port: int,
    pem: str,
    fetch: dict[str, dict[str, Any]],
    remote_port: int = 6443,
    required: bool = True,
) -> dict[str, Any]:
    return {
        "host": host,
        "user": "tester",
        "port": port,
        "ssh_pkey": pem,
        "remote_targets": {"p": f"127.0.0.1:{remote_port}"},
        "required": required,
        "fetch_files": fetch,
    }


def test_fetch_single_file_roundtrip(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
    started_daemons: list[str],
) -> None:
    """A single fetched file returns identical bytes, size, and sha256."""
    payload = {
        "nodes": {
            "a": _node_with_fetch(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
                fetch={"kubeconfig": {"path": "/srv/files/kubeconfig"}},
            )
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    started_daemons.append(body["session_dir"])

    node_out = body["connections"]["a"]
    assert "ports" in node_out
    assert "fetch_files" in node_out
    ff = node_out["fetch_files"]["kubeconfig"]
    decoded = base64.b64decode(ff["content_b64"])
    host_bytes = prepared_files["kubeconfig"].read_bytes()
    assert decoded == host_bytes
    assert ff["size"] == len(host_bytes)
    assert ff["sha256"] == hashlib.sha256(host_bytes).hexdigest()


def test_fetch_required_missing_fails_node(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
) -> None:
    """Missing required file aborts the node with RequiredTunnelFailure."""
    payload = {
        "nodes": {
            "a": _node_with_fetch(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
                fetch={"absent": {"path": "/srv/files/does-not-exist"}},
            )
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 2
    body = outcome["json"]
    assert body["error"] == "RequiredTunnelFailure"


def test_fetch_optional_missing_soft_fails(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
    started_daemons: list[str],
) -> None:
    """Missing optional file is reported as SSH_FX_NO_SUCH_FILE; tunnel starts."""
    payload = {
        "nodes": {
            "a": _node_with_fetch(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
                fetch={
                    "kubeconfig": {"path": "/srv/files/kubeconfig"},
                    "absent": {"path": "/srv/files/does-not-exist", "required": False},
                },
            )
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    started_daemons.append(body["session_dir"])
    ff = body["connections"]["a"]["fetch_files"]
    assert ff["kubeconfig"]["content_b64"]
    assert ff["absent"]["error"] == "SSH_FX_NO_SUCH_FILE"


def test_fetch_perm_denied_required_fails(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
) -> None:
    """Permission-denied required file aborts the node."""
    payload = {
        "nodes": {
            "a": _node_with_fetch(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
                fetch={"locked": {"path": "/srv/files/no-perm.txt"}},
            )
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 2
    body = outcome["json"]
    assert body["error"] == "RequiredTunnelFailure"
    assert "locked" in str(body["details"])


def test_fetch_over_cap_efbig_required(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
) -> None:
    """Required file exceeding the EFBIG cap aborts the node."""
    payload = {
        "nodes": {
            "a": _node_with_fetch(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
                fetch={"big": {"path": "/srv/files/big.bin"}},
            )
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 2
    body = outcome["json"]
    assert body["error"] == "RequiredTunnelFailure"
    assert "big" in str(body["details"])


def test_fetch_over_cap_efbig_optional(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
    started_daemons: list[str],
) -> None:
    """Optional file exceeding the EFBIG cap is recorded as soft error."""
    payload = {
        "nodes": {
            "a": _node_with_fetch(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
                fetch={"big": {"path": "/srv/files/big.bin", "required": False}},
            )
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    started_daemons.append(body["session_dir"])
    ff = body["connections"]["a"]["fetch_files"]
    assert ff["big"]["error"] == "EFBIG"


def test_fetch_multiple_files_mixed(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
    started_daemons: list[str],
) -> None:
    """Mixed required-ok + optional-missing + optional-too-big all coexist."""
    payload = {
        "nodes": {
            "a": _node_with_fetch(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
                fetch={
                    "kubeconfig": {"path": "/srv/files/kubeconfig"},
                    "absent": {"path": "/srv/files/missing", "required": False},
                    "big": {"path": "/srv/files/big.bin", "required": False},
                },
            )
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0
    body = outcome["json"]
    started_daemons.append(body["session_dir"])
    ff = body["connections"]["a"]["fetch_files"]
    assert ff["kubeconfig"]["content_b64"]
    assert ff["absent"]["error"] == "SSH_FX_NO_SUCH_FILE"
    assert ff["big"]["error"] == "EFBIG"


def test_fetch_files_breaking_shape(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
    started_daemons: list[str],
) -> None:
    """Successful start emits exactly {ports, fetch_files} per node."""
    payload = {
        "nodes": {
            "a": _node_with_fetch(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
                fetch={"kubeconfig": {"path": "/srv/files/kubeconfig"}},
            )
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0
    body = outcome["json"]
    started_daemons.append(body["session_dir"])
    node_out = body["connections"]["a"]
    assert isinstance(node_out, dict)
    assert set(node_out.keys()) == {"ports", "fetch_files", "kube_targets"}
    assert isinstance(node_out["ports"], dict)
    assert isinstance(node_out["fetch_files"], dict)
