"""OpenTofu's kubernetes and helm providers, driven through a tunstrap tunnel.

Code: tests/e2e/module/, tests/e2e/shim/tofu-tunstrap.
Method: run the real shim against a per-test copy of the module and a real kind
cluster; read results back through an oracle that does not use the tunnel.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.e2e.rig import tofu_env

pytestmark = [pytest.mark.e2e]


def test_plan_succeeds_with_the_inert_branch(tofu_module: Path, tofu_plugin_cache: Path) -> None:
    """With TF_VAR_tunstrap unset the module still plans: try(jsondecode()) holds."""
    env = tofu_env(tofu_module, tofu_plugin_cache)
    assert "TF_VAR_tunstrap" not in env

    init = subprocess.run(
        ["tofu", "init", "-input=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init.returncode == 0, init.stdout + init.stderr

    planned = subprocess.run(
        ["tofu", "plan", "-input=false", "-refresh=false"],
        cwd=tofu_module,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert "2 to add" in planned.stdout

    # Exit 0 and "2 to add" both hold for a module that quietly reached a real
    # cluster - they say the plan happened, not that it was *inert*. The output
    # is what separates the two: local.kubepath is "" only when the try() chain
    # found no path, which is the state the Task 4.2 negative control depends
    # on. Without this line a module that had silently picked up an ambient
    # kubeconfig would sail through.
    assert 'kubepath_used = ""' in planned.stdout
