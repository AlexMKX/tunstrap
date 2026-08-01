"""InputSchema / NodeInput / OutputSchema base shape.

Validates: minimum-valid input, required pkey-or-password invariant,
and OutputSchema round-tripping.
Code: tunstrap/schemas.py
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.unit.conftest import make_node
from tunstrap.schemas import (
    DaemonOptions,
    InputSchema,
    NodeInput,
    NodeOutput,
    OutputSchema,
    SSHOptions,
    TunnelWarning,
)

pytestmark = pytest.mark.unit


def test_valid_minimum_input_parses() -> None:
    """A minimum-valid InputSchema fills defaults for port/required/options."""
    schema = InputSchema.model_validate({"nodes": {"a": make_node()}})
    assert schema.nodes["a"].port == 22
    assert schema.nodes["a"].required is True
    assert isinstance(schema.nodes["a"], NodeInput)
    assert isinstance(schema.daemon, DaemonOptions)
    assert isinstance(schema.nodes["a"].ssh_options, SSHOptions)


def test_node_requires_pkey_or_password_without_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node with neither ssh_pkey nor ssh_password is rejected when SSH_AUTH_SOCK is absent."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    payload = {"nodes": {"a": {"host": "h", "user": "u", "remote_targets": {"p": "127.0.0.1:22"}}}}
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate(payload)
    assert "ssh-agent" in str(excinfo.value)


def test_node_accepts_neither_pkey_nor_password_with_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node with neither ssh_pkey nor ssh_password is accepted when SSH_AUTH_SOCK is set."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")
    payload = {"nodes": {"a": {"host": "h", "user": "u", "remote_targets": {"p": "127.0.0.1:22"}}}}
    schema = InputSchema.model_validate(payload)
    assert schema.nodes["a"].ssh_pkey is None
    assert schema.nodes["a"].ssh_password is None


def test_missing_required_host_fails() -> None:
    """A node missing the required host field is rejected."""
    payload = {
        "nodes": {"a": {"user": "u", "ssh_password": "p", "remote_targets": {"p": "127.0.0.1:22"}}}
    }
    with pytest.raises(ValidationError):
        InputSchema.model_validate(payload)


def test_output_schema_round_trips() -> None:
    """OutputSchema round-trips losslessly through JSON."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(
                ports={"p": 40001},
                fetch_files={},
            )
        },
        pid=12345,
        session_dir="/tmp/x",
        started_at="2026-05-16T14:30:00Z",
        warnings=[TunnelWarning(node="b", error="auth failed")],
    )
    rebuilt = OutputSchema.model_validate_json(out.model_dump_json())
    assert rebuilt == out


def test_daemon_options_auto_stop_idle_seconds_default_null() -> None:
    """The auto-stop field defaults to None (disabled)."""
    opts = DaemonOptions()
    assert opts.auto_stop_idle_seconds is None


def test_daemon_options_auto_stop_idle_seconds_accepts_positive_int() -> None:
    """A positive integer is accepted."""
    opts = DaemonOptions(auto_stop_idle_seconds=60)
    assert opts.auto_stop_idle_seconds == 60


def test_daemon_options_auto_stop_idle_seconds_rejects_zero() -> None:
    """Zero is rejected (timer would fire instantly)."""
    with pytest.raises(ValidationError):
        DaemonOptions(auto_stop_idle_seconds=0)


def test_daemon_options_auto_stop_idle_seconds_rejects_negative() -> None:
    """Negative values are rejected."""
    with pytest.raises(ValidationError):
        DaemonOptions(auto_stop_idle_seconds=-1)


def test_daemon_options_rejects_unknown_field() -> None:
    """extra=forbid still rejects typos (regression guard)."""
    with pytest.raises(ValidationError):
        DaemonOptions(auto_stop_idle_secunds=10)  # typo


def test_node_kube_only_allows_empty_remote_targets() -> None:
    """A node with kube_targets and no remote_targets is valid."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "n": {
                    "host": "h",
                    "user": "u",
                    "ssh_pkey": "k",
                    "remote_targets": {},
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                }
            },
        }
    )
    assert schema.nodes["n"].remote_targets == {}
    assert "k3s" in schema.nodes["n"].kube_targets


def test_node_doing_nothing_is_rejected() -> None:
    """A node with no remote_targets, kube_targets, or fetch_files is rejected."""
    with pytest.raises(ValidationError, match="at least one of"):
        InputSchema.model_validate(
            {
                "nodes": {"n": {"host": "h", "user": "u", "ssh_pkey": "k", "remote_targets": {}}},
            }
        )
