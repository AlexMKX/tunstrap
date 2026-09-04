"""close_transport teardown vs a held consumer connection (issue #50).

On Python >= 3.12.1 the asyncio ``Server.wait_closed`` that an asyncssh
forward listener awaits does not return while an accepted consumer
connection is still open, and asyncssh holds that connection open until
the SSH channel under it closes. A teardown that awaits listener
quiescence *before* tearing the connection down therefore cannot complete
while any consumer is connected: the stop escalates to SIGKILL.

These tests rebuild that structure with real asyncio primitives -- a real
server with a genuinely held client socket, and a connection double whose
``close()`` drops the accepted transports exactly like asyncssh's channel
teardown does -- and pin the behavioural property: teardown completes
anyway, and the held consumer is actually dropped rather than left
dangling.
Code: tunstrap/ssh.py::close_transport
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from tunstrap.ssh import close_transport

pytestmark = pytest.mark.unit


class _ForwardListener:
    """Listener double with asyncssh SSHForwardListener close semantics.

    ``close()`` stops the server's accept loop synchronously; ``wait_closed()``
    awaits the server's quiescence -- on Python >= 3.12.1 that means "closed
    AND every accepted connection dropped", which is the gate the defect
    hides behind.
    """

    def __init__(self, server: asyncio.AbstractServer) -> None:
        self._server = server

    def close(self) -> None:
        """Stop accepting, keeping accepted connections open (as the real one does)."""
        self._server.close()

    async def wait_closed(self) -> None:
        """Await full server quiescence: closed and no live accepted connections."""
        await self._server.wait_closed()


class _FakeSSHConnection:
    """Connection double mirroring asyncssh's teardown causality.

    ``close()`` is the load-bearing part: it drops every transport the
    listener accepted (what a channel teardown does to a forwarder's
    consumer socket) before setting the close event that ``wait_closed()``
    awaits. That is the order asyncssh's own connection cleanup takes:
    channels first, close event last -- which is why the tunnel-loss path
    never wedged (the mirror image of this bug).
    """

    def __init__(self) -> None:
        self._transports: list[asyncio.WriteTransport] = []
        self._accepted = asyncio.Event()
        self._closed = asyncio.Event()

    def track(self, transport: asyncio.WriteTransport) -> None:
        """Record one accepted consumer transport, mirroring a live channel."""
        self._transports.append(transport)
        self._accepted.set()

    async def wait_accepted(self) -> None:
        """Block until at least one consumer transport has been tracked."""
        await self._accepted.wait()

    def close(self) -> None:
        """Drop every tracked consumer transport, then mark the connection closed."""
        for transport in self._transports:
            transport.close()
        self._closed.set()

    async def wait_closed(self) -> None:
        """Await the closed event, as SSHClientConnection.wait_closed does."""
        await self._closed.wait()


async def test_teardown_completes_while_consumer_connection_is_held() -> None:
    """A held consumer connection must neither block nor survive close_transport."""
    conn = _FakeSSHConnection()
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    async def _hold_consumer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Server side of the held connection: track it, then wait for its death."""
        conn.track(writer.transport)
        await reader.read()  # returns only when the transport actually closes

    server = await asyncio.start_server(_hold_consumer, "127.0.0.1", 0)
    listener = _ForwardListener(server)
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        held.connect(server.sockets[0].getsockname())
        await conn.wait_accepted()

        await asyncio.wait_for(close_transport(conn, [listener]), timeout=5)

        # Not just "returned": the consumer must have been dropped, otherwise
        # the teardown left a half-open socket behind (an fd leak, issue #50
        # invariant 4).
        held.settimeout(5)
        assert held.recv(1) == b"", "consumer connection survived teardown"
    finally:
        held.close()
        server.close()
        await server.wait_closed()


async def test_teardown_swallows_close_and_wait_errors() -> None:
    """The best-effort contract: close/wait failures never escape close_transport."""
    listener = MagicMock(spec=["close", "wait_closed"])
    listener.close = MagicMock(side_effect=OSError("listener close boom"))
    listener.wait_closed = AsyncMock(side_effect=OSError("listener wait boom"))
    conn = MagicMock(spec=["close", "wait_closed"])
    conn.close = MagicMock(side_effect=OSError("conn close boom"))
    conn.wait_closed = AsyncMock(side_effect=OSError("conn wait boom"))

    await close_transport(conn, [listener])  # must not raise


async def test_teardown_without_connection_is_bounded_when_consumer_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection-less teardown path cannot hang on a consumer that never leaves.

    ``open_local_forwards``' failure path calls ``close_transport`` with no
    connection: nothing then exists to drop an accepted consumer whose
    remote EOF echo never arrives (asyncssh's forwarder deliberately holds
    the local transport open until that echo), so the trailing listener
    drain must be bounded by ``_LISTENER_DRAIN_TIMEOUT`` rather than by
    the peer's behaviour. A CI run on Python 3.12 caught exactly this
    shape -- and on 3.13+ the same state only resolves when the discarded
    server-side writer is garbage-collected, which is luck, not a
    guarantee. The consumer here never closes and its server-side writer
    is kept referenced, so no interpreter can resolve it by accident.
    """
    monkeypatch.setattr("tunstrap.ssh._LISTENER_DRAIN_TIMEOUT", 0.2, raising=False)
    accepted: list[asyncio.StreamWriter] = []

    def _hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)

    server = await asyncio.start_server(_hold, "127.0.0.1", 0)
    listener = _ForwardListener(server)
    stuck = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stuck.connect(server.sockets[0].getsockname())
    await asyncio.sleep(0.05)  # let the accept land; both ends then stay open
    try:
        await asyncio.wait_for(close_transport(None, [listener]), timeout=2)
        assert not server.is_serving(), "listener was not closed"
    finally:
        stuck.close()
        for writer in accepted:
            writer.close()
        server.close()
        await server.wait_closed()
