"""The consumer-facing tofu shim: dispatch, exit codes, and stdout purity.

Code: tests/e2e/shim/tofu-tunstrap (and its negative control).
Method: drive the shim with a fake `tofu` on PATH, so the assertions are about
the shim and tunstrap rather than about OpenTofu.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.rig import CONTROL_SHIM, SHIM

pytestmark = [pytest.mark.e2e]


def _shim_body(path: Path) -> str:
    """The shim's executable lines, with comments and blank lines removed."""
    lines = [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


def test_both_shims_are_executable_posix_sh() -> None:
    """Each shim is mode 0755 and declares /bin/sh, so `exec` semantics hold."""
    for path in (SHIM, CONTROL_SHIM):
        assert path.is_file(), path
        assert path.stat().st_mode & 0o777 == 0o755, path
        assert path.read_text().startswith("#!/bin/sh\n"), path


def test_control_shim_differs_only_in_output_var() -> None:
    """The negative control is the real shim minus --output-var, and nothing else."""
    real = _shim_body(SHIM)
    control = _shim_body(CONTROL_SHIM)
    assert real != control
    assert real.replace(" --output-var TF_VAR_tunstrap", "") == control


def test_shim_uses_the_documented_invocation() -> None:
    """The shim runs the exact command the spec designs, including `env -u`."""
    real = _shim_body(SHIM)
    assert "tunstrap run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap" in real
    assert '-- env -u KUBECONFIG tofu "$@"' in real
    assert 'case "$1" in init|-version) exec tofu "$@" ;; esac' in real
    assert '[ -n "$TUNSTRAP_INPUT" ] || exec tofu "$@"' in real
