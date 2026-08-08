import json

import pytest

from tunstrap.envrender import (
    format_exports,
    predicted_env_keys,
    render_kube_env,
    render_output_var,
    render_unified_output,
)
from tunstrap.schemas import (
    FetchedFile,
    InputSchema,
    KubeTargetOutput,
    NodeOutput,
    OutputSchema,
    TunnelWarning,
)

pytestmark = pytest.mark.unit


def _kube_out(port, path):
    return KubeTargetOutput(
        cluster_name="c",
        context_name="ctx",
        local_port=port,
        endpoint=f"https://127.0.0.1:{port}",
        tls_server_name="c",
        certificate_authority_data="",
        client_certificate_data="",
        client_key_data="",
        content_b64="",
        path=path,
    )


def _kube_out_full(port, path, *, context):
    result = _kube_out(port, path)
    result.context_name = context
    return result


def test_render_unified_output_shape() -> None:
    """Ports, kube references, and fetched-file references use the unified shape."""
    out = OutputSchema(
        connections={
            "node1": NodeOutput(
                ports={"service1": 5432},
                kube_targets={
                    "k3s": _kube_out_full(
                        7000, "/s/tunnel-data/node1-k3s", context="tunstrap-node1-k3s"
                    )
                },
                fetch_files={
                    "hosts": FetchedFile(
                        content_b64="aG9zdHM=",
                        size=6,
                        sha256="ab" * 32,
                        path="/s/tunnel-data/node1-hosts",
                    )
                },
            )
        },
        pid=42,
        session_dir="/s",
        started_at="2026-08-07T00:00:00Z",
    )
    unified = render_unified_output(out)
    assert unified["session"] == {
        "session_dir": "/s",
        "pid": 42,
        "started_at": "2026-08-07T00:00:00Z",
        "warnings": [],
    }
    node = unified["nodes"]["node1"]
    assert node["ports"] == {"service1": "127.0.0.1:5432"}
    assert node["kube"]["k3s"] == {
        "path": "/s/tunnel-data/node1-k3s",
        "context": "tunstrap-node1-k3s",
        "endpoint": "https://127.0.0.1:7000",
    }
    assert node["fetch_files"]["hosts"] == {
        "path": "/s/tunnel-data/node1-hosts",
        "size": 6,
        "sha256": "ab" * 32,
    }
    dumped = json.dumps(unified)
    for leaked in ("client_certificate_data", "client_key_data", "content_b64"):
        assert leaked not in dumped


def test_render_unified_output_multi_node() -> None:
    """Node dimension is a nested key: two nodes, two independent bodies."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(ports={"db": 1}),
            "b": NodeOutput(ports={"db": 2}),
        },
        pid=1,
        session_dir="/s",
        started_at="now",
    )
    unified = render_unified_output(out)
    assert set(unified["nodes"]) == {"a", "b"}
    assert unified["nodes"]["a"]["ports"]["db"] == "127.0.0.1:1"
    assert unified["nodes"]["b"]["ports"]["db"] == "127.0.0.1:2"


def test_render_output_var_serializes_the_unified_shape() -> None:
    """The output variable decodes to the unified output shape."""
    out = OutputSchema(
        connections={"h": NodeOutput(ports={"db": 1})},
        pid=1,
        session_dir="/s",
        started_at="now",
    )
    decoded = json.loads(render_output_var(out))
    assert decoded == render_unified_output(out)


def test_render_kube_env_zero_files_returns_empty() -> None:
    """No kube_targets anywhere -> no keys at all."""
    out = OutputSchema(
        connections={"h": NodeOutput(ports={"db": 1})},
        pid=1,
        session_dir="/s",
        started_at="now",
    )
    assert render_kube_env(out) == {}


def test_render_kube_env_one_file_sets_path_not_paths() -> None:
    """Exactly one materialized file: KUBECONFIG + KUBE_CONFIG_PATH, no _PATHS."""
    out = OutputSchema(
        connections={"h": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, "/s/k3s")})},
        pid=1,
        session_dir="/s",
        started_at="now",
    )
    env = render_kube_env(out)
    assert env == {"KUBECONFIG": "/s/k3s", "KUBE_CONFIG_PATH": "/s/k3s"}
    assert "KUBE_CONFIG_PATHS" not in env


def test_render_kube_env_two_files_sets_paths_not_path() -> None:
    """Two materialized files use KUBE_CONFIG_PATHS and not KUBE_CONFIG_PATH."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, "/s/a-k3s")}),
            "b": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7001, "/s/b-k3s")}),
        },
        pid=1,
        session_dir="/s",
        started_at="now",
    )
    env = render_kube_env(out)
    assert env == {
        "KUBECONFIG": "/s/a-k3s:/s/b-k3s",
        "KUBE_CONFIG_PATHS": "/s/a-k3s:/s/b-k3s",
    }
    assert "KUBE_CONFIG_PATH" not in env


def test_render_kube_env_not_materialized_raises() -> None:
    """An unmaterialized target is rejected by render_kube_env itself."""
    out = OutputSchema(
        connections={"h": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, None)})},
        pid=1,
        session_dir="/s",
        started_at="now",
    )
    with pytest.raises(ValueError, match="not materialized"):
        render_kube_env(out)


