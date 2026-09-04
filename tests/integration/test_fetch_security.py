"""Fetched-file content stays out of logs, --output-var, and the materialized manifest.

Validates: fetched file bytes are emitted only on `start`'s raw stdout
envelope (that channel is unaffected and out of scope for R16/#15); never
copied to the daemon log file or to stderr; and -- the stronger property this
task adds -- never present in `--output-var`/the materialized `output.json`,
only reachable through the reported `path` on disk.
Code: tunstrap/manager.py, tunstrap/daemon.py, tunstrap/cli.py, tunstrap/envrender.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
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
                    "port": ssh_test_cluster["bastion_port"],
                    "ssh_pkey": ssh_test_cluster["private_pem"],
                    "remote_targets": {"p": "127.0.0.1:2222"},
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
    """Fetched content_b64 appears on start's raw stdout envelope, never on stderr.

    This test sends the default unmaterialized payload, for which start's JSON
    stdout retains the complete envelope. The stronger claim -- that fetched
    content never rides the consumer-facing
    --output-var/materialized channels -- is
    test_output_var_and_materialized_output_never_carry_fetched_content, below.
    """
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "user": "tester",
                "port": ssh_test_cluster["bastion_port"],
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"p": "127.0.0.1:2222"},
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


_FETCH_SECURITY_PROBE = """
import base64, hashlib, json, os, stat, sys

decoded = json.loads(os.environ["TF_VAR_tunstrap"])
ff = decoded["nodes"]["a"]["fetch_files"]["kubeconfig"]
assert "content_b64" not in ff, "content_b64 leaked into --output-var"
assert set(ff) == {"path", "size", "sha256"}, ff

materialized = json.load(open(os.environ["TUNSTRAP_OUTPUT_FILE"]))
mff = materialized["nodes"]["a"]["fetch_files"]["kubeconfig"]
assert "content_b64" not in mff, "content_b64 leaked into the materialized output.json"
assert mff["path"] == ff["path"]

# The checks that must run inside this process, before run's teardown
# removes tunnel-data/: mode and byte-identity of the materialized file.
raw = open(mff["path"], "rb").read()
assert stat.S_IMODE(os.stat(mff["path"]).st_mode) == 0o600
assert hashlib.sha256(raw).hexdigest() == mff["sha256"]
assert base64.b64encode(raw).decode() not in os.environ["TF_VAR_tunstrap"]
sys.stdout.write("PROBE_OK")
"""


def test_output_var_and_materialized_output_never_carry_fetched_content(
    ssh_test_cluster: dict[str, Any],
    prepared_files: dict[str, Path],
    tmp_path: Path,
    started_daemons: list[str],
) -> None:
    """Fetched content never rides --output-var or the materialized output.json
    -- only the 0600 on-disk file at the reported path carries it.

    A STRONGER security property than the sibling test above: that test
    accepts content_b64 riding start's raw stdout envelope (unaffected,
    unchanged, out of scope). This one proves fetched bytes are absent from
    every consumer-facing channel `run` exposes -- TF_VAR_tunstrap, the
    materialized output.json, stdout, and stderr -- and are reachable only
    through the reported `path`, whose bytes match the fetched source exactly.
    """
    session_dir = tmp_path / "session"
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "127.0.0.1",
                    "user": "tester",
                    "port": ssh_test_cluster["bastion_port"],
                    "ssh_pkey": ssh_test_cluster["private_pem"],
                    "remote_targets": {"p": "127.0.0.1:2222"},
                    "fetch_files": {"kubeconfig": {"path": "/srv/files/kubeconfig"}},
                }
            }
        }
    )
    env = dict(os.environ)
    env["TUNSTRAP_INPUT"] = payload
    started_daemons.append(str(session_dir))
    result = subprocess.run(
        [
            "tunstrap",
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
            _FETCH_SECURITY_PROBE,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout == "PROBE_OK", result.stdout

    # The mode/byte-identity checks against the materialized file itself run
    # inside the probe, above -- run's teardown removes tunnel-data/ once this
    # subprocess returns, so the file is gone by the time this assertion runs.
    host_bytes = prepared_files["kubeconfig"].read_bytes()
    content_b64 = base64.b64encode(host_bytes).decode()
    assert content_b64 not in result.stdout
    assert content_b64 not in result.stderr
    assert not (session_dir / "tunnel-data").exists(), "teardown left tunnel-data behind"
