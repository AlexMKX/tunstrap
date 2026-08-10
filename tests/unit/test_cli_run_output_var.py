"""`--output-var`: name validation, collision rejection, and injection.

Validates: NAME must be a valid env-var name and must not collide with a key
run already injects; the child receives the unified output structure as JSON
under NAME, projected to drop the kube credentials (see
test_cli_run_output_var_projection.py); multi-node input succeeds unconditionally
now that materialization covers it, with or without --output-var.
Code: tunstrap/cli.py (_validate_output_var, _build_child_env)
Assertion: exit codes and messages for the rejections; the child env contents
for the injections.
Method: CliRunner with spawn_daemon, subprocess.Popen and _teardown_run
monkeypatched; payloads supplied with monkeypatch.setenv.
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
from tunstrap.schemas import OutputSchema

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


def _success(connections: dict[str, Any], *, session_dir: str) -> dict[str, Any]:
    return {
        "kind": "success",
        "payload": {
            "connections": connections,
            "pid": 99,
            "session_dir": session_dir,
            "started_at": "2026-07-31T00:00:00Z",
        },
    }


_RICH_KUBE: dict[str, Any] = {
    "cluster_name": "probe-cluster",
    "context_name": "probe-context",
    "local_port": 41111,
    "endpoint": "https://127.0.0.1:41111",
    "tls_server_name": "probe-control-plane",
    "certificate_authority_data": "Y2E=",
    "client_certificate_data": "Y2VydA==",
    "client_key_data": "a2V5",
    "content_b64": "a3ViZWNvbmZpZw==",
    "path": "/s/tunnel-data/node-k3s",
}


def _rich_payload(session_dir: str) -> dict[str, Any]:
    """Non-default in every field the projection drops: a real warning, a
    fetched file, and the seven kube_target fields beyond path/endpoint.

    ``fetch_files.hosts`` carries both ``content_b64`` (required by
    FetchedFile's success/error xor -- it stays internal plumbing, never
    deleted) and ``path`` (already materialized daemon-side by the time a
    success envelope reaches ``run``); the *decoded* --output-var value must
    carry only ``path``, never ``content_b64``.
    """
    return {
        "connections": {
            "node": {
                "ports": {"db": 5432},
                "fetch_files": {
                    "hosts": {
                        "content_b64": "aG9zdHM=",
                        "path": f"{session_dir}/tunnel-data/node-hosts",
                        "size": 6,
                        "sha256": "ab" * 32,
                    }
                },
                "kube_targets": {"k3s": _RICH_KUBE},
            }
        },
        "pid": 99,
        "session_dir": session_dir,
        "started_at": "2026-07-31T00:00:00Z",
        "warnings": [
            {"node": "edge", "error": "optional node refused the forward", "skipped": True}
        ],
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
        def _spawn_daemon(
            schema: Any, session_dir: str | None = None, *, input_env: str | None = None
        ) -> dict[str, Any]:
            seen.append(schema)
            return message

        monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_daemon)

    monkeypatch.setattr(cli_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    FakePopen.last_env = None
    seen.append(_install)  # seen[0] is the installer; schemas follow
    return seen


def test_collision_with_kubeconfig_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """KUBECONFIG is injected whenever the node has a kube target, so it collides."""
    spawn[0](_success({"node": _conn()}, session_dir=str(tmp_path)))
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


@pytest.mark.parametrize("name", ["KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"])
def test_collision_with_kube_names_is_usage_error_even_without_kube_targets(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path, name: str
) -> None:
    """``run`` scrubs the three kube names from the inherited environment
    *unconditionally* -- regardless of schema -- so they collide with
    ``--output-var`` even for a payload that declares zero kube targets
    (issue #23). Without this guard the scrubber would delete the operator's
    inherited value and the output-var assignment would write the unified JSON
    under it, silently clobbering it."""
    spawn[0](_success({"node": _conn(db=5432)}, session_dir=str(tmp_path)))
    monkeypatch.setenv(VAR, _payload())  # _node() declares no kube_targets
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", name, "--", "true"]
    )
    assert result.exit_code == 64
    assert name in result.output
    assert len(spawn) == 1, "a usage error must not spawn a daemon"


def test_tunstrap_prefixed_output_var_name_is_accepted(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """--output-var TUNSTRAP_ANYTHING is not rejected just for the prefix."""
    spawn[0](_success({"node": _conn(db=5432)}, session_dir=str(tmp_path)))
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main,
        ["run", "--input-env", VAR, "--output-var", "TUNSTRAP_WEB_PORT", "--", "true"],
    )
    assert result.exit_code == 0, result.stderr
    assert len(spawn) == 2, "a legal name must reach spawn_daemon"


def test_multi_node_run_succeeds_without_output_var(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """Multi-node input with NO --output-var succeeds: materialization covers
    multi-node unconditionally, so the opt-in gate has nothing left to force."""
    session_dir = str(tmp_path)
    survivor_a = {"ports": {}, "fetch_files": {}, "kube_targets": {"k3s": _RICH_KUBE}}
    other_kube = dict(_RICH_KUBE, path=f"{session_dir}/tunnel-data/node-b-k3s")
    survivor_b = {"ports": {}, "fetch_files": {}, "kube_targets": {"k3s": other_kube}}
    spawn[0](
        {
            "kind": "success",
            "payload": {
                "connections": {"a": survivor_a, "b": survivor_b},
                "pid": 99,
                "session_dir": session_dir,
                "started_at": "2026-08-07T00:00:00Z",
            },
        }
    )
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node()}))
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    joined = f"{_RICH_KUBE['path']}:{session_dir}/tunnel-data/node-b-k3s"
    assert FakePopen.last_env["KUBECONFIG"] == joined
    assert FakePopen.last_env["KUBE_CONFIG_PATHS"] == joined
    assert "KUBE_CONFIG_PATH" not in FakePopen.last_env
    assert FakePopen.last_env["TUNSTRAP_SESSION_DIR"] == session_dir
    assert FakePopen.last_env["TUNSTRAP_PID"] == "99"
    assert FakePopen.last_env["TUNSTRAP_OUTPUT_FILE"] == f"{session_dir}/tunnel-data/output.json"


def test_suppress_kubeconfig_drops_only_injected_kubeconfig() -> None:
    """suppress_kubeconfig (the tunstrap_tofu proxy's guard) drops only the
    injected KUBECONFIG. KUBE_CONFIG_PATH must survive -- it is the
    provider-facing name Mode A relies on through the proxy (issue #14); a
    guard that also dropped it would make Mode A unusable through
    tunstrap_tofu, the documented entry point (ADR entry 20)."""
    from tunstrap.cli import _build_child_env

    out = OutputSchema.model_validate(
        {
            "connections": {"h": {"ports": {}, "kube_targets": {"k3s": _RICH_KUBE}}},
            "pid": 1,
            "session_dir": "/s",
            "started_at": "now",
        }
    )
    env = _build_child_env(out, output_var=None, input_env=None, suppress_kubeconfig=True)
    assert "KUBECONFIG" not in env
    assert env["KUBE_CONFIG_PATH"] == _RICH_KUBE["path"]


def test_suppress_kubeconfig_drops_only_injected_kubeconfig_multi_file() -> None:
    """Same guarantee on the >=2-file branch: KUBE_CONFIG_PATHS survives."""
    from tunstrap.cli import _build_child_env

    other = dict(_RICH_KUBE, path="/s/tunnel-data/node-b-k3s")
    out = OutputSchema.model_validate(
        {
            "connections": {
                "a": {"ports": {}, "kube_targets": {"k3s": _RICH_KUBE}},
                "b": {"ports": {}, "kube_targets": {"k3s": other}},
            },
            "pid": 1,
            "session_dir": "/s",
            "started_at": "now",
        }
    )
    env = _build_child_env(out, output_var=None, input_env=None, suppress_kubeconfig=True)
    assert "KUBECONFIG" not in env
    assert "KUBE_CONFIG_PATH" not in env
    assert env["KUBE_CONFIG_PATHS"] == f"{_RICH_KUBE['path']}:{other['path']}"


def test_plain_path_keeps_full_kube_channel_untouched() -> None:
    """Without suppress_kubeconfig (plain `tunstrap run`), _build_child_env
    passes render_kube_env's channel through unfiltered -- all names the
    conditional cardinality contract sets reach the child."""
    from tunstrap.cli import _build_child_env

    out = OutputSchema.model_validate(
        {
            "connections": {"h": {"ports": {}, "kube_targets": {"k3s": _RICH_KUBE}}},
            "pid": 1,
            "session_dir": "/s",
            "started_at": "now",
        }
    )
    env = _build_child_env(out, output_var=None, input_env=None, suppress_kubeconfig=False)
    assert env["KUBECONFIG"] == _RICH_KUBE["path"]
    assert env["KUBE_CONFIG_PATH"] == _RICH_KUBE["path"]
    assert "KUBE_CONFIG_PATHS" not in env


@pytest.mark.parametrize("suppress_kubeconfig", [False, True])
def test_inherited_kube_env_never_survives_even_without_kube_targets(
    monkeypatch: pytest.MonkeyPatch, suppress_kubeconfig: bool
) -> None:
    """A stray operator KUBECONFIG/KUBE_CONFIG_PATH(S) must never reach the
    child, on either path, even when there are no kube targets to inject a
    replacement that would otherwise overwrite it."""
    from tunstrap.cli import _build_child_env

    monkeypatch.setenv("KUBECONFIG", "/home/operator/.kube/config")
    monkeypatch.setenv("KUBE_CONFIG_PATH", "/home/operator/.kube/config")
    monkeypatch.setenv("KUBE_CONFIG_PATHS", "/home/operator/.kube/config:/other")
    out = OutputSchema.model_validate(
        {
            "connections": {"h": {"ports": {}, "kube_targets": {}}},
            "pid": 1,
            "session_dir": "/s",
            "started_at": "now",
        }
    )
    env = _build_child_env(
        out, output_var=None, input_env=None, suppress_kubeconfig=suppress_kubeconfig
    )
    assert "KUBECONFIG" not in env
    assert "KUBE_CONFIG_PATH" not in env
    assert "KUBE_CONFIG_PATHS" not in env


def test_multi_node_with_output_var_reaches_spawn(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """--output-var is the node-keyed channel; multi-node input reaches spawn.

    spawn_daemon is made to fail immediately so this asserts only that the
    pre-spawn validation let the run through -- the child-env half of
    multi-node behaviour is exercised by test_multi_node_run_succeeds_without_output_var
    and the other injection tests in this file.
    """

    def _spawn_daemon(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        spawn.append(schema)
        raise DaemonError("captured; stop before the child runs", {})

    monkeypatch.setattr(cli_mod, "spawn_daemon", _spawn_daemon)
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node()}))
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "TF_VAR_t", "--", "true"]
    )
    assert result.exit_code == 4, result.stderr
    assert len(spawn) == 2, "multi-node input with --output-var must reach spawn_daemon"


def test_single_node_without_output_var_still_works(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """--output-var is optional; single-node flagless runs are untouched."""
    spawn[0](_success({"node": _conn(db=5432)}, session_dir=str(tmp_path)))
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert len(spawn) == 2


def test_output_var_carries_the_unified_structure_minus_kube_credentials(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """The child receives every field except the kube target's credentials, in
    the unified node-qualified shape.

    The credential-absence property is unchanged from the old scalar-era
    version of this test, but the container shape narrows: ``connections`` ->
    ``nodes``, ports become "host:port" strings, and the kube channel is
    ``{path, context, endpoint}`` only.

    The payload is non-default in every field, so a projection that collapsed a
    dict to ``{}`` or dropped ``warnings`` still fails here.
    """
    session_dir = str(tmp_path)
    payload = _rich_payload(session_dir)
    spawn[0]({"kind": "success", "payload": payload})
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "TF_VAR_t", "--", "true"]
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    decoded = json.loads(FakePopen.last_env["TF_VAR_t"])

    assert decoded["session"]["warnings"][0]["node"] == "edge"
    assert decoded["session"]["warnings"][0]["error"] == "optional node refused the forward"
    assert decoded["session"]["started_at"] == "2026-07-31T00:00:00Z"
    assert decoded["session"]["pid"] == 99
    assert decoded["session"]["session_dir"] == session_dir

    node = decoded["nodes"]["node"]
    assert node["ports"] == {"db": "127.0.0.1:5432"}
    assert node["fetch_files"]["hosts"]["sha256"] == "ab" * 32
    assert node["fetch_files"]["hosts"]["path"] == f"{session_dir}/tunnel-data/node-hosts"

    kube = node["kube"]["k3s"]
    assert kube == {
        "path": "/s/tunnel-data/node-k3s",
        "context": "probe-context",
        "endpoint": "https://127.0.0.1:41111",
    }

    # The credential fields the projection exists to remove -- and everything
    # beyond path/context/endpoint, since UnifiedKubeRef narrows to references.
    assert "client_certificate_data" not in kube
    assert "client_key_data" not in kube
    assert "content_b64" not in kube
    assert "cluster_name" not in kube
    assert "local_port" not in kube
    assert "tls_server_name" not in kube
    assert "certificate_authority_data" not in kube


def test_multi_node_injects_output_var_and_no_target_scoped_scalars(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """More than one node: the structure carries both, plus the three session
    survivors -- but never a target-scoped scalar (no node dimension to
    disambiguate one)."""
    session_dir = str(tmp_path)
    spawn[0](_success({"a": _conn(db=5432), "b": _conn(db=5433)}, session_dir=session_dir))
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node()}))
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "TF_VAR_t", "--", "true"]
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    survivors = {"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE", VAR}
    leaked = [k for k in FakePopen.last_env if k.startswith("TUNSTRAP_") and k not in survivors]
    assert leaked == [], f"multi-node run injected a target-scoped scalar: {leaked}"
    decoded = json.loads(FakePopen.last_env["TF_VAR_t"])
    assert sorted(decoded["nodes"]) == ["a", "b"]
    assert decoded["nodes"]["a"]["ports"] == {"db": "127.0.0.1:5432"}
    assert decoded["nodes"]["b"]["ports"] == {"db": "127.0.0.1:5433"}


def test_optional_node_failure_does_not_affect_kube_channel_or_unified_output(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """One surviving node out of two declared: the kube channel still fires for
    the survivor, and the unified structure reflects only that node -- the
    failure is visible in session.warnings, not as an absence anywhere else.
    """
    session_dir = str(tmp_path)
    survivor = {
        "ports": {"db": 5432},
        "fetch_files": {},
        "kube_targets": {"k3s": _RICH_KUBE},
    }
    spawn[0](
        {
            "kind": "success",
            "payload": {
                "connections": {"a": survivor},
                "pid": 99,
                "session_dir": session_dir,
                "started_at": "2026-07-31T00:00:00Z",
                "warnings": [{"node": "b", "error": "optional node failed"}],
            },
        }
    )
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node(required=False)}))
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "TF_VAR_t", "--", "true"]
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    assert "KUBECONFIG" in FakePopen.last_env, "the surviving node's kube channel must still fire"
    assert "KUBE_CONFIG_PATH" in FakePopen.last_env
    assert "TF_VAR_t" in FakePopen.last_env
    decoded = json.loads(FakePopen.last_env["TF_VAR_t"])
    assert list(decoded["nodes"]) == ["a"], "the failed node is absent, not present-with-error"
    assert decoded["session"]["warnings"] == [
        {"node": "b", "error": "optional node failed", "skipped": True}
    ]


def test_child_env_without_output_var_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any], tmp_path: Path
) -> None:
    """Without --output-var the injected set is exactly the three session
    survivors, plus the kube channel when kube_targets exist -- nothing else."""
    # The developer's own environment must not decide this assertion.
    monkeypatch.delenv("KUBECONFIG", raising=False)
    session_dir = str(tmp_path)
    spawn[0](_success({"node": _conn(db=5432)}, session_dir=session_dir))
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    injected = {
        k: v
        for k, v in FakePopen.last_env.items()
        if k != VAR and k.startswith(("TUNSTRAP_", "KUBECONFIG"))
    }
    assert injected == {
        "TUNSTRAP_SESSION_DIR": session_dir,
        "TUNSTRAP_PID": "99",
        "TUNSTRAP_OUTPUT_FILE": f"{session_dir}/tunnel-data/output.json",
    }
    assert "PATH" in FakePopen.last_env, "child env must still inherit os.environ"
