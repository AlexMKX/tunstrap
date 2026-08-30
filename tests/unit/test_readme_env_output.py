"""Keep README's ``start --output env`` variable table aligned with its emitter."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tunstrap.cli import _session_scalars
from tunstrap.envrender import render_kube_env
from tunstrap.schemas import OutputSchema

pytestmark = pytest.mark.unit

README = Path(__file__).resolve().parents[2] / "README.md"


def _output(kube_paths: list[str]) -> OutputSchema:
    """Make an emitted success envelope with exactly the requested kube files."""
    kube = {
        f"kube{index}": {
            "cluster_name": f"cluster{index}",
            "context_name": f"context{index}",
            "local_port": 6400 + index,
            "endpoint": f"https://127.0.0.1:{6400 + index}",
            "tls_server_name": "example.test",
            "certificate_authority_data": "ca",
            "client_certificate_data": "cert",
            "client_key_data": "key",
            "content_b64": "config",
            "path": path,
        }
        for index, path in enumerate(kube_paths)
    }
    return OutputSchema.model_validate(
        {
            "connections": {"node": {"ports": {}, "kube_targets": kube}},
            "pid": 42,
            "session_dir": "/tmp/session",
            "started_at": "2026-08-10T00:00:00Z",
        }
    )


def _readme_env_variable_names() -> set[str]:
    """Extract the Variable column from README's emitted-variable table."""
    match = re.search(
        r"Variables emitted.*?\n\n\| Variable \| Meaning \|\n\|---\|---\|\n(?P<rows>(?:\|.*\|\n)+)",
        README.read_text(),
    )
    assert match is not None, "README is missing the start --output env variable table"
    return {
        row.split("|")[1].strip().strip("`")
        for row in match.group("rows").splitlines()
        if row.strip()
    }


def test_readme_env_table_matches_every_key_start_output_env_can_emit() -> None:
    """README lists the actual scalar plus conditional kube-channel key union.

    ``RUN_ENV_KEYS`` is deliberately not used: it reserves all scrubbed
    kube names for ``run --output-var`` collisions, while this table documents
    only keys ``start --output env`` can actually emit. The union of zero, one,
    and two materialized kube files covers its cardinality branches. This test
    compares only variable names, not the mutually exclusive emission of
    ``KUBE_CONFIG_PATH`` and ``KUBE_CONFIG_PATHS``.
    """
    actual = set(_session_scalars(_output([])))
    actual.update(render_kube_env(_output(["/tmp/one"])))
    actual.update(render_kube_env(_output(["/tmp/one", "/tmp/two"])))

    assert _readme_env_variable_names() == actual
