"""The silent stop primitive.

Validates: stop_session performs the stop and returns a StopOutcome for every
branch, and writes absolutely nothing to stdout or stderr — that silence is
what lets `run` keep fd 1 for the child while `stop` still prints its JSON.
Code: tunstrap/session.py
Assertion: each identity/kill scenario yields the documented StopOutcome, and
capsys shows empty out and err.
Method: monkeypatch session.verify_session and session.os.kill; no real
processes and no real signals, so this passes unchanged on macOS.

These are the ``stop``-verb tests: the daemon is *not* a child of the stopping
process, so ``no_child_pid`` below pins ``os.waitpid`` to ECHILD for the whole
module and every case here exercises the signal-0 fallback in ``_has_exited``.
The child topology — ``run``, where the daemon is a Popen child and its pid
survives its exit as a zombie — cannot be modelled with a patched ``os.kill``
at all, and lives in test_stop_session_child.py against a real process.
"""

from __future__ import annotations

import errno
import signal
from typing import Any

import pytest

from tunstrap import session as session_mod
from tunstrap.identity import IdentityCheckResult
from tunstrap.session import StopOutcome, stop_session

pytestmark = pytest.mark.unit

PID = 4242
SESSION = "/nonexistent/session"


@pytest.fixture(autouse=True)
def no_child_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """PID is nobody's child here; keep that explicit rather than accidental.

    Without this the real ``os.waitpid`` would run inside the test process,
    which is both nondeterministic and, if PID ever were a live child of
    pytest, actively harmful.
    """

    def _echild(_pid: int, _options: int) -> tuple[int, int]:
        raise ChildProcessError(errno.ECHILD, "No child processes")

    monkeypatch.setattr(session_mod.os, "waitpid", _echild)


def _fixed_check(result: IdentityCheckResult) -> Any:
    return lambda _session_dir, _pid: result


def _checks(*results: IdentityCheckResult) -> Any:
    seq = list(results)

    def _check(_session_dir: str, _pid: int) -> IdentityCheckResult:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _check


@pytest.mark.parametrize(
    "check, expected",
    [
        (IdentityCheckResult.not_found, StopOutcome(False, "not found")),
        (IdentityCheckResult.mismatch, StopOutcome(False, "identity mismatch")),
        (
            IdentityCheckResult.unavailable,
            StopOutcome(False, "identity check unavailable"),
        ),
    ],
)
def test_identity_branches(
    monkeypatch: pytest.MonkeyPatch,
    check: IdentityCheckResult,
    expected: StopOutcome,
) -> None:
    """A non-matching identity is reported and no signal is ever sent."""
    sent: list[int] = []
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(check))
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, s: sent.append(s))
    assert stop_session(SESSION, PID, 10, force=True) == expected
    assert sent == [], "must not signal a process it could not identify"


def test_sigterm_on_already_dead_process_is_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ProcessLookupError on SIGTERM means it is already gone: stopped, unforced."""
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    calls: list[int] = []

    def _kill(_pid: int, sig: int) -> None:
        calls.append(sig)
        raise ProcessLookupError

    monkeypatch.setattr(session_mod.os, "kill", _kill)
    assert stop_session(SESSION, PID, 10, force=True) == StopOutcome(True)
    assert calls == [signal.SIGTERM]


def test_exits_within_grace_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that dies during the grace poll yields stopped=True, forced=False."""
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    calls: list[int] = []

    def _kill(_pid: int, sig: int) -> None:
        calls.append(sig)
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(session_mod.os, "kill", _kill)
    monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)
    assert stop_session(SESSION, PID, 10, force=True) == StopOutcome(True)
    assert calls[0] == signal.SIGTERM
    assert signal.SIGKILL not in calls, "must not escalate when the grace poll succeeded"


def test_grace_poll_uses_strict_deadline_and_half_second_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed poll sleeps 0.5 seconds and equality with the deadline ends grace."""
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    monotonic_values = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: next(monotonic_values))
    sleeps: list[float] = []
    monkeypatch.setattr(session_mod.time, "sleep", lambda seconds: sleeps.append(seconds))
    calls: list[int] = []
    monkeypatch.setattr(session_mod.os, "kill", lambda _pid, sig: calls.append(sig))

    assert stop_session(SESSION, PID, 1, force=True) == StopOutcome(True, forced=True)
    assert calls == [signal.SIGTERM, 0, signal.SIGKILL]
    assert sleeps == [0.5]


def test_not_force_reports_still_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """force=False after an expired grace reports 'still alive' and never SIGKILLs."""
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    calls: list[int] = []
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, s: calls.append(s))
    assert stop_session(SESSION, PID, 0, force=False) == StopOutcome(False, "still alive")
    assert calls == [signal.SIGTERM]


def test_identity_changed_during_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pid recycled during grace is refused, not SIGKILLed."""
    monkeypatch.setattr(
        session_mod,
        "verify_session",
        _checks(IdentityCheckResult.match, IdentityCheckResult.mismatch),
    )
    calls: list[int] = []
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, s: calls.append(s))
    assert stop_session(SESSION, PID, 0, force=True) == StopOutcome(
        False, "identity changed during grace"
    )
    assert calls == [signal.SIGTERM]


