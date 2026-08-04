"""`run`'s argument surface: one variadic, split after parsing.

Validates: the exact documented shim invocation binds no connection and the
whole child command; flag mode is unchanged; option-looking child arguments
after `--` are never absorbed by tunstrap; every unusable arity is exit 64.
Code: tunstrap/cli.py (_split_run_args, run_command)
Assertion: the command handed to Popen, the schema handed to spawn_daemon,
and the exit codes/messages for the bad arities.
Method: CliRunner with spawn_daemon, subprocess.Popen and _teardown_run
monkeypatched; --input-env payloads supplied with monkeypatch.setenv. No
daemon, no docker: runs unchanged on macOS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap.cli import main

pytestmark = pytest.mark.unit

VAR = "TUNSTRAP_INPUT"

_NODE = {
    "host": "h.example.net",
    "user": "u",
    "ssh_password": "p",
    "remote_targets": {"db": "127.0.0.1:5432"},
}


def _payload(**daemon: Any) -> str:
    body: dict[str, Any] = {"nodes": {"node": _NODE}}
    if daemon:
        body["daemon"] = daemon
    return json.dumps(body)


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


class FakePopen:
    """Popen stand-in recording the command and env it was handed."""

    last_cmd: list[str] | None = None
    last_env: dict[str, str] | None = None

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        FakePopen.last_cmd = cmd
        FakePopen.last_env = env
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        """Accept forwarded signals; the fake child ignores them."""


@pytest.fixture(name="captured")
def _captured(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture the schema handed to spawn_daemon; stub out child + teardown."""
    seen: list[Any] = []

    def _spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        seen.append(schema)
        return _success_payload()

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    FakePopen.last_cmd = None
    FakePopen.last_env = None
    return seen


def test_exact_shim_invocation(monkeypatch: pytest.MonkeyPatch, captured: list[Any]) -> None:
    """The documented shim invocation, verbatim, binds no connection."""
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main,
        [
            "run",
            "--input-env",
            "TUNSTRAP_INPUT",
            "--output-var",
            "TF_VAR_tunstrap",
            "--",
            "tofu",
            "plan",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_cmd == ["tofu", "plan"]
    assert sorted(captured[0].nodes) == ["node"]
    assert captured[0].nodes["node"].host == "h.example.net"


def test_flag_mode_unchanged(tmp_path: Path, captured: list[Any]) -> None:
    """Flag mode still binds CONNECTION, the flags, and the child command."""
    key = tmp_path / "id"
    key.write_text("KEYMATERIAL\n")
    result = CliRunner().invoke(
        main,
        [
            "run",
            "user@host",
            "--ssh-key",
            str(key),
            "--target",
            "web=127.0.0.1:80",
            "--",
            "helm",
            "list",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_cmd == ["helm", "list"]
    node = captured[0].nodes["node"]
    assert (node.user, node.host, node.port) == ("user", "host", 22)
    assert sorted(node.remote_targets) == ["web"]
    assert node.ssh_pkey == "KEYMATERIAL\n"


def test_child_dash_arguments_survive(monkeypatch: pytest.MonkeyPatch, captured: list[Any]) -> None:
    """Every `-`-prefixed child token after `--` reaches the child verbatim."""
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main,
        ["run", "--input-env", VAR, "--", "tofu", "plan", "-out=x", "-var", "a=b"],
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_cmd == ["tofu", "plan", "-out=x", "-var", "a=b"]


def test_child_may_use_tunstraps_own_flag_names(
    monkeypatch: pytest.MonkeyPatch, captured: list[Any]
) -> None:
    """A child flag spelled like a tunstrap flag is the child's, not tunstrap's."""
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--", "env", "--ssh-key", "sneaky"]
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_cmd == ["env", "--ssh-key", "sneaky"]
    assert captured[0].nodes["node"].ssh_pkey is None, "tunstrap absorbed --ssh-key"


def test_doubled_separator_is_stripped(tmp_path: Path, captured: list[Any]) -> None:
    """Click consumes only the first `--`; the second is stripped by run."""
    key = tmp_path / "id"
    key.write_text("K\n")
    result = CliRunner().invoke(
        main,
        [
            "run",
            "user@host",
            "--ssh-key",
            str(key),
            "--target",
            "w=127.0.0.1:80",
            "--",
            "--",
            "helm",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_cmd == ["helm"]


def test_missing_separator_before_dash_argument_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, captured: list[Any]
) -> None:
    """Without `--`, a `-`-prefixed child argument is parsed as a tunstrap option."""
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "tofu", "-version"])
    assert result.exit_code == 64
    assert "no such option" in result.output.lower()
    assert captured == [], "usage error must not spawn a daemon"


def test_input_env_without_command_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, captured: list[Any]
) -> None:
    """--input-env with no command is exit 64 and spawns nothing."""
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(main, ["run", "--input-env", VAR])
    assert result.exit_code == 64
    assert "run requires a command" in result.output
    assert captured == [], "usage error must not spawn a daemon"


def test_connection_without_command_is_usage_error(captured: list[Any]) -> None:
    """A connection with no command is exit 64 and spawns nothing."""
    result = CliRunner().invoke(main, ["run", "user@host"])
    assert result.exit_code == 64
    assert "run requires a command" in result.output
    assert captured == [], "usage error must not spawn a daemon"


def test_no_arguments_at_all_is_usage_error(captured: list[Any]) -> None:
    """Bare `run` names both input channels instead of Click's 'Missing argument'."""
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 64
    assert "run requires USER@HOST[:PORT] or --input-env VAR" in result.output
    assert captured == [], "usage error must not spawn a daemon"


def test_run_forces_materialize_on_env_payload(
    monkeypatch: pytest.MonkeyPatch, captured: list[Any]
) -> None:
    """A payload saying materialize=false still reaches spawn_daemon as True."""
    monkeypatch.setenv(VAR, _payload(materialize=False))
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert captured[0].daemon.materialize is True


@pytest.mark.parametrize("name", ["1BAD", "has-dash", "has space", "", "a.b"])
def test_invalid_output_var_name_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, captured: list[Any], name: str
) -> None:
    """--output-var must be a valid environment-variable name."""
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", name, "--", "true"]
    )
    assert result.exit_code == 64
    assert "--output-var" in result.output
    assert captured == [], "usage error must not spawn a daemon"


def test_input_env_unset_is_exit_1_before_spawn(
    monkeypatch: pytest.MonkeyPatch, captured: list[Any]
) -> None:
    """An unset payload variable is a typed exit-1 error on stderr, pre-spawn."""
    monkeypatch.delenv(VAR, raising=False)
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"] == "SchemaValidationError"
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    assert captured == [], "a bad payload must not spawn a daemon"
