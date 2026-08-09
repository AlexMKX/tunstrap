"""Session-identity verification via fcntl flock on <session_dir>/session.lock."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tunstrap.identity import (
    IdentityCheckResult,
    _process_exists,
    acquire_session_lock,
    release_session_lock,
    verify_session,
)

pytestmark = pytest.mark.unit


def _spawn_locker(session_dir: Path) -> subprocess.Popen[bytes]:
    """Child that acquires session.lock and sleeps, holding the flock."""
    code = (
        "import sys, time;"
        "from tunstrap.identity import acquire_session_lock;"
        "acquire_session_lock(sys.argv[1]);"
        "print('locked', flush=True);"
        "time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(session_dir)],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    proc.stdout.readline()  # wait for 'locked'
    return proc


def test_verify_session_match(tmp_path: Path) -> None:
    proc = _spawn_locker(tmp_path)
    try:
        assert verify_session(tmp_path, proc.pid) == IdentityCheckResult.match
    finally:
        proc.terminate()
        proc.wait()


def test_verify_session_not_found_when_no_lockfile(tmp_path: Path) -> None:
    assert verify_session(tmp_path, os.getpid()) == IdentityCheckResult.not_found


def test_verify_session_not_found_for_dead_pid(tmp_path: Path) -> None:
    assert verify_session(tmp_path, 2**31 - 1) == IdentityCheckResult.not_found


def test_verify_session_not_found_when_lock_free(tmp_path: Path) -> None:
    (tmp_path / "session.lock").write_text("12345\n")
    assert verify_session(tmp_path, 12345) == IdentityCheckResult.not_found


@pytest.mark.parametrize("pid", [0, -1], ids=["zero", "minus-one"])
def test_process_exists_refuses_non_positive_pid(pid: int) -> None:
    """A non-positive pid is a group/broadcast selector, not a liveness check.

    ``os.kill(-1, 0)`` probes *every* process the caller can signal and answers
    True for as long as any one of them exists — which is always — while
    ``os.kill(0, 0)`` targets the caller's own process group. Either way the
    probe cannot distinguish "the recorded daemon is alive" from "something is",
    which is what let a ``daemon.pid`` of ``-1`` through the gate as ``match``.
    The pid is therefore not a process the verifier will confirm.
    """
    assert _process_exists(pid) is False


@pytest.mark.parametrize("pid", [0, -1], ids=["zero", "minus-one"])
def test_verify_session_not_found_for_non_positive_pid(tmp_path: Path, pid: int) -> None:
    """A held lock whose body matches changes nothing: the pid is still refused.

    This is the exact shape of the reported defect: a hostile ``session.lock``
    body of ``-1`` held against a recorded pid of ``-1`` used to verify as
    ``match``, because ``_process_exists(-1)`` answered True and the lock body
    then compared equal. Once the pid is refused at the liveness probe the lock
    is never consulted, so the answer is ``not_found``. That shape is not a
    safe preserve in general — ``cli._stop_resolved`` treats
    ``reason == "not found"`` as resolved and deletes the session — which is
    exactly why ``stop_session`` does not rely on it for a non-positive pid:
    its entry guard returns ``identity check unavailable`` before
    ``verify_session`` is consulted at all, so the hostile value is preserved
    rather than cleaned. What this test pins is the verifier's own refusal,
    independent of that guard.
    """
    lock_path = tmp_path / "session.lock"
    lock_path.write_text(f"{pid}\n")
    fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # hold the lock so _check_lock takes its held branch
    try:
        assert verify_session(tmp_path, pid) == IdentityCheckResult.not_found
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_acquire_is_mutually_exclusive(tmp_path: Path) -> None:
    fd = acquire_session_lock(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            acquire_session_lock(tmp_path)
    finally:
        release_session_lock(fd, tmp_path)


def test_release_unlinks_lockfile(tmp_path: Path) -> None:
    fd = acquire_session_lock(tmp_path)
    assert (tmp_path / "session.lock").exists()
    release_session_lock(fd, tmp_path)
    assert not (tmp_path / "session.lock").exists()
