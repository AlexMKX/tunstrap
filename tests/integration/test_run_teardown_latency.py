"""A successful `run` must not pay for the shutdown grace window.

Validates: `run` against a live daemon tears down as soon as the daemon is
gone, and says nothing about it. This is the product-level claim behind the
tofu-proxy use case — `run` wraps every OpenTofu invocation, so a teardown
that always waits out `--grace-seconds` is a fixed tax on every command, and
the diagnostic it then emitted was a false failure line on a run that worked.

Code: tunstrap/session.py (_has_exited, stop_session), tunstrap/cli.py
(_teardown_run_inner)
Assertion: wall-clock duration of a successful run stays far below the grace
window it was given, and stderr carries no teardown diagnostic.
Method: the installed console script as a subprocess against a real daemon,
forwarding through `sshd-bastion` — the only rig service with
AllowTcpForwarding enabled and a route to the internal `target-1`. The grace
window is set to 30s, well above anything the connection setup can cost, so
the two outcomes are unambiguous.

Why an integration test: the unit suite for `stop_session` monkeypatches
`os.kill`, which makes an unreaped-child zombie unrepresentable. Only a real
daemon spawned by the real CLI reproduces the topology, and only a wall-clock
assertion notices that the answer arrived 30 seconds late.

How these fail if the defect returns: with the reap removed from
`_has_exited`, `os.kill(pid, 0)` keeps succeeding against the daemon's zombie
for the entire window. `test_successful_run_does_not_wait_out_the_grace_window`
then measures >= GRACE seconds against a BUDGET of half that, and
`test_successful_run_reports_no_teardown_diagnostic` finds
`run: daemon not stopped cleanly: identity changed during grace` on stderr.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_TIMEOUT = 120
# Deliberately larger than the 10s default: a run that burns this is impossible
# to mistake for a slow runner, and a healthy run is unaffected by its size.
GRACE = 30
# Teardown of a healthy daemon costs one 0.5s poll interval. Everything else in
# the budget is SSH connection setup, measured at well under a second.
BUDGET = GRACE / 2


def _run_once(cluster: dict[str, Any], tmp_path: Path, session_dir: Path) -> tuple[float, bytes]:
    """Run a trivial child through `run`; return (elapsed seconds, stderr)."""
    key = tmp_path / "id_test"
    key.write_text(cluster["private_pem"])
    key.chmod(0o600)
    argv = [
        "tunstrap",
        "run",
        f"{cluster['user']}@localhost:{cluster['bastion_port']}",
        "--ssh-key",
        str(key),
        "--target",
        "web=target-1:80",
        "--session-dir",
        str(session_dir),
        "--grace-seconds",
        str(GRACE),
        "--",
        "true",
    ]
    started = time.monotonic()
    result = subprocess.run(argv, capture_output=True, check=False, timeout=_TIMEOUT)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, f"run failed: {result.stderr!r}"
    return elapsed, result.stderr


def test_successful_run_does_not_wait_out_the_grace_window(
    ssh_test_cluster: dict[str, Any], tmp_path: Path, started_daemons: list[str]
) -> None:
    """A run whose daemon shuts down cleanly returns promptly."""
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
    elapsed, _stderr = _run_once(ssh_test_cluster, tmp_path, session_dir)
    assert elapsed < BUDGET, (
        f"run took {elapsed:.1f}s with --grace-seconds {GRACE}; "
        "teardown is waiting out the grace window instead of noticing the daemon exited"
    )


def test_successful_run_reports_no_teardown_diagnostic(
    ssh_test_cluster: dict[str, Any], tmp_path: Path, started_daemons: list[str]
) -> None:
    """A clean stop takes stop_session's success branch, so run stays quiet.

    Asserts the whole stream is empty rather than just the absence of one
    string: every `_teardown_run` diagnostic — a failed stop, a surviving
    tunnel-data, an unremovable session root — is a real problem on a run that
    otherwise succeeded, and none of them should ever appear here.
    """
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
    _elapsed, stderr = _run_once(ssh_test_cluster, tmp_path, session_dir)
    assert stderr == b"", f"a successful run wrote to stderr: {stderr!r}"
