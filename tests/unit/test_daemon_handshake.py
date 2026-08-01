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
from typing import IO, Any

import pytest

from tunstrap import daemon as daemon_mod
from tunstrap.daemon import spawn_daemon
from tunstrap.exceptions import DaemonError, DaemonHandshakeError, exit_code_for
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


def test_worker_authored_frames_are_returned_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control: a well-formed frame is data, not a handshake failure.

    Without this, an implementation that raised DaemonHandshakeError for
    *every* post-detach outcome would pass all the cases above.
    """
    frame = json.dumps({"kind": "daemon_error", "payload": {"error": "DaemonError"}}).encode()
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", _fake_popen(frame))
    message = spawn_daemon(_schema())
    assert message["kind"] == "daemon_error"
