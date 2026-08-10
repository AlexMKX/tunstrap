"""TunnelManager required/optional node policy.

Validates: required-node failures abort start_all and optional-node
failures degrade to warnings; the full output is OutputSchema vs
ErrorOutput depending on which path is hit.
Code: tunstrap/manager.py
"""

from __future__ import annotations

from typing import Any

import asyncssh
import pytest

from tunstrap import manager as manager_mod
from tunstrap.manager import TunnelManager
from tunstrap.schemas import ErrorOutput, InputSchema, OutputSchema

pytestmark = pytest.mark.unit


def _two_node_schema(a_required: bool, b_required: bool) -> InputSchema:
    a: dict[str, Any] = {
        "host": "a-host",
        "user": "u",
        "ssh_password": "p",
        "remote_targets": {"p": "127.0.0.1:6443"},
        "required": a_required,
    }
    b: dict[str, Any] = {
        "host": "b-host",
        "user": "u",
        "ssh_password": "p",
        "remote_targets": {"p": "127.0.0.1:6443"},
        "required": b_required,
    }
    return InputSchema.model_validate({"nodes": {"a": a, "b": b}})


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _patch_transport_per_host(
    monkeypatch: pytest.MonkeyPatch,
    bad_host: str | None,
) -> None:
    async def fake_open_connection(node: Any) -> _FakeConn:
        if bad_host is not None and node.host == bad_host:
            raise asyncssh.PermissionDenied("auth failed")
        return _FakeConn()

    async def fake_open_local_forwards(
        conn: Any, node: Any, *, tracker_factory: Any = None
    ) -> tuple[dict[str, int], list[Any]]:
        return {"p": 40000}, []

    async def fake_fetch_files(
        conn: Any, specs: Any, *, timeout: float
    ) -> tuple[dict[str, Any], list[str]]:
        return {}, []

    monkeypatch.setattr(manager_mod, "open_connection", fake_open_connection)
    monkeypatch.setattr(manager_mod, "open_local_forwards", fake_open_local_forwards)
    monkeypatch.setattr(manager_mod, "fetch_files", fake_fetch_files)


@pytest.mark.asyncio
async def test_required_failure_aborts_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required-node failure causes start_all to return ErrorOutput."""
    _patch_transport_per_host(monkeypatch, bad_host="b-host")
    mgr = TunnelManager(_two_node_schema(a_required=True, b_required=True))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, ErrorOutput)


@pytest.mark.asyncio
async def test_optional_failure_becomes_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """An optional-node failure is reported as a TunnelWarning, not an error."""
    _patch_transport_per_host(monkeypatch, bad_host="b-host")
    mgr = TunnelManager(_two_node_schema(a_required=True, b_required=False))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, OutputSchema)
    assert "a" in out.connections
    assert "b" not in out.connections
    assert any(w.node == "b" for w in out.warnings)


@pytest.mark.asyncio
async def test_all_optional_all_fail_returns_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-optional all-failing case returns OutputSchema with warnings only."""

    async def fake_open_connection_all_fail(node: Any) -> Any:
        raise asyncssh.PermissionDenied("auth failed")

    monkeypatch.setattr(manager_mod, "open_connection", fake_open_connection_all_fail)

    mgr = TunnelManager(_two_node_schema(a_required=False, b_required=False))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, OutputSchema)
    assert out.connections == {}
    assert len(out.warnings) == 2


@pytest.mark.asyncio
async def test_all_required_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-required all-succeed case returns OutputSchema with both nodes."""
    _patch_transport_per_host(monkeypatch, bad_host=None)
    mgr = TunnelManager(_two_node_schema(a_required=True, b_required=True))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, OutputSchema)
    assert sorted(out.connections.keys()) == ["a", "b"]
