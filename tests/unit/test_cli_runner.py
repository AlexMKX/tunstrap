"""CLI runner unit tests.

Validates: tunstrap/cli.py command surface (start/stop/status)
including exit codes, JSON output, and error paths.
Code: tunstrap/cli.py
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tunstrap import cli as cli_mod
from tunstrap import cli_stop as cli_stop_mod
from tunstrap.cli import main
from tunstrap.session import StopOutcome

pytestmark = pytest.mark.unit


def _patch_spawn_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_spawn_daemon(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {},
                "pid": 4242,
                "session_dir": "/tmp/session",
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

    def fake_spawn_daemon(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
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

    def fake_spawn_daemon(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
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

    monkeypatch.setattr(cli_stop_mod, "verify_session", fake_verify)
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


def test_stop_session_error_reports_and_exits_one(tmp_path: Path) -> None:
    """stop --session-dir <nonexistent> returns structured JSON + exit 1.

    Reads ``result.stdout``, not ``result.output``: click 8.4's CliRunner
    interleaves stderr into ``.output``, so once this outcome grew its
    stderr preservation notice, decoding ``.output`` as JSON broke. The
    envelope has always been a stdout-only contract.
    """
    from tunstrap.cli import main as cli_main

    runner = CliRunner()
    missing = tmp_path / "no-such-session"
    result = runner.invoke(cli_main, ["stop", "--session-dir", str(missing)])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["stopped"] is False
    assert (
        "cannot read identity" in payload["reason"].lower()
        or "no such" in payload["reason"].lower()
    )


def test_stop_removes_tunnel_data_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful stop is rendered and removes <session-dir>/tunnel-data."""
    import tunstrap.cli as cli_mod

    sd = tmp_path / "session"
    data = sd / "tunnel-data"
    data.mkdir(parents=True)
    (data / "daemon.pid").write_text(f"{os.getpid()}\n")

    calls: list[tuple[str, int, int, bool]] = []

    def _stop_session(
        session_dir: str, pid: int, grace_seconds: int, *, force: bool
    ) -> StopOutcome:
        calls.append((session_dir, pid, grace_seconds, force))
        return StopOutcome(True)

    monkeypatch.setattr(cli_stop_mod, "stop_session", _stop_session)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.main,
        ["stop", "--session-dir", str(sd), "--grace-seconds", "17"],
    )
    assert result.exit_code == 0
    assert result.stdout == '{"stopped": true}\n'
    assert calls == [(str(sd), os.getpid(), 17, True)]
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

    def boom(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
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

    def fake_spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
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


def test_start_output_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--output env prints the three survivors plus the kube channel, materializing output.json."""
    payload_session_dir = tmp_path / "s"
    payload_session_dir.mkdir()

    def fake_spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {
                    "h": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}}
                },
                "pid": 7,
                "session_dir": str(payload_session_dir),
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
    assert f"export TUNSTRAP_SESSION_DIR='{payload_session_dir}'" in res.output
    assert "export TUNSTRAP_PID='7'" in res.output
    materialized = payload_session_dir / "tunnel-data" / "output.json"
    assert f"export TUNSTRAP_OUTPUT_FILE='{materialized}'" in res.output
    assert "TUNSTRAP_DB_PORT" not in res.output
    assert "KUBECONFIG" not in res.output, "no kube_targets in this payload"
    assert materialized.exists()
    assert stat.S_IMODE(materialized.stat().st_mode) == 0o600


def test_start_json_materialized_kubeconfig_never_prints_credential_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Materialized start JSON exposes only the kube reference, never its content."""
    payload_session_dir = tmp_path / "s"
    payload_session_dir.mkdir()
    kube_path = payload_session_dir / "tunnel-data" / "kube-node-k3s"

    def fake_spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {
                    "node": {
                        "ports": {},
                        "fetch_files": {},
                        "kube_targets": {
                            "k3s": {
                                "cluster_name": "cluster",
                                "context_name": "context",
                                "local_port": 7000,
                                "endpoint": "https://127.0.0.1:7000",
                                "tls_server_name": "tls-name",
                                "certificate_authority_data": "CA-MARKER",
                                "client_certificate_data": "CERTIFICATE-MARKER",
                                "client_key_data": "PRIVATE-KEY-MARKER",
                                "content_b64": "FULL-KUBECONFIG-MARKER",
                                "path": str(kube_path),
                            }
                        },
                    }
                },
                "pid": 7,
                "session_dir": str(payload_session_dir),
                "started_at": "now",
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    # No ssh_pkey/ssh_password supplied: independent of ambient ssh-agent state.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")

    res = CliRunner().invoke(main, ["start", "u@h", "--target", "db=127.0.0.1:5432"])

    assert res.exit_code == 0, res.output
    for secret in (
        "CA-MARKER",
        "CERTIFICATE-MARKER",
        "PRIVATE-KEY-MARKER",
        "FULL-KUBECONFIG-MARKER",
    ):
        assert secret not in res.output
    target = json.loads(res.output)["connections"]["node"]["kube_targets"]["k3s"]
    assert target == {
        "path": str(kube_path),
        "context": "context",
        "endpoint": "https://127.0.0.1:7000",
    }


def test_start_json_unmaterialized_kubeconfig_keeps_stdout_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unmaterialized kubeconfig remains available through start JSON."""
    payload_session_dir = tmp_path / "s"
    payload_session_dir.mkdir()

    def fake_spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {
                    "node": {
                        "ports": {},
                        "fetch_files": {},
                        "kube_targets": {
                            "k3s": {
                                "cluster_name": "cluster",
                                "context_name": "context",
                                "local_port": 7000,
                                "endpoint": "https://127.0.0.1:7000",
                                "tls_server_name": "tls-name",
                                "certificate_authority_data": "CA-MARKER",
                                "client_certificate_data": "CERTIFICATE-MARKER",
                                "client_key_data": "PRIVATE-KEY-MARKER",
                                "content_b64": "FULL-KUBECONFIG-MARKER",
                                "path": None,
                            }
                        },
                    }
                },
                "pid": 7,
                "session_dir": str(payload_session_dir),
                "started_at": "now",
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    # No ssh_pkey/ssh_password supplied: independent of ambient ssh-agent state.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")

    res = CliRunner().invoke(main, ["start", "u@h", "--target", "db=127.0.0.1:5432"])

    assert res.exit_code == 0, res.output
    target = json.loads(res.output)["connections"]["node"]["kube_targets"]["k3s"]
    assert target["content_b64"] == "FULL-KUBECONFIG-MARKER"
    assert target["path"] is None


def test_start_json_materialized_fetch_file_never_prints_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A materialized fetched file exposes only its on-disk reference."""
    payload_session_dir = tmp_path / "s"
    payload_session_dir.mkdir()
    fetch_path = payload_session_dir / "tunnel-data" / "fetch-node-kubeconfig"

    def fake_spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {
                    "node": {
                        "ports": {},
                        "fetch_files": {
                            "kubeconfig": {
                                "content_b64": "FETCHED-SECRET-MARKER",
                                "path": str(fetch_path),
                                "size": 21,
                                "sha256": "a" * 64,
                            }
                        },
                        "kube_targets": {},
                    }
                },
                "pid": 7,
                "session_dir": str(payload_session_dir),
                "started_at": "now",
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    # No ssh_pkey/ssh_password supplied: independent of ambient ssh-agent state.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")

    res = CliRunner().invoke(main, ["start", "u@h", "--target", "db=127.0.0.1:5432"])

    assert res.exit_code == 0, res.output
    assert "FETCHED-SECRET-MARKER" not in res.output
    fetched = json.loads(res.output)["connections"]["node"]["fetch_files"]["kubeconfig"]
    assert fetched == {"path": str(fetch_path), "size": 21, "sha256": "a" * 64}


def test_start_json_unmaterialized_fetch_file_keeps_stdout_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unmaterialized fetched file remains available through start JSON."""
    payload_session_dir = tmp_path / "s"
    payload_session_dir.mkdir()

    def fake_spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {
                    "node": {
                        "ports": {},
                        "fetch_files": {
                            "kubeconfig": {
                                "content_b64": "FETCHED-SECRET-MARKER",
                                "path": None,
                                "size": 21,
                                "sha256": "a" * 64,
                            }
                        },
                        "kube_targets": {},
                    }
                },
                "pid": 7,
                "session_dir": str(payload_session_dir),
                "started_at": "now",
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    # No ssh_pkey/ssh_password supplied: independent of ambient ssh-agent state.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")

    res = CliRunner().invoke(main, ["start", "u@h", "--target", "db=127.0.0.1:5432"])

    assert res.exit_code == 0, res.output
    fetched = json.loads(res.output)["connections"]["node"]["fetch_files"]["kubeconfig"]
    assert fetched["content_b64"] == "FETCHED-SECRET-MARKER"
    assert fetched["path"] is None


def test_stdin_payload_output_env_forces_materialize_for_kube_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stdin payload declaring materialize: false and kube_targets under
    --output env must not reach render_kube_env with an unmaterialized path
    (a bare ValueError, not a typed error) -- option (a): --output env forces
    daemon.materialize = True for the stdin channel too, matching flag mode's
    own force_materialize precedent."""
    captured: dict[str, Any] = {}
    payload_session_dir = tmp_path / "s"
    payload_session_dir.mkdir()

    def fake_spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        captured["schema"] = schema
        return {
            "kind": "success",
            "payload": {
                "connections": {
                    "h": {
                        "ports": {},
                        "fetch_files": {},
                        "kube_targets": {
                            "k3s": {
                                "cluster_name": "c",
                                "context_name": "ctx",
                                "local_port": 7000,
                                "endpoint": "https://127.0.0.1:7000",
                                "tls_server_name": "c",
                                "certificate_authority_data": "Y2E=",
                                "client_certificate_data": "Y2VydA==",
                                "client_key_data": "a2V5",
                                "content_b64": "a3ViZWNvbmZpZw==",
                                "path": str(payload_session_dir / "tunnel-data" / "k3s"),
                            }
                        },
                    }
                },
                "pid": 7,
                "session_dir": str(payload_session_dir),
                "started_at": "now",
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    stdin_payload = json.dumps(
        {
            "nodes": {
                "h": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                }
            },
            "daemon": {"materialize": False},
        }
    )
    res = CliRunner().invoke(main, ["start", "--output", "env"], input=stdin_payload)
    assert res.exit_code == 0, res.output
    forced_materialize = captured["schema"].daemon.materialize is True
    assert forced_materialize, "--output env must force materialize=True for a stdin payload too"
    assert f"export KUBECONFIG='{payload_session_dir / 'tunnel-data' / 'k3s'}'" in res.output


def test_start_post_spawn_render_failure_preserves_recovery_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A post-spawn output failure identifies the possibly-live daemon to its operator.

    The returned success payload has an unmaterialized kube target, which makes
    ``render_kube_env`` raise during ``start --output env``.  The worker is
    represented by this live test-process PID: the contract under test is that
    the error still gives the operator its session directory and a command that
    accepts that directory, rather than whether this test process is stopped.
    """
    session_path = str(tmp_path / "session")

    def fake_spawn(
        _schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {
                    "node": {
                        "ports": {},
                        "fetch_files": {},
                        "kube_targets": {
                            "k3s": {
                                "cluster_name": "c",
                                "context_name": "ctx",
                                "local_port": 7000,
                                "endpoint": "https://127.0.0.1:7000",
                                "tls_server_name": "c",
                                "certificate_authority_data": "Y2E=",
                                "client_certificate_data": "Y2VydA==",
                                "client_key_data": "a2V5",
                                "content_b64": "a3ViZWNvbmZpZw==",
                                "path": None,
                            }
                        },
                    }
                },
                "pid": os.getpid(),
                "session_dir": session_path,
                "started_at": "now",
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    # No ssh_pkey/ssh_password supplied: independent of ambient ssh-agent state.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")

    result = CliRunner().invoke(
        main,
        ["start", "u@h", "--target", "db=127.0.0.1:5432", "--output", "env"],
    )

    assert result.exit_code == 4
    error = json.loads(result.stdout)
    assert error["error"] == "DaemonError"
    assert error["details"]["type"] == "ValueError"
    assert error["details"]["session_dir"] == session_path
    assert error["details"]["pid"] == os.getpid()
    assert f"tunstrap stop --session-dir {session_path}" in result.stderr


def test_start_post_spawn_unusable_envelope_preserves_supplied_session_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed success reply still leaves a caller-supplied root recoverable.

    Deleting the fallback to ``session_dir`` in
    ``_report_start_post_spawn_failure`` makes this fail: the daemon's worker
    uses the supplied root verbatim, despite not providing a usable reply.
    """
    session_path = str(tmp_path / "session")

    def fake_spawn(
        _schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        return {"kind": "success", "payload": None}

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    # No ssh_pkey/ssh_password supplied: independent of ambient ssh-agent state.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")

    result = CliRunner().invoke(
        main,
        [
            "start",
            "u@h",
            "--target",
            "db=127.0.0.1:5432",
            "--session-dir",
            session_path,
        ],
    )

    assert result.exit_code == 4
    error = json.loads(
        next(line for line in result.output.splitlines() if line.startswith('{"error"'))
    )
    assert error["details"] == {"type": "ValidationError", "session_dir": session_path}
    assert f"tunstrap stop --session-dir {session_path}" in result.output


@pytest.mark.parametrize(
    "message",
    [
        {"kind": "daemon_error", "payload": {"session_dir": "/s", "pid": 7}},
        {"kind": "success", "payload": None},
        {"kind": "success", "payload": {"session_dir": 7, "pid": 7}},
        {"kind": "success", "payload": {"session_dir": "/s", "pid": True}},
        {"kind": "success", "payload": {"session_dir": "/s", "pid": 0}},
    ],
)
def test_start_recovery_handles_reject_unusable_envelopes(message: object) -> None:
    """Only a successful envelope with safe scalar handles gets recovery output."""
    assert cli_mod._start_recovery_handles(message) is None
