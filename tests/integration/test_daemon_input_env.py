"""The detached worker must not retain run's secret input environment.

Validates: ``spawn_daemon`` removes *the variable ``--input-env`` actually
names* from the worker's environment — not a hardcoded ``TUNSTRAP_INPUT``.
``--input-env VAR`` takes an arbitrary name; that is the whole point of the
option, and the recipe's own consumers are free to call it anything. A scrub
keyed on one literal leaves the SSH private key PEM in the environment of a
long-lived detached process for every other name.

Code: tunstrap/daemon.py (spawn_daemon), tunstrap/cli.py (run_command)
Assertion: neither the variable name nor a distinctive marker carried *inside
its value* appears in ``/proc/<worker>/environ``, paired with a positive
``PATH=`` check so a worker handed an empty environment could not pass
vacuously.
Method: the real CLI, the real detached worker, read back through ``/proc`` —
no mocked ``Popen``. Parametrized over the canonical name and a deliberately
non-canonical one; the non-canonical case is the one that discriminates.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Lives only inside the input variable's *value*. The worker learns it over
# stdin (as daemon.log_file), never through the environment, so finding these
# bytes in the worker's environ means the variable's value survived — catching
# a scrub that dropped the name but left the value under some other key.
MARKER = "TUNSTRAP-WORKER-ENV-MARKER"


@pytest.mark.parametrize(
    "var_name",
    ["TUNSTRAP_INPUT", "TG_TUNSTRAP_PAYLOAD"],
    ids=["canonical", "non-canonical"],
)
def test_real_worker_does_not_inherit_the_input_variable(tmp_path: Path, var_name: str) -> None:
    """Inspect the real detached worker rather than a mocked Popen call.

    The ``non-canonical`` case is the regression guard: it is red against a
    scrub hardcoded to ``TUNSTRAP_INPUT`` and green only once the name
    ``--input-env`` was given is threaded through to ``spawn_daemon``.
    """
    session_dir = tmp_path / "session"
    log_file = tmp_path / f"{MARKER}.log"
    env = dict(os.environ)
    env.pop("TUNSTRAP_INPUT", None)  # no ambient value may satisfy this test
    env[var_name] = json.dumps({"nodes": {}, "daemon": {"log_file": str(log_file)}})
    proc = subprocess.Popen(
        [
            "tunstrap",
            "run",
            "--input-env",
            var_name,
            "--output-var",
            "TF_VAR_tunstrap",
            "--session-dir",
            str(session_dir),
            "--",
            "sleep",
            "10",
        ],
        env=env,
    )
    try:
        identity = session_dir / "tunnel-data" / "daemon.pid"
        deadline = time.monotonic() + 5
        while not identity.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert identity.exists(), "worker did not publish its identity"
        worker_env = Path(f"/proc/{identity.read_text().strip()}/environ").read_bytes()

        # Messages hoisted to locals per the pattern established repo-wide
        # when black and ruff format disagreed on the parenthesised
        # assert-message construct (black is gone, #34; the pattern stays).
        leaked_name = f"the worker inherited {var_name}, which holds the SSH private key"
        leaked_value = "the input variable's value survived in the worker environment"
        assert f"{var_name}=".encode() not in worker_env, leaked_name
        assert MARKER.encode() not in worker_env, leaked_value
        # Anti-vacuity: the worker env must still be a real inherited one, so a
        # scrub that handed the worker an empty environment cannot pass here.
        assert b"PATH=" in worker_env, "worker env must still inherit the parent environment"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
