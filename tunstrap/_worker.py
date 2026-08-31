"""Worker process entry point. Invoked via ``python -m tunstrap._worker``.

Reads ``InputSchema`` JSON from stdin, acquires its session lock via
``SessionDir.create``, runs ``TunnelManager.start_all_and_build_output``,
writes the IPC message to ``--ipc-fd``, then blocks on signals.

This module is not part of the public CLI surface.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import signal
import sys
from typing import Any

import asyncssh
from pydantic import ValidationError

from tunstrap.activity import ActivityTracker
from tunstrap.exceptions import DaemonError, SessionActive
from tunstrap.fdio import ShortWriteError, write_all
from tunstrap.manager import TunnelManager
from tunstrap.schemas import ErrorOutput, InputSchema
from tunstrap.session import SessionDir, SessionError

_SCHEMA_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB is more than enough for any sane input

_TERMINAL_LOSS = (
    "required tunnel lost: the daemon terminated itself and released its session lock; "
    "session data is preserved"
)
_DEGRADED_LOSS = (
    "non-required tunnel lost: the daemon is degraded and still running; "
    "the remaining forwards are unaffected"
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse worker-only arguments so the public CLI remains the user interface."""
    parser = argparse.ArgumentParser(prog="tunstrap._worker", add_help=False)
    parser.add_argument("--ipc-fd", type=int, required=True)
    parser.add_argument("--session-dir", default=None)
    parser.add_argument("--generated-session-dir", action="store_true")
    args = parser.parse_args(argv)
    if args.generated_session_dir and args.session_dir is None:
        parser.error("--generated-session-dir requires --session-dir")
    return args


