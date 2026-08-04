"""`run`'s teardown: silent on stdout, diagnostic on stderr, never raising.

Validates: after the child exits, tunstrap writes nothing to fd 1 — that is the
invariant the tofu-proxy pattern rests on — while a genuine teardown failure is
still reported, on stderr, without changing the exit code.
Code: tunstrap/cli.py (_teardown_run)
Assertion: result.stdout carries only the child's bytes; failure text appears in
result.stderr; a raising stop primitive does not change the child's exit code.
Method: CliRunner with spawn_daemon, subprocess.Popen, stop_session,
SessionDir.read_identity and SessionDir.cleanup_path all monkeypatched, so no
daemon, no signals and no filesystem work — this passes unchanged on macOS.
"""

from __future__ import annotations

import json
import shutil
import signal as signal_mod
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap import session as session_mod
from tunstrap.cli import main
from tunstrap.exceptions import DaemonError, DaemonHandshakeError
from tunstrap.identity import IdentityCheckResult
from tunstrap.session import StopOutcome

pytestmark = pytest.mark.unit


def _success_payload() -> dict[str, Any]:
    return {
        "kind": "success",
        "payload": {
            "connections": {"node": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}}},
            "pid": 99,
            "session_dir": "/s",
            "started_at": "now",
        },
    }


class QuietPopen:
    """Popen stand-in that writes nothing and exits 7."""

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        self.cmd = cmd
        self.env = env
        self.returncode = 7

    def wait(self) -> int:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        """Accept forwarded signals; the fake child ignores them."""


@pytest.fixture(name="spawned")
def _spawned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_mod, "spawn_daemon", lambda _schema, session_dir=None: _success_payload()
    )
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    monkeypatch.setattr(cli_mod.SessionDir, "read_identity", staticmethod(lambda _sd: 4242))


_ARGS = [
    "run",
    "u@h",
    "--target",
    "db=127.0.0.1:5432",
    "--ssh-password-stdin",
    "--",
    "true",
]


def test_teardown_silent_on_success(monkeypatch: pytest.MonkeyPatch, spawned: None) -> None:
    """A clean teardown writes nothing at all to stdout."""
    monkeypatch.setattr(cli_mod, "stop_session", lambda _sd, _pid, _g, force: StopOutcome(True))
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"


def test_teardown_stop_failure_goes_to_stderr(
    monkeypatch: pytest.MonkeyPatch, spawned: None, tmp_path: Path
) -> None:
    """A non-stopped outcome is reported on stderr and stdout stays clean."""
    monkeypatch.setattr(
        cli_mod,
        "stop_session",
        lambda _sd, _pid, _g, force: StopOutcome(False, "identity mismatch"),
    )
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(
        main, [*_ARGS[:-2], "--session-dir", str(tmp_path), "--", "true"], input="secret\n"
    )
    assert result.exit_code == 7
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert "identity mismatch" in result.stderr


def test_teardown_unremovable_paths_go_to_stderr(
    monkeypatch: pytest.MonkeyPatch, spawned: None
) -> None:
    """Paths cleanup could not remove are named on stderr, not swallowed."""
    monkeypatch.setattr(cli_mod, "stop_session", lambda _sd, _pid, _g, force: StopOutcome(True))
    monkeypatch.setattr(
        cli_mod.SessionDir,
        "cleanup_path",
        classmethod(lambda _cls, _sd: ["/s/tunnel-data"]),
    )
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert "/s/tunnel-data" in result.stderr


