"""``stop_session`` against a real, unreaped child process.

Validates: when the daemon is a child of the stopping process — which is
exactly ``run``'s topology, since ``spawn_daemon`` uses ``subprocess.Popen``
and never waits — a clean SIGTERM shutdown is detected within the grace
window and reported as the unforced success ``StopOutcome(True)``.

Why this file exists: ``test_stop_session.py`` monkeypatches ``os.kill``, so
a zombie is unrepresentable there. It cannot distinguish "the process is
gone" from "the process exited but its pid is still allocated because nobody
reaped it", and that distinction *was* the bug: ``os.kill(zombie, 0)``
succeeds, so the poll ran the full 10s grace and then reported
``identity changed during grace`` on every successful ``run``.

Code: tunstrap/session.py (_has_exited, stop_session)
Assertion: the call returns StopOutcome(True) and takes far less than the
grace window it was given.
Method: a real child that takes the session flock and dies on SIGTERM,
started with ``subprocess.Popen`` and deliberately never waited on, so the
kernel really does leave a zombie behind — no mocking at any layer.

How these fail if the defect returns: drop the reap from ``_has_exited`` and
``os.kill(pid, 0)`` keeps succeeding against the zombie for the whole grace
window. ``test_clean_child_stop_is_prompt`` then blows its 5s budget against
a 10s grace, and ``test_clean_child_stop_reports_unforced_success`` gets
``StopOutcome(False, "identity changed during grace")`` because the post-grace
re-check finds the flock released. Both assertions are load-bearing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

from tunstrap.identity import IdentityCheckResult, verify_session
from tunstrap.session import StopOutcome, stop_session

pytestmark = pytest.mark.unit

# Long enough that burning it is unmistakable, short enough that a regression
# does not stall the suite for a minute.
GRACE = 10
# A healthy stop costs one 0.5s poll interval; it is sleep-bound, not CPU-bound,
# so this budget holds on a slow runner while staying far below GRACE.
BUDGET = 5.0


def _spawn_lock_holder(session_dir: Path) -> subprocess.Popen[bytes]:
    """Child holding session.lock that exits cleanly on SIGTERM.

    Never waited on by the test, so after it dies it stays in the process
    table as a zombie child of pytest — the exact condition ``run``'s CLI
    creates for the daemon.
    """
    code = (
        "import sys, signal, time;"
        "from tunstrap.identity import acquire_session_lock;"
        "acquire_session_lock(sys.argv[1]);"
        "signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0));"
        "print('locked', flush=True);"
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(session_dir)],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline() == b"locked\n"
    return proc


@pytest.fixture(name="lock_holder")
def _lock_holder(tmp_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    proc = _spawn_lock_holder(tmp_path)
    # Guard the premise: if identity did not verify, every assertion below
    # would pass or fail for the wrong reason.
    assert verify_session(tmp_path, proc.pid) == IdentityCheckResult.match
    try:
        yield proc
    finally:
        proc.kill()
        # poll() rather than wait(): stop_session has normally reaped the pid
        # already, and poll() turns the resulting ECHILD into a returncode
        # instead of blocking or raising.
        proc.poll()
        if proc.returncode is None:  # pragma: no cover - only on a failed stop
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


def test_clean_child_stop_is_prompt(tmp_path: Path, lock_holder: subprocess.Popen[bytes]) -> None:
    """Stopping a child that exits on SIGTERM does not wait out the grace window."""
    started = time.monotonic()
    stop_session(str(tmp_path), lock_holder.pid, GRACE, force=True)
    elapsed = time.monotonic() - started
    assert elapsed < BUDGET, f"stop_session burned {elapsed:.1f}s of a {GRACE}s grace window"


def test_clean_child_stop_reports_unforced_success(
    tmp_path: Path, lock_holder: subprocess.Popen[bytes]
) -> None:
    """The in-grace success branch is reachable: no reason, no SIGKILL escalation."""
    outcome = stop_session(str(tmp_path), lock_holder.pid, GRACE, force=True)
    assert outcome == StopOutcome(True), f"expected a clean stop, got {outcome}"


def test_stopped_child_pid_is_reaped(tmp_path: Path, lock_holder: subprocess.Popen[bytes]) -> None:
    """The pid is released, not merely observed: no zombie survives the stop.

    Fails if ``_has_exited`` is rewritten to detect the exit without reaping
    (reading /proc state, say), which would leave the process-table entry — and
    the pid it pins — in place: a second ``waitpid`` would then return the pid
    instead of raising ECHILD.
    """
    stop_session(str(tmp_path), lock_holder.pid, GRACE, force=True)
    with pytest.raises(ChildProcessError):
        os.waitpid(lock_holder.pid, os.WNOHANG)
