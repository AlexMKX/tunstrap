"""Session-identity check via fcntl.flock on ``<session_dir>/session.lock``.

The daemon acquires an exclusive flock on the session dir's ``session.lock``
at startup and holds the fd for its lifetime. ``verify_session`` consults the
same file: if it is locked and the recorded PID matches, identity is confirmed.
"""

from __future__ import annotations

import enum
import errno
import fcntl
import os
import stat
from pathlib import Path

from tunstrap.fdio import write_all

_LOCK_NAME = "session.lock"


class IdentityCheckResult(str, enum.Enum):
    """Outcome of session verification used by stop/status."""

    # pylint: disable=invalid-name
    match = "match"
    mismatch = "mismatch"
    not_found = "not_found"
    unavailable = "unavailable"


def _lock_path(session_dir: str | Path) -> Path:
    """Return the absolute path to ``<session_dir>/session.lock``."""
    return Path(session_dir).resolve() / _LOCK_NAME


def acquire_session_lock(session_dir: str | Path) -> int:
    """Exclusively flock ``session.lock`` non-blocking; record pid; return fd.

    Raises ``BlockingIOError`` if another live process already holds it. Raises
    ``OSError`` (``errno.EPERM``) if the lock file is unsafe to truncate:
    ``O_NOFOLLOW`` rejects a symlinked lock at the ``open`` itself (the issue
    #25 vector -- without it a symlink let an attacker truncate an arbitrary
    victim the runner could open), and the post-open ``fstat`` rejects anything
    that is not a regular file owned by the current user. ``O_NOFOLLOW`` alone is
    not enough: a regular file some other uid planted in a writable root would
    still be opened and truncated, which is why the ``S_ISREG`` + ownership
    check is not redundant. ``st_nlink != 1`` is refused for the sibling vector
    the other two miss: a *hardlink* is not a symlink, so ``O_NOFOLLOW`` is
    silent, and it shares the victim's inode, so ``S_ISREG`` and the ownership
    check both pass on a runner-owned victim -- the ``ftruncate`` below would
    then destroy it. A lock this call is entitled to truncate has exactly one
    link; anything else is a second name for a file that is not ours to clear.
    ``SessionDir._secure_supplied_root`` only stops such a link being planted
    *after* tunstrap first runs, so this is the check that covers one planted
    before it. The fd must stay open for the holder's lifetime; the kernel
    releases the flock automatically when the process exits, clean or not.
    """
    path = _lock_path(session_dir)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
            raise OSError(
                errno.EPERM,
                "session.lock is not a singly-linked regular file owned by the current user",
                str(path),
            )
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Covers the fstat refusal above and BlockingIOError (a subclass) from a
        # held flock: either way nothing was recorded, so close the fd we opened
        # and let the caller's translation decide the domain error.
        os.close(fd)
        raise
    # Truncate + write only AFTER winning the lock, so a losing racer's open()
    # can never clobber the winner's recorded pid.
    os.ftruncate(fd, 0)
    write_all(fd, f"{os.getpid()}\n".encode("ascii"))
    os.fsync(fd)
    return fd


def release_session_lock(lock_fd: int, session_dir: str | Path) -> None:
    """Unlink ``session.lock`` and close the fd. Best-effort; never raises."""
    try:
        _lock_path(session_dir).unlink()
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def verify_session(session_dir: str | Path, pid: int) -> IdentityCheckResult:
    """Return whether ``pid`` is alive and holds the session lock."""
    if not _process_exists(pid):
        return IdentityCheckResult.not_found
    path = _lock_path(session_dir)
    if not path.is_file():
        return IdentityCheckResult.not_found
    return _check_lock(path, pid)


def _check_lock(lock_path: Path, pid: int) -> IdentityCheckResult:
    """Determine identity from flock state and the recorded PID."""
    try:
        # ``O_NOFOLLOW`` so a symlinked lock is reported unavailable rather than
        # followed -- a symlinked lock is never legitimate (the daemon never
        # writes one), and following it would probe flock state on an arbitrary
        # attacker-chosen file. ``ELOOP`` from the symlink is absorbed by the
        # existing ``except OSError`` below.
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return IdentityCheckResult.unavailable
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Held — a daemon is alive. Verify the PID matches.
            try:
                recorded_pid = int(lock_path.read_bytes().strip())
            except (OSError, ValueError):
                return IdentityCheckResult.unavailable
            if recorded_pid != pid:
                return IdentityCheckResult.mismatch
            return IdentityCheckResult.match
        # Got the lock — no live holder. Release and report dead.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return IdentityCheckResult.not_found
    finally:
        os.close(fd)


def _process_exists(pid: int) -> bool:
    """True iff a process with the given PID currently exists.

    A non-positive pid is never a process: ``kill(2)`` reads 0 as "the
    caller's process group" and negatives as a process-group selector (with
    ``-1`` meaning *every* process the caller can signal). ``os.kill`` with
    such a pid is therefore a group/broadcast probe that answers True for as
    long as any signalable process exists — which is always — so it cannot
    confirm "the recorded daemon is alive". Refusing it here is what keeps a
    corrupt ``daemon.pid`` (or a hostile ``--session-dir``) of ``-1`` from
    verifying as ``match``; ``stop_session`` re-asserts ``pid > 0`` at its
    entry as defence in depth.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
