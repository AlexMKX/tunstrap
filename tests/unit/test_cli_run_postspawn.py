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

import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap import session as session_mod
from tunstrap.cli import main
from tunstrap.exceptions import DaemonError
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
    monkeypatch: pytest.MonkeyPatch, spawned: None
) -> None:
    """A non-stopped outcome is reported on stderr and stdout stays clean."""
    monkeypatch.setattr(
        cli_mod,
        "stop_session",
        lambda _sd, _pid, _g, force: StopOutcome(False, "identity mismatch"),
    )
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
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
    monkeypatch: pytest.MonkeyPatch, spawned: None
) -> None:
    """A raising stop primitive is reported on stderr; the child's 7 still wins."""

    def _boom(_sd: str, _pid: int, _g: int, force: bool) -> StopOutcome:
        raise RuntimeError("stop exploded")

    monkeypatch.setattr(cli_mod, "stop_session", _boom)
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7, "teardown failure must never override the child code"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert "stop exploded" in result.stderr
    assert not any(Path(p).exists() for p in Path(tempfile.gettempdir()).glob("tunstrap-run-*")), (
        "a raising stop primitive leaked the minted session root"
    )


def test_teardown_permission_error_does_not_change_exit_code(
    monkeypatch: pytest.MonkeyPatch, spawned: None
) -> None:
    """A recycled pid permission failure is reported; the child's 7 still wins."""
    monkeypatch.setattr(session_mod, "verify_session", lambda _sd, _pid: IdentityCheckResult.match)

    def _permission_denied(_pid: int, _sig: int) -> None:
        raise PermissionError("recycled pid")

    monkeypatch.setattr(session_mod.os, "kill", _permission_denied)
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 7, "teardown failure must never override the child code"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert "PermissionError: recycled pid" in result.stderr


def test_teardown_keyboard_interrupt_does_not_change_exit_code(
    monkeypatch: pytest.MonkeyPatch, spawned: None
) -> None:
    """A KeyboardInterrupt from teardown does not override the child's exit code."""

    def _interrupted(_sd: str, _pid: int, _g: int, *, force: bool) -> StopOutcome:
        raise KeyboardInterrupt("second Ctrl-C")

    monkeypatch.setattr(cli_mod, "stop_session", _interrupted)
    monkeypatch.setattr(cli_mod.SessionDir, "cleanup_path", classmethod(lambda _cls, _sd: []))
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
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


def test_supplied_session_dir_is_never_minted_or_removed(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    """A caller-supplied --session-dir is passed through with minted_root=None."""
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
    assert supplied.is_dir(), "run must never remove a caller-supplied session dir"


def test_minted_root_is_discarded_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch, teardowns: list[tuple[Any, ...]]
) -> None:
    """A failed spawn leaves no empty minted directory behind."""
    seen: list[str | None] = []

    def _spawn(_schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        seen.append(session_dir)
        raise DaemonError("worker died", {})

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    result = CliRunner().invoke(main, _ARGS, input="secret\n")
    assert result.exit_code == 4
    assert seen[0] is not None
    assert not Path(seen[0]).exists(), "a failed spawn leaked a minted session root"
    assert teardowns == [], "no daemon exists, so there is nothing to tear down"


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
