"""Constants and helpers shared by the e2e tests.

A plain module rather than a conftest, so test modules can import these by name
without depending on how pytest loaded the conftest. Fixtures live in
``conftest.py``; everything importable lives here.

Self-contained by construction. This suite generates its own Ed25519 keypair
into ``tests/e2e/_keys/`` and commits its own sshd drop-in under
``tests/e2e/_sshd_conf/``. It reaches into no other suite: the integration rig's
``_keys/`` directory is gitignored and is created only as a side effect of a
fixture pytest never loads here, so borrowing it would work on a developer
machine and fail in a clean CI checkout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

HERE = Path(__file__).resolve().parent

CLUSTER_NAME = "tunstrap-e2e"
# kind derives the container name from the cluster name. That name is both the
# SSH forward target and the expected tls_server_name, which is why the cluster
# name is fixed rather than randomised.
CONTROL_PLANE = f"{CLUSTER_NAME}-control-plane"
NODE_IMAGE = "kindest/node:v1.34.0"
COMPOSE_FILE = HERE / "docker-compose.yml"
IN_NODE_KUBECONFIG = "/etc/kube/admin.conf"
SHIM = HERE / "shim" / "tofu-tunstrap"
CONTROL_SHIM = HERE / "shim" / "tofu-tunstrap-novar"


def skip_or_fail(reason: str) -> NoReturn:
    """Skip locally, fail when TUNSTRAP_E2E_REQUIRE_ALL=1.

    A missing external tool is an environment fact, not a product failure, so on
    a workstation it is a skip naming the tool. In CI the job installs every
    tool itself, so a skip there means the job reports green while most of the
    tier never ran - which is exactly how a cluster tier rots into decoration.
    The e2e job sets TUNSTRAP_E2E_REQUIRE_ALL=1, which turns every such skip
    into a failure.
    """
    if os.environ.get("TUNSTRAP_E2E_REQUIRE_ALL") == "1":
        pytest.fail(reason + " [TUNSTRAP_E2E_REQUIRE_ALL=1: skipping is not allowed here]")
    pytest.skip(reason)


def require_tools(*names: str) -> None:
    """Skip (or, in CI, fail) unless every named binary is on PATH.

    ``tunstrap`` is deliberately not handled here - see ``e2e_preflight`` in
    conftest.py, where it is always a failure.
    """
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        skip_or_fail("e2e tier requires " + ", ".join(missing) + " on PATH")


def kubectl_in_node(*args: str) -> subprocess.CompletedProcess[str]:
    """Run kubectl *inside* the kind control-plane container.

    Deliberately an independent oracle. It does not traverse the tunnel, so a
    broken tunnel can never make a read-back appear to succeed, and it uses the
    version-matched kubectl shipped in the node image rather than whatever the
    host happens to have installed.
    """
    return subprocess.run(
        [
            "docker",
            "exec",
            CONTROL_PLANE,
            "kubectl",
            "--kubeconfig",
            "/etc/kubernetes/admin.conf",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
