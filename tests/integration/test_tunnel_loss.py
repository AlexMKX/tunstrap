"""A worker outliving its own tunnel, end to end against a real sshd (issue #33).

Validates the whole reported incident and its fix: an SSH connection that dies
after a successful start used to leave the worker running forever -- PID alive,
``session.lock`` held, every local listener already closed by asyncssh, so
consumers got ``connection refused`` while ``status`` reported ``alive: true``
and ``start`` on the same directory refused with ``SessionActive``. The daemon
now detects that itself, exits, preserves its session data and records why.
Code: tunstrap/_worker.py::_tunnel_loss_watchdog,
      tunstrap/_worker.py::_dispose_session,
      tunstrap/cli_stop.py::status_command,
      tunstrap/cli.py::_teardown_run_inner
Method: a host-side TCP relay stands between tunstrap and the container's sshd,
so the test kills the transport itself rather than the shared compose service --
see ``_TcpRelay``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

pytestmark = pytest.mark.integration


class _TcpRelay:
    """A host-side TCP relay standing between tunstrap and a container's sshd.

    Killing the tunnel by stopping the sshd container is not usable here: the
    compose stack is session-scoped and ``ssh_test_cluster`` reads each
    published port exactly once, so restarting a service hands every later test
    a stale port. The relay keeps the blast radius inside this module --
    ``kill()`` drops the live sockets and nothing else in the suite notices.

    It is a faithful stand-in, not a simulation: the SSH transport really dies,
    so asyncssh really runs ``SSHConnection._cleanup``, which is the routine
    that closes every local listener and sets the connection's close event. That
    is the same code path a keepalive timeout takes, which is how the reported
    incident began.
    """

    def __init__(self, upstream: tuple[str, int]) -> None:
        """Bind an ephemeral listener and start accepting in the background."""
        self._upstream = upstream
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port: int = self._server.getsockname()[1]
        self._live: list[socket.socket] = []
        self._lock = threading.Lock()
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        """Pair each accepted client with a fresh upstream connection."""
        while self._running:
            try:
                client, _addr = self._server.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self._upstream, timeout=10)
            except OSError:
                client.close()
                continue
            with self._lock:
                self._live.extend((client, upstream))
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(src, dst), daemon=True).start()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        """Copy bytes one way until either end goes away."""
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def kill(self) -> None:
        """Drop every live connection; the listener stays up for a later start."""
        with self._lock:
            live, self._live = self._live, []
        for sock in live:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self) -> None:
        """Stop accepting and drop everything. Safe to call twice."""
        self._running = False
        self.kill()
        self._server.close()


@pytest.fixture(name="relay")
def _relay(ssh_test_cluster: dict[str, Any]) -> Iterator[_TcpRelay]:
    """A relay in front of sshd-bastion, torn down with the test."""
    relay = _TcpRelay(("127.0.0.1", ssh_test_cluster["bastion_port"]))
    try:
        yield relay
    finally:
        relay.close()


def _payload(cluster: dict[str, Any], relay: _TcpRelay, *, required: bool = True) -> dict[str, Any]:
    """One node reached through the relay, so the test controls its transport."""
    return {
        "nodes": {
            "edge": {
                "host": "127.0.0.1",
                "port": relay.port,
                "user": "tester",
                "ssh_pkey": cluster["private_pem"],
                "remote_targets": {"p": "127.0.0.1:2222"},
                "required": required,
            }
        }
    }


def _start(payload: dict[str, Any], session_dir: str) -> dict[str, Any]:
    """Start a daemon into an explicit session dir and return its output body."""
    done = subprocess.run(
        ["tunstrap", "start", "--session-dir", session_dir],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    body: dict[str, Any] = json.loads(done.stdout)
    return body


def _alive(pid: int) -> bool:
    """True while the recorded worker process still exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_dead(pid: int, timeout: float = 15.0) -> bool:
    """Poll for the worker's exit; the watchdog itself is event-driven."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def _status(session_dir: str) -> dict[str, Any]:
    """Read ``tunstrap status``'s envelope for a session dir."""
    done = subprocess.run(
        ["tunstrap", "status", "--session-dir", session_dir],
        capture_output=True,
        text=True,
        check=False,
    )
    body: dict[str, Any] = json.loads(done.stdout)
    return body


