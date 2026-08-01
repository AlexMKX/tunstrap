"""Fetched-file content stays out of logs.

Validates: fetched file bytes are emitted only on stdout; never copied
to the daemon log file or to stderr.
Code: tunstrap/manager.py, tunstrap/daemon.py
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import tunstrap_start

pytestmark = pytest.mark.integration


def test_log_file_does_not_contain_file_content(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
    started_daemons: list[str],
) -> None:
    """Fetched file content never appears in the daemon log file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as log:
        log_path = log.name
    try:
        payload = {
            "nodes": {
                "a": {
                    "host": "127.0.0.1",
                    "user": "tester",
                    "port": ssh_test_cluster["ports"]["sshd-a"],
                    "ssh_pkey": ssh_test_cluster["private_pem"],
                    "remote_targets": {"p": "127.0.0.1:6443"},
                    "fetch_files": {"kubeconfig": {"path": "/srv/files/kubeconfig"}},
                }
            },
            "daemon": {"log_file": log_path},
        }
        outcome = tunstrap_start(payload)
        assert outcome["returncode"] == 0, outcome["stderr"]
        body = outcome["json"]
        started_daemons.append(body["session_dir"])

        content_b64 = body["connections"]["a"]["fetch_files"]["kubeconfig"]["content_b64"]
        host_text = prepared_files["kubeconfig"].read_text()
        log_text = Path(log_path).read_text()
        assert content_b64 not in log_text
        assert "apiVersion: v1" not in log_text
        for needle in host_text.split("\n"):
            if needle.strip():
                assert needle not in log_text, f"leaked: {needle!r}"
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass


def test_stdout_only_carrier_of_content(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
    started_daemons: list[str],
) -> None:
    """Fetched content_b64 appears on stdout only, never on stderr."""
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "user": "tester",
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"p": "127.0.0.1:6443"},
                "fetch_files": {"kubeconfig": {"path": "/srv/files/kubeconfig"}},
            }
        }
    }
    outcome = tunstrap_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    started_daemons.append(body["session_dir"])

    content_b64 = body["connections"]["a"]["fetch_files"]["kubeconfig"]["content_b64"]
    assert content_b64 in outcome["stdout"]
    assert content_b64 not in outcome["stderr"]
