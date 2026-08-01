"""Typed exception hierarchy with stable exit-code mapping."""

from __future__ import annotations

from typing import Any

_SECRET_KEYS = frozenset({"ssh_pkey", "ssh_password", "ssh_pkey_passphrase"})


def _scrub(value: Any) -> Any:
    """Return a copy of values with SSH secret keys removed at every depth."""
    if isinstance(value, dict):
        return {key: _scrub(nested) for key, nested in value.items() if key not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_scrub(nested) for nested in value]
    return value


class TunstrapError(Exception):
    """Base class for every error this tool reports as structured JSON."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        """Store message and a scrubbed copy of details for JSON output."""
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = _scrub(details or {})

    def to_error_output(self) -> dict[str, Any]:
        """Serialise the exception into the public ErrorOutput dict shape."""
        return {
            "error": type(self).__name__,
            "message": self.message,
            "details": self.details,
        }


class SchemaValidationError(TunstrapError):
    """Input JSON failed pydantic validation; details carries errors()."""


class TunnelStartupError(TunstrapError):
    """A single node failed to open its transport or local forward."""


class RequiredTunnelFailure(TunstrapError):
    """At least one required node could not be started; the daemon aborts."""


class DaemonError(TunstrapError):
    """Generic daemon-side failure surfaced via the IPC handshake."""


class DaemonHandshakeError(DaemonError):
    """The *parent* could not complete the handshake with a worker it launched.

    The distinction from ``DaemonError`` is **who failed**, and it decides
    whether a daemon is left running. A ``daemon_error`` IPC frame is
    worker-authored: the worker reached its own guard, released the session
    lock and removed its session dir before reporting, then exited — nothing
    survives it. This one is raised only past the point where
    ``subprocess.Popen`` has already detached the worker, so the worker may be
    perfectly healthy, holding the session lock with tunnels open, while the
    parent is the side that failed. Callers must stop it rather than delete its
    session directory and walk away.

    A subclass so that every existing ``except DaemonError`` keeps working; it
    needs its own ``_EXIT_CODES`` entry because ``exit_code_for`` keys on the
    exact type.
    """


class KubeParseError(TunstrapError):
    """A kubeconfig could not be parsed or lacked a usable current-context."""


class SessionActive(TunstrapError):
    """A daemon session is already running; a second start is rejected."""


class MultiNodeEnvUnsupported(TunstrapError):
    """A multi-node result cannot be rendered as TUNSTRAP_* scalars.

    ``TUNSTRAP_<TARGET>_*`` has no node dimension, so two nodes with a
    same-named target collide irreducibly. ``run`` requires ``--output-var``
    (which is keyed by node) for multi-node input and raises this
    **before spawning**, so the rejection can never orphan a daemon.
    """


_EXIT_CODES: dict[type[TunstrapError], int] = {
    SchemaValidationError: 1,
    MultiNodeEnvUnsupported: 1,
    RequiredTunnelFailure: 2,
    KubeParseError: 2,
    SessionActive: 3,
    DaemonError: 4,
    DaemonHandshakeError: 4,
}


def exit_code_for(exc: TunstrapError) -> int:
    """Map a domain exception to its CLI exit code; default to 1."""
    return _EXIT_CODES.get(type(exc), 1)
