"""Session-identity verification via fcntl flock on <session_dir>/session.lock."""

from __future__ import annotations

import errno
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


def test_verify_session_treats_symlinked_lock_as_unavailable(tmp_path: Path) -> None:
    """A symlinked session.lock is reported ``unavailable`` rather than followed.

    Mirrors ``acquire_session_lock``'s ``O_NOFOLLOW`` on the verify path:
    ``_check_lock`` opens the lock read-only to probe flock state, and without
    ``O_NOFOLLOW`` it would follow a symlink and probe flock on an arbitrary
    attacker-chosen file. ``ELOOP`` from ``O_NOFOLLOW`` is absorbed by the
    existing ``except OSError`` arm and surfaced as ``unavailable``. Removing the
    flag makes the open follow the symlink, the free flock then resolves to
    ``not_found`` -- a different result, which is what this assertion pins.
    """
    victim = tmp_path / "victim"
    victim.write_bytes(b"probe-target\n")
    (tmp_path / "session.lock").symlink_to(victim)

    assert verify_session(tmp_path, os.getpid()) == IdentityCheckResult.unavailable


def test_acquire_refuses_symlink_lock_leaving_target_intact(tmp_path: Path) -> None:
    """A symlinked session.lock is refused and its target is never truncated.

    The core of issue #25: ``acquire_session_lock`` opened the lock path without
    ``O_NOFOLLOW`` and then ``ftruncate``-d the resulting fd, so a symlinked
    ``session.lock`` let an attacker truncate an arbitrary victim file the
    runner could open. The security property is not merely that an exception is
    raised but that the victim's bytes are byte-for-byte intact afterwards --
    a refusal that still destroyed the target would be no fix at all.
    """
    victim = tmp_path / "victim"
    payload = b"sensitive-bytes-that-must-survive-the-acquire\n"
    victim.write_bytes(payload)
    victim.chmod(0o600)
    lock = tmp_path / "session.lock"
    lock.symlink_to(victim)

    with pytest.raises(OSError) as excinfo:
        acquire_session_lock(tmp_path)
    assert excinfo.value.errno == errno.ELOOP, "O_NOFOLLOW specifically must reject the symlink"

    assert victim.read_bytes() == payload, "symlink target was truncated"
    assert lock.is_symlink(), "the symlink was replaced instead of refused"


def test_acquire_refuses_foreign_owned_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing session.lock owned by another uid is refused, untruncated.

    ``O_NOFOLLOW`` rejects a symlinked lock but says nothing about a regular
    file some other uid planted in a writable root; without the ``fstat``
    ownership check the daemon would happily ``ftruncate`` that file and write
    its pid into it. Stand-in: an unprivileged test runner cannot ``chown`` a
    file to another uid, so the foreign ownership is reported by patching
    ``os.fstat`` for the lock's inode to return a uid that is not the file's real
    owner -- the inequality ``st.st_uid != os.getuid()`` is the guard under test,
    and forging the reported owner (rather than process-wide ``getuid``) keeps
    the failure local to this one fd and lets the bytes-survive assertion below
    pin the real security property.
    """
    lock_path = tmp_path / "session.lock"
    lock_path.write_bytes(b"hostile\n")
    real_fstat = os.fstat
    lock_ino = lock_path.stat().st_ino
    foreign_uid = os.getuid() + 1

    def fake_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if st.st_ino == lock_ino:
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    foreign_uid,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(os, "fstat", fake_fstat)

    with pytest.raises(OSError, match="not a singly-linked regular file"):
        acquire_session_lock(tmp_path)

    # A refusal is only a fix if nothing was truncated: the guard fires before
    # ftruncate, so the planted body survives verbatim.
    assert lock_path.read_bytes() == b"hostile\n"


def test_acquire_refuses_non_regular_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session.lock that is not a regular file is refused.

    ``O_NOFOLLOW`` bars symlinks but a path that resolves to some other
    non-regular type a foreign uid could plant in a writable root (the realistic
    one is a foreign-owned regular file, covered above) must still be rejected
    before ``ftruncate``. Stand-in: a non-regular mode is reported via
    ``os.fstat`` since a test runner cannot materialise a device node in
    ``tmp_path``; the guard under test is ``stat.S_ISREG(st.st_mode)``.
    """
    (tmp_path / "session.lock").write_bytes(b"x\n")
    real_fstat = os.fstat
    lock_ino = (tmp_path / "session.lock").stat().st_ino

    def fake_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if st.st_ino == lock_ino:
            # Report a directory mode (S_IFDIR) for the lock fd.
            return os.stat_result(
                (
                    0o040700,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(os, "fstat", fake_fstat)

    with pytest.raises(OSError, match="not a singly-linked regular file"):
        acquire_session_lock(tmp_path)


def test_acquire_refuses_hardlinked_lock_leaving_victim_intact(tmp_path: Path) -> None:
    """A session.lock hardlinked to a runner-owned victim is refused, unharmed.

    The sibling of the symlink vector, and the one the other two guards miss:
    a hardlink is not a symlink, so ``O_NOFOLLOW`` stays silent, and it shares
    the victim's inode, so ``S_ISREG`` and the ownership check both pass -- the
    victim really is a regular file really owned by us. Only ``st_nlink`` tells
    the two names apart. Needs no stand-in: an unprivileged runner can create a
    real hardlink, so this drives the true precondition. Drop the ``st_nlink``
    check and the victim below is truncated to the daemon pid.
    """
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"KEEP-ME" * 8)
    before = victim.read_bytes()
    os.link(victim, tmp_path / "session.lock")

    with pytest.raises(OSError, match="not a singly-linked regular file"):
        acquire_session_lock(tmp_path)

    assert victim.read_bytes() == before, "the hardlinked victim was truncated"
