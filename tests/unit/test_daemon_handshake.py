"""Parent-side handshake failures, on the far side of the detach point.

Validates: once ``subprocess.Popen`` has returned, a worker exists and is
detached. Every failure ``spawn_daemon`` can hit after that moment is
*parent-side* — the worker may be perfectly healthy, running, and holding the
session lock — so it must reach the caller as ``DaemonHandshakeError``, the
signal that a daemon needs stopping. A worker-authored failure arrives as an
IPC frame instead and is not this class.

Code: tunstrap/daemon.py (spawn_daemon, _read_ipc_response)
Assertion: each post-detach failure raises DaemonHandshakeError and keeps
DaemonError's exit code 4.
Method: subprocess.Popen replaced by a fake that emulates the worker's side of
the IPC pipe, so the failures can be produced exactly and no real process is
started.

How these fail if the defect returns: revert the raises to plain
``DaemonError`` and every case here fails, because ``DaemonHandshakeError`` is
the strictly narrower type the CLI keys its teardown decision on — a plain
``DaemonError`` sends ``run`` down the discard-without-teardown path that
orphans a live worker. ``test_missing_stdin_pipe_is_a_handshake_error``
additionally fails today with ``AssertionError``, which is not a
``TunstrapError`` at all and so escapes the CLI's handler entirely.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
from typing import IO, Any

import pytest

from tunstrap import daemon as daemon_mod
from tunstrap.daemon import spawn_daemon
from tunstrap.exceptions import (
    DaemonError,
    DaemonHandshakeError,
    DaemonHandshakeTimeoutError,
    exit_code_for,
)
from tunstrap.schemas import InputSchema

pytestmark = pytest.mark.unit


def _schema() -> InputSchema:
    return InputSchema.model_validate({"nodes": {}})


def _fake_popen(frame: bytes | None, *, stdin: bool = True) -> Any:
    """Build a Popen stand-in that writes ``frame`` to the inherited IPC fd.

    The parent closes its own copy of the write end right after Popen returns,
    so writing here and never holding the fd open reproduces exactly what the
    real worker's pipe looks like from the read side: the bytes, then EOF.
    """

    class _FakePopen:  # pylint: disable=too-few-public-methods
        def __init__(
            self,
            argv: list[str],
            *,
            pass_fds: list[int],
            **_kwargs: object,
        ) -> None:
            self.argv = argv
            self.pid = 424242
            self.stdin: IO[bytes] | None = io.BytesIO() if stdin else None
            if frame is not None:
                os.write(pass_fds[0], frame)

    return _FakePopen


@pytest.mark.parametrize(
    "frame, expected_message",
    [
        (b"", "worker IPC pipe closed without a message"),
        (b"{not json", "worker IPC produced invalid JSON"),
        (json.dumps({"kind": "surprise"}).encode(), "unexpected IPC message kind"),
    ],
)
def test_post_detach_ipc_failures_are_handshake_errors(
    monkeypatch: pytest.MonkeyPatch, frame: bytes, expected_message: str
) -> None:
    """Every _read_ipc_response failure names the parent, not the worker."""
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", _fake_popen(frame))
    with pytest.raises(DaemonHandshakeError) as caught:
        spawn_daemon(_schema())
    assert caught.value.message == expected_message


def test_missing_stdin_pipe_is_a_handshake_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replaced ``assert``: a real check, of the right type, kept under -O.

    ``assert proc.stdin is not None`` was an AssertionError — outside the
    TunstrapError hierarchy, so it escaped ``run``'s handler as a traceback —
    and ``python -O`` erased it entirely, leaving an AttributeError on the next
    line. Both failure modes are on the far side of the detach point.
    """
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", _fake_popen(None, stdin=False))
    with pytest.raises(DaemonHandshakeError) as caught:
        spawn_daemon(_schema())
    assert caught.value.message == "worker stdin pipe unavailable"


def test_handshake_error_is_a_daemon_error_and_still_exits_4() -> None:
    """Narrowing the type must not move the documented exit code.

    ``exit_code_for`` keys on the exact type, so a subclass without its own
    registry entry would silently fall through to the default 1 and change
    every parent-side IPC failure's exit code from 4.
    """
    exc = DaemonHandshakeError("boom", {})
    assert isinstance(exc, DaemonError), "callers catching DaemonError must still see this"
    assert exit_code_for(exc) == 4