def test_teardown_exception_does_not_change_exit_code(
    monkeypatch: pytest.MonkeyPatch, spawned: None, tmp_path: Path
) -> None:
    """A raising stop primitive is reported on stderr; the child's 7 still wins.

    Supplies ``--session-dir`` so nothing is minted: what this test owns is the
    exit code and stdout purity, and the fate of a minted root under a raising
    stop belongs to
    ``test_raising_stop_preserves_the_minted_session_root``.
    """

    def _boom(_sd: str, _pid: int, _g: int, force: bool) -> StopOutcome:
        raise RuntimeError("stop exploded")

    monkeypatch.setattr(cli_mod, "stop_session", _boom)
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(
        main, [*_ARGS[:-2], "--session-dir", str(tmp_path), "--", "true"], input="secret\n"
    )
    assert result.exit_code == 7, "teardown failure must never override the child code"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert "stop exploded" in result.stderr


def test_raising_stop_preserves_the_minted_session_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop that *raises* preserves the session root, as a reported failure does.

    ``StopOutcome(False, …)`` means we know the daemon survived; an exception
    means we do not know its state at all, which is the stronger reason to keep
    the identity file rather than destroy it. The root ``run`` minted is the
    only place that file can be, so a raising teardown must not take it along —
    otherwise a surviving daemon becomes exactly the orphan this window exists
    to prevent.

    A *minted* root is the only falsifiable shape for this claim: a
    caller-supplied ``--session-dir`` is never removed on any path, so
    asserting its survival could not fail.

    Scoped to the path this test's own spawn stub observed, never a glob of the
    shared temp directory — such a glob also sees roots minted by other tests
    and is order-dependent by construction.
    """
    minted: list[str | None] = []

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        minted.append(session_dir)
        return _success_payload()

    def _boom(_sd: str, _pid: int, _g: int, force: bool) -> StopOutcome:
        raise RuntimeError("stop exploded")

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    monkeypatch.setattr(cli_mod.SessionDir, "read_identity", staticmethod(lambda _sd: 4242))
    monkeypatch.setattr(cli_mod, "stop_session", _boom)

    result = CliRunner().invoke(main, _ARGS, input="secret\n")

    root = minted[0]
    assert root is not None
    try:
        assert result.exit_code == 7
        assert Path(root).is_dir(), "a raising stop destroyed the only handle on the daemon"
        assert f"tunstrap stop --session-dir {root} --force" in result.stderr
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_teardown_permission_error_does_not_change_exit_code(
    monkeypatch: pytest.MonkeyPatch, spawned: None, tmp_path: Path
) -> None:
    """A recycled pid permission failure is reported; the child's 7 still wins.

    ``--session-dir`` for the same reason as the sibling above: a raising stop
    now preserves a minted root by contract, so minting one here would leak it.
    """
    monkeypatch.setattr(session_mod, "verify_session", lambda _sd, _pid: IdentityCheckResult.match)

    def _permission_denied(_pid: int, _sig: int) -> None:
        raise PermissionError("recycled pid")

    monkeypatch.setattr(session_mod.os, "kill", _permission_denied)
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(
        main, [*_ARGS[:-2], "--session-dir", str(tmp_path), "--", "true"], input="secret\n"
    )
    assert result.exit_code == 7, "teardown failure must never override the child code"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert "PermissionError: recycled pid" in result.stderr


def test_teardown_keyboard_interrupt_does_not_change_exit_code(
    monkeypatch: pytest.MonkeyPatch, spawned: None, tmp_path: Path
) -> None:
    """A KeyboardInterrupt from teardown does not override the child's exit code.

    ``--session-dir`` for the same reason as the two siblings above.
    """

    def _interrupted(_sd: str, _pid: int, _g: int, *, force: bool) -> StopOutcome:
        raise KeyboardInterrupt("second Ctrl-C")

    monkeypatch.setattr(cli_mod, "stop_session", _interrupted)
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(
        main, [*_ARGS[:-2], "--session-dir", str(tmp_path), "--", "true"], input="secret\n"
    )
    assert result.exit_code == 7, "teardown interruption must never override the child code"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert "KeyboardInterrupt: second Ctrl-C" in result.stderr


def test_teardown_already_exited_daemon_is_silent(
    monkeypatch: pytest.MonkeyPatch, spawned: None
) -> None:
    """A daemon that already exited is normal and produces no teardown warning."""
    monkeypatch.setattr(
        cli_mod, "stop_session", lambda _sd, _pid, _g, *, force: StopOutcome(False, "not found")
    )
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert result.stderr == "", f"run warned about an already-exited daemon: {result.stderr!r}"


def test_teardown_stderr_write_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A diagnostic write failure cannot escape the never-raise teardown wrapper."""

    class BrokenStderr:
        def write(self, _message: str) -> int:
            raise BrokenPipeError("stderr closed")

    def _boom(_sd: str, _pid: int, _g: int, *, force: bool) -> StopOutcome:
        raise RuntimeError("stop exploded")

    monkeypatch.setattr(cli_mod.SessionDir, "read_identity", staticmethod(lambda _sd: 4242))
    monkeypatch.setattr(cli_mod, "stop_session", _boom)
    monkeypatch.setattr(cli_mod.sys, "stderr", BrokenStderr())
    cli_mod._teardown_run("/s", 0, minted_root=None)


