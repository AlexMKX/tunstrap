"""Tunnel orchestration on asyncssh: per-node connection + forwards + SFTP."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone

import asyncssh

from tunstrap.activity import ActivityTracker
from tunstrap.exceptions import RequiredTunnelFailure, TunnelStartupError
from tunstrap.fetcher import fetch_files
from tunstrap.kube import default_san_probe, run_kube_targets
from tunstrap.schemas import (
    ErrorOutput,
    FetchedFile,
    InputSchema,
    KubeTargetOutput,
    NodeOutput,
    OutputSchema,
    TunnelWarning,
)
from tunstrap.session import SessionDir
from tunstrap.ssh import close_transport, open_connection, open_local_forwards

# Errors we expect from a remote SSH peer, local sshd handshake, or
# from loading inline client material. Anything outside this tuple is a
# program bug and must propagate, not be flattened into a node failure.
_NODE_STARTUP_ERRORS: tuple[type[BaseException], ...] = (
    asyncssh.Error,
    asyncssh.KeyImportError,
    OSError,
    asyncio.TimeoutError,
    TunnelStartupError,
)


@dataclass
class _NodeRuntime:
    """Per-node bookkeeping shared between start_all and stop_all."""

    name: str
    success: bool
    ports: dict[str, int] = field(default_factory=dict)
    fetched_files: dict[str, FetchedFile] = field(default_factory=dict)
    conn: asyncssh.SSHClientConnection | None = None
    listeners: list[asyncssh.SSHListener] = field(default_factory=list)
    error: str | None = None
    kube_targets: dict[str, KubeTargetOutput] = field(default_factory=dict)
    kube_warnings: list[TunnelWarning] = field(default_factory=list)


class TunnelManager:
    """Orchestrate asyncssh transports + fetch_files for an InputSchema."""

    def __init__(self, schema: InputSchema, session: SessionDir | None = None) -> None:
        """Store the parsed input schema and optional SessionDir for materialize."""
        self._schema = schema
        self._session = session
        self._runtimes: list[_NodeRuntime] = []
        self.activity_tracker = ActivityTracker()

    async def stop_all(self) -> None:
        """Close every active listener and connection, best-effort."""
        runtimes = list(self._runtimes)
        self._runtimes.clear()
        for runtime in runtimes:
            await close_transport(runtime.conn, runtime.listeners)

    async def start_all_and_build_output(
        self,
        *,
        pid: int,
        session_dir: str,
    ) -> OutputSchema | ErrorOutput:
        """Open every node concurrently; build OutputSchema or ErrorOutput."""
        results = await self._start_all()
        failed_required = [
            r for r in results if not r.success and self._schema.nodes[r.name].required
        ]
        if failed_required:
            await self.stop_all()
            exc = RequiredTunnelFailure(
                "required tunnel(s) failed to start",
                {
                    "failed": [
                        {"node": r.name, "error": r.error or "unknown"} for r in failed_required
                    ],
                },
            )
            return ErrorOutput(
                error=type(exc).__name__,
                message=exc.message,
                details=exc.details,
            )

        connections: dict[str, NodeOutput] = {
            r.name: NodeOutput(
                ports=r.ports,
                fetch_files=r.fetched_files,
                kube_targets=r.kube_targets,
            )
            for r in results
            if r.success
        }
        warnings = [
            TunnelWarning(node=r.name, error=r.error or "unknown error")
            for r in results
            if not r.success and not self._schema.nodes[r.name].required
        ]
        for r in results:
            if r.success:
                warnings.extend(r.kube_warnings)
        return OutputSchema(
            connections=connections,
            pid=pid,
            session_dir=session_dir,
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            warnings=warnings,
        )

    async def _start_all(self) -> list[_NodeRuntime]:
        """Start every node in the schema concurrently and return their runtimes."""
        names = list(self._schema.nodes.keys())
        coros = [self._start_one(name) for name in names]
        return await asyncio.gather(*coros)

    async def _start_one(self, name: str) -> _NodeRuntime:
        """Open one node end-to-end: connection, local forwards, optional fetch."""
        node = self._schema.nodes[name]
        runtime = _NodeRuntime(name=name, success=False)
        try:
            runtime.conn = await open_connection(node)
        except _NODE_STARTUP_ERRORS as exc:
            runtime.error = str(exc)
            return runtime

        try:
            entries, listeners = await open_local_forwards(
                runtime.conn, node, tracker_factory=self.activity_tracker.make_tracker
            )
        except _NODE_STARTUP_ERRORS as exc:
            runtime.error = str(exc)
            await close_transport(runtime.conn, [])
            runtime.conn = None
            return runtime
        runtime.ports = entries
        runtime.listeners = listeners

        if node.fetch_files:
            try:
                fetched, required_failures = await fetch_files(runtime.conn, node.fetch_files)
            except _NODE_STARTUP_ERRORS as exc:
                runtime.error = str(exc)
                await close_transport(runtime.conn, runtime.listeners)
                runtime.conn = None
                runtime.listeners = []
                return runtime
            runtime.fetched_files = fetched
            if self._session is not None:
                self._materialize_fetch_files(name, runtime.fetched_files)
            if required_failures:
                runtime.error = f"required fetch_files failed: {required_failures}"
                await close_transport(runtime.conn, runtime.listeners)
                runtime.conn = None
                runtime.listeners = []
                return runtime

        if node.kube_targets:
            try:
                kube_out, kube_required, kube_warn = await run_kube_targets(
                    runtime.conn,
                    node.kube_targets,
                    connect_timeout=node.ssh_options.connect_timeout,
                    probe=default_san_probe,
                    node_name=name,
                )
            except _NODE_STARTUP_ERRORS as exc:
                runtime.error = str(exc)
                await close_transport(runtime.conn, runtime.listeners)
                runtime.conn = None
                runtime.listeners = []
                return runtime
            runtime.kube_targets = kube_out
            runtime.kube_warnings = kube_warn
            if self._session is not None:
                self._materialize_kube_targets(name, runtime.kube_targets)
            if kube_required:
                runtime.error = f"required kube_targets failed: {kube_required}"
                await close_transport(runtime.conn, runtime.listeners)
                runtime.conn = None
                runtime.listeners = []
                return runtime

        runtime.success = True
        self._runtimes.append(runtime)
        return runtime

    def _materialize_kube_targets(
        self, node_name: str, kube_out: dict[str, KubeTargetOutput]
    ) -> None:
        """Write each kube target's patched kubeconfig to tunnel-data/<node>-<name>, set .path."""
        assert self._session is not None
        for kname, kout in kube_out.items():
            path = self._session.materialize(
                f"{node_name}-{kname}", base64.b64decode(kout.content_b64)
            )
            kube_out[kname] = kout.model_copy(update={"path": path})

    def _materialize_fetch_files(self, node_name: str, fetched: dict[str, FetchedFile]) -> None:
        """Write each successfully fetched file to tunnel-data/<node>-<name>, set .path.

        Mirrors the kube materialization step above, using the atomic-replace
        primitive (not the mode-fixed-but-non-atomic ``materialize``) because
        this write has no live SessionDir-owning caller to serialize behind.
        A failed fetch (``.error`` set) materializes nothing.
        """
        assert self._session is not None
        for fname, ff in fetched.items():
            if ff.error is not None or ff.content_b64 is None:
                continue
            path = self._session.materialize_atomic(
                f"{node_name}-{fname}", base64.b64decode(ff.content_b64)
            )
            fetched[fname] = ff.model_copy(update={"path": path})
