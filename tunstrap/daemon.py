"""Spawn the worker daemon via subprocess.Popen with a dedicated IPC pipe."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from typing import IO, Any

from tunstrap.exceptions import DaemonHandshakeError, DaemonHandshakeTimeoutError
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


def _worker_env(input_env: str | None) -> dict[str, str]:
    """Copy the parent environment minus the one variable known to be secret-bearing.

    ``input_env`` is the name ``--input-env`` was actually given, not a
    literal: the option takes an arbitrary name, so a scrub keyed on
    ``TUNSTRAP_INPUT`` would leave the SSH private key PEM in the environment
    of a long-lived detached process for every other name. Mirrors
    ``cli._build_child_env``, which scrubs the same name from ``tofu``'s
    environment — one rule, both children.

    **Deliberately a filtered copy, not a minimal environment.** The worker
    receives its schema over stdin and needs nothing else *from tunstrap*, but
    it is still an ordinary process in the operator's session: it resolves
    imports through ``PYTHONPATH``, may authenticate through ``SSH_AUTH_SOCK``,
    and reaches the network under the operator's proxy and CA-bundle settings.
    Handing it a minimal environment would trade a bounded, demonstrated leak
    for an unbounded set of setups that silently stop working. It would also
    buy no privilege boundary: worker and parent run as the same uid, and
    ``/proc/<pid>/environ`` is owner-readable, so anyone who can read the
    worker's environment can already read the parent's. The named variable is
    different in kind — it is tunstrap's own injected secret, and tunstrap is
    the only component that knows it is secret-bearing.
    """
    env = dict(os.environ)
    if input_env is not None:
        env.pop(input_env, None)
    return env


def spawn_daemon(
    schema: InputSchema, session_dir: str | None = None, *, input_env: str | None = None
) -> dict[str, Any]:
    """Spawn the worker, send the schema, read the IPC response, return it.

    Returns the structured IPC message for any of the four worker outcomes:
    ``success``, ``required_failure``, ``daemon_error``, ``session_active``.
    Callers dispatch on ``message["kind"]`` and map to CLI exit codes. Those
    are worker-authored: the worker cleaned up after itself before writing the
    frame, so no daemon of ours survives them.

    When no path is supplied, the parent mints one before ``Popen`` so every
    later handshake error can report it. The worker-only ownership flag keeps
    the old lifecycle contract: normal worker cleanup still removes that whole
    generated root rather than treating it as an operator-owned directory.

    **This function is not atomic, and the seam is ``Popen``.** Before it, no
    worker exists and a failure leaves nothing behind. After it, the worker is
    launched and detached — so every failure from there on is *parent-side* and
    raises ``DaemonHandshakeError``, telling the caller a daemon may be running
    and must be stopped rather than abandoned. Callers that treat any spawn
    failure as "no daemon exists" orphan the worker on exactly those paths.
    """
    worker_env = _worker_env(input_env)
    ipc_read_fd, ipc_write_fd = os.pipe()
    log_target = _open_log_target(schema.daemon.log_file)
    generated_session_dir = session_dir is None
    worker_session_dir: str | None = None
    try:
        # ``realpath`` so the handle the parent reports is byte-identical to the
        # one the worker itself would report: ``SessionDir.create`` resolves a
        # supplied path, and a recovery handle that does not match ``status``
        # and ``stop`` output is one an operator has to second-guess.
        worker_session_dir = (
            os.path.realpath(tempfile.mkdtemp(prefix="tunstrap-"))
            if session_dir is None
            else session_dir
        )
        proc = subprocess.Popen(  # pylint: disable=consider-using-with  # detached; never wait()ed
            [
                sys.executable,
                "-m",
                "tunstrap._worker",
                f"--ipc-fd={ipc_write_fd}",
                f"--session-dir={worker_session_dir}",
                *(["--generated-session-dir"] if generated_session_dir else []),
            ],
            stdin=subprocess.PIPE,
            stdout=log_target,
            stderr=log_target,
            pass_fds=[ipc_write_fd],
            start_new_session=True,
            close_fds=True,
            env=worker_env,
        )
    except BaseException:
        # Pre-detach by construction: this handler is reachable only while
        # ``Popen`` is still raising, so no worker exists and the minted root is
        # unambiguously ours to remove. ``rmdir``, never ``rmtree``: it succeeds
        # only on an empty directory, so it can never destroy session state even
        # if some future change lets a worker reach this path.
        #
        # The read end of the IPC pipe is ours to close here for the same
        # reason no worker exists: nothing was ever launched to write a
        # handshake into it. It cannot go into the shared ``finally`` instead,
        # because that also runs on success — past the detach the read end is
        # the parent's only handle on the worker's IPC frame, so closing it
        # there would break every ``spawn_daemon`` caller that reads the
        # handshake. Left unclosed on this path it would leak for the life of
        # the process (issue #40). The write end needs no such care: the
        # ``finally`` below closes it on every path already. A failing close
        # must not mask the exception being re-raised, hence the swallow.
        try:
            os.close(ipc_read_fd)
        except OSError:
            pass
        if generated_session_dir and worker_session_dir is not None:
            try:
                os.rmdir(worker_session_dir)
            except OSError:
                pass
        raise
    finally:
        os.close(ipc_write_fd)
        if isinstance(log_target, int):
            # subprocess.DEVNULL is an int sentinel; nothing to close.
            pass
        else:
            log_target.close()

    # Everything below this line runs with a detached worker already alive.
    try:
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

        return _read_ipc_response(
            ipc_read_fd,
            proc,
            timeout=schema.daemon.startup_timeout_seconds,
            reap_timeout=schema.daemon.shutdown_grace_seconds,
        )
    except DaemonHandshakeError as exc:
        if worker_session_dir is not None:
            exc.details.setdefault("session_dir", worker_session_dir)
        if proc.pid > 0:
            exc.details.setdefault("pid", proc.pid)
        raise


def _kill_timed_out_worker(proc: subprocess.Popen[bytes], reap_timeout: int) -> bool:
    """Escalate a timed-out termination to SIGKILL without leaking OSError."""
    if proc.pid <= 0 or proc.poll() is not None:
        return proc.poll() is not None
    try:
        proc.kill()
        try:
            proc.wait(timeout=reap_timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
    except OSError:
        return proc.poll() is not None


def _reap_timed_out_worker(proc: subprocess.Popen[bytes], reap_timeout: int) -> bool:
    """Terminate a live worker from Popen, escalating once, and report reaping.

    ``Popen`` is the verified owner handle created in this process. Its positive
    pid is checked before either signal so malformed stand-ins cannot target a
    process group or a non-positive pid. Each wait is bounded by the configured
    shutdown grace, keeping the parent-side startup failure bounded even if the
    worker ignores both signals.
    """
    if proc.pid <= 0 or proc.poll() is not None:
        return proc.poll() is not None
    try:
        proc.terminate()
        proc.wait(timeout=reap_timeout)
        return True
    except subprocess.TimeoutExpired:
        return _kill_timed_out_worker(proc, reap_timeout)
    except OSError:
        return proc.poll() is not None


def _read_ipc_response(
    read_fd: int,
    proc: subprocess.Popen[bytes],
    *,
    timeout: int,
    reap_timeout: int,
) -> dict[str, Any]:
    """Read one worker IPC frame through EOF before the startup deadline.

    Called only after ``Popen`` has detached the worker, so every failure here
    is parent-side by construction and raises ``DaemonHandshakeError``. A
    truncated or unparsable frame is precisely the case where we cannot tell
    whether a worker is live, which is why the caller must assume one is. On a
    deadline expiry the parent owns the verified ``Popen`` handle, terminates
    that worker, and waits for it before surfacing the distinct timeout error.
    """
    deadline = time.monotonic() + timeout
    try:
        raw_parts: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            readable, _, _ = select.select([read_fd], [], [], remaining)
            if not readable:
                raise TimeoutError
            chunk = os.read(read_fd, 8192)
            if not chunk:
                break
            raw_parts.append(chunk)
    except TimeoutError as exc:
        reaped = _reap_timed_out_worker(proc, reap_timeout)
        raise DaemonHandshakeTimeoutError(
            "worker IPC startup response timed out",
            {"timeout_seconds": timeout, "worker_reaped": reaped, "pid": proc.pid},
        ) from exc
    except OSError as exc:
        raise DaemonHandshakeError("failed to read worker IPC pipe", {"errno": exc.errno}) from exc
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass

    raw = b"".join(raw_parts)

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