async def _idle_watchdog(
    tracker: ActivityTracker,
    timeout_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    """Poll the tracker every timeout/4s; set stop_event when idle past threshold.

    Cancellation-safe: returns cleanly on CancelledError so the cleanup
    finally-block in `_run` can await the task without raising.
    """
    poll_interval = max(1.0, timeout_seconds / 4)
    while not stop_event.is_set():
        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return
        if tracker.is_idle and tracker.seconds_since_activity >= timeout_seconds:
            stop_event.set()
            return


@dataclasses.dataclass(frozen=True)
class _NodeLoss:
    """One node whose SSH connection dropped *after* a successful startup."""

    node: str
    required: bool


@dataclasses.dataclass(frozen=True)
class _LossWatch:
    """State every tunnel-loss watchdog shares: where to stop, record and accumulate.

    Bundled rather than passed as four more parameters so each watchdog keeps a
    signature that names only what varies per node. ``losses`` is deliberately
    mutable on a frozen dataclass: the container is fixed, its contents are the
    running record.
    """

    stop_event: asyncio.Event
    session: SessionDir
    losses: list[_NodeLoss]


def _record_losses(watch: _LossWatch) -> None:
    """Write the cumulative loss record into tunnel-data/. Never raises.

    Runs synchronously on the event-loop thread with no ``await`` inside, so two
    watchdogs firing in the same tick cannot interleave a partial list, and the
    write itself is atomic (``SessionDir.write_tunnel_loss``), so no reader ever
    observes half an object.

    A failure to record must not stop the daemon from exiting: the exit is the
    fix for issue #33, the record is only its explanation. ``SessionError`` is
    caught alongside ``OSError`` because ``materialize_atomic`` raises it when
    ``tunnel-data`` has been substituted underneath us — precisely the moment
    when refusing to shut down would be worst.
    """
    terminal = any(loss.required for loss in watch.losses)
    payload: dict[str, object] = {
        "self_terminated": terminal,
        "reason": _TERMINAL_LOSS if terminal else _DEGRADED_LOSS,
        "losses": [{"node": loss.node, "required": loss.required} for loss in watch.losses],
    }
    try:
        watch.session.write_tunnel_loss(payload)
    except (OSError, SessionError):
        pass


async def _tunnel_loss_watchdog(
    node: str,
    conn: asyncssh.SSHClientConnection,
    required: bool,
    watch: _LossWatch,
) -> None:
    """Await one node's connection loss; set stop_event when that node was required.

    The structural sibling of ``_idle_watchdog`` -- a task racing the same
    ``stop_event`` that ``_run``'s shutdown wait blocks on -- but event-driven
    rather than polled, because asyncssh already publishes the exact edge we
    need. ``SSHConnection._cleanup`` is the single routine that closes every
    local listener (the ``connection refused`` a consumer sees) and then sets
    the connection's close event, so ``conn.wait_closed()`` returns on exactly
    the transition that made the tunnel useless, whatever caused it: a keepalive
    timeout, a peer disconnect or a transport error. Polling the local listener
    instead would be strictly worse -- it would add latency and could not
    distinguish "asyncssh tore the forward down" from "the port is momentarily
    busy".

    One task per node rather than one task racing every connection: ``required``
    is per node, so the decision this coroutine makes is per node too, and an
    ``asyncio.wait`` fan-in would have to re-derive which connection completed.

    Cancellation-safe in the same way ``_idle_watchdog`` is, and for the same
    reason: ``_run``'s cleanup cancels these tasks *before* ``stop_all`` closes
    the connections, so a normal shutdown must not be recorded as a loss.
    """
    try:
        await conn.wait_closed()
    except asyncio.CancelledError:
        return
    watch.losses.append(_NodeLoss(node=node, required=required))
    _record_losses(watch)
    if required:
        watch.stop_event.set()


def _dispose_session(session: SessionDir, losses: list[_NodeLoss]) -> None:
    """Delete the session data on a normal stop; preserve it on required-tunnel loss.

    Issue #33, decision 1. ``cleanup`` is *not* called on the loss path — that
    is the whole guarantee, expressed as the single branch that reaches it — so
    neither ``tunnel-data`` nor a generated session root is ever removed when
    the daemon terminated itself. Only ``release_lock_preserving_data`` runs,
    which drops the flock and touches nothing else.

    The condition is ``any(loss.required)`` and not "were there any losses at
    all", because a non-required loss never terminates the daemon: reaching
    here with only those means the daemon was stopped normally afterwards, and a
    normal stop still cleans up.
    """
    if any(loss.required for loss in losses):
        session.release_lock_preserving_data()
        return
    session.cleanup()


def _read_schema_from_stdin() -> InputSchema:
    """Read the schema JSON from stdin (parent has closed its write end)."""
    raw = sys.stdin.buffer.read()
    if len(raw) > _SCHEMA_MAX_BYTES:
        raise DaemonError("schema pipe exceeded size limit", {"limit": _SCHEMA_MAX_BYTES})
    return InputSchema.model_validate_json(raw.decode("utf-8"))


def _write_message(fd: int, message: dict[str, Any]) -> None:
    """Finish partial writes so the parent receives a complete IPC frame."""
    payload = (json.dumps(message) + "\n").encode("utf-8")
    try:
        write_all(fd, payload)
    except ShortWriteError as exc:
        raise DaemonError("short write to IPC pipe", {"remaining": exc.remaining}) from exc


def _report_pre_run_failure(ipc_fd: int, exc: BaseException) -> None:
    """Best-effort: write a daemon_error frame so parent does not block on empty pipe."""
    err = (
        exc
        if isinstance(exc, DaemonError)
        else DaemonError("worker failed before reporting", {"type": type(exc).__name__})
    )
    try:
        _write_message(ipc_fd, {"kind": "daemon_error", "payload": err.to_error_output()})
    except OSError:
        pass


async def _run(args: argparse.Namespace, session: SessionDir) -> int:
    """Run the detached daemon and return a status the parent can map reliably."""
    try:
        schema = _read_schema_from_stdin()
    except (DaemonError, ValidationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _report_pre_run_failure(args.ipc_fd, exc)
        try:
            os.close(args.ipc_fd)
        except OSError:
            pass
        session.cleanup()
        return 4

    manager = TunnelManager(schema, session=session if schema.daemon.materialize else None)

    try:
        result = await manager.start_all_and_build_output(
            pid=os.getpid(), session_dir=session.session_dir
        )
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Worker top-level guard: any uncaught failure here must reach the
        # parent as a `daemon_error` IPC frame, otherwise the parent blocks
        # forever on an empty pipe.
        await manager.stop_all()
        _report_pre_run_failure(args.ipc_fd, exc)
        try:
            os.close(args.ipc_fd)
        except OSError:
            pass
        session.cleanup()
        return 4

    if isinstance(result, ErrorOutput):
        await manager.stop_all()
        _write_message(
            args.ipc_fd,
            {"kind": "required_failure", "payload": result.model_dump(mode="json")},
        )
        os.close(args.ipc_fd)
        session.cleanup()
        return 2

    # result is OutputSchema here: start_all_and_build_output returns
    # OutputSchema | ErrorOutput, and the ErrorOutput branch above returns 2,
    # so mypy narrows the union to OutputSchema without a runtime check.
    _write_message(
        args.ipc_fd,
        {"kind": "success", "payload": result.model_dump(mode="json")},
    )
    os.close(args.ipc_fd)

    await _supervise(schema, manager, session)
    return 0


async def _supervise(schema: InputSchema, manager: TunnelManager, session: SessionDir) -> None:
    """Block until something asks the daemon to stop, then tear down and dispose.

    Three things can end the wait, and they all resolve to the same
    ``stop_event``: a signal, the idle watchdog, and — new in issue #33 — a
    tunnel-loss watchdog per started node. Before this, ``await
    stop_event.wait()`` was reachable only by signal or idle timeout, so a
    worker whose SSH connection died kept its PID alive and its session lock
    held forever while every local listener was already closed.

    Order in the cleanup is load-bearing: the watchdogs are cancelled *before*
    ``stop_all`` closes the connections, because ``stop_all`` would otherwise
    complete every ``wait_closed()`` and a clean shutdown would record itself as
    a tunnel loss.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    watch = _LossWatch(stop_event=stop_event, session=session, losses=[])
    watchdogs: list[asyncio.Task[None]] = []
    if schema.daemon.auto_stop_idle_seconds is not None:
        watchdogs.append(
            asyncio.create_task(
                _idle_watchdog(
                    tracker=manager.activity_tracker,
                    timeout_seconds=schema.daemon.auto_stop_idle_seconds,
                    stop_event=stop_event,
                )
            )
        )
    watchdogs.extend(
        asyncio.create_task(_tunnel_loss_watchdog(name, conn, required, watch))
        for name, conn, required in manager.live_nodes()
    )

    try:
        await stop_event.wait()
    finally:
        for task in watchdogs:
            task.cancel()
        for task in watchdogs:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await manager.stop_all()
        _dispose_session(session, watch.losses)


def main(argv: list[str] | None = None) -> None:
    """Worker entry: create+lock session dir, run loop, clean up, exit."""
    args = _parse_args(argv)
    try:
        session = SessionDir.create(
            supplied=args.session_dir,
            owns_supplied_root=args.generated_session_dir,
        )
    except SessionActive as exc:
        try:
            _write_message(
                args.ipc_fd,
                {"kind": "session_active", "payload": exc.to_error_output()},
            )
            os.close(args.ipc_fd)
        except OSError:
            pass
        os._exit(3)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        _report_pre_run_failure(args.ipc_fd, exc)
        os._exit(4)

    try:
        session.write_identity(pid=os.getpid())
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        session.cleanup()
        _report_pre_run_failure(args.ipc_fd, exc)
        os._exit(4)

    rc = asyncio.run(_run(args, session))
    os._exit(rc)


if __name__ == "__main__":  # pragma: no cover - exercised by integration test
    main()
