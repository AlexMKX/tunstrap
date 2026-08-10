"""Forward-connection activity tracker for idle-based auto-stop.

asyncssh calls a ``tracker_factory`` once per accepted forward connection
and expects it to return a fresh :class:`asyncssh.SSHForwardTracker`. We
split that into a pair:

* :class:`ActivityTracker` owns the daemon-wide aggregate state (active
  connection count + last-activity timestamp) and acts as the factory via
  :meth:`ActivityTracker.make_tracker`.
* :class:`_IdleConnectionTracker` is the lightweight per-connection object
  asyncssh drives; it reports open/close back into the shared aggregate.

One :class:`ActivityTracker` is owned by
:class:`~tunstrap.manager.TunnelManager` and its ``make_tracker``
bound method is passed as ``tracker_factory`` to every
``forward_local_port`` call, so all node forwards feed a single counter.
"""

from __future__ import annotations

import time

import asyncssh


class _IdleConnectionTracker(asyncssh.SSHPortForwardTracker):
    """Per-connection tracker that reports open/close to a shared aggregate.

    asyncssh instantiates one of these (via the factory) for each accepted
    TCP forward connection. All hooks run on the asyncio loop thread; no
    locking is needed. Byte hooks are intentionally not overridden;
    inherited no-op hooks avoid touching aggregate state on the data path.
    """

    def __init__(self, aggregate: ActivityTracker) -> None:
        """Bind each AsyncSSH tracker to its shared idle accounting state."""
        self._aggregate = aggregate
        self._opened = False
        self._closed = False

    # asyncssh.SSHPortForwardTracker hooks --------------------------------

    def connection_made(
        self, forwarder: asyncssh.SSHForwarder, orig_host: str, orig_port: int
    ) -> None:
        """asyncssh hook: a new client TCP connection was accepted."""
        del forwarder, orig_host, orig_port  # Per-connection detail unused.
        if self._opened:
            return
        self._opened = True
        self._aggregate.note_connection_made()

    def connection_lost(self, exc: Exception | None) -> None:
        """asyncssh hook: this connection has closed (clean or with error)."""
        del exc  # Close reason unused for aggregate idle accounting.
        if self._closed or not self._opened:
            return
        self._closed = True
        self._aggregate.note_connection_lost()


class ActivityTracker:
    """Aggregate counter + last-activity timestamp shared across all forwards.

    Acts as the ``tracker_factory`` for asyncssh via :meth:`make_tracker`.
    All mutations run on the asyncio loop thread; no locking needed.

    Public properties ``is_idle`` and ``seconds_since_activity`` answer
    the daemon-wide question "should auto-stop fire now?".
    """

    def __init__(self) -> None:
        """Start idle accounting at construction so an unused daemon can expire."""
        self._active_count = 0
        self._last_activity_at = time.monotonic()

    # tracker_factory ------------------------------------------------------

    def make_tracker(self) -> asyncssh.SSHPortForwardTracker:
        """Factory: build a per-connection tracker bound to this aggregate."""
        return _IdleConnectionTracker(self)

    # Aggregate mutation API (called by _IdleConnectionTracker) ------------

    def note_connection_made(self) -> None:
        """Record that one forward connection has opened."""
        self._active_count += 1
        self._last_activity_at = time.monotonic()

    def note_connection_lost(self) -> None:
        """Record that one forward connection has closed (clamped at zero)."""
        self._active_count = max(0, self._active_count - 1)
        self._last_activity_at = time.monotonic()

    # Aggregate query API --------------------------------------------------

    @property
    def is_idle(self) -> bool:
        """True iff there are zero active forward connections right now."""
        return self._active_count == 0

    @property
    def seconds_since_activity(self) -> float:
        """Monotonic-clock seconds since the most recent open/close event."""
        return time.monotonic() - self._last_activity_at
