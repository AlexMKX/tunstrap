"""Session directory: session.lock + materialized files under tunnel-data/.

The daemon always works inside a well-known `tunnel-data/` subdirectory of
the session dir, beside a `session.lock` flock that `SessionDir` owns. When
the daemon generates the session dir itself, cleanup removes the whole dir;
when the caller supplies it, cleanup removes only `tunnel-data/` (the caller's
directory is never touched). `--session-dir` is untrusted: an existing
tunnel-data that is a symlink, a non-directory, or not owned by the current
user is rejected.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path

from tunstrap.exceptions import SessionActive
from tunstrap.identity import (
    IdentityCheckResult,
    acquire_session_lock,
    release_session_lock,
    verify_session,
)

_TUNNEL_DATA = "tunnel-data"


class SessionError(Exception):
    """The session dir or its tunnel-data subdir failed validation."""


class SessionDir:
    """Owns session.lock + the tunnel-data/ subdir for one daemon instance."""

    def __init__(self, *, session_dir: Path, generated: bool, lock_fd: int) -> None:
        self.session_dir = str(session_dir)
        self._root = session_dir
        self._generated = generated
        self._data = session_dir / _TUNNEL_DATA
        self._lock_fd = lock_fd

    @classmethod
    def create(cls, *, supplied: str | None, base: Path | None = None) -> SessionDir:
        """Resolve the session dir, acquire session.lock, (re)create tunnel-data/.

        Raises ``SessionActive`` if a live daemon already holds the lock.
        """
        if supplied is None:
            parent = base if base is not None else Path(tempfile.gettempdir())
            root = Path(tempfile.mkdtemp(prefix="tunstrap-", dir=parent))
            generated = True
        else:
            supplied_path = Path(supplied)
            if not supplied_path.is_absolute():
                raise SessionError("session dir must be an absolute path")
            root = supplied_path.resolve()
            root.mkdir(parents=True, exist_ok=True)
            generated = False

        try:
            lock_fd = acquire_session_lock(root)
        except BlockingIOError as exc:
            raise SessionActive(
                "session already active",
                {"session_dir": str(root)},
            ) from exc

        try:
            data = root / _TUNNEL_DATA
            cls._reclaim_data_slot(data)
            data.mkdir(mode=0o700)
        except BaseException:
            release_session_lock(lock_fd, root)
            raise
        return cls(session_dir=root, generated=generated, lock_fd=lock_fd)

    @staticmethod
    def _reclaim_data_slot(data: Path) -> None:
        """Wipe an orphaned tunnel-data/; reject an unsafe pre-existing slot.

        The caller holds the exclusive session.lock, so any existing tunnel-data
        belongs to a dead session and is safe to remove. Symlinks, non-dirs, and
        foreign-owned dirs are still rejected (untrusted --session-dir).
        """
        if data.is_symlink():
            raise SessionError("tunnel-data is a symlink; refusing to follow")
        if data.exists():
            if not data.is_dir():
                raise SessionError("tunnel-data exists and is not a directory")
            if data.stat().st_uid != os.getuid():
                raise SessionError("tunnel-data exists and is not owned by this user")
            shutil.rmtree(data)

    def write_identity(self, *, pid: int) -> None:
        """Write daemon.pid (mode 0600) into tunnel-data/."""
        self._write_file("daemon.pid", f"{pid}\n".encode("ascii"))

    def materialize(self, name: str, content: bytes) -> str:
        """Write `content` to tunnel-data/<name> (mode 0600); return the path."""
        return self._write_file(name, content)

    def _write_file(self, name: str, content: bytes) -> str:
        if "/" in name or "\\" in name:
            raise SessionError(f"unsafe materialized file name: {name!r}")
        if name in (".", ".."):
            raise SessionError(f"unsafe materialized file name: {name!r}")
        path = self._data / name
        if path.resolve().parent != self._data.resolve():
            raise SessionError(f"unsafe materialized file name: {name!r}")
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return str(path)

    def cleanup(self) -> None:
        """Release the lock, then remove tunnel-data/ (or the whole generated dir)."""
        release_session_lock(self._lock_fd, self._root)
        if self._generated:
            shutil.rmtree(self._root, ignore_errors=True)
        else:
            shutil.rmtree(self._data, ignore_errors=True)

    @staticmethod
    def read_identity(session_dir: str) -> int:
        """Read the recorded pid from a session dir's tunnel-data/daemon.pid."""
        data = Path(session_dir).resolve() / _TUNNEL_DATA
        try:
            return int((data / "daemon.pid").read_text().strip())
        except (OSError, ValueError) as exc:
            raise SessionError(f"cannot read identity from {data}: {exc}") from exc

    @classmethod
    def cleanup_path(cls, session_dir: str) -> list[str]:
        """Remove ``<session_dir>/tunnel-data`` best-effort; return what survived.

        Never raises, so ``stop``'s behaviour is unchanged. The returned list
        is empty on success and holds the still-present path when removal
        failed, which is what gives ``run`` something to report on stderr —
        the old ``ignore_errors=True`` discarded every error, making a
        promise to report cleanup failures unsatisfiable.
        """
        return cls._rmtree_reporting(Path(session_dir).resolve() / _TUNNEL_DATA)

    @classmethod
    def remove_root(cls, root: str) -> list[str]:
        """Remove a ``run``-minted session root entirely; return what survived.

        ``run`` supplies its own ``--session-dir``, which makes the worker's
        ``SessionDir`` non-generated, so the worker never removes the root.
        ``run`` therefore removes the root it minted itself, and only that one
        — a caller-supplied ``--session-dir`` is never touched.

        ``.resolve()`` for parity with ``cleanup_path`` and ``read_identity``.
        It is safe to follow a symlink here specifically because the only
        caller passes a ``tempfile.mkdtemp`` path, which is always a real
        directory this process just created — unlike ``--session-dir``, this
        argument is never caller-controlled.
        """
        return cls._rmtree_reporting(Path(root).resolve())

    @staticmethod
    def _rmtree_reporting(path: Path) -> list[str]:
        """rmtree ignoring errors, then report the path if it survived.

        ``shutil.rmtree(onexc=...)`` is 3.12+ and ``onerror=`` is deprecated
        from 3.12; with a 3.10 floor the portable outcome check is
        "did the path go away?".
        """
        shutil.rmtree(path, ignore_errors=True)
        try:
            path.stat()
        except FileNotFoundError:
            return []
        except OSError:
            return [str(path)]
        return [str(path)]


