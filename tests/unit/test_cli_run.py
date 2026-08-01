"""Unit tests for the ``run`` command (issue #5).

These tests patch ``cli_mod.subprocess.Popen`` (NOT ``subprocess.run``): the
implementation uses ``Popen(...).wait()`` so it can forward SIGINT/SIGTERM to
the child via ``proc.send_signal(...)``. The fake exposes the shape the code
relies on: ``.wait()``, ``.send_signal()`` and ``.returncode``.
"""

from __future__ import annotations

import signal as signal_mod
from typing import Any

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap.cli import main

pytestmark = pytest.mark.unit


def _success_payload() -> dict[str, Any]:
    return {
        "kind": "success",
        "payload": {
            "connections": {"h": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}}},
            "pid": 99,
            "session_dir": "/s",
            "started_at": "now",
        },
    }


class FakePopen:
    """Minimal stand-in for ``subprocess.Popen`` used by ``run``."""

    last_env: dict[str, str] | None = None
    last_cmd: list[str] | None = None
    signals: list[int] = []

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        FakePopen.last_cmd = cmd
        FakePopen.last_env = env
        FakePopen.signals = []
        self.returncode = 7

    def wait(self) -> int:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        FakePopen.signals.append(signum)


def test_run_injects_env_and_propagates_exit(monkeypatch):
    monkeypatch.setattr(
        cli_mod, "spawn_daemon", lambda schema, session_dir=None: _success_payload()
    )
    stops: list[tuple[str, int]] = []

    def _record(sd: str, gs: int, *, minted_root: str | None = None) -> None:
        stops.append((sd, gs))
        cleaning_teardown(sd, gs, minted_root=minted_root)

    monkeypatch.setattr(cli_mod, "_teardown_run", _record)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)

    res = CliRunner().invoke(
        main,
        [
            "run",
            "u@h",
            "--target",
            "db=127.0.0.1:5432",
            "--ssh-password-stdin",
            "--session-dir",
            "/x",
            "--",
            "echo",
            "hi",
        ],
        input="secret\n",
    )

    assert res.exit_code == 7
    assert FakePopen.last_cmd == ["echo", "hi"]
    assert FakePopen.last_env is not None
    assert FakePopen.last_env["TUNSTRAP_DB_PORT"] == "5432"
    # Child env is os.environ + render_env(output), not a bare dict.
    assert "PATH" in FakePopen.last_env
    assert stops == [("/x", 10)], "teardown must run with the session dir run owns"


class SignalledPopen(FakePopen):
    """Child killed by a signal: ``Popen.wait()`` reports that as ``-N``."""

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        super().__init__(cmd, env)
        self.returncode = -signal_mod.SIGTERM


def test_signalled_child_exits_with_the_shell_convention(monkeypatch):
    """A child killed by SIGTERM surfaces as 143, not the truncated 241.

    ``Popen.wait()`` returns ``-N`` for "killed by signal N", and ``sys.exit``
    hands that straight to the OS, which truncates it modulo 256 -- so SIGTERM
    arrived as 241 (verified: ``sys.exit(-15)`` yields returncode 241), a value
    no caller can interpret. 128+N is what every shell puts in ``$?`` and what
    a wrapper around tofu will compare against.

    Fails with ``-15`` if the normalisation in ``_run_child`` is removed. The
    OS truncation to 241 is stdlib behaviour and is not re-tested here; what
    this pins is the value tunstrap chooses to exit with.
    """
    monkeypatch.setattr(
        cli_mod, "spawn_daemon", lambda schema, session_dir=None: _success_payload()
    )
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", SignalledPopen)

    res = CliRunner().invoke(
        main,
        ["run", "u@h", "--target", "db=127.0.0.1:5432", "--ssh-password-stdin", "--", "sleep", "1"],
        input="secret\n",
    )
    assert res.exit_code == 128 + signal_mod.SIGTERM


