"""KubeTarget + NodeInput.kube_targets validation.

Validates: KubeTarget path rules, default values, and kube_targets
key/value limits on NodeInput.
Code: tunstrap/schemas.py
Assertion: invalid paths/keys raise ValidationError; defaults resolve
to insecure_fallback=False and required=True.
Method: construct models via model_validate and assert resolved fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.unit.conftest import make_node
from tunstrap.schemas import InputSchema, KubeTarget, NodeInput

pytestmark = pytest.mark.unit


def test_kube_target_defaults() -> None:
    """KubeTarget defaults: insecure_fallback False, required True, tls hint None."""
    kt = KubeTarget.model_validate({"kubeconfig_path": "/etc/rancher/k3s/k3s.yaml"})
    assert kt.insecure_fallback is False
    assert kt.required is True
    assert kt.tls_server_name is None


def test_kube_target_rejects_relative_path() -> None:
    """A relative kubeconfig_path is rejected."""
    with pytest.raises(ValidationError):
        KubeTarget.model_validate({"kubeconfig_path": "etc/k3s.yaml"})


def test_kube_target_rejects_tilde_path() -> None:
    """A tilde-prefixed kubeconfig_path is rejected (no shell expansion)."""
    with pytest.raises(ValidationError):
        KubeTarget.model_validate({"kubeconfig_path": "~/.kube/config"})


def test_kube_target_rejects_extra_field() -> None:
    """KubeTarget is closed (extra='forbid')."""
    with pytest.raises(ValidationError):
        KubeTarget.model_validate({"kubeconfig_path": "/x", "bogus": 1})


def test_node_kube_targets_default_none() -> None:
    """NodeInput.kube_targets defaults to None when omitted."""
    node = NodeInput.model_validate(make_node())
    assert node.kube_targets is None


def test_node_kube_targets_rejects_empty_dict() -> None:
    """An empty kube_targets dict is rejected (omit instead)."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate({"nodes": {"a": make_node(kube_targets={})}})


def test_node_kube_targets_rejects_bad_key() -> None:
    """kube_targets keys must match the identifier pattern."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate(
            {"nodes": {"a": make_node(kube_targets={"bad name": {"kubeconfig_path": "/x"}})}}
        )


def test_node_kube_targets_happy_path() -> None:
    """A well-formed kube_targets block parses into KubeTarget values."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": make_node(
                    kube_targets={"k3s": {"kubeconfig_path": "/etc/rancher/k3s/k3s.yaml"}}
                )
            }
        }
    )
    kt = schema.nodes["a"].kube_targets
    assert kt is not None
    assert kt["k3s"].kubeconfig_path == "/etc/rancher/k3s/k3s.yaml"


def test_kube_target_rejects_too_long_path() -> None:
    """Paths exceeding the documented 4096-char cap are rejected."""
    with pytest.raises(ValidationError):
        KubeTarget.model_validate({"kubeconfig_path": "/" + "a" * 4100})


def test_node_kube_targets_rejects_too_many_entries() -> None:
    """kube_targets rejects more than 16 entries per node."""
    entries = {f"k{i}": {"kubeconfig_path": f"/etc/k{i}.yaml"} for i in range(17)}
    with pytest.raises(ValidationError):
        InputSchema.model_validate({"nodes": {"a": make_node(kube_targets=entries)}})


def test_node_kube_targets_rejects_too_long_key() -> None:
    """kube_targets keys exceeding the 64-char cap are rejected."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate(
            {"nodes": {"a": make_node(kube_targets={"a" * 65: {"kubeconfig_path": "/x"}})}}
        )


def test_daemon_materialize_default_false() -> None:
    """DaemonOptions.materialize defaults to False."""
    schema = InputSchema.model_validate({"nodes": {"a": make_node()}})
    assert schema.daemon.materialize is False


def test_daemon_materialize_explicit_true() -> None:
    """DaemonOptions.materialize honours an explicit True."""
    schema = InputSchema.model_validate(
        {"nodes": {"a": make_node()}, "daemon": {"materialize": True}}
    )
    assert schema.daemon.materialize is True


def test_input_rejects_node_key_with_slash() -> None:
    """A node key containing '/' is rejected (path-traversal guard)."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate({"nodes": {"../evil": make_node()}})


def test_input_rejects_node_key_bad_chars() -> None:
    """A node key not matching the identifier pattern is rejected."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate({"nodes": {"bad name": make_node()}})


def test_input_rejects_node_key_too_long() -> None:
    """A node key exceeding 64 chars is rejected."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate({"nodes": {"a" * 65: make_node()}})


def test_input_accepts_valid_node_key() -> None:
    """A normal identifier node key is accepted."""
    schema = InputSchema.model_validate({"nodes": {"hub": make_node()}})
    assert "hub" in schema.nodes
