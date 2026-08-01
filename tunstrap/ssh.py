"""asyncssh-backed transport helpers.

This module owns the asyncssh side of the daemon: opening exactly one
``SSHClientConnection`` per node, layering local port forwards on it,
and exposing the same connection so callers (the fetcher) can
multiplex an SFTP channel without a second authentication.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import asyncssh

from tunstrap.exceptions import TunnelStartupError
from tunstrap.schemas import NodeInput


def _probe_local_port(host: str, port: int, timeout: float) -> bool:
    """Open a short TCP probe; True iff the forward is actually accepting."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


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
            ports[handle] = actual_port
    except BaseException:  # pylint: disable=broad-exception-caught
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
    """Best-effort teardown: close listeners then the connection.

    Teardown must never raise; partial cleanup is preferable to leaving the
    asyncio loop with a dangling channel.
    """
    for lst in listeners:
        try:
            lst.close()
            await lst.wait_closed()
        except (asyncssh.Error, OSError):
            continue
    if conn is not None:
        try:
            conn.close()
            await conn.wait_closed()
        except (asyncssh.Error, OSError):
            pass
