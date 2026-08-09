"""Session directory: session.lock + materialized files under tunnel-data/.

The daemon always works inside a well-known `tunnel-data/` subdirectory of
the session dir, beside a `session.lock` flock that `SessionDir` owns. When
the daemon generates the session dir itself, cleanup removes the whole dir;
when the caller supplies it, cleanup removes only `tunnel-data/` (the caller's
directory is never touched). `--session-dir` is untrusted: an existing
tunnel-data that is a symlink, a non-directory, or not owned by the current
user is rejected. A supplied root must be owned by the current user and has
its group/other write bits cleared on use, because it hosts 0600 credentials.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import signal
import stat
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


def _write_all(fd: int, content: bytes) -> None:
    """Write all of ``content`` to ``fd``, looping past short writes.

    ``os.write`` is permitted to return fewer bytes than requested (a "short
    write"); ignoring the count silently truncates the file. This mirrors the
    loop in ``_worker._write_message`` so the two raw-fd writers in this
    codebase agree, including the ``written <= 0`` no-progress guard (a 0
    return would otherwise spin forever). A ``memoryview`` avoids copying the
    tail on each slice.
    """
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("os.write made no progress; cannot complete write")
        view = view[written:]


def atomic_write(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path`` (mode 0600) via temp file + ``os.replace``.

    True atomic replace, not ``_write_file``'s ``O_TRUNC``: a truncated file at
    the final path is indistinguishable from a valid short one to a naive
    reader, while a temp file plus ``os.replace`` guarantees only a complete
    old or complete new file is ever observable at ``path`` -- load-bearing
    for a process killed mid-write.

    The temp name is pinned to ``os.getpid()``, so distinct processes never
    compete for it; ``O_EXCL`` therefore guards the *same* process against
    re-entering on top of its own leftover temp, not a separate writer. Any
    failure between the create and a successful ``os.replace`` unlinks the
    temp first, so a same-pid retry is never permanently blocked by
    ``O_EXCL``. The only way a stale temp survives is a hard crash
    (``SIGKILL``) between ``os.open`` and the cleanup, which Python cannot
    intercept; that residual is what would surface as ``FileExistsError`` on
    a later same-pid call, flagging that a prior run died mid-write.

    The mode is fixed at the temp file's creation, so ``os.replace`` never
    exposes a wider-than-0600 window. ``path.parent`` is created with mode
    0700 to match ``SessionDir.create``'s ``tunnel-data``. In production this
    ``mkdir(exist_ok=True)`` is a no-op: every call site
    (``materialize_atomic`` in the daemon, ``write_materialized_output`` in
    the start/run parent) reaches ``atomic_write`` with ``path.parent``
    already minted at 0700 by ``SessionDir.create``, which runs in the daemon
    before the parent ever writes. The ``mode=0o700`` here is therefore
    defence-in-depth for a direct caller, not a live-hole fix. ``mkdir``'s
    ``mode`` applies only to the leaf directory; that is sufficient because
    ``path.parent`` is always exactly ``tunnel-data`` and its parent (the
    session dir) is guaranteed to pre-exist at every call site, so no
    intermediate component is ever created under the ambient umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    try:
        _write_all(fd, content)
        os.replace(tmp_path, path)
    except BaseException:
        # Orphaning the temp would make O_EXCL reject every later same-pid
        # retry (the name is pid-pinned); remove it so the next call starts
        # clean. The inner FileNotFoundError suppress covers a real race, not a
        # hypothetical one: the temp lives inside tunnel-data, and a concurrent
        # teardown (``SessionDir.cleanup``'s rmtree) can remove it between the
        # ``os.open`` above and this unlink -- an interrupted run is exactly
        # when both happen at once. Without the suppress that ENOENT would
        # raise and mask the original error we are about to re-raise.
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)


class SessionError(Exception):
    """The session dir or its tunnel-data subdir failed validation."""


class SessionIdentityUnreadable(SessionError):
    """``daemon.pid`` could not be turned into a pid, and it is not simply absent.

    Split out because the three ways ``read_identity`` fails do not mean the
    same thing to a caller deciding whether to delete state. A *missing* file
    means nothing was ever recorded, so nothing is running. A file that cannot
    be read (permissions, EIO, a directory in its place) or that holds
    something that is not a pid (the shape a truncated write takes) means a
    daemon got far enough to be there and we cannot address it — the daemon's
    state is unknown, which is a reason to preserve, not to clean up.

    A subclass rather than a sibling so that every existing ``except
    SessionError`` handler keeps its current behaviour; only callers that care
    about the distinction have to name it.
    """


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
            # Explicit 0o700 so a freshly minted root can never carry group/other
            # write bits under a permissive umask and then fail its own check.
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            cls._secure_supplied_root(root)
            generated = False

        try:
            lock_fd = acquire_session_lock(root)
        except BlockingIOError as exc:
            raise SessionActive(
                "session already active",
                {"session_dir": str(root)},
            ) from exc
        except OSError as exc:
            # acquire_session_lock raises OSError on an unsafe lock file (a
            # symlink, or a regular file not owned by us). Translate it to the
            # domain SessionError every other session refusal uses, mirroring
            # _reclaim_data_slot; the original OSError is chained for the cause.
            raise SessionError(f"cannot acquire session lock at {root}: {exc}") from exc

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

    @staticmethod
    def _secure_supplied_root(root: Path) -> None:
        """Tighten a caller-supplied root: proven-owned, write bits cleared.

        Why this guard is NOT redundant given ``acquire_session_lock``'s
        ``O_NOFOLLOW`` + ``fstat`` (issue #25 part (A)): that pair validates an
        *inode* reached through an fd. Directory write permission, by contrast,
        is authority over the *entries* of that directory -- create, unlink,
        rename -- and is wholly independent of the mode and ownership of the
        files inside it. No amount of fstat hardening on an opened fd reaches an
        entry-level attack. With write access to the root another uid can unlink
        the live ``session.lock`` (a fresh inode then passes every (A) check and
        wins flock, since flock is per-inode), rename ``tunnel-data`` aside and
        substitute a symlink (a rename within the parent needs write on the
        parent only), or hardlink ``session.lock`` to a runner-owned victim. The
        root guard is therefore the load-bearing premise of
        ``_reclaim_data_slot``'s ``shutil.rmtree`` (lock exclusivity) and of
        ``_validated_path``'s containment -- remove it and both collapse.

        Note the limit of *tightening* specifically: clearing the write bits
        stops a hostile entry being planted from now on, but says nothing about
        one planted before tunstrap first ran. The hardlink case is therefore
        closed where it lands rather than here, by ``acquire_session_lock``'s
        ``st_nlink`` refusal.

        Why refusal became tightening: the mode cannot distinguish a user-private
        group (the Debian/Ubuntu default, zero cross-uid risk) from a genuinely
        shared one, and as verified on a stock umask-0002 account refusing it
        broke ``mkdir d && tunstrap run --session-dir d`` with a generic
        ``DaemonError``. Refusing was the wrong enforcement because it rejected a
        safe common case.

        Why tightening is legitimate: ownership is already proven before the
        chmod (an unowned root is refused, never tightened), so the runner is
        within its rights to set the mode. The tool already forces 0700 on a
        root it creates itself and on ``tunnel-data``, so clearing write bits on
        a supplied root is the same posture applied where the runner cannot pick
        the initial mode.

        Why only the write bits (``S_IWGRP | S_IWOTH``) are cleared, and why the
        parent is never inspected: clearing preserves read/exec, so a legitimate
        0755 root is left at 0755 rather than force-chmodded to 0700. The parent
        is out of scope -- a root under a 1777 ``/tmp`` with its own mode is
        safe, and inspecting the parent would break ``mkdtemp``-based tests and
        reach outside what the runner owns.

        fd-based and TOCTOU-free: the mode is read and set through one fd held
        open for the call, so a concurrent rename-symlink swap between a stat
        and a chmod cannot retarget the change. After fchmod the mode is re-stat
        through the same fd, and if the write bits survive (an ACL mask or an
        exotic filesystem that silently ignores fchmod) the root is refused
        rather than accepted on a no-op chmod -- a silently-failing fchmod must
        not ship as "accept anything".
        """
        try:
            dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise SessionError(f"cannot open session dir {root}: {exc}") from exc
        try:
            st = os.fstat(dirfd)
            if st.st_uid != os.getuid():
                raise SessionError("session dir is not owned by the current user")
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                try:
                    os.fchmod(
                        dirfd,
                        stat.S_IMODE(st.st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH),
                    )
                except OSError as exc:
                    raise SessionError(
                        f"session dir {root} is group- or world-writable and "
                        f"could not be tightened: {exc}; run chmod go-w {root}"
                    ) from exc
                # Re-stat through the same fd: an ACL mask or exotic filesystem
                # can let fchmod succeed yet leave the bits set, and accepting
                # that as a tightening would be a no-op guard.
                if os.fstat(dirfd).st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise SessionError(
                        f"session dir {root} is group- or world-writable and "
                        f"could not be tightened (write bits survived fchmod); "
                        f"run chmod go-w {root}"
                    )
        finally:
            os.close(dirfd)

    def write_identity(self, *, pid: int) -> None:
        """Write daemon.pid (mode 0600) into tunnel-data/."""
        self._write_file("daemon.pid", f"{pid}\n".encode("ascii"))

    def materialize(self, name: str, content: bytes) -> str:
        """Write `content` to tunnel-data/<name> (mode 0600); return the path."""
        return self._write_file(name, content)

    def materialize_atomic(self, name: str, content: bytes) -> str:
        """Write `content` to tunnel-data/<name> via atomic replace; return the path.

        Same name-safety rules as ``materialize``, but the true-atomic write
        primitive (temp file + ``os.replace``) fetched-file materialization
        needs -- see ``atomic_write``.
        """
        path = self._validated_path(name)
        atomic_write(path, content)
        return str(path)

    def _validated_path(self, name: str) -> Path:
        # ``path.resolve().parent != self._data.resolve()`` is a no-op when
        # ``tunnel-data`` itself is the symlink -- both sides resolve through
        # the attacker's link and compare equal. The explicit ``is_symlink``
        # check is therefore what actually keeps materialization inside the
        # session dir; without it a substituted ``tunnel-data`` symlink would
        # pass containment and write a patched kubeconfig into attacker space.
        if self._data.is_symlink():
            raise SessionError("tunnel-data is a symlink; refusing to follow")
        if "/" in name or "\\" in name:
            raise SessionError(f"unsafe materialized file name: {name!r}")
        if name in (".", ".."):
            raise SessionError(f"unsafe materialized file name: {name!r}")
        path = self._data / name
        if path.resolve().parent != self._data.resolve():
            raise SessionError(f"unsafe materialized file name: {name!r}")
        return path

    def _write_file(self, name: str, content: bytes) -> str:
        path = self._validated_path(name)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            _write_all(fd, content)
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
        """Read the recorded pid from a session dir's tunnel-data/daemon.pid.

        Raises ``SessionIdentityUnreadable`` — a ``SessionError`` subclass —
        for everything except a genuinely absent file, so a caller can tell
        "nothing was ever recorded" from "there is something here I cannot
        address". Every unreadable case shares the ``cannot read identity
        from <data>: …`` shape; only the tail differs (the underlying
        ``OSError``/``ValueError`` text, or, for a non-positive value, the
        explicit reason).

        A non-positive value is unreadable on purpose. Under ``kill(2)`` a pid
        of 0 means the caller's own process group and a negative pid a process
        group — with ``-1`` meaning *every* process the caller can signal, a
        broadcast rather than a single group — so handing such a value to
        ``os.kill`` widens a signal far beyond the recorded daemon. Under
        ``waitpid(2)`` the same encodings select a child group (or, for ``-1``,
        any child), the exact hazard ``_has_exited`` already guards against. An
        attacker-controlled ``--session-dir`` (or a corrupt ``daemon.pid``)
        could plant such a value deliberately; refusing it here is the gate that
        keeps it off the kill path, which re-asserts ``pid > 0`` at its entry as
        defence in depth.
        """
        data = Path(session_dir).resolve() / _TUNNEL_DATA
        try:
            raw = (data / "daemon.pid").read_text()
        except FileNotFoundError as exc:
            raise SessionError(f"cannot read identity from {data}: {exc}") from exc
        except OSError as exc:
            raise SessionIdentityUnreadable(f"cannot read identity from {data}: {exc}") from exc
        try:
            pid = int(raw.strip())
        except ValueError as exc:
            raise SessionIdentityUnreadable(f"cannot read identity from {data}: {exc}") from exc
        if pid <= 0:
            raise SessionIdentityUnreadable(
                f"cannot read identity from {data}: pid {pid} is not positive"
            )
        return pid

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

    A non-positive ``pid`` is refused at function entry, before any syscall.
    Under ``kill(2)`` such a value selects a process *group* (0 is the caller's
    group; ``-1`` is every process the caller can signal), so
    ``os.kill(-1, SIGTERM)`` would broadcast the signal. ``read_identity`` is
    the gate that keeps a corrupt ``daemon.pid`` (or a hostile ``--session-dir``)
    from reaching this function in production, and ``_process_exists`` refuses
    the same value independently, but the guard here does not lean on either:
    it runs first, so it also covers ``verify_session``'s own signal-0 probe,
    and a direct caller that bypasses ``read_identity`` — or a host where the
    upstream gates have failed — still cannot widen a signal. It returns
    ``identity check unavailable``: an unresolved outcome, so the caller
    preserves rather than deletes — the same disposal ``read_identity`` demands
    for the identical value.
    """
    if pid <= 0:
        return StopOutcome(False, "identity check unavailable")
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
