"""`run`'s stdout must be byte-for-byte the child's stdout.

Validates: nothing tunstrap writes reaches fd 1 after the child starts, across
a clean run, a non-zero child, a zero-grace stop (which takes the SIGKILL or
identity-changed branch), and a teardown whose identity check fails. Under the
tofu proxy this stream is parsed by Terragrunt, so a single injected byte is a
correctness bug, not a cosmetic one.

This is the only place the invariant is checked at the file-descriptor level.
The unit tests cannot do it: CliRunner swaps `sys.stdout` for an in-memory
object, so `result.stdout == ""` there proves only that no Python-level write
happened -- it would not notice an `os.write(1, ...)`, a C-level write, or a
grandchild that inherited fd 1.

The oracle is differential, not a literal. Each test compares the wrapped
process's stdout with the bytes the *same* child script produces when run
without the wrapper. Comparing against a hand-written constant would only
prove tunstrap did not inject one specific thing; comparing against the
unwrapped child also catches a byte that was dropped, reordered or translated.

Two deliberate choices make that comparison meaningful:

* stdout is captured as **bytes** (no ``text=True``). Universal-newline
  translation would silently rewrite ``\\r\\n`` and hide exactly the class of
  corruption this test exists to catch.
* the child emits a CR, an LF and a final byte with **no trailing newline**, so
  an appended diagnostic, a stripped terminator or a translated line ending all
  change the result.

Code: tunstrap/cli.py (_teardown_run), tunstrap/session.py (stop_session)
Assertion: result.stdout equals the unwrapped child's stdout exactly;
diagnostics, when any, appear only on stderr.
Method: the installed console script as a subprocess, forwarding through
`sshd-bastion` -- the only rig service with AllowTcpForwarding enabled and a
route to the internal `target-1`.

Note: these tests assert on stdout only, except where a diagnostic is the point
(the tampered-identity case at the bottom). That a successful run also leaves
stderr *empty* is a separate invariant and is asserted in
test_run_teardown_latency.py. An earlier version of this note recorded the
opposite -- that every successful run burned the full grace window and then
wrote `run: daemon not stopped cleanly: identity changed during grace`, which
is why these tests used to take ~10s each. That was the unreaped-zombie defect
in the grace poll, since fixed in session.py (`_has_exited`).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

SENTINEL = "CHILD_STDOUT_SENTINEL"
_TAIL = "TAIL_NO_TRAILING_NEWLINE"
_TIMEOUT = 120

# printf, not echo: no trailing newline, and \r\n is emitted literally.
_CHILD_SCRIPT = f"printf '%s\\r\\n%s' {SENTINEL} {_TAIL}"
_EXPECTED = f"{SENTINEL}\r\n{_TAIL}".encode()


def _write_key(tmp_path: Path, pem: str) -> Path:
    key_path = tmp_path / "id_test"
    key_path.write_text(pem)
    key_path.chmod(0o600)
    return key_path


def _base_args(cluster: dict[str, Any], key: Path, session_dir: Path) -> list[str]:
    return [
        "tunstrap",
        "run",
        f"{cluster['user']}@localhost:{cluster['bastion_port']}",
        "--ssh-key",
        str(key),
        "--target",
        "web=target-1:80",
        "--session-dir",
        str(session_dir),
    ]


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run argv capturing raw bytes: no newline translation, no decoding."""
    return subprocess.run(argv, capture_output=True, check=False, timeout=_TIMEOUT)


def _baseline(script: str, tmp_path: Path) -> bytes:
    """Stdout of the identical child script with no wrapper: the oracle.

    ``TUNSTRAP_SESSION_DIR`` is pointed at a scratch directory so the tamper
    variant's redirect succeeds here too, and the two runs really do execute
    the same script.
    """
    root = tmp_path / "baseline"
    (root / "tunnel-data").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["TUNSTRAP_SESSION_DIR"] = str(root)
    produced = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        check=False,
        env=env,
        timeout=_TIMEOUT,
    ).stdout
    # Guard the oracle itself: an empty or malformed baseline would make every
    # comparison below vacuous, which is the failure mode this suite keeps
    # finding elsewhere.
    assert produced == _EXPECTED, f"baseline child produced {produced!r}"
    return produced


