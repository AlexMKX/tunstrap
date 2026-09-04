"""Unit tests for ssh.open_local_forwards and ssh.open_connection kwargs."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from tunstrap.exceptions import TunnelStartupError
from tunstrap.schemas import InputSchema
from tunstrap.ssh import open_connection, open_local_forwards

pytestmark = pytest.mark.unit


def make_node(
    *,
    remote_targets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal NodeInput payload for tests."""
    return {
        "host": "127.0.0.1",
        "user": "tester",
        "ssh_pkey": "PEM",
        "remote_targets": remote_targets or {"p": "127.0.0.1:6443"},
    }


def _fake_listener(port: int) -> MagicMock:
    """Mock listener that reports a fixed port and supports close()/wait_closed()."""
    listener = MagicMock(spec=asyncssh.SSHListener)
    listener.get_port.return_value = port
    listener.close = MagicMock()
    listener.wait_closed = AsyncMock()
    return listener


def _fake_conn(
    listeners: list[MagicMock],
) -> MagicMock:
    """Mock connection whose forward_local_port yields the given listeners.

    ``open_connection`` (the required-node far-end probe) succeeds and
    returns a writer double, so tests exercise the forward path itself.
    """
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(side_effect=listeners)
    conn.open_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
    return conn


@pytest.mark.asyncio
async def test_forward_called_with_target_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each handle drives one forward_local_port call with (target.host, target.port)."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": make_node(
                    remote_targets={
                        "kubeapi": "10.0.0.1:6443",
                        "prom": "10.0.0.2:9090",
                    }
                )
            }
        }
    )
    node = schema.nodes["a"]
    conn = _fake_conn([_fake_listener(54321), _fake_listener(54322)])
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    ports, listeners = await open_local_forwards(conn, node)

    assert ports == {"kubeapi": 54321, "prom": 54322}
    assert len(listeners) == 2
    args_first = conn.forward_local_port.await_args_list[0].args
    assert args_first == ("127.0.0.1", 0, "10.0.0.1", 6443)
    args_second = conn.forward_local_port.await_args_list[1].args
    assert args_second == ("127.0.0.1", 0, "10.0.0.2", 9090)


@pytest.mark.asyncio
async def test_probe_failure_raises_tunnel_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the local probe fails, raise TunnelStartupError with handle in details."""
    schema = InputSchema.model_validate(
        {"nodes": {"a": make_node(remote_targets={"kubeapi": "10.0.0.1:6443"})}}
    )
    node = schema.nodes["a"]
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(return_value=_fake_listener(54321))
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: False)

    with pytest.raises(TunnelStartupError) as exc:
        await open_local_forwards(conn, node)
    assert "local forward did not accept connection" in str(exc.value)
    assert exc.value.details["handle"] == "kubeapi"
    assert exc.value.details["target"] == "10.0.0.1:6443"
    assert exc.value.details["local_port"] == 54321


@pytest.mark.asyncio
async def test_required_node_unreachable_target_raises_tunnel_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required node whose target the far end refuses must fail forward setup.

    The local-accept probe alone cannot see a dead remote target (sshd
    answers a channel-open failure per connection), so `required: true`
    nodes get one far-end probe per target. Its failure is a
    TunnelStartupError so the manager reports it as RequiredTunnelFailure.
    """
    schema = InputSchema.model_validate(
        {"nodes": {"a": make_node(remote_targets={"kubeapi": "10.0.0.1:6443"})}}
    )
    node = schema.nodes["a"]
    assert node.required is True
    conn = MagicMock()
    listener = _fake_listener(54321)
    conn.forward_local_port = AsyncMock(return_value=listener)
    conn.open_connection = AsyncMock(side_effect=asyncssh.ChannelOpenError(1, "Connection refused"))
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    with pytest.raises(TunnelStartupError) as exc:
        await open_local_forwards(conn, node)
    assert "unreachable" in str(exc.value)
    assert "10.0.0.1:6443" in str(exc.value)
    assert exc.value.details["handle"] == "kubeapi"
    assert exc.value.details["local_port"] == 54321
    # The probe failure path must not leak the half-open listener either.
    listener.close.assert_called_once()


