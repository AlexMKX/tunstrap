"""asyncssh-backed transport helpers.

This module owns the asyncssh side of the daemon: opening exactly one
``SSHClientConnection`` per node, layering local port forwards on it,
and exposing the same connection so callers (the fetcher) can
multiplex an SFTP channel without a second authentication.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from typing import Any

import asyncssh

from tunstrap.exceptions import TunnelStartupError
from tunstrap.schemas import NodeInput, RemoteTarget


def _probe_local_port(host: str, port: int, timeout: float) -> bool:
    """Open a short TCP probe; True iff the forward is actually accepting."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


# Bound for close_transport's trailing listener drain, in seconds. The
# listener is already closed by then and the connection (when there is one)
# already torn down, so this wait is pure observation; but the
# connection-less path has nothing that would drop an accepted consumer
# whose remote EOF echo never arrives. Five seconds is far above the
# millisecond-scale drain measured after a connection teardown, yet well
# inside the default 10 s shutdown grace.
_LISTENER_DRAIN_TIMEOUT = 5.0


def _load_client_keys(node: NodeInput) -> list[Any] | None:
    """Import the node's ssh_pkey PEM into an asyncssh client key list."""
    if node.ssh_pkey is None:
        return None
    key = asyncssh.import_private_key(node.ssh_pkey, node.ssh_pkey_passphrase)
    return [key]


async def open_connection(node: NodeInput) -> asyncssh.SSHClientConnection:
    """Open exactly one SSH connection per node. No second auth, ever."""
    kwargs: dict[str, Any] = {
        "host": node.host,
        "port": node.port,
        "username": node.user,
        "known_hosts": None,
        "connect_timeout": node.ssh_options.connect_timeout,
        "keepalive_interval": 30,
    }
    client_keys = _load_client_keys(node)
    if client_keys is not None:
        kwargs["client_keys"] = client_keys
    if node.ssh_password is not None:
        kwargs["password"] = node.ssh_password
    if node.ssh_options.compression:
        kwargs["compression_algs"] = ("zlib@openssh.com", "zlib")
    return await asyncssh.connect(**kwargs)


async def _probe_required_target(
    conn: asyncssh.SSHClientConnection,
    handle: str,
    target: RemoteTarget,
    local_port: int,
    timeout: float,
) -> None:
    """Open one channel to a required forward's far end; fail if unreachable.

    The local-accept probe cannot see a dead remote target: the kernel
    completes the loopback handshake regardless, and the SSH server answers
    a channel-open failure only per connection (issue #51). This probe
    costs one channel-open round trip per target and raises
    ``TunnelStartupError`` -- the existing vocabulary the manager maps to
    ``RequiredTunnelFailure`` for required nodes. Optional nodes never
    call it.
    """
    try:
        _reader, writer = await asyncio.wait_for(
            conn.open_connection(target.host, target.port), timeout=timeout
        )
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
        raise TunnelStartupError(
            f"required forward target unreachable: {target.host}:{target.port}",
            {
                "handle": handle,
                "target": f"{target.host}:{target.port}",
                "local_port": local_port,
                "reason": str(exc),
            },
        ) from exc
    # Reachability is proven at channel open; closing the writer hands the
    # channel back without waiting on the peer's close confirmation.
    writer.close()


async def open_local_forwards(
    conn: asyncssh.SSHClientConnection,
    node: NodeInput,
    tracker_factory: Callable[[], asyncssh.SSHForwardTracker] | None = None,
) -> tuple[dict[str, int], list[asyncssh.SSHListener]]:
    """Open one direct-tcpip forward per remote_target.

    If ``tracker_factory`` is provided, asyncssh calls it once per accepted
    connection to build a per-connection tracker whose hooks observe that
    connection's lifecycle. Used by the daemon for idle-based auto-shutdown.

    Returns ``(handle->local_port, listeners)``. Local bind host is always
    ``127.0.0.1``; the listen port is OS-assigned. ``target.host`` is the
    remote-side address (resolved on the SSH server).

    Every forward is probed for local accept; for ``required`` nodes the
    far end is probed too, so ``start`` cannot report success for a
    target the SSH server cannot reach. Optional nodes are probed
    locally only, keeping their startup cost and failure modes exactly
    as before.
    """
    ports: dict[str, int] = {}
    listeners: list[asyncssh.SSHListener] = []
    timeout = float(node.ssh_options.connect_timeout)

    try:
        for handle, target in node.remote_targets.items():
            listener = await conn.forward_local_port(
                "127.0.0.1",
                0,
                target.host,
                target.port,
                tracker_factory=tracker_factory,
            )
            listeners.append(listener)
            actual_port = listener.get_port()
            if not _probe_local_port("127.0.0.1", actual_port, timeout):
                raise TunnelStartupError(
                    "local forward did not accept connection",
                    {
                        "handle": handle,
                        "target": f"{target.host}:{target.port}",
                        "local_port": actual_port,
                    },
                )
            if node.required:
                await _probe_required_target(conn, handle, target, actual_port, timeout)
            ports[handle] = actual_port
    except BaseException:
        # Caller never sees the listeners on failure; cleanup must cover
        # KeyboardInterrupt / CancelledError to avoid leaking SSH channels.
        # Re-raised immediately so the failure propagates intact.
        await close_transport(None, listeners)
        raise
    return ports, listeners


async def close_transport(
    conn: asyncssh.SSHClientConnection | None,
    listeners: list[asyncssh.SSHListener],
) -> None:
    """Best-effort teardown: close listeners, tear the connection down, then wait.

    The order is load-bearing (issue #50). On Python >= 3.12.1 the asyncio
    ``Server.wait_closed`` that an asyncssh forward listener awaits does not
    return while an accepted consumer connection is still open, and asyncssh
    holds that connection open until the SSH channel under it closes -- which
    only the connection teardown does. Awaiting listener quiescence first
    therefore wedges every stop issued while a consumer is connected (the
    consumer sockets only die once the connection does), and ``stop``
    escalates to SIGKILL. Closing the listeners (synchronously, so new
    consumers are refused immediately), then the connection (whose cleanup
    closes every channel and with it each forwarder's consumer transport --
    the same order asyncssh's own tunnel-loss path takes), makes both waits
    complete promptly afterwards.

    The trailing listener drain is bounded by ``_LISTENER_DRAIN_TIMEOUT``:
    after the connection teardown it is pure observation, but the
    connection-less path (forward-setup failure) has no connection to drop
    an accepted consumer whose remote EOF echo never arrives -- asyncssh's
    forwarder holds the local transport open until that echo -- so an
    unbounded drain could wedge teardown again, issue #50 through a
    narrower door. The connection's own ``wait_closed`` stays unbounded
    because a forced close completes locally, without any peer round trip.

    In-flight forwarded data gets the same treatment asyncssh gives an
    application-initiated disconnect: the per-channel close flushes each
    channel's unsent buffer before closing, and no wait is placed on a peer
    that may never let go.

    Teardown must never raise and must never hang; partial cleanup is
    preferable to leaving the asyncio loop with a dangling channel.
    """
    for lst in listeners:
        try:
            lst.close()
        except (asyncssh.Error, OSError):
            continue
    if conn is not None:
        try:
            conn.close()
            await conn.wait_closed()
        except (asyncssh.Error, OSError):
            pass
    for lst in listeners:
        try:
            await asyncio.wait_for(lst.wait_closed(), timeout=_LISTENER_DRAIN_TIMEOUT)
        except (asyncssh.Error, OSError, asyncio.TimeoutError):
            continue
