"""`run --input-env` / `--output-var` against the real docker SSH rig.

Validates: a complete InputSchema handed to run through the environment opens
real tunnels; the child receives the unified output structure as JSON
(projected to drop the kube credentials — see
tests/unit/test_cli_run_output_var_projection.py) and the advertised endpoints
actually accept connections; multi-node results carry every node and no
target-scoped TUNSTRAP_* scalars; and the teardown removes the session.

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
assert sorted(envelope["nodes"]) == ["hub"], envelope["nodes"]
endpoint = envelope["nodes"]["hub"]["ports"]["web"]
materialized = json.load(open(os.environ["TUNSTRAP_OUTPUT_FILE"]))
assert materialized["nodes"]["hub"]["ports"]["web"] == endpoint, (
    materialized["nodes"]["hub"]["ports"]["web"], endpoint
)
port = int(endpoint.rsplit(":", 1)[1])
assert envelope["session"]["pid"] > 0, envelope["session"]["pid"]
assert envelope["session"]["session_dir"], envelope["session"]["session_dir"]
socket.create_connection(("127.0.0.1", port), 5).close()
sys.stdout.write("PROBE_OK")
"""

_PROBE_MULTI = """
import json, os, socket, sys
envelope = json.loads(os.environ["TF_VAR_tunstrap"])
assert sorted(envelope["nodes"]) == ["edge", "hub"], sorted(envelope["nodes"])
# TUNSTRAP_INPUT is the payload variable the parent inherited, not an injected
# scalar; TUNSTRAP_SESSION_DIR/_PID/_OUTPUT_FILE are the three sanctioned
# survivors, unconditional on node count -- not the ambiguous per-target scalars.
survivors = {"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE", "TUNSTRAP_INPUT"}
leaked = sorted(k for k in os.environ if k.startswith("TUNSTRAP_") and k not in survivors)
assert leaked == [], leaked
for name in ("hub", "edge"):
    port = int(envelope["nodes"][name]["ports"]["web"].rsplit(":", 1)[1])
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


def test_multi_node_without_output_var_now_succeeds(
    ssh_test_cluster: dict[str, Any], tmp_path: Path, started_daemons: list[str]
) -> None:
    """Two nodes and no --output-var succeeds: materialization covers multi-node
    unconditionally, so the opt-in --output-var gate has nothing left to force.

    Materialization *content* is not re-verified here -- that is the unit
    tier's job (test_cli_run_materialize.py); this test's remaining job is
    confirming the real console script allows the case.
    """
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
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
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "MultiNodeEnvUnsupported" not in result.stderr, result.stderr
    assert not (session_dir / "tunnel-data").exists(), "teardown left tunnel-data behind"


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
