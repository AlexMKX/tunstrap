"""The detached worker must not retain run's secret input environment."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_real_worker_does_not_inherit_tunstrap_input(tmp_path: Path) -> None:
    """Inspect the real detached worker rather than a mocked Popen call."""
    session_dir = tmp_path / "session"
    env = dict(os.environ)
    env["TUNSTRAP_INPUT"] = json.dumps({"nodes": {}})
    proc = subprocess.Popen(
        [
            "tunstrap",
            "run",
            "--input-env",
            "TUNSTRAP_INPUT",
            "--output-var",
            "TF_VAR_tunstrap",
            "--session-dir",
            str(session_dir),
            "--",
            "sleep",
            "10",
        ],
        env=env,
    )
    try:
        identity = session_dir / "tunnel-data" / "daemon.pid"
        deadline = time.monotonic() + 5
        while not identity.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert identity.exists(), "worker did not publish its identity"
        worker_env = Path(f"/proc/{identity.read_text().strip()}/environ").read_bytes()
        assert b"TUNSTRAP_INPUT=" not in worker_env
    finally:
        proc.terminate()
        proc.wait(timeout=10)