@pytest.fixture(name="teardowns")
def _teardowns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Record every _teardown_run call as (session_dir, grace, minted_root).

    Records *and* cleans: `run` mints a real temp directory before spawning,
    so a fixture that only recorded would leak one per test in this module.
    """
    calls: list[tuple[Any, ...]] = []

    def _record(session_dir: str, grace_seconds: int, *, minted_root: str | None) -> None:
        calls.append((session_dir, grace_seconds, minted_root))
        cleaning_teardown(session_dir, grace_seconds, minted_root=minted_root)

    monkeypatch.setattr(cli_mod, "_teardown_run", _record)
    return calls


def test_run_mints_the_session_path_before_spawning(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """With no --session-dir, run creates the directory itself and passes it on."""
    spawned: list[str | None] = []

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        spawned.append(session_dir)
        assert session_dir is not None, "run must not let the worker generate the path"
        assert Path(session_dir).is_dir(), "the minted path must exist before spawning"
        return _success_payload()

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7
    minted = spawned[0]
    assert minted is not None
    assert minted.startswith(tempfile.gettempdir())
    assert teardowns == [(minted, 10, minted)]


def test_teardown_uses_the_minted_path_not_the_payload(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """The success payload's session_dir is ignored; the minted path wins.

    This is the orphan fix: cleanup must not depend on the object whose
    validation can fail.
    """
    spawned: list[str | None] = []

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        spawned.append(session_dir)
        payload = _success_payload()
        payload["payload"]["session_dir"] = "/completely/bogus"
        return payload

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7
    assert teardowns[0][0] == spawned[0]
    assert teardowns[0][0] != "/completely/bogus"


def test_supplied_session_dir_is_never_minted(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    """A caller-supplied --session-dir is passed through with minted_root=None.

    This covers the *wiring* only: which path run hands to teardown, and that
    it claims no ownership of it. It deliberately makes no claim about the
    directory surviving -- the `teardowns` fixture substitutes
    `cleaning_teardown`, which removes only `minted_root`, so a supplied
    directory survives it by construction and a survival assertion here could
    never fail. That claim belongs to
    `test_production_teardown_keeps_a_supplied_session_dir`, which lets the
    real `_teardown_run` run.
    """
    supplied = tmp_path / "work"
    supplied.mkdir()
    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda _s, session_dir=None: _success_payload())
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    result = CliRunner().invoke(
        main,
        [
            "run",
            "u@h",
            "--target",
            "db=127.0.0.1:5432",
            "--ssh-password-stdin",
            "--session-dir",
            str(supplied),
            "--",
            "true",
        ],
        input="secret\n",
    )
    assert result.exit_code == 7
    assert teardowns == [(str(supplied), 10, None)]


def test_production_teardown_keeps_a_supplied_session_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Production teardown removes tunnel-data and nothing else the caller owns.

    The ownership asymmetry exercised against the real `_teardown_run`: no
    `teardowns` fixture, no `cleanup_path` or `read_identity` stub, so
    `_teardown_run_inner` does its own filesystem work. A supplied path makes
    the worker's SessionDir non-generated, so the root is the caller's; an
    implementation that removed `session_dir` unconditionally would take the
    sentinel and the root with it.
    """
    supplied = tmp_path / "work"
    supplied.mkdir()
    sentinel = supplied / "caller-owned.txt"
    sentinel.write_text("do not delete me")
    data = supplied / "tunnel-data"
    data.mkdir()
    (data / "daemon.pid").write_text("4242\n")

    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda _s, session_dir=None: _success_payload())
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    monkeypatch.setattr(cli_mod, "stop_session", lambda _sd, _pid, _g, force: StopOutcome(True))

    result = CliRunner().invoke(
        main,
        [
            "run",
            "u@h",
            "--target",
            "db=127.0.0.1:5432",
            "--ssh-password-stdin",
            "--session-dir",
            str(supplied),
            "--",
            "true",
        ],
        input="secret\n",
    )
    assert result.exit_code == 7
    assert result.stderr == "", f"a clean teardown warned: {result.stderr!r}"
    assert not data.exists(), "teardown must remove tunnel-data from a supplied session dir"
    assert supplied.is_dir(), "run must never remove a caller-supplied session dir"
    assert sentinel.read_text() == "do not delete me", "teardown destroyed caller-owned content"


