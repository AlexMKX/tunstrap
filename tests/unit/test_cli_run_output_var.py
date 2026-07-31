"""`--output-var`: name validation, collision rejection, and injection.

Validates: NAME must be a valid env-var name and must not collide with a key
run already injects; the child receives the complete OutputSchema as JSON
under NAME; multi-node results get NAME and no TUNSTRAP_* scalars.
Code: tunstrap/cli.py (_validate_output_var, _build_child_env)
Assertion: exit codes and messages for the rejections; the child env contents
for the injections.
Method: CliRunner with spawn_daemon, subprocess.Popen and _teardown_run
monkeypatched; payloads supplied with monkeypatch.setenv.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap.cli import main
from tunstrap.exceptions import DaemonError

pytestmark = pytest.mark.unit

VAR = "TUNSTRAP_INPUT"


def _node(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "host": "h.example.net",
        "user": "u",
        "ssh_password": "p",
        "remote_targets": {"db": "127.0.0.1:5432"},
    }
    base.update(overrides)
    return base


def _payload(nodes: dict[str, Any] | None = None) -> str:
    return json.dumps({"nodes": nodes if nodes is not None else {"node": _node()}})


def _conn(**ports: int) -> dict[str, Any]:
    return {"ports": dict(ports), "fetch_files": {}, "kube_targets": {}}


def _success(connections: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "success",
        "payload": {
            "connections": connections,
            "pid": 99,
            "session_dir": "/s",
            "started_at": "2026-07-31T00:00:00Z",
        },
    }


class FakePopen:
    last_env: dict[str, str] | None = None

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        FakePopen.last_env = env
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        """Accept forwarded signals; the fake child ignores them."""


@pytest.fixture(name="spawn")
def _spawn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []

    def _install(message: dict[str, Any]) -> None:
        def _spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
            seen.append(schema)
            return message

        monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_daemon)

    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    FakePopen.last_env = None
    seen.append(_install)  # seen[0] is the installer; schemas follow
    return seen


def test_collision_with_injected_scalar_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """--output-var TUNSTRAP_DB_PORT would overwrite a key run injects."""
    spawn[0](_success({"node": _conn(db=5432)}))
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main,
        ["run", "--input-env", VAR, "--output-var", "TUNSTRAP_DB_PORT", "--", "true"],
    )
    assert result.exit_code == 64
    assert "TUNSTRAP_DB_PORT" in result.output
    assert len(spawn) == 1, "a usage error must not spawn a daemon"


def test_collision_with_kubeconfig_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """KUBECONFIG is injected whenever the node has a kube target, so it collides."""
    spawn[0](_success({"node": _conn()}))
    monkeypatch.setenv(
        VAR,
        _payload({"node": _node(kube_targets={"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}})}),
    )
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "KUBECONFIG", "--", "true"]
    )
    assert result.exit_code == 64
    assert "KUBECONFIG" in result.output
    assert len(spawn) == 1, "a usage error must not spawn a daemon"


def test_non_colliding_tunstrap_prefixed_name_is_accepted(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """Only keys run actually injects are protected, not the whole TUNSTRAP_ prefix."""
    spawn[0](_success({"node": _conn(db=5432)}))
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main,
        ["run", "--input-env", VAR, "--output-var", "TUNSTRAP_WEB_PORT", "--", "true"],
    )
    assert result.exit_code == 0, result.stderr
    assert len(spawn) == 2, "a legal name must reach spawn_daemon"


def test_multi_node_without_output_var_is_exit_1_pre_spawn(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """Multi-node input rejects pre-spawn even if optional node b has no output."""
    spawn[0](_success({"a": _conn(db=5432)}))
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node(required=False)}))
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 1
    assert result.stdout == "", f"run leaked to stdout: {result.stdout!r}"
    error = json.loads(result.stderr)
    assert error["error"] == "MultiNodeEnvUnsupported"
    assert error["details"]["nodes"] == ["a", "b"]
    assert len(spawn) == 1, "the multi-node rejection must happen before spawn_daemon"


def test_multi_node_with_output_var_reaches_spawn(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """--output-var is the node-keyed channel, so multi-node input passes the gate.

    spawn_daemon is made to fail immediately so this asserts only that the
    gate let the run through — the child-env half of multi-node behaviour is
    Task 3.6's, and until it lands render_env would still reject a two-node
    envelope post-spawn.
    """

    def _spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        spawn.append(schema)
        raise DaemonError("captured; stop before the child runs", {})

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_daemon)
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node()}))
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "TF_VAR_t", "--", "true"]
    )
    assert result.exit_code == 4, result.stderr
    assert len(spawn) == 2, "the gate must let a multi-node run with --output-var through"


def test_single_node_without_output_var_still_works(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """The gate is about node count only; single-node flagless runs are untouched."""
    spawn[0](_success({"node": _conn(db=5432)}))
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert len(spawn) == 2