def test_render_kube_env_multi_node_not_materialized_raises() -> None:
    """An unmaterialized target is rejected during multi-node aggregation."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, "/s/a-k3s")}),
            "b": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7001, None)}),
        },
        pid=1,
        session_dir="/s",
        started_at="now",
    )
    with pytest.raises(ValueError, match="not materialized"):
        render_kube_env(out)


def test_predicted_env_keys_reserves_all_three_for_one_kube_target() -> None:
    """Reserve every conditional kube-channel key whenever kube targets exist."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "node": {
                    "host": "h.example.net",
                    "user": "u",
                    "ssh_password": "p",
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                }
            }
        }
    )
    keys = predicted_env_keys(schema)
    assert {"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"} <= keys


def test_predicted_env_keys_reserves_all_three_for_two_kube_targets_one_node() -> None:
    """Reserve every conditional kube-channel key for multiple declared targets."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "node": {
                    "host": "h.example.net",
                    "user": "u",
                    "ssh_password": "p",
                    "kube_targets": {
                        "a": {"kubeconfig_path": "/etc/a.yaml"},
                        "b": {"kubeconfig_path": "/etc/b.yaml"},
                    },
                }
            }
        }
    )
    keys = predicted_env_keys(schema)
    assert {"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"} <= keys


def test_format_exports_quotes_safely():
    txt = format_exports({"A": "x'y", "B": "z"})
    assert "export A='x'\\''y'" in txt
    assert "export B='z'" in txt


def test_predicted_env_keys_is_session_scalars_plus_kube_channel() -> None:
    """predicted_env_keys collapses to the three survivors + the CONSERVATIVE
    kube channel (all three names, not just the branch this input's exact
    declared cardinality would hit) -- there is no other injected key left,
    and the formula does not vary by exact count."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                },
                "b": {
                    "host": "h2",
                    "user": "u",
                    "ssh_password": "p",
                    "kube_targets": {"k4s": {"kubeconfig_path": "/etc/k4s.yaml"}},
                },
            }
        }
    )
    assert predicted_env_keys(schema) == {
        "TUNSTRAP_SESSION_DIR",
        "TUNSTRAP_PID",
        "TUNSTRAP_OUTPUT_FILE",
        "KUBECONFIG",
        "KUBE_CONFIG_PATH",
        "KUBE_CONFIG_PATHS",
    }


def test_predicted_env_keys_no_kube_is_just_the_three_survivors() -> None:
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"db": "127.0.0.1:1"},
                }
            }
        }
    )
    assert predicted_env_keys(schema) == {
        "TUNSTRAP_SESSION_DIR",
        "TUNSTRAP_PID",
        "TUNSTRAP_OUTPUT_FILE",
    }


def test_predicted_env_keys_covers_actual_injected_keys_under_cardinality_shrink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety-envelope half of the two-part anti-drift guard: predicted must be
    a superset of actual, driven by the exact scenario that falsifies a
    predictor that got the conservatism backwards -- two kube targets
    DECLARED (one on an optional node that fails), only ONE materializes. A
    NAME colliding with a key _build_child_env actually injects, but which
    predicted_env_keys failed to reserve, would sail through the pre-spawn
    collision check and then genuinely collide post-spawn."""
    from tunstrap import cli as cli_mod
    from tunstrap.cli import _build_child_env

    # _build_child_env starts from dict(os.environ) (cli.py:394), so without
    # isolating it first, `set(actual)` is the whole ambient environment
    # (PATH, HOME, ...) and any comparison against it is meaningless in any
    # real process. Isolate BEFORE calling it, not after: subtracting
    # os.environ back out (`set(actual) - set(os.environ)`) is NOT an
    # acceptable substitute -- a key that is both inherited AND injected (an
    # operator-set KUBECONFIG, or a NAME matching --output-var) would be
    # subtracted away too, silently under-checking exactly the collision
    # this guard exists to catch.
    monkeypatch.setattr(cli_mod.os, "environ", {})

    # Input: two kube targets declared, on two nodes -- one optional and about
    # to fail. predicted_env_keys sees only this schema.
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": {
                    "host": "h1",
                    "user": "u",
                    "ssh_password": "p",
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                },
                "b": {
                    "host": "h2",
                    "user": "u",
                    "ssh_password": "p",
                    "required": False,
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                },
            }
        }
    )
    # Output: node "b" failed (required: false), only node "a"'s kube target
    # actually materialized -- output cardinality (1) SHRANK below input
    # cardinality (2). This is the real _build_child_env sees post-spawn.
    out = OutputSchema(
        connections={
            "a": NodeOutput(
                ports={}, kube_targets={"k3s": _kube_out(7000, "/run/s/tunnel-data/k3s")}
            ),
        },
        pid=1,
        session_dir="/run/s",
        started_at="now",
        warnings=[TunnelWarning(node="b", error="optional node refused the forward")],
    )
    actual = _build_child_env(out, output_var=None, input_env=None)
    # Subset, not equality: predicted (conservative, computed from input
    # cardinality 2) legitimately claims MORE than actual (exact, computed
    # from output cardinality 1) -- that asymmetry is the whole point.
    assert set(actual) <= predicted_env_keys(schema)
    # Anti-vacuity: KUBE_CONFIG_PATHS specifically must be in the prediction
    # even though it is NOT in the actual export (the >=2 branch never fires
    # here) -- this is the exact key an exact-cardinality predictor would
    # have wrongly omitted.
    assert "KUBE_CONFIG_PATHS" in predicted_env_keys(schema)
    assert "KUBE_CONFIG_PATHS" not in actual