def test_forced_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that survives the grace and still owns the session is SIGKILLed."""
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    calls: list[int] = []
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, s: calls.append(s))
    assert stop_session(SESSION, PID, 0, force=True) == StopOutcome(True, forced=True)
    assert calls == [signal.SIGTERM, signal.SIGKILL]


def test_reaped_child_ends_the_poll_even_though_signal_zero_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The zombie case, stated as a unit: waitpid decides, signal 0 does not.

    ``os.kill`` here never raises, which is precisely how an unreaped child
    behaves — its pid answers signal 0 until somebody collects it. Revert
    ``_has_exited`` to a bare ``os.kill(pid, 0)`` and this poll runs to the
    deadline and escalates, so the outcome becomes ``StopOutcome(True,
    forced=True)`` and the SIGKILL assertion fails too.
    """
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    calls: list[int] = []
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, s: calls.append(s))
    monkeypatch.setattr(session_mod.os, "waitpid", lambda pid, _options: (pid, 0))
    monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)

    assert stop_session(SESSION, PID, 10, force=True) == StopOutcome(True)
    assert calls == [signal.SIGTERM], "a reaped child must not be escalated to SIGKILL"


def test_still_running_child_keeps_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """waitpid returning 0 means "not yet", not "gone".

    Fails if the reap is written as a bare ``os.waitpid(...)`` whose return
    value is discarded: the very first poll would then report success and the
    outcome would lose its ``forced=True``.
    """
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    monotonic_values = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)
    calls: list[int] = []
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, s: calls.append(s))
    monkeypatch.setattr(session_mod.os, "waitpid", lambda _pid, _options: (0, 0))

    assert stop_session(SESSION, PID, 1, force=True) == StopOutcome(True, forced=True)
    assert calls == [signal.SIGTERM, signal.SIGKILL]


def test_waitpid_failure_falls_back_instead_of_escaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waitpid error is absorbed and the signal-0 probe still runs.

    stop_session raising would short-circuit ``_teardown_run_inner`` before it
    removes tunnel-data, so ``run`` would leak the session on a kernel-level
    oddity. Delete the ``except OSError`` arm and this test errors out with the
    OSError instead of comparing an outcome; narrow the arm to
    ``ChildProcessError`` and it errors the same way.
    """
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    monotonic_values = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)
    calls: list[int] = []
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, s: calls.append(s))

    def _einval(_pid: int, _options: int) -> tuple[int, int]:
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(session_mod.os, "waitpid", _einval)

    assert stop_session(SESSION, PID, 1, force=True) == StopOutcome(True, forced=True)
    assert calls == [signal.SIGTERM, 0, signal.SIGKILL], "the signal-0 probe must still run"


def test_non_positive_pid_never_reaches_waitpid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt daemon.pid must not turn the reap into a process-group wait.

    ``waitpid(0, ...)`` collects any child in the caller's process group, which
    under ``run`` includes the foreground command whose exit status is the CLI's
    return value. Drop the ``pid > 0`` guard and this records that call.
    """
    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    monotonic_values = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, _s: None)
    waited: list[int] = []
    monkeypatch.setattr(
        session_mod.os, "waitpid", lambda pid, _options: (waited.append(pid), (pid, 0))[1]
    )

    stop_session(SESSION, 0, 1, force=True)
    assert waited == [], "waitpid must never be handed a pid that selects a process group"


def test_stop_session_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The primitive is silent on every branch: that is its whole purpose."""
    for check in IdentityCheckResult:
        monkeypatch.setattr(session_mod, "verify_session", _fixed_check(check))
        monkeypatch.setattr(session_mod.os, "kill", lambda _p, _s: None)
        stop_session(SESSION, PID, 0, force=True)

    def _already_dead(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    monkeypatch.setattr(session_mod.os, "kill", _already_dead)
    stop_session(SESSION, PID, 0, force=True)

    def _dies_during_grace(_pid: int, sig: int) -> None:
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    monkeypatch.setattr(session_mod.os, "kill", _dies_during_grace)
    monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)
    stop_session(SESSION, PID, 10, force=True)

    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    monkeypatch.setattr(session_mod.os, "kill", lambda _p, _s: None)
    stop_session(SESSION, PID, 0, force=False)

    monkeypatch.setattr(
        session_mod,
        "verify_session",
        _checks(IdentityCheckResult.match, IdentityCheckResult.mismatch),
    )
    stop_session(SESSION, PID, 0, force=True)

    def _dies_on_sigkill(_pid: int, sig: int) -> None:
        if sig == signal.SIGKILL:
            raise ProcessLookupError

    monkeypatch.setattr(session_mod, "verify_session", _fixed_check(IdentityCheckResult.match))
    monkeypatch.setattr(session_mod.os, "kill", _dies_on_sigkill)
    stop_session(SESSION, PID, 0, force=True)

    captured = capsys.readouterr()
    assert captured.out == "", f"stop_session wrote to stdout: {captured.out!r}"
    assert captured.err == "", f"stop_session wrote to stderr: {captured.err!r}"
