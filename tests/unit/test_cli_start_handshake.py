"""Recovery output for ``start`` handshake failures after daemon detach.

Validates: a parent-side IPC failure reports the known session path and worker
pid without stopping the daemon or deleting its state. JSON mode keeps the
typed error on stdout; env mode keeps stdout safe for shell evaluation and
writes diagnostics to stderr.

Code: tunstrap/cli.py (start_command)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tunstrap import cli as cli_mod
from tunstrap.exceptions import DaemonHandshakeError, DaemonHandshakeTimeoutError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("output_fmt", ["json", "env"])
@pytest.mark.parametrize(
    ("error_type", "details"),
    [
        (
            DaemonHandshakeTimeoutError,
            {"timeout_seconds": 1, "worker_reaped": True, "pid": 4242},
        ),
        (DaemonHandshakeError, {"errno": 5, "pid": 4242}),
    ],
    ids=["timeout", "ipc_read_oserror"],
)
def test_start_handshake_failure_preserves_and_reports_recovery_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_fmt: str,
    error_type: type[DaemonHandshakeError],
    details: dict[str, Any],
) -> None:
    """A failed parent handshake leaves a named, stoppable daemon session."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    marker = session_dir / "still-present"
    marker.write_text("preserve me")
    error_details = {**details, "session_dir": str(session_dir)}
    error = error_type("parent lost the worker handshake", error_details)

    def fail_spawn(
        schema: Any, session_dir: str | None = None, *, input_env: str | None = None
    ) -> dict[str, Any]:
        raise error

    def fail_if_stopped(*_args: object, **_kwargs: object) -> object:
        pytest.fail("start must not stop a daemon whose handshake state is unknown")

    monkeypatch.setattr(cli_mod, "spawn_daemon", fail_spawn)
    monkeypatch.setattr(cli_mod, "stop_session", fail_if_stopped)

    result = CliRunner().invoke(
        cli_mod.main,
        ["start", "--output", output_fmt],
        input='{"nodes": {}}',
    )

    expected = error.to_error_output()
    assert result.exit_code == 4
    assert marker.read_text() == "preserve me"
    assert f"tunstrap stop --session-dir {session_dir}" in result.stderr
    if output_fmt == "json":
        assert json.loads(result.stdout) == expected
    else:
        assert result.stdout == ""
        assert json.loads(result.stderr.splitlines()[-1]) == expected
