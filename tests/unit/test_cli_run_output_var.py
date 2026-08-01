"""`--output-var`: name validation, collision rejection, and injection.

Validates: NAME must be a valid env-var name and must not collide with a key
run already injects; the child receives the OutputSchema as JSON under NAME,
projected to drop the kube credentials (see test_cli_run_secret_scrub.py);
multi-node results get NAME and no TUNSTRAP_* scalars.
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

# Non-default in every field the TUNSTRAP_* scalars drop: a real warning, a
# fetched file, and the seven kube_target fields beyond path/endpoint.
_RICH_PAYLOAD: dict[str, Any] = {
    "connections": {
        "node": {
            "ports": {"db": 5432},
            "fetch_files": {"hosts": {"content_b64": "aG9zdHM=", "size": 6, "sha256": "ab" * 32}},
            "kube_targets": {"k3s": _RICH_KUBE},
        }
    },
    "pid": 99,
    "session_dir": "/s",
    "started_at": "2026-07-31T00:00:00Z",
    "warnings": [{"node": "edge", "error": "optional node refused the forward", "skipped": True}],
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


def test_output_var_carries_the_whole_envelope_minus_kube_credentials(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """The child receives every field except the kube target's credentials.

    This test previously asserted the envelope was carried *complete*, which
    is precisely the exposure fixed here: the value becomes a Terraform
    variable and OpenTofu persists root-module variables in the plan file. The
    lossless property is now deliberately false, and only for the three
    credential fields — everything else must still survive, which is what the
    whole-object comparison below pins.

    The payload is non-default in every field, so a projection that collapsed a
    dict to ``{}`` or dropped ``warnings`` still fails here.
    """
    spawn[0]({"kind": "success", "payload": _RICH_PAYLOAD})
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "TF_VAR_t", "--", "true"]
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    decoded = json.loads(FakePopen.last_env["TF_VAR_t"])

    # Round-tripped through OutputSchema so defaulted fields (FetchedFile.error)
    # are normalised on both sides; only kube_targets is then overridden.
    expected = json.loads(OutputSchema.model_validate(_RICH_PAYLOAD).model_dump_json())
    expected["connections"]["node"]["kube_targets"]["k3s"] = {
        key: value
        for key, value in _RICH_KUBE.items()
        if key not in {"client_certificate_data", "client_key_data", "content_b64"}
    }
    # Whole-object equality: any *other* dropped, renamed or re-defaulted field
    # fails here, so the projection cannot quietly widen.
    assert decoded == expected

    # Restated field by field so the failure message names what was lost.
    assert decoded["warnings"][0]["node"] == "edge"
    assert decoded["warnings"][0]["error"] == "optional node refused the forward"
    assert decoded["started_at"] == "2026-07-31T00:00:00Z"
    assert decoded["pid"] == 99
    assert decoded["session_dir"] == "/s"
    kube = decoded["connections"]["node"]["kube_targets"]["k3s"]
    assert kube["tls_server_name"] == "probe-control-plane"
    assert kube["certificate_authority_data"] == "Y2E="
    assert kube["path"] == "/s/tunnel-data/node-k3s"
    assert kube["cluster_name"] == "probe-cluster"
    assert kube["context_name"] == "probe-context"
    assert kube["local_port"] == 41111
    assert kube["endpoint"] == "https://127.0.0.1:41111"
    assert decoded["connections"]["node"]["fetch_files"]["hosts"]["sha256"] == "ab" * 32

    # The three fields the projection exists to remove.
    assert "client_certificate_data" not in kube
    assert "client_key_data" not in kube
    assert "content_b64" not in kube


def test_single_node_keeps_scalars_alongside_output_var(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """--output-var is additive: the TUNSTRAP_* scalars are still injected."""
    spawn[0](_success({"node": _conn(db=5432)}))
    monkeypatch.setenv(VAR, _payload())
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "TF_VAR_t", "--", "true"]
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    assert FakePopen.last_env["TUNSTRAP_DB_PORT"] == "5432"
    assert FakePopen.last_env["TUNSTRAP_DB_ENDPOINT"] == "127.0.0.1:5432"
    assert "TF_VAR_t" in FakePopen.last_env


def test_multi_node_injects_output_var_and_no_scalars(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """More than one node: the structure only, never the ambiguous scalars."""
    spawn[0](_success({"a": _conn(db=5432), "b": _conn(db=5433)}))
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node()}))
    result = CliRunner().invoke(
        main, ["run", "--input-env", VAR, "--output-var", "TF_VAR_t", "--", "true"]
    )
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    # VAR itself is the payload variable this test set; it is inherited, not injected.
    leaked = [k for k in FakePopen.last_env if k.startswith("TUNSTRAP_") and k != VAR]
    assert leaked == [], f"multi-node run injected ambiguous scalars: {leaked}"
    decoded = OutputSchema.model_validate(json.loads(FakePopen.last_env["TF_VAR_t"]))
    assert sorted(decoded.connections) == ["a", "b"]
    assert decoded.connections["a"].ports == {"db": 5432}
    assert decoded.connections["b"].ports == {"db": 5433}


def test_multi_node_suppression_uses_input_count(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """Two nodes in, one connection out: still no scalars, still no KUBECONFIG.

    This is the falsifiable half of the multi-node rule, and the only place the
    KUBECONFIG check can actually fire. An implementation that decided from
    ``len(out.connections)`` instead of the input node count sees 1 here, calls
    render_env successfully, and injects the surviving node's scalars --
    including KUBECONFIG, which points at a real materialized kubeconfig. The
    two-in/two-out case above cannot catch that: render_env rejects a two-node
    envelope outright, so a wrong implementation errors there instead of
    leaking, and a KUBECONFIG assertion on it could never fail.
    """
    # Clear the operator's own KUBECONFIG: it is the one injected key with no
    # TUNSTRAP_ prefix, so an inherited copy would mask a wrongly-injected one.
    monkeypatch.delenv("KUBECONFIG", raising=False)
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
                "session_dir": "/s",
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
    leaked = [k for k in FakePopen.last_env if k.startswith("TUNSTRAP_") and k != VAR]
    assert leaked == [], f"scalars injected from the output node count: {leaked}"
    assert "KUBECONFIG" not in FakePopen.last_env, "KUBECONFIG injected despite multi-node input"
    assert "TF_VAR_t" in FakePopen.last_env


def test_child_env_without_output_var_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """Without --output-var the injected set is exactly what it was before."""
    # The developer's own environment must not decide this assertion.
    monkeypatch.delenv("KUBECONFIG", raising=False)
    spawn[0](_success({"node": _conn(db=5432)}))
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
        "TUNSTRAP_SESSION_DIR": "/s",
        "TUNSTRAP_PID": "99",
        "TUNSTRAP_DB_HOST": "127.0.0.1",
        "TUNSTRAP_DB_PORT": "5432",
        "TUNSTRAP_DB_ENDPOINT": "127.0.0.1:5432",
    }
    assert "PATH" in FakePopen.last_env, "child env must still inherit os.environ"