def test_failed_teardown_keeps_identity_data_for_manual_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unverified live daemon retains the data needed to stop it safely."""
    session_dir = tmp_path / "session"
    tunnel_data = session_dir / "tunnel-data"
    tunnel_data.mkdir(parents=True)
    identity = tunnel_data / "daemon.pid"
    identity.write_text("4242\n")
    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda _s, session_dir=None: _success_payload())
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    monkeypatch.setattr(cli_mod.SessionDir, "read_identity", staticmethod(lambda _sd: 4242))
    monkeypatch.setattr(
        cli_mod,
        "stop_session",
        lambda _sd, _pid, _grace, force: StopOutcome(False, "identity mismatch"),
    )

    result = CliRunner().invoke(
        main, [*_ARGS[:-2], "--session-dir", str(session_dir), "--", "true"], input="secret\n"
    )

    assert result.exit_code == 7
    assert identity.read_text() == "4242\n"
    assert f"tunstrap stop --session-dir {session_dir} --force" in result.stderr


def test_minted_root_is_discarded_when_the_worker_reports_the_failure(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """A worker-authored failure needs no teardown, and leaves no minted dir.

    This is the narrower of the two spawn-failure classes. A plain
    ``DaemonError`` is what the worker itself authored: it reached its own
    guard, released the session lock and removed its session dir before
    reporting (``_worker.py:169-190``), then exited. Nothing is running, so
    ``teardowns == []`` is right *here* — but it is right because of who
    failed, not because "spawn raised". The sibling test below covers the case
    where that inference does not hold.
    """
    seen: list[str | None] = []

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        seen.append(session_dir)
        raise DaemonError("worker died", {})

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 4
    assert seen[0] is not None
    assert not Path(seen[0]).exists(), "a failed spawn leaked a minted session root"
    assert teardowns == [], "the worker cleaned up after itself; nothing to tear down"


def test_handshake_failure_stops_the_worker_instead_of_orphaning_it(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """A parent-side handshake failure tears down: the worker may be alive.

    ``spawn_daemon`` detaches the worker at ``Popen`` and only then attempts
    the handshake. If *that* fails, the worker is running, holding the session
    lock, with a tunnel open — and the old code took the worker-authored path:
    delete the session root and exit, leaving a daemon with no directory and
    nobody to stop it.

    Fails if the handler is collapsed back into the generic ``TunstrapError``
    arm: ``teardowns`` is then empty and the daemon is orphaned.
    """
    seen: list[str | None] = []

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        seen.append(session_dir)
        raise DaemonHandshakeError("worker IPC produced invalid JSON", {"position": 0})

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    minted = seen[0]
    assert minted is not None
    assert result.exit_code == 4, "a handshake failure keeps DaemonError's exit code"
    assert teardowns == [(minted, 10, minted)], "a possibly-live worker must be stopped"
    assert not Path(minted).exists(), "teardown must still remove the minted session root"
    assert json.loads(result.stderr)["error"] == "DaemonHandshakeError"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"


def test_minted_root_is_removed_after_a_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a real teardown, the minted root itself is gone from disk."""
    seen: list[str | None] = []

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        seen.append(session_dir)
        return _success_payload()

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    monkeypatch.setattr(cli_mod, "stop_session", lambda _sd, _pid, _g, force: StopOutcome(True))
    monkeypatch.setattr(cli_mod.SessionDir, "read_identity", staticmethod(lambda _sd: 4242))
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7
    assert seen[0] is not None
    assert not Path(seen[0]).exists(), "teardown left the minted session root behind"


