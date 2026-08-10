"""Worker process entry point. Invoked via ``python -m tunstrap._worker``.

Reads ``InputSchema`` JSON from stdin, acquires its session lock via
``SessionDir.create``, runs ``TunnelManager.start_all_and_build_output``,
writes the IPC message to ``--ipc-fd``, then blocks on signals.

This module is not part of the public CLI surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from typing import Any

from pydantic import ValidationError

from tunstrap.activity import ActivityTracker
from tunstrap.exceptions import DaemonError, SessionActive
from tunstrap.manager import TunnelManager
from tunstrap.schemas import ErrorOutput, InputSchema
from tunstrap.session import SessionDir

_SCHEMA_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB is more than enough for any sane input


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse worker-only arguments so the public CLI remains the user interface."""
    parser = argparse.ArgumentParser(prog="tunstrap._worker", add_help=False)
    parser.add_argument("--ipc-fd", type=int, required=True)
    parser.add_argument("--session-dir", default=None)
    return parser.parse_args(argv)


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


def _read_schema_from_stdin() -> InputSchema:
    """Read the schema JSON from stdin (parent has closed its write end)."""
    raw = sys.stdin.buffer.read()
    if len(raw) > _SCHEMA_MAX_BYTES:
        raise DaemonError("schema pipe exceeded size limit", {"limit": _SCHEMA_MAX_BYTES})
    return InputSchema.model_validate_json(raw.decode("utf-8"))


def _write_message(fd: int, message: dict[str, Any]) -> None:
    """Finish partial writes so the parent receives a complete IPC frame."""
    payload = (json.dumps(message) + "\n").encode("utf-8")
    while payload:
        written = os.write(fd, payload)
        if written <= 0:
            raise DaemonError("short write to IPC pipe", {"remaining": len(payload)})
        payload = payload[written:]


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

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    idle_task: asyncio.Task[None] | None = None
    if schema.daemon.auto_stop_idle_seconds is not None:
        idle_task = asyncio.create_task(
            _idle_watchdog(
                tracker=manager.activity_tracker,
                timeout_seconds=schema.daemon.auto_stop_idle_seconds,
                stop_event=stop_event,
            )
        )

    try:
        await stop_event.wait()
    finally:
        if idle_task is not None:
            idle_task.cancel()
            try:
                await idle_task
            except asyncio.CancelledError:
                pass
        await manager.stop_all()
        session.cleanup()
    return 0


def main(argv: list[str] | None = None) -> None:
    """Worker entry: create+lock session dir, run loop, clean up, exit."""
    args = _parse_args(argv)
    try:
        session = SessionDir.create(supplied=args.session_dir)
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