def test_stdout_is_only_the_child_on_success(
    ssh_test_cluster: dict[str, Any], tmp_path: Path, started_daemons: list[str]
) -> None:
    """A clean run emits the child's bytes and nothing else."""
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
    key = _write_key(tmp_path, ssh_test_cluster["private_pem"])
    result = _run(
        [*_base_args(ssh_test_cluster, key, session_dir), "--", "sh", "-c", _CHILD_SCRIPT]
    )
    expected = _baseline(_CHILD_SCRIPT, tmp_path)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout == expected, f"run injected bytes: {result.stdout!r}"


def test_stdout_is_only_the_child_on_nonzero_exit(
    ssh_test_cluster: dict[str, Any], tmp_path: Path, started_daemons: list[str]
) -> None:
    """A failing child keeps its exit code and its exclusive claim on stdout."""
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
    key = _write_key(tmp_path, ssh_test_cluster["private_pem"])
    script = f"{_CHILD_SCRIPT}; exit 7"
    result = _run([*_base_args(ssh_test_cluster, key, session_dir), "--", "sh", "-c", script])
    expected = _baseline(script, tmp_path)
    assert result.returncode == 7, f"stderr={result.stderr!r}"
    assert result.stdout == expected, f"run injected bytes: {result.stdout!r}"


def test_stdout_is_only_the_child_with_zero_grace(
    ssh_test_cluster: dict[str, Any], tmp_path: Path, started_daemons: list[str]
) -> None:
    """--grace-seconds 0 takes the escalation path; stdout is still pure."""
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
    key = _write_key(tmp_path, ssh_test_cluster["private_pem"])
    result = _run(
        [
            *_base_args(ssh_test_cluster, key, session_dir),
            "--grace-seconds",
            "0",
            "--",
            "sh",
            "-c",
            _CHILD_SCRIPT,
        ]
    )
    expected = _baseline(_CHILD_SCRIPT, tmp_path)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout == expected, f"run injected bytes: {result.stdout!r}"


def test_stdout_is_pure_when_teardown_identity_fails(
    ssh_test_cluster: dict[str, Any], tmp_path: Path, started_daemons: list[str]
) -> None:
    """A tampered identity makes teardown fail loudly — on stderr only.

    The child overwrites tunnel-data/daemon.pid with 1 before exiting. pid 1
    exists but does not hold this session's lock, so verify_session returns
    `mismatch`, stop_session refuses to signal it, and the real daemon
    survives. The test then stops that daemon itself, using the pid recorded
    in session.lock, so nothing leaks.
    """
    session_dir = tmp_path / "session"
    started_daemons.append(str(session_dir))
    key = _write_key(tmp_path, ssh_test_cluster["private_pem"])
    script = f'{_CHILD_SCRIPT}; printf "1\\n" > "$TUNSTRAP_SESSION_DIR/tunnel-data/daemon.pid"'
    result = _run(
        [
            *_base_args(ssh_test_cluster, key, session_dir),
            "--auto-stop-idle-seconds",
            "30",
            "--",
            "sh",
            "-c",
            script,
        ]
    )
    try:
        expected = _baseline(script, tmp_path)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert result.stdout == expected, f"run injected bytes: {result.stdout!r}"
        assert b"identity mismatch" in result.stderr, result.stderr
    finally:
        lock = session_dir / "session.lock"
        if lock.is_file():
            daemon_pid = int(lock.read_text().strip())
            os.kill(daemon_pid, signal.SIGTERM)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and lock.exists():
                time.sleep(0.2)
            assert not lock.exists(), "the orphaned daemon did not exit within 30s"
