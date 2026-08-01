"""`run --input-env` / `--output-var` against the real docker SSH rig.

Validates: a complete InputSchema handed to run through the environment opens
real tunnels; the child receives the full OutputSchema as JSON and the
advertised endpoints actually accept connections; multi-node results carry
every node and no ambiguous TUNSTRAP_* scalars; and the teardown removes the
session.

Both nodes point at `sshd-bastion`: it is the only service in the rig with
AllowTcpForwarding enabled and a route to the internal `target-1`, so it is
the only host through which bytes can move. Using two node keys for the same
container still exercises the multi-node output path end to end.

Code: tunstrap/cli.py, tunstrap/cli_input.py, tunstrap/envrender.py
Method: run the installed `tunstrap` console script as a subprocess with the
payload in its environment; the child is a Python probe that asserts on the
injected variables and opens real TCP connections to the forwarded ports.

No assertion on `result.stderr` here: these tests own the env-injection
contract, and the separate claim that a successful run leaves stderr empty is
asserted in test_run_teardown_latency.py. An earlier version of this note said
the opposite -- that every successful run emitted `run: daemon not stopped
cleanly: identity changed during grace` and spent the whole 10s grace window
doing it. That was the unreaped-zombie defect in the grace poll, since fixed
in session.py (`_has_exited`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_TIMEOUT = 120

_PROBE_SINGLE = """
import json, os, socket, sys
envelope = json.loads(os.environ["TF_VAR_tunstrap"])
assert sorted(envelope["connections"]) == ["hub"], envelope["connections"]
port = envelope["connections"]["hub"]["ports"]["web"]
assert os.environ["TUNSTRAP_WEB_PORT"] == str(port), (
    os.environ["TUNSTRAP_WEB_PORT"], port
)
assert envelope["pid"] > 0, envelope["pid"]
assert envelope["session_dir"], envelope["session_dir"]
socket.create_connection(("127.0.0.1", port), 5).close()
sys.stdout.write("PROBE_OK")
"""

_PROBE_MULTI = """
import json, os, socket, sys
envelope = json.loads(os.environ["TF_VAR_tunstrap"])
assert sorted(envelope["connections"]) == ["edge", "hub"], sorted(envelope["connections"])
# TUNSTRAP_INPUT is the payload variable the parent inherited, not an injected scalar.
leaked = sorted(
    k for k in os.environ if k.startswith("TUNSTRAP_") and k != "TUNSTRAP_INPUT"
)
assert leaked == [], leaked
for name in ("hub", "edge"):
    port = envelope["connections"][name]["ports"]["web"]
    socket.create_connection(("127.0.0.1", port), 5).close()
sys.stdout.write("PROBE_OK")
"""


def _node(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": cluster["host"],
        "port": cluster["bastion_port"],
        "user": cluster["user"],
        "ssh_pkey": cluster["private_pem"],
        "remote_targets": {"web": "target-1:80"},
        "required": True,
    }


def _payload(cluster: dict[str, Any], *, names: list[str]) -> str:
    return json.dumps({"nodes": {name: _node(cluster) for name in names}})


def _run(
    args: list[str], payload: str | None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # Environment hygiene, mirroring the shim's `env -u KUBECONFIG`: an
    # inherited KUBECONFIG is a silent fallback route to a cluster and has no
    # business in a test about tunnels. It is hygiene here, not an oracle --
    # these nodes declare no kube_targets, so run would never inject one and an
    # assertion on its absence could not fail. The falsifiable version of that
    # check is the unit test `test_multi_node_suppression_uses_input_count`.
    env.pop("KUBECONFIG", None)
    if payload is None:
        env.pop("TUNSTRAP_INPUT", None)
    else:
        env["TUNSTRAP_INPUT"] = payload
    return subprocess.run(
        ["tunstrap", *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
        cwd=cwd,
        timeout=_TIMEOUT,
    )


def test_single_node_env_input_and_structured_output(
    ssh_test_cluster: dict[str, Any],
    tmp_path: Path,
    started_daemons: list[str],
) -> None:
    """One node: the child gets the envelope, the scalars, and a live endpoint."""
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
    result = _run(
        [
            "run",
            "--input-env",
            "TUNSTRAP_INPUT",
            "--output-var",
            "TF_VAR_tunstrap",
            "--session-dir",
            str(session_dir),
            "--",
            sys.executable,
            "-c",
            _PROBE_SINGLE,
        ],
        _payload(ssh_test_cluster, names=["hub"]),
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout == "PROBE_OK"
    assert not (session_dir / "tunnel-data").exists(), "teardown left tunnel-data behind"


def test_multi_node_env_input_carries_every_node_and_no_scalars(
    ssh_test_cluster: dict[str, Any],
    tmp_path: Path,
    started_daemons: list[str],
) -> None:
    """Two nodes: both endpoints live, envelope keyed by node, zero TUNSTRAP_* leak."""
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
    result = _run(
        [
            "run",
            "--input-env",
            "TUNSTRAP_INPUT",
            "--output-var",
            "TF_VAR_tunstrap",
            "--session-dir",
            str(session_dir),
            "--",
            sys.executable,
            "-c",
            _PROBE_MULTI,
        ],
        _payload(ssh_test_cluster, names=["hub", "edge"]),
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout == "PROBE_OK"
    assert not (session_dir / "tunnel-data").exists(), "teardown left tunnel-data behind"


def test_multi_node_without_output_var_is_exit_1(
    ssh_test_cluster: dict[str, Any], tmp_path: Path
) -> None:
    """Two nodes and no --output-var: rejected before any daemon is spawned."""
    session_dir = tmp_path / "session"
    result = _run(
        [
            "run",
            "--input-env",
            "TUNSTRAP_INPUT",
            "--session-dir",
            str(session_dir),
            "--",
            "true",
        ],
        _payload(ssh_test_cluster, names=["hub", "edge"]),
    )
    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert json.loads(result.stderr)["error"] == "MultiNodeEnvUnsupported"
    assert not session_dir.exists(), "a pre-spawn rejection created a session dir"


def test_unset_payload_variable_is_exit_1(tmp_path: Path) -> None:
    """An unset payload variable is a typed exit-1 error on stderr."""
    del tmp_path  # the run is rejected before any session dir is touched
    result = _run(["run", "--input-env", "TUNSTRAP_INPUT", "--", "true"], None)
    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert json.loads(result.stderr)["error"] == "SchemaValidationError"


def test_connection_flags_under_input_env_are_exit_64(
    ssh_test_cluster: dict[str, Any],
) -> None:
    """The conflict matrix holds through the real console script, not just CliRunner."""
    result = _run(
        [
            "run",
            "--input-env",
            "TUNSTRAP_INPUT",
            "--target",
            "web=target-1:80",
            "--",
            "true",
        ],
        _payload(ssh_test_cluster, names=["hub"]),
    )
    assert result.returncode == 64, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "connection flags are redundant" in result.stderr