@pytest.fixture(name="signal_guard", autouse=True)
def _signal_guard() -> Iterator[None]:
    """Guarantee this process's SIGINT/SIGTERM handlers survive the test.

    Autouse because every test here that reaches ``_run_child`` installs
    ``_forward`` as this process's SIGINT/SIGTERM handler. If such a test fails
    before restoration, ``_forward`` stays bound to a dead ``QuietPopen`` for
    the remainder of the session, and a later test — or a real Ctrl-C — would
    behave unpredictably.

    ``signal_mod.signal`` is captured at setup: a test that makes restoration
    raise does so by patching that very function, and this fixture may be torn
    down *before* monkeypatch undoes the patch, so calling it by attribute
    would re-enter the flaky stub and error out in teardown.
    """
    real_signal = signal_mod.signal
    saved = [(s, signal_mod.getsignal(s)) for s in (signal_mod.SIGINT, signal_mod.SIGTERM)]
    try:
        yield
    finally:
        for signum, handler in saved:
            real_signal(signum, handler)


def test_malformed_success_payload_still_tears_down(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """A success payload missing session_dir must not orphan the daemon."""
    spawned: list[str | None] = []

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        spawned.append(session_dir)
        payload = _success_payload()
        del payload["payload"]["session_dir"]
        return payload

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 4
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert json.loads(result.stderr)["error"] == "DaemonError"
    assert len(teardowns) == 1, "teardown must run exactly once"
    assert teardowns[0][0] == spawned[0], "teardown must use the minted path"


def test_non_string_session_dir_still_tears_down(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """A non-string session_dir fails validation post-spawn but still tears down."""

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        payload = _success_payload()
        payload["payload"]["session_dir"] = 17
        return payload

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 4
    assert len(teardowns) == 1


@pytest.mark.parametrize("missing", ["kind", "payload"])
def test_unreadable_envelope_still_tears_down(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]], missing: str
) -> None:
    """An envelope run cannot index must not orphan a daemon that may be live.

    ``message["kind"]`` and ``message["payload"]`` are read after the spawn.
    Indexing them outside cleanup ownership -- including as the argument
    expression of the supervise call, which is evaluated in the caller -- lets
    a KeyError escape while a worker may already be running.
    """
    envelope = _success_payload()
    del envelope[missing]
    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda _s, session_dir=None: envelope)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 4
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert json.loads(result.stderr)["error"] == "DaemonError"
    assert len(teardowns) == 1, "an unreadable envelope must still tear down, exactly once"


@pytest.mark.parametrize("target", ["render_env", "_build_child_env"])
def test_post_spawn_exception_tears_down_once(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]], target: str
) -> None:
    """Anything raised between the spawn and Popen still stops the daemon, exit 4."""

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError(f"{target} exploded")

    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda _s, session_dir=None: _success_payload())
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    monkeypatch.setattr(cli_mod, target, _boom)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 4
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert json.loads(result.stderr)["error"] == "DaemonError"
    assert len(teardowns) == 1, "teardown must run exactly once"


