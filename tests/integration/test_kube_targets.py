"""End-to-end kube_targets over a real sshd forward + fake apiserver.

Validates: start with a kube_target produces a patched kubeconfig whose
server points at the local forwarded port and whose tls-server-name is the
probed SAN; materialize writes the file; stop cleans up the session dir.
Code: tunstrap kube mode (kube.py, manager.py, _worker.py, cli.py)
Assertion: output.connections[node].kube_targets.k3s.endpoint is local; the
materialized kubeconfig has tls-server-name == 'dev-kube-1'; path exists then is removed.
Method: drive `tunstrap start`/`stop` subprocesses against compose.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from tests.compose import compose_command

pytestmark = [pytest.mark.integration]
REPO_ROOT = Path(__file__).resolve().parents[2]


def _wait_for_apiserver(compose_file: Path, service: str, port: int = 6443) -> None:
    """Wait until the openssl s_server inside `service` is accepting TCP."""
    for _ in range(60):
        probe = subprocess.run(
            compose_command(
                compose_file,
                REPO_ROOT,
                "integration",
                "exec",
                "-T",
                "sshd-bastion",
                "nc",
                "-z",
                service,
                str(port),
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.5)
    pytest.fail(f"{service}:{port} never came up")


def test_kube_target_end_to_end(
    ssh_test_cluster: dict[str, Any], prepared_files: dict[str, Path]
) -> None:
    """A kube_target yields a locally-usable, SAN-correct, patched kubeconfig."""
    # Compose file path is relative to the integration test dir.
    compose_file = Path(__file__).resolve().parent / "docker-compose.yml"
    _wait_for_apiserver(compose_file, "fake-apiserver")

    # Ensure the prepared kube_k3s fixture is in place (via prepared_files).
    assert prepared_files["kube_k3s"].is_file()

    session_dir = tempfile.mkdtemp(prefix="gt-kube-it-")
    payload = {
        "nodes": {
            "node": {
                "host": ssh_test_cluster["host"],
                "port": ssh_test_cluster["bastion_port"],
                "user": ssh_test_cluster["user"],
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"keep": "127.0.0.1:2222"},
                "kube_targets": {"k3s": {"kubeconfig_path": "/srv/files/kube_k3s"}},
            }
        },
        "daemon": {"materialize": True, "auto_stop_idle_seconds": 60},
    }
    result = subprocess.run(
        ["tunstrap", "start", "--session-dir", session_dir],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    out = json.loads(result.stdout)
    kt = out["connections"]["node"]["kube_targets"]["k3s"]
    assert kt["endpoint"].startswith("https://127.0.0.1:"), kt
    assert kt["context"] == "tunstrap-node-k3s", kt
    assert kt["path"] is not None and Path(kt["path"]).is_file(), kt
    patched = Path(kt["path"]).read_text()
    assert "127.0.0.1" in patched
    assert "tls-server-name: dev-kube-1" in patched
    assert set(kt) == {"path", "context", "endpoint"}, kt

    # Stop cleans up the session dir's tunnel-data.
    stop = subprocess.run(
        ["tunstrap", "stop", "--session-dir", session_dir],
        text=True,
        capture_output=True,
        check=False,
    )
    assert stop.returncode == 0, f"stop stdout={stop.stdout!r} stderr={stop.stderr!r}"
    assert not (Path(session_dir) / "tunnel-data").exists()


def test_kube_target_insecure_fallback(
    ssh_test_cluster: dict[str, Any], prepared_files: dict[str, Path]
) -> None:
    """A SAN-less cert with insecure_fallback=True yields insecure-skip-tls-verify."""
    compose_file = Path(__file__).resolve().parent / "docker-compose.yml"
    _wait_for_apiserver(compose_file, "fake-apiserver-nosan")

    assert prepared_files["kube_nosan"].is_file()

    session_dir = tempfile.mkdtemp(prefix="gt-kube-it-")
    payload = {
        "nodes": {
            "node": {
                "host": ssh_test_cluster["host"],
                "port": ssh_test_cluster["bastion_port"],
                "user": ssh_test_cluster["user"],
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"keep": "127.0.0.1:2222"},
                "kube_targets": {
                    "k3s": {
                        "kubeconfig_path": "/srv/files/kube_nosan",
                        "insecure_fallback": True,
                    }
                },
            }
        },
        "daemon": {"auto_stop_idle_seconds": 60},
    }
    result = subprocess.run(
        ["tunstrap", "start", "--session-dir", session_dir],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    out = json.loads(result.stdout)
    kt = out["connections"]["node"]["kube_targets"]["k3s"]
    assert kt["tls_server_name"] is None, kt
    assert kt["certificate_authority_data"] == "", kt
    patched = base64.b64decode(kt["content_b64"]).decode()
    assert "insecure-skip-tls-verify: true" in patched
    assert any("insecure_fallback" in w["error"] for w in out["warnings"]), out["warnings"]

    subprocess.run(
        ["tunstrap", "stop", "--session-dir", session_dir],
        text=True,
        capture_output=True,
        check=False,
    )
