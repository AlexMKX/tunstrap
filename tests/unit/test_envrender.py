import pytest

from tunstrap.envrender import format_exports, predicted_env_keys, render_env
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


def test_predicted_env_keys_matches_render_env() -> None:
    """The predictor and render_env agree exactly for a matching input/output pair.

    This is the anti-drift guard: adding a key to render_env without adding it
    to predicted_env_keys makes this test fail.
    """
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
    assert predicted_env_keys(schema) == set(render_env(out))


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
    """Multi-node input injects no scalars at all, so nothing can collide."""
    node = {
        "host": "h.example.net",
        "user": "u",
        "ssh_password": "p",
        "remote_targets": {"db": "127.0.0.1:5432"},
    }
    schema = InputSchema.model_validate({"nodes": {"a": node, "b": dict(node)}})
    assert predicted_env_keys(schema) == set()
