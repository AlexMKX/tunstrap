"""TunnelManager fetch_files integration.

Validates: TunnelManager threads fetch_files results into NodeOutput and
treats required-file failures as node failures while soft-failing
optional file errors.
Code: tunstrap/manager.py
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from tunstrap import manager as manager_mod
from tunstrap.exceptions import TunnelStartupError
from tunstrap.manager import TunnelManager
from tunstrap.schemas import (
    ErrorOutput,
    FetchedFile,
    FileSpec,
    InputSchema,
    NodeOutput,
    OutputSchema,
)
from tunstrap.session import SessionDir

pytestmark = pytest.mark.unit


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _input(fetch: dict[str, FileSpec] | None = None, required: bool = True) -> InputSchema:
    node: dict[str, Any] = {
        "host": "h",
        "user": "u",
        "ssh_password": "p",
        "remote_targets": {"p": "127.0.0.1:6443"},
        "required": required,
    }
    if fetch is not None:
        node["fetch_files"] = {k: v.model_dump() for k, v in fetch.items()}
    return InputSchema.model_validate({"nodes": {"a": node}})


def _patch_transport(monkeypatch: pytest.MonkeyPatch, fake_conn: _FakeConn) -> None:
    async def fake_open_connection(node: Any) -> _FakeConn:
        return fake_conn

    async def fake_open_local_forwards(
        conn: Any, node: Any, *, tracker_factory: Any = None
    ) -> tuple[dict[str, int], list[Any]]:
        return {"p": 40000}, []

    monkeypatch.setattr(manager_mod, "open_connection", fake_open_connection)
    monkeypatch.setattr(manager_mod, "open_local_forwards", fake_open_local_forwards)


@pytest.mark.asyncio
async def test_no_fetch_files_skips_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """When fetch_files is None the fetcher is never invoked."""
    called: list[Any] = []

    async def fake_fetch_files(conn: Any, specs: Any) -> tuple[dict[str, FetchedFile], list[str]]:
        called.append((conn, specs))
        return {}, []

    monkeypatch.setattr(manager_mod, "fetch_files", fake_fetch_files)
    _patch_transport(monkeypatch, _FakeConn())

    mgr = TunnelManager(_input(fetch=None))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, OutputSchema)
    assert called == []
    assert out.connections["a"].fetch_files == {}


@pytest.mark.asyncio
async def test_fetch_files_results_populate_node_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fetcher results are materialized to their fetch-prefixed leaf, path set."""
    fake_result = {"kubeconfig": FetchedFile(content_b64="YQ==", size=1, sha256="ca97")}

    async def fake_fetch_files(conn: Any, specs: Any) -> tuple[dict[str, FetchedFile], list[str]]:
        return fake_result, []

    monkeypatch.setattr(manager_mod, "fetch_files", fake_fetch_files)
    _patch_transport(monkeypatch, _FakeConn())

    session = SessionDir.create(supplied=None, base=tmp_path)
    mgr = TunnelManager(_input(fetch={"kubeconfig": FileSpec(path="/k")}), session=session)
    out = await mgr.start_all_and_build_output(pid=1, session_dir=session.session_dir)
    assert isinstance(out, OutputSchema)
    materialized = out.connections["a"].fetch_files["kubeconfig"]
    expected_path = str(Path(session.session_dir) / "tunnel-data" / "fetch-a-kubeconfig")
    assert materialized.path == expected_path
    assert Path(expected_path).read_bytes() == base64.b64decode("YQ==")
    assert materialized.content_b64 == "YQ=="


@pytest.mark.asyncio
async def test_required_file_failure_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required-file failure aborts the node and closes its connection."""
    fake_conn = _FakeConn()

    async def fake_fetch_files(conn: Any, specs: Any) -> tuple[dict[str, FetchedFile], list[str]]:
        return {"k": FetchedFile(error="SSH_FX_NO_SUCH_FILE")}, ["k"]

    monkeypatch.setattr(manager_mod, "fetch_files", fake_fetch_files)
    _patch_transport(monkeypatch, fake_conn)

    mgr = TunnelManager(_input(fetch={"k": FileSpec(path="/x")}))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, ErrorOutput)
    assert fake_conn.closed is True


@pytest.mark.asyncio
async def test_soft_fail_file_keeps_node_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """An optional-file error keeps the node in OutputSchema."""

    async def fake_fetch_files(conn: Any, specs: Any) -> tuple[dict[str, FetchedFile], list[str]]:
        return {"k": FetchedFile(error="SSH_FX_NO_SUCH_FILE")}, []

    monkeypatch.setattr(manager_mod, "fetch_files", fake_fetch_files)
    _patch_transport(monkeypatch, _FakeConn())

    mgr = TunnelManager(_input(fetch={"k": FileSpec(path="/x", required=False)}))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, OutputSchema)
    assert isinstance(out.connections["a"], NodeOutput)
    assert out.connections["a"].fetch_files["k"].error == "SSH_FX_NO_SUCH_FILE"


@pytest.mark.asyncio
async def test_fetch_skipped_when_forward_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local-forward failure aborts before any fetch attempt."""
    fake_conn = _FakeConn()
    fetch_called: list[bool] = []

    async def fake_fetch_files(conn: Any, specs: Any) -> tuple[dict[str, FetchedFile], list[str]]:
        fetch_called.append(True)
        return {}, []

    async def fake_open_connection(node: Any) -> _FakeConn:
        return fake_conn

    async def boom(conn: Any, node: Any, *, tracker_factory: Any = None) -> Any:
        raise TunnelStartupError(
            "local forward did not accept connection",
            {"remote_port": 6443, "local_port": 40000},
        )

    monkeypatch.setattr(manager_mod, "fetch_files", fake_fetch_files)
    monkeypatch.setattr(manager_mod, "open_connection", fake_open_connection)
    monkeypatch.setattr(manager_mod, "open_local_forwards", boom)

    mgr = TunnelManager(_input(fetch={"k": FileSpec(path="/x")}))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, ErrorOutput)
    assert fetch_called == []
    assert fake_conn.closed is True


@pytest.mark.asyncio
async def test_fetch_transport_failure_stops_forwarder_and_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport-level failure in the fetcher aborts and closes resources."""
    fake_conn = _FakeConn()

    async def fake_fetch_files(conn: Any, specs: Any) -> tuple[dict[str, FetchedFile], list[str]]:
        raise ConnectionResetError("peer closed mid-fetch")

    monkeypatch.setattr(manager_mod, "fetch_files", fake_fetch_files)
    _patch_transport(monkeypatch, fake_conn)

    mgr = TunnelManager(_input(fetch={"k": FileSpec(path="/x")}))
    out = await mgr.start_all_and_build_output(pid=1, session_dir="/tmp/x")
    assert isinstance(out, ErrorOutput)
    assert "peer closed mid-fetch" in out.message + str(out.details)
    assert fake_conn.closed is True