def test_normal_child_exit_code_is_untouched(monkeypatch):
    """The negative control: a plain non-zero code is passed through as-is.

    Without this, normalising unconditionally (say ``abs(code)``) would pass
    the test above while corrupting every ordinary exit code.
    """
    monkeypatch.setattr(
        cli_mod, "spawn_daemon", lambda schema, session_dir=None: _success_payload()
    )
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)

    res = CliRunner().invoke(
        main,
        ["run", "u@h", "--target", "db=127.0.0.1:5432", "--ssh-password-stdin", "--", "true"],
        input="secret\n",
    )
    assert res.exit_code == 7


def test_run_requires_command():
    res = CliRunner().invoke(main, ["run", "u@h", "--target", "db=192.0.2.1:1"])
    assert res.exit_code == 64


def test_run_teardown_on_child_exception(monkeypatch):
    monkeypatch.setattr(
        cli_mod, "spawn_daemon", lambda schema, session_dir=None: _success_payload()
    )
    stops: list[str] = []

    def _record(sd: str, gs: int, *, minted_root: str | None = None) -> None:
        stops.append(sd)
        cleaning_teardown(sd, gs, minted_root=minted_root)

    monkeypatch.setattr(cli_mod, "_teardown_run", _record)

    def boom(cmd, env=None):
        raise OSError("no such binary")

    monkeypatch.setattr(cli_mod.subprocess, "Popen", boom)

    res = CliRunner().invoke(
        main,
        ["run", "u@h", "--target", "db=192.0.2.1:1", "--ssh-password-stdin", "--", "nope"],
        input="secret\n",
    )

    assert res.exit_code != 0
    assert stops, "teardown must run even when child fails to launch"


def test_run_session_active_exit3(monkeypatch):
    monkeypatch.setattr(
        cli_mod,
        "spawn_daemon",
        lambda schema, session_dir=None: {
            "kind": "session_active",
            "payload": {"error": "SessionActive"},
        },
    )
    res = CliRunner().invoke(
        main,
        [
            "run",
            "u@h",
            "--target",
            "db=192.0.2.1:1",
            "--ssh-password-stdin",
            "--session-dir",
            "/x",
            "--",
            "echo",
        ],
        input="secret\n",
    )
    assert res.exit_code == 3


def test_run_forwards_signals(monkeypatch):
    """The signal handler installed by ``run`` forwards to the child Popen."""
    import signal as signal_mod

    monkeypatch.setattr(
        cli_mod, "spawn_daemon", lambda schema, session_dir=None: _success_payload()
    )
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)

    captured: dict[str, Any] = {}

    class SignalCapturingPopen(FakePopen):
        def wait(self) -> int:
            # While the child is "running", the handler should be installed.
            captured["handler"] = signal_mod.getsignal(signal_mod.SIGTERM)
            return self.returncode

    monkeypatch.setattr(cli_mod.subprocess, "Popen", SignalCapturingPopen)

    res = CliRunner().invoke(
        main,
        [
            "run",
            "u@h",
            "--target",
            "db=127.0.0.1:5432",
            "--ssh-password-stdin",
            "--",
            "echo",
            "hi",
        ],
        input="secret\n",
    )
    assert res.exit_code == 7
    # Handler installed during child lifetime forwards to the child.
    handler = captured["handler"]
    assert callable(handler)
    handler(signal_mod.SIGTERM, None)
    assert signal_mod.SIGTERM in SignalCapturingPopen.signals


def test_run_rejects_output_option() -> None:
    """`run` has no --output; passing it is a usage error (exit 64), not leaked to child."""
    res = CliRunner().invoke(
        main,
        ["run", "u@h", "--target", "db=127.0.0.1:5432", "--output", "env", "--", "echo", "hi"],
    )
    assert res.exit_code == 64
    assert "no such option" in res.output.lower()


def test_run_preserves_child_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens after `--` (including child's own flags) reach the child verbatim."""
    monkeypatch.setattr(
        cli_mod, "spawn_daemon", lambda schema, session_dir=None: _success_payload()
    )
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)
    res = CliRunner().invoke(
        main,
        [
            "run",
            "u@h",
            "--target",
            "db=127.0.0.1:5432",
            "--ssh-password-stdin",
            "--",
            "helm",
            "list",
            "--all",
        ],
        input="secret\n",
    )
    assert res.exit_code == 7  # FakePopen.returncode
    assert FakePopen.last_cmd == ["helm", "list", "--all"]