def test_launch_failure_is_127_and_tears_down(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """An unlaunchable child is 127, distinct from the post-spawn guard's 4."""

    def _boom(_cmd: list[str], env: dict[str, str] | None = None) -> Any:
        raise OSError("no such binary")

    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda _s, session_dir=None: _success_payload())
    monkeypatch.setattr(cli_mod.subprocess, "Popen", _boom)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 127
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert "failed to launch command" in result.stderr
    assert len(teardowns) == 1


def test_failing_signal_restoration_cannot_skip_teardown(
    monkeypatch: pytest.MonkeyPatch,
    teardowns: list[tuple[Any, ...]],
    signal_guard: None,
) -> None:
    """If restoring the handlers raises, the daemon is still stopped."""
    real_signal = signal_mod.signal
    calls = {"n": 0}

    def _flaky(signum: int, handler: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > 2:  # the first two are installs, the rest are restores
            raise RuntimeError("cannot restore handler")
        return real_signal(signum, handler)

    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda _s, session_dir=None: _success_payload())
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    monkeypatch.setattr(cli_mod.signal, "signal", _flaky)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert len(teardowns) == 1, "teardown must run even when restoration raises"
    assert result.exit_code == 4
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert json.loads(result.stderr)["error"] == "DaemonError"


def test_signal_handlers_are_restored_on_the_happy_path(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """After a normal run the process's original handlers are back in place."""
    before = (
        signal_mod.getsignal(signal_mod.SIGINT),
        signal_mod.getsignal(signal_mod.SIGTERM),
    )
    monkeypatch.setattr(cli_mod, "spawn_daemon", lambda _s, session_dir=None: _success_payload())
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7
    after = (
        signal_mod.getsignal(signal_mod.SIGINT),
        signal_mod.getsignal(signal_mod.SIGTERM),
    )
    assert after == before, "run left its own SIGINT/SIGTERM handlers installed"


def test_lone_optional_node_failure_keeps_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """A required:false node that failed is an expected outcome, not an internal error.

    manager.py builds ``connections`` from successful nodes only
    (``manager.py:99-107``), so a lone optional node that never came up yields
    a *success* envelope with ``connections == {}`` and a warning.
    ``inject_scalars`` is True because it is decided from the one *input* node,
    so ``render_env`` raises ``MultiNodeEnvUnsupported`` inside the post-spawn
    window. That is a TunstrapError with its own mapping
    (``exceptions.py:71-78`` -> 1); routing it through the generic guard would
    report an expected result as "unexpected failure during run" with exit 4.
    """
    payload = {
        "connections": {},
        "pid": 99,
        "session_dir": "/s",
        "started_at": "now",
        "warnings": [{"node": "edge", "error": "optional node refused the forward"}],
    }
    monkeypatch.setattr(
        cli_mod,
        "spawn_daemon",
        lambda _s, session_dir=None: {"kind": "success", "payload": payload},
    )
    monkeypatch.setattr(cli_mod.subprocess, "Popen", QuietPopen)
    monkeypatch.setenv(
        "TUNSTRAP_INPUT",
        json.dumps(
            {
                "nodes": {
                    "edge": {
                        "host": "h.example.net",
                        "user": "u",
                        "ssh_password": "p",
                        "required": False,
                        "remote_targets": {"db": "127.0.0.1:5432"},
                    }
                }
            }
        ),
    )
    result = CliRunner().invoke(main, ["run", "--input-env", "TUNSTRAP_INPUT", "--", "true"])
    assert result.exit_code == 1, result.stderr
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    error = json.loads(result.stderr)
    assert error["error"] == "MultiNodeEnvUnsupported"
    assert len(teardowns) == 1, "the daemon must still be stopped"