@pytest.mark.asyncio
async def test_required_node_probes_target_and_closes_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required node's probe is one channel open to the far end, promptly closed."""
    schema = InputSchema.model_validate(
        {"nodes": {"a": make_node(remote_targets={"kubeapi": "10.0.0.1:6443"})}}
    )
    node = schema.nodes["a"]
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(return_value=_fake_listener(54321))
    writer = MagicMock()
    conn.open_connection = AsyncMock(return_value=(MagicMock(), writer))
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    ports, listeners = await open_local_forwards(conn, node)

    conn.open_connection.assert_awaited_once_with("10.0.0.1", 6443)
    writer.close.assert_called_once()
    assert ports == {"kubeapi": 54321}
    assert len(listeners) == 1


@pytest.mark.asyncio
async def test_optional_node_skips_far_end_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional nodes keep today's startup contract: no far-end round trip at all.

    The local-accept probe still runs (it costs no SSH round trip), but no
    channel is opened to the far end, so an optional node whose target is
    dead still starts successfully and keeps failing at first-byte time.
    """
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": {**make_node(remote_targets={"kubeapi": "10.0.0.1:6443"}), "required": False}
            }
        }
    )
    node = schema.nodes["a"]
    assert node.required is False
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(return_value=_fake_listener(54321))
    conn.open_connection = AsyncMock()
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    ports, _listeners = await open_local_forwards(conn, node)

    conn.open_connection.assert_not_awaited()
    assert ports == {"kubeapi": 54321}


@pytest.mark.asyncio
async def test_forward_failure_cleans_up_previous_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If forward_local_port fails mid-loop, previously opened listeners are closed."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": make_node(
                    remote_targets={
                        "ok": "10.0.0.1:6443",
                        "bad": "10.0.0.2:9090",
                    }
                )
            }
        }
    )
    node = schema.nodes["a"]
    first = _fake_listener(54321)
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(
        side_effect=[first, asyncssh.ChannelOpenError(1, "no route")]
    )
    conn.open_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    with pytest.raises(asyncssh.ChannelOpenError):
        await open_local_forwards(conn, node)
    first.close.assert_called_once()


@pytest.mark.asyncio
async def test_forward_local_port_receives_tracker_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open_local_forwards passes its tracker_factory kwarg through to asyncssh."""
    schema = InputSchema.model_validate(
        {"nodes": {"a": make_node(remote_targets={"p": "10.0.0.1:6443"})}}
    )
    node = schema.nodes["a"]

    received_tracker_factory: list[object] = []

    async def fake_forward_local_port(
        listen_host: str,
        listen_port: int,
        dest_host: str,
        dest_port: int,
        *,
        tracker_factory: object = None,
    ) -> MagicMock:
        received_tracker_factory.append(tracker_factory)
        return _fake_listener(54321)

    conn = MagicMock()
    conn.forward_local_port = AsyncMock(side_effect=fake_forward_local_port)
    conn.open_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    sentinel = object()
    await open_local_forwards(conn, node, tracker_factory=sentinel)

    assert received_tracker_factory == [sentinel]


@pytest.mark.asyncio
async def test_open_connection_agent_fallback_omits_client_keys_and_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no pkey/password and SSH_AUTH_SOCK is set, open_connection must NOT pass
    client_keys or password — letting asyncssh discover the agent itself."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": {
                    "host": "127.0.0.1",
                    "user": "tester",
                    "remote_targets": {"p": "127.0.0.1:6443"},
                }
            }
        }
    )
    node = schema.nodes["a"]
    assert node.ssh_pkey is None
    assert node.ssh_password is None

    captured_kwargs: dict[str, Any] = {}

    async def fake_connect(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return MagicMock(spec=asyncssh.SSHClientConnection)

    with patch("tunstrap.ssh.asyncssh.connect", side_effect=fake_connect):
        await open_connection(node)

    assert "client_keys" not in captured_kwargs
    assert "password" not in captured_kwargs
