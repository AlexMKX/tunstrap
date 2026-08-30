"""CLI run-mode integration scenarios (#6 flag mode / --output env, #5 run wrapper).

Validates against the same Docker SSH fixtures as the other integration tests:
- `start USER@HOST:PORT --ssh-key <file> --target NAME=... --output env` emits
  shell `export` lines for the three session survivors; the port materialized
  under TUNSTRAP_OUTPUT_FILE accepts a TCP connection (proves the forward is
  live); `stop --session-dir` cleans up.
- `run USER@HOST ... -- CMD` injects TUNSTRAP_*/KUBECONFIG, runs the child,
  tears the session down afterwards, and propagates the child's exit code.

Auth + host wiring reuse the `ssh_test_cluster` fixture (conftest.py): SSH key
auth (PEM written to a tmp file for the `--ssh-key` flag) into the bastion
container, forwarding to the internal-network `target-1:80` HTTP service.
The connection host is spelled ``localhost`` (which resolves to the fixture's
``127.0.0.1``) because flag mode derives the InputSchema node key from the host
and the node-key grammar (^[a-zA-Z_][a-zA-Z0-9_-]*$) rejects IP literals; this
is the same container and same key auth, just a hostname instead of a dotted
quad. Code: tunstrap/cli.py (start flag mode + --output env, run wrapper),
tunstrap/cli_input.py, tunstrap/envrender.py.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _connection(cluster: dict[str, Any]) -> str:
    """Build USER@HOST:PORT for flag mode using a hostname for the loopback host.

    The fixture host is the IP literal ``127.0.0.1``; flag mode uses the host as
    the schema node key, whose grammar rejects IP literals, so we spell the same
    loopback address as ``localhost`` (resolves to 127.0.0.1) to reach the very
    same bastion container with the very same key auth.
    """
    assert cluster["host"] == "127.0.0.1", cluster["host"]
    return f"{cluster['user']}@localhost:{cluster['bastion_port']}"


def _write_key(tmp_path: Path, pem: str) -> Path:
    """Write the fixture PEM to a 0600 file for the --ssh-key flag."""
    key_path = tmp_path / "id_test"
    key_path.write_text(pem)
    key_path.chmod(0o600)
    return key_path


def _parse_exports(stdout: str) -> dict[str, str]:
    """Parse ``export K='V'`` lines emitted by --output env into a dict."""
    env: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        assignment = line[len("export ") :]
        key, _, value = assignment.partition("=")
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("'\\''", "'")
        env[key] = value
    return env


def _tcp_connect_ok(port: int, timeout: float = 5.0) -> bool:
    """Return True if a TCP connection to 127.0.0.1:port succeeds."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def test_start_output_env_live_forward(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[str],
    tmp_path: Path,
) -> None:
    """`start ... --output env` advertises a port that accepts a TCP connect."""
    key_path = _write_key(tmp_path, ssh_test_cluster["private_pem"])
    session_dir = tmp_path / "session"
    connection = _connection(ssh_test_cluster)

    result = subprocess.run(
        [
            "tunstrap",
            "start",
            connection,
            "--ssh-key",
            str(key_path),
            "--target",
            "web=target-1:80",
            "--output",
            "env",
            "--session-dir",
            str(session_dir),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    env = _parse_exports(result.stdout)
    started_daemons.append(str(session_dir))

    assert "TUNSTRAP_WEB_PORT" not in env, env
    assert env["TUNSTRAP_SESSION_DIR"] == str(session_dir), env
    assert "TUNSTRAP_OUTPUT_FILE" in env, env
    with open(env["TUNSTRAP_OUTPUT_FILE"], encoding="utf-8") as materialized_file:
        materialized = json.load(materialized_file)
    endpoint = materialized["nodes"]["node"]["ports"]["web"]
    port = int(endpoint.rsplit(":", 1)[1])
    assert _tcp_connect_ok(port), f"TCP connect to forwarded port {port} failed"

    stop = subprocess.run(
        ["tunstrap", "stop", "--session-dir", str(session_dir)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert stop.returncode == 0, f"stop stdout={stop.stdout!r} stderr={stop.stderr!r}"


def test_run_success_and_teardown(
    ssh_test_cluster: dict[str, Any],
    tmp_path: Path,
) -> None:
    """`run ... -- <connect probe>` exits 0 and removes the session afterwards."""
    key_path = _write_key(tmp_path, ssh_test_cluster["private_pem"])
    session_dir = tmp_path / "session"
    connection = _connection(ssh_test_cluster)

    # Child probes the materialized web port via a host-side TCP connect.
    probe = (
        "import json, os, socket; "
        "m = json.load(open(os.environ['TUNSTRAP_OUTPUT_FILE'])); "
        "endpoint = m['nodes']['node']['ports']['web']; "
        "port = int(endpoint.rsplit(':', 1)[1]); "
        "socket.create_connection(('127.0.0.1', port), 5).close()"
    )
    result = subprocess.run(
        [
            "tunstrap",
            "run",
            connection,
            "--ssh-key",
            str(key_path),
            "--target",
            "web=target-1:80",
            "--session-dir",
            str(session_dir),
            "--",
            sys.executable,
            "-c",
            probe,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    # Teardown must have removed the session's tunnel-data (no leaked daemon).
    assert not (session_dir / "tunnel-data").exists(), "teardown left tunnel-data behind"


def test_run_propagates_child_exit_code(
    ssh_test_cluster: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A non-zero child exit code is propagated by `run` (child code wins)."""
    key_path = _write_key(tmp_path, ssh_test_cluster["private_pem"])
    session_dir = tmp_path / "session"
    connection = _connection(ssh_test_cluster)

    result = subprocess.run(
        [
            "tunstrap",
            "run",
            connection,
            "--ssh-key",
            str(key_path),
            "--target",
            "web=target-1:80",
            "--session-dir",
            str(session_dir),
            "--",
            "sh",
            "-c",
            "exit 7",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 7, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert not (session_dir / "tunnel-data").exists(), "teardown left tunnel-data behind"