@dataclasses.dataclass(frozen=True)
class StopOutcome:
    """What ``stop_session`` did, with no opinion about where to report it.

    ``reason`` is ``None`` for the two success shapes and otherwise carries
    ``stop``'s documented wording verbatim. ``forced`` is True only when the
    daemon had to be SIGKILLed after the grace period.
    """

    stopped: bool
    reason: str | None = None
    forced: bool = False


def _has_exited(pid: int) -> bool:
    """True once ``pid`` has terminated, reaping it first when it is our child.

    ``os.kill(pid, 0)`` alone is not a liveness probe for a process we spawned.
    ``run`` starts the daemon with ``subprocess.Popen`` (daemon.py:41) and never
    waits on it, so the daemon is a *child* of the CLI: when it exits it becomes
    a zombie, and the pid stays allocated — and keeps answering signal 0 — until
    somebody reaps it. The grace poll below therefore ran to its full deadline on
    every clean shutdown, then found the flock already released and reported
    "identity changed during grace" for a daemon that had exited in milliseconds.

    ``waitpid(WNOHANG)`` answers the question *and* frees the pid, so the
    signal-0 probe becomes truthful again. Falling back to it on ``ECHILD``
    also keeps the answer independent of whether ``subprocess``'s own
    ``_active`` bookkeeping happened to reap the daemon first — that is CPython
    internals, not a promise across the supported 3.10-3.13 range.

    Only ``pid > 0`` reaches ``waitpid``: 0 and negatives select a process
    *group*, which would let a corrupt ``daemon.pid`` reap ``run``'s foreground
    child and steal its exit status.
    """
    if pid > 0:
        try:
            return os.waitpid(pid, os.WNOHANG)[0] == pid
        except OSError:
            # ECHILD: not, or no longer, our child — the `stop` verb runs in a
            # process that never spawned the daemon, and there signal 0 is
            # already correct because nobody holds the exit status open.
            # Anything else means the reap is simply unavailable. Neither may
            # escape: a raising stop_session short-circuits _teardown_run
            # before it removes tunnel-data.
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def stop_session(  # pylint: disable=too-many-return-statements
    session_dir: str, pid: int, grace_seconds: int, *, force: bool
) -> StopOutcome:
    """Stop the daemon recorded for ``session_dir``. Performs the stop, writes nothing.

    Silent by design, because it has two callers wanting different channels:
    ``cli.stop_command`` renders the returned outcome as ``stop``'s stdout JSON
    (``cli._stop_outcome_json``), while ``cli._teardown_run_inner`` prints
    nothing on success and stderr on failure, so a foreground child keeps fd 1
    to itself. Deciding here would serve only one of them.
    """
    check = verify_session(session_dir, pid)
    if check == IdentityCheckResult.not_found:
        return StopOutcome(False, "not found")
    if check == IdentityCheckResult.mismatch:
        return StopOutcome(False, "identity mismatch")
    if check == IdentityCheckResult.unavailable:
        return StopOutcome(False, "identity check unavailable")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return StopOutcome(True)

    deadline = time.monotonic() + max(0, grace_seconds)
    while time.monotonic() < deadline:
        if _has_exited(pid):
            return StopOutcome(True)
        time.sleep(0.5)

    if not force:
        return StopOutcome(False, "still alive")
    if verify_session(session_dir, pid) != IdentityCheckResult.match:
        return StopOutcome(False, "identity changed during grace")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return StopOutcome(True)
    return StopOutcome(True, forced=True)
