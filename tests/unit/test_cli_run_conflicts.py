"""`run`'s conflict matrix under --input-env.

Validates: every flag --input-env makes redundant is a usage error (64), and
none of them can leak a daemon — each rejection is proven to happen before
spawn_daemon is reached.
Code: tunstrap/cli.py (_reject_flags_under_input_env)
Assertion: exit code 64, a message naming the offending flag, and an empty
spawn-call log.
Method: CliRunner with spawn_daemon monkeypatched to record and fail loudly.
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
from tunstrap.exceptions import DaemonError

pytestmark = pytest.mark.unit

VAR = "TUNSTRAP_INPUT"

_PAYLOAD = json.dumps(
    {
        "nodes": {
            "node": {
                "host": "h.example.net",
                "user": "u",
                "ssh_password": "p",
                "remote_targets": {"db": "127.0.0.1:5432"},
            }
        }
    }
)


@pytest.fixture(name="spawns")
def _spawns(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every spawn_daemon call. The list must stay empty in this module."""
    calls: list[Any] = []

    def _spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        calls.append(schema)
        raise AssertionError("spawn_daemon must not be reached by a usage error")

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    monkeypatch.setenv(VAR, _PAYLOAD)
    return calls


@pytest.mark.parametrize(
    "extra",
    [
        ["--ssh-key-passphrase", "x"],
        ["--ssh-password-stdin"],
        ["--target", "web=127.0.0.1:80"],
        ["--kube", "k3s=/etc/k3s.yaml"],
        ["--fetch", "f=/etc/hosts"],
    ],
)
def test_connection_flags_rejected(spawns: list[Any], extra: list[str]) -> None:
    """Connection flags are redundant under --input-env and are exit 64."""
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, *extra, "--", "true"], input="pw\n"
    )
    assert result.exit_code == 64
    assert "connection flags are redundant" in result.output
    assert spawns == []


def test_ssh_key_flag_rejected(tmp_path: Path, spawns: list[Any]) -> None:
    """--ssh-key is rejected before its file is even read."""
    key = tmp_path / "id"
    key.write_text("K\n")
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--ssh-key", str(key), "--", "true"]
    )
    assert result.exit_code == 64
    assert "connection flags are redundant" in result.output
    assert spawns == []


@pytest.mark.parametrize(
    "extra, needle",
    [
        (["--auto-stop-idle-seconds", "30"], "daemon.auto_stop_idle_seconds"),
        (["--grace-seconds", "30"], "daemon.shutdown_grace_seconds"),
        (["--log-file", "/tmp/t.log"], "daemon.log_file"),
        (["--materialize"], "always materializes"),
    ],
)
def test_daemon_flags_rejected(spawns: list[Any], extra: list[str], needle: str) -> None:
    """Daemon flags are usage errors under --input-env, not overrides or no-ops."""
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, *extra, "--", "true"])
    assert result.exit_code == 64
    assert needle in result.output
    assert spawns == []


def test_daemon_flags_still_work_in_flag_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rejection is scoped to --input-env; flag mode still honours them."""
    seen: list[Any] = []

    def _spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        seen.append(schema)
        # A TunstrapError, not SystemExit: it takes run's own pre-spawn error
        # path, which from Task 4.1 on also discards the minted session root.
        raise DaemonError("captured; stop here", {})

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    key = tmp_path / "id"
    key.write_text("K\n")
    CliRunner().invoke(
        main,
        [
            "run",
            "user@host",
            "--ssh-key",
            str(key),
            "--target",
            "web=127.0.0.1:80",
            "--auto-stop-idle-seconds",
            "30",
            "--log-file",
            "/tmp/t.log",
            "--materialize",
            "--",
            "true",
        ],
    )
    assert len(seen) == 1
    assert seen[0].daemon.auto_stop_idle_seconds == 30
    assert seen[0].daemon.log_file == "/tmp/t.log"
    assert seen[0].daemon.materialize is True


@pytest.mark.parametrize(
    "extra, stdin, expected_passphrase, expected_fetch_path",
    [
        (["--ssh-key-passphrase", "x"], None, "x", None),
        (["--ssh-password-stdin"], "pw\n", None, None),
        (["--target", "web=127.0.0.1:80"], None, None, None),
        (["--kube", "k3s=/etc/k3s.yaml"], None, None, None),
        (["--fetch", "f=/etc/hosts"], None, None, "/etc/hosts"),
    ],
)
def test_connection_flags_still_work_in_flag_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra: list[str],
    stdin: str | None,
    expected_passphrase: str | None,
    expected_fetch_path: str | None,
) -> None:
    """Each connection flag rejected for env input still reaches flag-mode spawn."""
    seen: list[Any] = []

    def _spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        seen.append(schema)
        raise DaemonError("captured; stop here", {})

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn)
    # The base command supplies auth via --ssh-key rather than relying on an
    # ambient SSH_AUTH_SOCK (an ssh-agent runs on a dev workstation but not on
    # a CI runner). Without explicit auth, InputSchema._validate_auth rejects a
    # keyless/passwordless node and `run` exits before spawn — which is the
    # product working as intended, not something this flag-mode test should
    # depend on. Mirrors test_daemon_flags_still_work_in_flag_mode below.
    key = tmp_path / "id"
    key.write_text("K\n")
    CliRunner().invoke(
        main,
        [
            "run",
            "user@host",
            "--ssh-key",
            str(key),
            "--target",
            "base=127.0.0.1:80",
            *extra,
            "--",
            "true",
        ],
        input=stdin,
    )
    assert len(seen) == 1
    node = seen[0].nodes["node"]
    if expected_passphrase is not None:
        assert node.ssh_pkey_passphrase == expected_passphrase
    if expected_fetch_path is not None:
        assert node.fetch_files["f"].path == expected_fetch_path
