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

import os
import shutil
import tempfile
from pathlib import Path

from tunstrap.exceptions import SessionActive
from tunstrap.identity import acquire_session_lock, release_session_lock

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
    def create(cls, *, supplied: str | None, base: Path | None = None) -> "SessionDir":
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
        """
        return cls._rmtree_reporting(Path(root))

    @staticmethod
    def _rmtree_reporting(path: Path) -> list[str]:
        """rmtree ignoring errors, then report the path if it survived.

        ``shutil.rmtree(onexc=...)`` is 3.12+ and ``onerror=`` is deprecated
        from 3.12; with a 3.10 floor the portable outcome check is
        "did the path go away?".
        """
        shutil.rmtree(path, ignore_errors=True)
        return [str(path)] if path.exists() else []