def test_handshake_timeout_error_is_a_handshake_error_and_still_exits_4() -> None:
    """The exact-type exit registry keeps startup timeouts on exit code 4."""
    exc = DaemonHandshakeTimeoutError("boom", {})
    assert isinstance(exc, DaemonHandshakeError)
    assert exit_code_for(exc) == 4


def test_worker_authored_frames_are_returned_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control: a well-formed frame is data, not a handshake failure.

    Without this, an implementation that raised DaemonHandshakeError for
    *every* post-detach outcome would pass all the cases above.
    """
    frame = json.dumps({"kind": "daemon_error", "payload": {"error": "DaemonError"}}).encode()
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", _fake_popen(frame))
    message = spawn_daemon(_schema())
    assert message["kind"] == "daemon_error"


def test_retained_ipc_writer_times_out_terminates_and_reaps_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker retaining its IPC writer cannot indefinitely block startup.

    This fails if the startup deadline or either reap operation is removed:
    the protective outer event expires, releases the writer, and the result is
    a plain EOF handshake error rather than the timeout error.
    """
    held: list[int] = []
    held_lock = threading.Lock()
    completed = threading.Event()
    outcome: list[BaseException] = []

    def close_retained_writer() -> None:
        """Close the retained writer exactly once across the two test threads."""
        with held_lock:
            if held:
                os.close(held.pop())

    class _RetainedWriterPopen:  # pylint: disable=too-few-public-methods
        def __init__(self, _argv: list[str], *, pass_fds: list[int], **_kwargs: object) -> None:
            self.pid = 424242
            self.stdin: IO[bytes] | None = io.BytesIO()
            self.terminated = False
            self.wait_calls: list[float | None] = []
            held.append(os.dup(pass_fds[0]))

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True
            close_retained_writer()

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            return 0

    created: list[_RetainedWriterPopen] = []

    def fake_popen(*args: object, **kwargs: object) -> _RetainedWriterPopen:
        proc = _RetainedWriterPopen(*args, **kwargs)  # type: ignore[arg-type]
        created.append(proc)
        return proc

    def call_spawn() -> None:
        try:
            spawn_daemon(
                InputSchema.model_validate({"nodes": {}, "daemon": {"startup_timeout_seconds": 1}})
            )
        except (DaemonHandshakeError, DaemonHandshakeTimeoutError) as exc:
            outcome.append(exc)
        finally:
            completed.set()

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", fake_popen)
    thread = threading.Thread(target=call_spawn)
    thread.start()
    completed_in_time = completed.wait(5)
    if not completed_in_time:
        close_retained_writer()
    thread.join(timeout=1)

    assert completed_in_time
    assert len(outcome) == 1
    assert isinstance(outcome[0], DaemonHandshakeTimeoutError)
    assert outcome[0].details == {
        "timeout_seconds": 1,
        "worker_reaped": True,
        "pid": 424242,
    }
    assert created[0].terminated is True
    assert created[0].wait_calls == [10]


def test_reap_does_not_signal_non_positive_pid() -> None:
    """A malformed Popen stand-in cannot turn timeout cleanup into group signalling."""

    class _NonPositivePidPopen:  # pylint: disable=too-few-public-methods
        pid = 0

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            pytest.fail("timeout cleanup must not signal a non-positive pid")

    assert daemon_mod._reap_timed_out_worker(_NonPositivePidPopen(), 1) is False  # type: ignore[arg-type]


def test_reap_returns_false_when_kill_raises_oserror() -> None:
    """A failed kill is contained so timeout reporting remains a domain error."""

    class _KillOSErrorPopen:  # pylint: disable=too-few-public-methods
        pid = 424242

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            raise subprocess.TimeoutExpired("worker", 1)

        def kill(self) -> None:
            raise OSError("kill failed")

    assert daemon_mod._reap_timed_out_worker(_KillOSErrorPopen(), 1) is False  # type: ignore[arg-type]
