"""Spawn the worker daemon via subprocess.Popen with a dedicated IPC pipe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import IO, Any

from tunstrap.exceptions import DaemonHandshakeError
from tunstrap.schemas import InputSchema


def _open_log_target(path: str | None) -> int | IO[bytes]:
    """Return a file or DEVNULL suitable for Popen's stdout/stderr arg.

    DEVNULL is the integer sentinel ``subprocess.DEVNULL``. A real path opens
    in append-binary mode so multiple daemons can share a log file. Caller
    must close the file in the parent after Popen returns; the worker keeps
    its own dup'd fd.
    """
    if path is None:
        return subprocess.DEVNULL
    return open(path, "ab", buffering=0)  # closed by caller


def spawn_daemon(schema: InputSchema, session_dir: str | None = None) -> dict[str, Any]:
    """Spawn the worker, send the schema, read the IPC response, return it.

    Returns the structured IPC message for any of the four worker outcomes:
    ``success``, ``required_failure``, ``daemon_error``, ``session_active``.
    Callers dispatch on ``message["kind"]`` and map to CLI exit codes. Those
    are worker-authored: the worker cleaned up after itself before writing the
    frame, so no daemon of ours survives them.

    **This function is not atomic, and the seam is ``Popen``.** Before it, no
    worker exists and a failure leaves nothing behind. After it, the worker is
    launched and detached — so every failure from there on is *parent-side* and
    raises ``DaemonHandshakeError``, telling the caller a daemon may be running
    and must be stopped rather than abandoned. Callers that treat any spawn
    failure as "no daemon exists" orphan the worker on exactly those paths.
    """
    ipc_read_fd, ipc_write_fd = os.pipe()
    log_target = _open_log_target(schema.daemon.log_file)
    try:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with  # detached; never wait()ed
            [
                sys.executable,
                "-m",
                "tunstrap._worker",
                f"--ipc-fd={ipc_write_fd}",
                *([f"--session-dir={session_dir}"] if session_dir is not None else []),
            ],
            stdin=subprocess.PIPE,
            stdout=log_target,
            stderr=log_target,
            pass_fds=[ipc_write_fd],
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(ipc_write_fd)
        if isinstance(log_target, int):
            # subprocess.DEVNULL is an int sentinel; nothing to close.
            pass
        else:
            log_target.close()

    # Everything below this line runs with a detached worker already alive.
    if proc.stdin is None:
        # Unreachable with stdin=PIPE, but it must not be an `assert`: that is
        # an AssertionError, which is outside TunstrapError and so escapes the
        # CLI's handler as a traceback, and `python -O` erases the check
        # altogether, leaving an AttributeError on the next line instead.
        os.close(ipc_read_fd)
        raise DaemonHandshakeError("worker stdin pipe unavailable", {})

    try:
        proc.stdin.write(schema.model_dump_json().encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        # Worker died before reading schema. Still try to read the IPC pipe;
        # the worker's main() guard may have written a daemon_error frame.
        proc.stdin = None
        _ = exc  # discarded; we surface via the IPC read path below

    return _read_ipc_response(ipc_read_fd)


def _read_ipc_response(read_fd: int) -> dict[str, Any]:
    """Block on the IPC pipe until EOF, parse, and return the message.

    Called only after ``Popen`` has detached the worker, so every failure here
    is parent-side by construction and raises ``DaemonHandshakeError``. A
    truncated or unparsable frame is precisely the case where we cannot tell
    whether a worker is live, which is why the caller must assume one is.
    """
    try:
        with os.fdopen(read_fd, "rb") as reader:
            raw = reader.read()
    except OSError as exc:
        raise DaemonHandshakeError("failed to read worker IPC pipe", {"errno": exc.errno}) from exc

    if not raw:
        raise DaemonHandshakeError("worker IPC pipe closed without a message", {})

    try:
        message: dict[str, Any] = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DaemonHandshakeError(
            "worker IPC produced invalid JSON", {"position": exc.pos}
        ) from exc

    kind = message.get("kind")
    if kind in {"success", "required_failure", "daemon_error", "session_active"}:
        return message
    raise DaemonHandshakeError("unexpected IPC message kind", {"kind": str(kind)})