def test_required_tunnel_death_ends_the_daemon_and_frees_the_session(
    ssh_test_cluster: dict[str, Any],
    relay: _TcpRelay,
    tmp_path: Path,
) -> None:
    """The reported incident, start to finish, on the same session directory.

    Every symptom in the ticket is asserted in the order an operator met them:
    the forward stops accepting, the worker is gone rather than ``Ssl`` forever,
    the lock is released, the credentials it materialized are still on disk, the
    reason is recorded, ``status`` stops claiming ``alive: true``, and -- the
    thing the 20-hour incident actually needed -- ``start`` on the very same
    directory succeeds instead of returning ``SessionActive``.
    """
    session_dir = str(tmp_path / "sess")
    body = _start(_payload(ssh_test_cluster, relay), session_dir)
    pid = body["pid"]
    local_port = body["connections"]["edge"]["ports"]["p"]
    with socket.create_connection(("127.0.0.1", local_port), timeout=5):
        pass  # the forward accepts while the tunnel is healthy
    assert _status(session_dir)["alive"] is True
    assert (Path(session_dir) / "session.lock").exists()

    relay.kill()

    assert _wait_until_dead(pid), f"worker pid={pid} outlived its own tunnel"
    assert not (Path(session_dir) / "session.lock").exists(), "the session lock was never released"
    assert (Path(session_dir) / "tunnel-data" / "daemon.pid").exists(), (
        "session data was deleted; the consumer loses its files instead of seeing "
        "connection refused"
    )
    status = _status(session_dir)
    assert status["alive"] is False
    assert status["tunnel_loss"]["self_terminated"] is True
    assert status["tunnel_loss"]["losses"] == [{"node": "edge", "required": True}]

    # The point of releasing the lock: the same directory is usable again.
    restarted = _start(_payload(ssh_test_cluster, relay), session_dir)
    subprocess.run(
        ["tunstrap", "stop", "--session-dir", session_dir], capture_output=True, check=False
    )
    assert restarted["pid"] != pid


def test_non_required_tunnel_death_leaves_the_daemon_running(
    ssh_test_cluster: dict[str, Any],
    relay: _TcpRelay,
    tmp_path: Path,
    started_daemons: list[str],
) -> None:
    """Decision 2 end to end: ``required=False`` degrades, it does not terminate.

    The negative control for the test above, and the one that keeps the fix from
    being "exit whenever anything closes". The record is still written -- a
    degraded daemon that says nothing is how the original incident stayed
    invisible -- but ``self_terminated`` is false and the process is still there.
    """
    session_dir = str(tmp_path / "sess")
    body = _start(_payload(ssh_test_cluster, relay, required=False), session_dir)
    started_daemons.append(session_dir)
    pid = body["pid"]

    relay.kill()
    time.sleep(3.0)

    assert _alive(pid), "a non-required tunnel took the whole daemon down"
    assert (Path(session_dir) / "session.lock").exists(), (
        "the daemon released a lock it still needs"
    )
    status = _status(session_dir)
    assert status["alive"] is True
    assert status["tunnel_loss"]["self_terminated"] is False
    assert status["tunnel_loss"]["losses"] == [{"node": "edge", "required": False}]


def test_run_lets_its_child_finish_when_the_daemon_self_terminates(
    ssh_test_cluster: dict[str, Any],
    relay: _TcpRelay,
    tmp_path: Path,
) -> None:
    """Decision 3: the child's exit code still wins, and the notice lands on stderr.

    ``run``'s documented contract is that the wrapped command's exit code is
    ``run``'s own, and the wrapped command is typically ``tofu apply`` -- killing
    it because the tunnel died could abort a non-idempotent operation mid-flight.
    So the child is left alone to fail on its own terms.

    Three independent things are asserted because each fails differently: the
    marker file proves the child was not killed *during* its work, exit code 42
    proves ``run`` propagated the child's status rather than substituting a
    daemon error, and the stderr notice proves the operator is told why -- on fd
    2, because under the tofu-proxy pattern fd 1 belongs to the child.
    """
    started = tmp_path / "child-started"
    marker = tmp_path / "child-finished"
    child = (
        f"import pathlib,sys,time; pathlib.Path({str(started)!r}).write_text('go'); "
        f"time.sleep(6); pathlib.Path({str(marker)!r}).write_text('done'); sys.exit(42)"
    )

    def _kill_once_the_child_is_working() -> None:
        """Drop the tunnel only after the child is demonstrably mid-flight.

        A fixed timer would race the daemon's own startup and could kill the
        transport before ``run`` ever handed off, which would test the
        pre-child failure path instead of this one.
        """
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not started.exists():
            time.sleep(0.05)
        relay.kill()

    killer = threading.Thread(target=_kill_once_the_child_is_working, daemon=True)
    killer.start()
    env = dict(os.environ, TUNSTRAP_INPUT=json.dumps(_payload(ssh_test_cluster, relay)))
    done = subprocess.run(
        [
            "tunstrap",
            "run",
            "--input-env",
            "TUNSTRAP_INPUT",
            "--session-dir",
            str(tmp_path / "sess"),
            "--",
            "python",
            "-c",
            child,
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    killer.join(timeout=5)

    assert marker.read_text() == "done", "run killed its child when the daemon self-terminated"
    assert done.returncode == 42, f"the child's exit code did not win: {done.stderr}"
    assert "daemon reported tunnel loss" in done.stderr, done.stderr
    assert "daemon reported tunnel loss" not in done.stdout, "the notice must not touch fd 1"
