"""Session-identity verification via fcntl flock on <session_dir>/session.lock."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tunstrap.identity import (
    IdentityCheckResult,
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
