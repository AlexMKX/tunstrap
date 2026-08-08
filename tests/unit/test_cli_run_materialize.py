"""run's unified-output materialization: <session_dir>/tunnel-data/output.json.

Validates: run always writes the unified structure to a deterministic path,
mode 0600, regardless of --output-var or node count; the file's content
equals render_unified_output's output for the same OutputSchema.
Code: tunstrap/cli.py (materialization call site)
Method: CliRunner + spawn_daemon/Popen/_teardown_run monkeypatched, as in
test_cli_run_output_var.py; read the file back after invoke().
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap.cli import main
from tunstrap.envrender import render_unified_output
from tunstrap.schemas import OutputSchema

pytestmark = pytest.mark.unit


def test_run_materializes_output_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-node run writes tunnel-data/output.json, mode 0600, matching content."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    payload = {
        "connections": {"h": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}}},
        "pid": 99,
        "session_dir": str(session_dir),
        "started_at": "2026-08-07T00:00:00Z",
    }
    monkeypatch.setattr(
        cli_mod,
        "spawn_daemon",
        lambda schema, session_dir=None, *, input_env=None: {"kind": "success", "payload": payload},
    )
    monkeypatch.setattr(cli_mod.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    monkeypatch.setenv(
        "TUNSTRAP_INPUT",
        json.dumps(
            {
                "nodes": {
                    "node": {
                        "host": "h",
                        "user": "u",
                        "ssh_password": "p",
                        "remote_targets": {"db": "127.0.0.1:5432"},
                    }
                }
            }
        ),
    )
    result = CliRunner().invoke(main, ["run", "--input-env", "TUNSTRAP_INPUT", "--", "true"])
    assert result.exit_code == 0, result.stderr
    materialized = session_dir / "tunnel-data" / "output.json"
    assert materialized.exists()
    assert stat.S_IMODE(materialized.stat().st_mode) == 0o600
    out = OutputSchema.model_validate(payload)
    assert json.loads(materialized.read_text()) == render_unified_output(out)
    assert _FakePopen.last_env is not None
    assert _FakePopen.last_env["TUNSTRAP_OUTPUT_FILE"] == str(materialized)


class _FakePopen:
    last_env: dict[str, str] | None = None
    returncode = 0

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        _FakePopen.last_env = env

    def wait(self) -> int:
        return 0

    def send_signal(self, _signum: int) -> None:
        pass
