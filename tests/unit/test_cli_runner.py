"""CLI runner unit tests.

Validates: tunstrap/cli.py command surface (start/stop/status)
including exit codes, JSON output, and error paths.
Code: tunstrap/cli.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tunstrap import cli as cli_mod
from tunstrap.cli import main

pytestmark = pytest.mark.unit


def _patch_spawn_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {},
                "pid": 4242,
                "started_at": "2026-05-20T00:00:00Z",
                "warnings": [],
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn_daemon)


def test_start_success_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy-path start returns 0 and prints success JSON."""
    _patch_spawn_success(monkeypatch)
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["pid"] == 4242


def test_start_required_failure_returns_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """RequiredTunnelFailure is surfaced via exit code 2."""

    def fake_spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        return {
            "kind": "required_failure",
            "payload": {
                "error": "RequiredTunnelFailure",
                "message": "required tunnel(s) failed to start",
                "details": {"failed": [{"node": "a", "error": "boom"}]},
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn_daemon)
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 2
    out = json.loads(result.output)
    assert out["error"] == "RequiredTunnelFailure"


def test_start_daemon_error_returns_four(monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon_error IPC kind surfaces via exit code 4."""

    def fake_spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        return {
            "kind": "daemon_error",
            "payload": {
                "error": "DaemonError",
                "message": "worker failed",
                "details": {},
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn_daemon)
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 4
    out = json.loads(result.output)
    assert out["error"] == "DaemonError"


def test_status_alive_by_session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """status --session-dir reads the recorded pid and verifies via verify_session."""
    from tunstrap.identity import IdentityCheckResult

    data = tmp_path / "tunnel-data"
    data.mkdir()
    (data / "daemon.pid").write_text(f"{os.getpid()}\n")

    captured: dict[str, object] = {"session_dir": None, "pid": None}

    def fake_verify(session_dir: str, pid: int) -> object:
        captured["session_dir"] = session_dir
        captured["pid"] = pid
        return IdentityCheckResult.match

    monkeypatch.setattr(cli_mod, "verify_session", fake_verify)
    result = CliRunner().invoke(cli_mod.main, ["status", "--session-dir", str(tmp_path)])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out == {"alive": True}
    assert captured["session_dir"] == str(tmp_path)
    assert captured["pid"] == os.getpid()


def test_status_unknown_session_dir_reports_not_alive(tmp_path: Path) -> None:
    """status against a session dir with no recorded pid returns alive=false."""
    missing = tmp_path / "no-such-session"
    result = CliRunner().invoke(main, ["status", "--session-dir", str(missing)])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out == {"alive": False}


def test_stop_session_error_reports_and_exits_zero(tmp_path: Path) -> None:
    """stop --session-dir <nonexistent> returns structured JSON + exit 0."""
    from tunstrap.cli import main as cli_main

    runner = CliRunner()
    missing = tmp_path / "no-such-session"
    result = runner.invoke(cli_main, ["stop", "--session-dir", str(missing)])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["stopped"] is False
    assert (
        "cannot read identity" in payload["reason"].lower()
        or "no such" in payload["reason"].lower()
    )


def test_stop_removes_tunnel_data_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop removes <session-dir>/tunnel-data after a successful match path."""
    import tunstrap.cli as cli_mod
    from tunstrap.identity import IdentityCheckResult

    sd = tmp_path / "session"
    data = sd / "tunnel-data"
    data.mkdir(parents=True)
    (data / "daemon.pid").write_text(f"{os.getpid()}\n")

    def fake_verify(_session_dir: str, _pid: int) -> object:
        return IdentityCheckResult.match

    call_count = {"n": 0}

    def fake_kill(_pid: int, sig: int) -> None:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise ProcessLookupError

    monkeypatch.setattr(cli_mod, "verify_session", fake_verify)
    monkeypatch.setattr(os, "kill", fake_kill)

    runner = CliRunner()
    result = runner.invoke(cli_mod.main, ["stop", "--session-dir", str(sd)])
    assert result.exit_code == 0
    assert not data.exists(), f"tunnel-data should be removed; result={result.output!r}"


def _make_session_dir(pid: int) -> str:
    """Create a temp session dir with a daemon.pid file under tunnel-data/."""
    sd = tempfile.mkdtemp()
    data = Path(sd) / "tunnel-data"
    data.mkdir()
    (data / "daemon.pid").write_text(f"{pid}\n")
    return sd


def test_stop_unknown_pid_reports_not_found() -> None:
    """stop with a session dir pointing at a non-existent PID reports stopped=False."""
    sd = _make_session_dir(99999999)
    result = CliRunner().invoke(main, ["stop", "--session-dir", sd])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out == {"stopped": False, "reason": "not found"}


def test_stop_identity_mismatch_reports_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop where the live holder's pid differs reports an identity mismatch."""
    from tunstrap.identity import IdentityCheckResult

    monkeypatch.setattr(
        cli_mod,
        "verify_session",
        lambda session_dir, pid: IdentityCheckResult.mismatch,
    )
    sd = _make_session_dir(12345)
    result = CliRunner().invoke(main, ["stop", "--session-dir", sd])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out == {"stopped": False, "reason": "identity mismatch"}


def test_stop_identity_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop reports unavailable identity (e.g., /proc not readable)."""
    from tunstrap.identity import IdentityCheckResult

    monkeypatch.setattr(
        cli_mod,
        "verify_session",
        lambda session_dir, pid: IdentityCheckResult.unavailable,
    )
    sd = _make_session_dir(12345)
    result = CliRunner().invoke(main, ["stop", "--session-dir", sd])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out == {"stopped": False, "reason": "identity check unavailable"}


def test_start_invalid_json_returns_one() -> None:
    """start with non-JSON stdin reports SchemaValidationError (exit 1)."""
    result = CliRunner().invoke(main, ["start"], input="not-json-at-all")
    assert result.exit_code == 1
    out = json.loads(result.output)
    assert out["error"] == "SchemaValidationError"


def test_start_schema_violation_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """start with a JSON object that fails InputSchema returns exit 1."""
    # Node without ssh_pkey/ssh_password triggers the cross-field validator.
    # Ensure SSH_AUTH_SOCK is absent so the schema correctly rejects the node.
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    payload = json.dumps(
        {"nodes": {"a": {"host": "h", "user": "u", "remote_targets": {"p": "127.0.0.1:22"}}}}
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 1
    out = json.loads(result.output)
    assert out["error"] == "SchemaValidationError"


def test_start_unexpected_exception_returns_four(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unexpected exception in spawn_daemon is wrapped in DaemonError (exit 4)."""

    def boom(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_mod, "spawn_daemon", boom)
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 4
    out = json.loads(result.output)
    assert out["error"] == "DaemonError"


# ---------------------------------------------------------------------------
# Task B3: flag mode, conflict validation, --output env
# ---------------------------------------------------------------------------


def test_start_flag_mode_builds_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag mode: USER@HOST + --target builds the correct single-node InputSchema."""
    captured: dict[str, Any] = {}

    def fake_spawn(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        captured["schema"] = schema
        return {
            "kind": "success",
            "payload": {
                "connections": {},
                "pid": 1,
                "session_dir": "/s",
                "started_at": "now",
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    # --ssh-password-stdin reads the password from stdin (first line)
    res = CliRunner().invoke(
        main,
        ["start", "root@h:22", "--target", "db=127.0.0.1:5432", "--ssh-password-stdin"],
        input="secret\n",
    )
    assert res.exit_code == 0, res.output
    assert captured["schema"].nodes["node"].user == "root"


def test_start_flag_model_validation_does_not_print_ssh_key(tmp_path: Path) -> None:
    """Flag-mode node validation must not expose the key read from --ssh-key."""
    secret = "FLAG-MODE-PRIVATE-KEY"
    key_file = tmp_path / "id_key"
    key_file.write_text(secret)

    result = CliRunner().invoke(main, ["start", "root@h", "--ssh-key", str(key_file)])

    assert result.exit_code == 1
    assert secret not in result.output
    payload = json.loads(result.output)
    error = payload["details"]["errors"][0]
    assert error["loc"] == ["nodes", "node"]
    assert "node must define at least one" in error["msg"]


def test_start_rejects_trailing_command() -> None:
    """start + trailing -- CMD is rejected (exit 64); output mentions 'run'."""
    res = CliRunner().invoke(main, ["start", "root@h", "--", "helm", "list"])
    assert res.exit_code == 64
    assert "run" in res.output.lower()


def test_start_connection_plus_stdin_rejected() -> None:
    """Providing a connection arg AND non-empty stdin is rejected (exit 64)."""
    res = CliRunner().invoke(
        main,
        ["start", "root@h", "--target", "a=192.0.2.1:1"],
        input='{"nodes":{}}',
    )
    assert res.exit_code == 64


def test_start_conn_flag_without_connection_rejected() -> None:
    """Conn flags without a connection argument are rejected (exit 64)."""
    res = CliRunner().invoke(main, ["start", "--target", "a=192.0.2.1:1"])
    assert res.exit_code == 64


def test_start_output_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """--output env prints export lines including TUNSTRAP_DB_PORT."""

    def fake_spawn(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {
                    "h": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}}
                },
                "pid": 7,
                "session_dir": "/s",
                "started_at": "now",
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    res = CliRunner().invoke(
        main,
        [
            "start",
            "u@h",
            "--target",
            "db=127.0.0.1:5432",
            "--output",
            "env",
            "--ssh-password-stdin",
        ],
        input="secret\n",
    )
    assert res.exit_code == 0, res.output
    assert "export TUNSTRAP_DB_PORT='5432'" in res.output
