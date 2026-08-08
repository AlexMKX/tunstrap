import pytest

from tunstrap.envrender import format_exports, predicted_env_keys, render_env, render_kube_env
from tunstrap.exceptions import MultiNodeEnvUnsupported
from tunstrap.schemas import InputSchema, KubeTargetOutput, NodeOutput, OutputSchema

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


def test_render_ports_and_session():
    out = OutputSchema(
        connections={"h": NodeOutput(ports={"db-1": 5432})},
        pid=42,
        session_dir="/run/s",
        started_at="now",
    )
    env = render_env(out)
    assert env["TUNSTRAP_SESSION_DIR"] == "/run/s"
    assert env["TUNSTRAP_PID"] == "42"
    assert env["TUNSTRAP_DB_1_PORT"] == "5432"
    assert env["TUNSTRAP_DB_1_ENDPOINT"] == "127.0.0.1:5432"
    assert "KUBECONFIG" not in env


def test_render_kube_sets_kubeconfig():
    out = OutputSchema(
        connections={
            "h": NodeOutput(
                ports={}, kube_targets={"k3s": _kube_out(7000, "/run/s/tunnel-data/k3s")}
            )
        },
        pid=1,
        session_dir="/run/s",
        started_at="now",
    )
    env = render_env(out)
    assert env["TUNSTRAP_K3S_KUBECONFIG"] == "/run/s/tunnel-data/k3s"
    assert env["KUBECONFIG"] == "/run/s/tunnel-data/k3s"
    assert env["TUNSTRAP_K3S_ENDPOINT"] == "https://127.0.0.1:7000"


def test_render_kube_not_materialized_raises():
    out = OutputSchema(
        connections={"h": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, None)})},
        pid=1,
        session_dir="/run/s",
        started_at="now",
    )
    with pytest.raises(ValueError, match="not materialized"):
        render_env(out)


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


def test_render_requires_single_node_zero() -> None:
    """Zero connections raise the typed error, not a bare ValueError."""
    out = OutputSchema(connections={}, pid=1, session_dir="/s", started_at="now")
    with pytest.raises(MultiNodeEnvUnsupported, match="exactly one node"):
        render_env(out)


def test_render_requires_single_node_two() -> None:
    """Two connections raise the typed error, so run maps them to exit 1."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(ports={"db": 1}),
            "b": NodeOutput(ports={"db": 2}),
        },
        pid=1,
        session_dir="/s",
        started_at="now",
    )
    with pytest.raises(MultiNodeEnvUnsupported, match="exactly one node"):
        render_env(out)


def test_format_exports_quotes_safely():
    txt = format_exports({"A": "x'y", "B": "z"})
    assert "export A='x'\\''y'" in txt
    assert "export B='z'" in txt


def test_predicted_env_keys_covers_render_env() -> None:
    """The predictor covers the actual keys for a matching input/output pair."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "node": {
                    "host": "h.example.net",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"db-1": "127.0.0.1:5432", "web": "127.0.0.1:80"},
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                }
            }
        }
    )
    out = OutputSchema(
        connections={
            "node": NodeOutput(
                ports={"db-1": 5432, "web": 8080},
                kube_targets={"k3s": _kube_out(7000, "/run/s/tunnel-data/k3s")},
            )
        },
        pid=1,
        session_dir="/run/s",
        started_at="now",
    )
    assert set(render_env(out)) <= predicted_env_keys(schema)


def test_predicted_env_keys_no_kube_omits_kubeconfig() -> None:
    """Without kube_targets the predictor must not claim KUBECONFIG."""
    schema = InputSchema.model_validate(
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
    assert "KUBECONFIG" not in predicted_env_keys(schema)
    assert predicted_env_keys(schema) == {
        "TUNSTRAP_SESSION_DIR",
        "TUNSTRAP_PID",
        "TUNSTRAP_DB_HOST",
        "TUNSTRAP_DB_PORT",
        "TUNSTRAP_DB_ENDPOINT",
    }


def test_predicted_env_keys_multi_node_is_empty() -> None:
    """Multi-node input injects no TUNSTRAP_* scalars, so nothing can collide."""
    node = {
        "host": "h.example.net",
        "user": "u",
        "ssh_password": "p",
        "remote_targets": {"db": "127.0.0.1:5432"},
    }
    schema = InputSchema.model_validate({"nodes": {"a": node, "b": dict(node)}})
    assert predicted_env_keys(schema) == set()
