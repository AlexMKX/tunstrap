"""Session-identity check via fcntl.flock on ``<session_dir>/session.lock``.

The daemon acquires an exclusive flock on the session dir's ``session.lock``
at startup and holds the fd for its lifetime. ``verify_session`` consults the
same file: if it is locked and the recorded PID matches, identity is confirmed.
"""

from __future__ import annotations

import enum
import fcntl
import os
from pathlib import Path

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

    Raises ``BlockingIOError`` if another live process already holds it. The
    fd must stay open for the holder's lifetime; the kernel releases the flock
    automatically when the process exits, clean or not.
    """
    path = _lock_path(session_dir)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    # Truncate + write only AFTER winning the lock, so a losing racer's open()
    # can never clobber the winner's recorded pid.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
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
        fd = os.open(lock_path, os.O_RDONLY)
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
