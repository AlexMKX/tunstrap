"""The daemon's tunnel-loss watchdog and its disposal decision (issue #33).

Validates: a worker whose SSH connection dies *after* startup stops waiting
instead of blocking on ``stop_event`` forever; that it does so only when the
dead node was ``required``; that the session data is preserved rather than
deleted on that path; and that a normal shutdown is never mistaken for a loss.
Code: tunstrap/_worker.py::_tunnel_loss_watchdog,
      tunstrap/_worker.py::_dispose_session,
      tunstrap/_worker.py::_record_losses,
      tunstrap/manager.py::live_nodes
Assertion: stop_event state, the recorded ``tunnel-loss.json`` body, and --
for disposal -- the real on-disk outcome of a real ``SessionDir``.
Method: a fake connection whose ``wait_closed()`` blocks on an
``asyncio.Event`` the test controls, reproducing the exact transition asyncssh
publishes from ``SSHConnection._cleanup`` without a real transport.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tunstrap._worker import (
    _dispose_session,
    _LossWatch,
    _NodeLoss,
    _record_losses,
    _tunnel_loss_watchdog,
)
from tunstrap.manager import TunnelManager, _NodeRuntime
from tunstrap.schemas import InputSchema
from tunstrap.session import SessionDir

pytestmark = pytest.mark.unit


class _FakeConn:
    """A connection whose ``wait_closed()`` completes exactly when the test says so.

    Mirrors the only property of ``asyncssh.SSHClientConnection`` the watchdog
    depends on: ``wait_closed()`` returns once ``SSHConnection._cleanup`` has
    run -- the same routine that closes every local listener, which is what
    turns a live forward into ``connection refused``.
    """

    def __init__(self) -> None:
        """Start open; ``drop()`` is what publishes the close transition."""
        self._closed = asyncio.Event()

    def drop(self) -> None:
        """Complete every pending ``wait_closed()``, as a lost connection does."""
        self._closed.set()

    async def wait_closed(self) -> None:
        """Block until the test drops the connection."""
        await self._closed.wait()


def _watch(tmp_path: Path) -> tuple[_LossWatch, SessionDir]:
    """Build a watch bound to a real, locked session dir."""
    session = SessionDir.create(supplied=str(tmp_path / "s"))
    return _LossWatch(stop_event=asyncio.Event(), session=session, losses=[]), session


def _loss_body(session_dir: Path) -> dict[str, Any]:
    """Read the record the watchdog wrote, failing loudly when it is absent."""
    body = SessionDir.read_tunnel_loss(str(session_dir))
    assert body is not None, "the watchdog recorded no reason for the exit"
    return body


async def test_required_loss_stops_the_daemon(tmp_path: Path) -> None:
    """The defect itself: a required tunnel dying must end the shutdown wait.

    Before this, ``_run`` blocked on ``stop_event.wait()`` with nothing able to
    set it but a signal or the idle timer, so the worker outlived its own
    tunnel -- PID alive, session lock held, every local listener already closed.
    """
    watch, session = _watch(tmp_path)
    conn = _FakeConn()
    task = asyncio.create_task(_tunnel_loss_watchdog("edge", conn, True, watch))

    conn.drop()
    await asyncio.wait_for(task, timeout=2.0)

    assert watch.stop_event.is_set(), "a required tunnel died and the daemon kept waiting"
    assert watch.losses == [_NodeLoss(node="edge", required=True)]
    session.cleanup()


async def test_non_required_loss_leaves_the_daemon_running(tmp_path: Path) -> None:
    """Decision 2: ``required=False`` degrades, it does not terminate.

    The negative control for the test above. Without it, "stop on any loss"
    would satisfy the required case while taking a whole multi-node daemon down
    because one optional node blinked -- inverting the meaning ``required``
    already carries at startup, where a non-required failure is downgraded to a
    ``TunnelWarning`` instead of aborting ``start``.
    """
    watch, session = _watch(tmp_path)
    conn = _FakeConn()
    task = asyncio.create_task(_tunnel_loss_watchdog("spare", conn, False, watch))

    conn.drop()
    await asyncio.wait_for(task, timeout=2.0)

    assert not watch.stop_event.is_set(), "a non-required tunnel took the daemon down"
    assert watch.losses == [_NodeLoss(node="spare", required=False)]
    body = _loss_body(Path(session.session_dir))
    assert body["self_terminated"] is False
    assert body["losses"] == [{"node": "spare", "required": False}]
    session.cleanup()


async def test_a_later_required_loss_upgrades_the_record(tmp_path: Path) -> None:
    """Losses accumulate, and one required node among them is terminal.

    A multi-node daemon can lose an optional node first and a required one
    later. The record must then name both, and ``self_terminated`` must follow
    the *set* of losses rather than whichever one happened to arrive last.
    """
    watch, session = _watch(tmp_path)
    spare, edge = _FakeConn(), _FakeConn()
    tasks = [
        asyncio.create_task(_tunnel_loss_watchdog("spare", spare, False, watch)),
        asyncio.create_task(_tunnel_loss_watchdog("edge", edge, True, watch)),
    ]

    spare.drop()
    await asyncio.wait_for(tasks[0], timeout=2.0)
    assert not watch.stop_event.is_set()
    edge.drop()
    await asyncio.wait_for(tasks[1], timeout=2.0)

    assert watch.stop_event.is_set()
    body = _loss_body(Path(session.session_dir))
    assert body["self_terminated"] is True
    assert body["losses"] == [
        {"node": "spare", "required": False},
        {"node": "edge", "required": True},
    ]
    session.cleanup()


async def test_cancellation_records_nothing(tmp_path: Path) -> None:
    """A normal shutdown must not be recorded as a tunnel loss.

    ``_supervise`` cancels these watchdogs *before* ``stop_all`` closes the
    connections, precisely so that a clean stop does not complete every
    ``wait_closed()`` and write a loss record for a daemon that was simply
    asked to exit. Reversing that order would preserve every session dir on the
    normal path and leak them all -- so the guarantee is pinned here.
    """
    watch, session = _watch(tmp_path)
    task = asyncio.create_task(_tunnel_loss_watchdog("edge", _FakeConn(), True, watch))
    await asyncio.sleep(0)  # let it reach the await

    task.cancel()
    assert await task is None, "cancellation must return cleanly, as _idle_watchdog does"

    assert watch.losses == []
    assert not watch.stop_event.is_set()
    assert SessionDir.read_tunnel_loss(session.session_dir) is None
    session.cleanup()


def test_record_losses_survives_an_unwritable_session(tmp_path: Path) -> None:
    """Failing to explain the exit must never prevent the exit.

    The exit is the fix for issue #33; the record is only its explanation. A
    ``tunnel-data`` that has gone away underneath the daemon is exactly the
    moment when refusing to shut down would be worst, so the writer swallows it.
    """
    session = SessionDir.create(supplied=str(tmp_path / "s"))
    watch = _LossWatch(stop_event=asyncio.Event(), session=session, losses=[])
    watch.losses.append(_NodeLoss(node="edge", required=True))
    (Path(session.session_dir) / "tunnel-data" / "daemon.pid").write_text("1\n")
    # Replace tunnel-data with a symlink: materialize_atomic refuses to follow
    # it and raises SessionError, the non-OSError failure mode.
    data = Path(session.session_dir) / "tunnel-data"
    for child in data.iterdir():
        child.unlink()
    data.rmdir()
    data.symlink_to(tmp_path)

    _record_losses(watch)  # must not raise

    session.cleanup()


def test_dispose_preserves_every_file_on_a_required_loss(tmp_path: Path) -> None:
    """Decision 1, stated as the on-disk outcome: lock gone, data untouched.

    Releasing the lock is what unblocks a later ``start`` from ``SessionActive``;
    keeping the files is what leaves the consumer the familiar ``connection
    refused`` instead of a kubeconfig vanishing mid-command. Both halves are
    asserted, because either one alone is a different (and wrong) behaviour.
    """
    root = tmp_path / "s"
    session = SessionDir.create(supplied=str(root))
    secret = Path(session.session_dir) / "tunnel-data" / "materialized.kubeconfig"
    secret.write_text("credential-bearing")

    _dispose_session(session, [_NodeLoss(node="edge", required=True)])

    assert not (root / "session.lock").exists(), "the lock was not released; start stays blocked"
    assert secret.read_text() == "credential-bearing", "session data was deleted on the loss path"


def test_dispose_cleans_up_on_a_normal_stop(tmp_path: Path) -> None:
    """The negative control: without a required loss, cleanup still happens.

    "Preserve on everything" would satisfy the preservation test above while
    leaking every session dir the daemon ever created. A non-required loss is
    included deliberately -- it never terminates the daemon, so reaching
    disposal with only those means a normal stop, which still cleans up.
    """
    root = tmp_path / "s"
    session = SessionDir.create(supplied=str(root))
    data = Path(session.session_dir) / "tunnel-data"
    (data / "materialized.kubeconfig").write_text("credential-bearing")

    _dispose_session(session, [_NodeLoss(node="spare", required=False)])

    assert not (root / "session.lock").exists()
    assert not data.exists(), "a normal stop must still remove tunnel-data"


def test_live_nodes_pairs_each_started_connection_with_its_required_flag() -> None:
    """``live_nodes`` is what tells the watchdog whether a node's death matters.

    It must report only nodes that actually started -- a node that failed at
    startup has no connection to await and was already reported by ``start`` --
    and it must carry ``required`` from the schema rather than a parallel notion
    of importance invented by the worker.
    """
    schema = InputSchema.model_validate(
        {
            "nodes": {
                name: {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "pw",
                    "required": required,
                    "remote_targets": {"p": "127.0.0.1:1"},
                }
                for name, required in (("edge", True), ("spare", False), ("dead", True))
            }
        }
    )
    manager = TunnelManager(schema)
    started: list[tuple[str, Any]] = [
        ("edge", _FakeConn()),
        ("spare", _FakeConn()),
        # "dead" never opened a connection: it must not be watched.
        ("dead", None),
    ]
    for name, conn in started:
        runtime = _NodeRuntime(name=name, success=True)
        runtime.conn = conn
        manager._runtimes.append(runtime)  # pylint: disable=protected-access

    assert [(name, required) for name, _conn, required in manager.live_nodes()] == [
        ("edge", True),
        ("spare", False),
    ]


def test_loss_record_is_valid_json_on_disk(tmp_path: Path) -> None:
    """The record is a file other processes parse, so its bytes are checked directly.

    ``status`` and ``run``'s teardown both read it out of band, after the daemon
    that wrote it is gone, so a shape only reachable through the writer's own
    helper would prove nothing about what they will find.
    """
    session = SessionDir.create(supplied=str(tmp_path / "s"))
    watch = _LossWatch(stop_event=asyncio.Event(), session=session, losses=[])
    watch.losses.append(_NodeLoss(node="edge", required=True))

    _record_losses(watch)

    raw = (Path(session.session_dir) / "tunnel-data" / "tunnel-loss.json").read_text()
    body = json.loads(raw)
    assert body["self_terminated"] is True
    assert "required tunnel lost" in body["reason"]
    session.cleanup()
