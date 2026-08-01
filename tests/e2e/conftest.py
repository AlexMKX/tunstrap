"""E2E fixtures: a kind cluster plus one sshd node joined to kind's network.

Constants and helpers live in ``tests/e2e/rig.py``; this file holds fixtures
only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Iterator

import pytest

from tests.e2e.rig import (
    CLUSTER_NAME,
    CONTROL_PLANE,
    HERE,
    NODE_IMAGE,
    kubectl_in_node,
    require_tools,
    skip_or_fail,
)


@pytest.fixture(scope="session")
def e2e_preflight() -> None:
    """Linux, and the product itself on PATH.

    A missing ``tunstrap`` is a hard failure rather than a skip: it means the
    suite was launched without the venv on PATH, and silently skipping would
    report a green tier that tested nothing.
    """
    if sys.platform != "linux":
        skip_or_fail("e2e tier requires Linux + Docker")
    if shutil.which("tunstrap") is None:
        pytest.fail(
            "tunstrap is not on PATH. Run the e2e tier as:\n"
            '  PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/e2e -m e2e -q'
        )


@pytest.fixture(scope="session")
def e2e_ssh_keypair() -> tuple[str, str]:
    """Generate (once) and return this suite's own Ed25519 keypair."""
    keys_dir = HERE / "_keys"
    keys_dir.mkdir(exist_ok=True)
    priv_path = keys_dir / "id_test"
    pub_path = keys_dir / "id_test.pub"
    if not priv_path.exists() or not pub_path.exists():
        # cryptography, not paramiko: paramiko 4 dropped Ed25519Key.generate,
        # and cryptography is already a hard dependency (pyproject.toml).
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv_obj = Ed25519PrivateKey.generate()
        priv_path.write_text(
            priv_obj.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.OpenSSH,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode()
        )
        os.chmod(priv_path, 0o600)
        public_line = (
            priv_obj.public_key()
            .public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )
        pub_path.write_text(public_line + " tunstrap-e2e\n")
        os.chmod(pub_path, 0o644)
    return priv_path.read_text(), pub_path.read_text()


@pytest.fixture(scope="session")
def kind_cluster(e2e_preflight: None) -> Iterator[str]:
    """Create `tunstrap-e2e` from a pinned node image; always delete it after."""
    del e2e_preflight  # ordering only
    # Deliberately not "kubectl": this tier never runs a host kubectl. The oracle
    # runs the version-matched binary *inside* the node image via docker exec
    # (see rig.kubectl_in_node), and `kind` shells out to no kubectl of its own.
    # Requiring it here would skip locally - and, under
    # TUNSTRAP_E2E_REQUIRE_ALL=1, fail CI - over a binary nothing uses. A later
    # task that genuinely needs a host kubectl should require it where it uses it.
    require_tools("docker", "kind")

    # `try` opens *before* the pre-delete, so the teardown covers cluster
    # creation too. `kind` self-cleans when it exits non-zero itself, but a
    # KeyboardInterrupt propagates straight out of subprocess.run - and Ctrl-C
    # during a ~35s create is a routine developer action, not an edge case.
    # Opened here rather than after the create, which would leave exactly that
    # expensive window uncovered.
    try:
        # A crashed prior run can leave a half-configured cluster of this name,
        # which would silently change every result. Delete first,
        # unconditionally. This also absorbs a cluster leaked by a SIGKILLed run,
        # where no teardown can possibly have run.
        subprocess.run(
            ["kind", "delete", "cluster", "--name", CLUSTER_NAME],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            [
                "kind",
                "create",
                "cluster",
                "--name",
                CLUSTER_NAME,
                "--image",
                NODE_IMAGE,
                "--wait",
                "90s",
            ],
            check=True,
        )
        ready = kubectl_in_node("get", "nodes", "-o", "name")
        if ready.returncode != 0 or ready.stdout.strip() != f"node/{CONTROL_PLANE}":
            pytest.fail(
                "kind cluster came up but the in-node kubectl oracle does not work: "
                f"rc={ready.returncode} stdout={ready.stdout!r} stderr={ready.stderr!r}"
            )
        yield CLUSTER_NAME
    finally:
        # Captured, not inherited: an uncaptured delete prints kind's progress
        # over `-q` output. check=False because a teardown must not mask the
        # failure that got us here - but a silent failed delete leaks ~1 GB, so
        # it is reported rather than swallowed.
        removed = subprocess.run(
            ["kind", "delete", "cluster", "--name", CLUSTER_NAME],
            check=False,
            capture_output=True,
            text=True,
        )
        if removed.returncode != 0:
            warnings.warn(
                f"failed to delete kind cluster {CLUSTER_NAME!r} "
                f"(rc={removed.returncode}); it is still running and will consume "
                f"~1 GB until removed: {removed.stderr.strip()}",
                stacklevel=1,
            )


@pytest.fixture(scope="session")
def node_kubeconfig(kind_cluster: str) -> Iterator[Path]:
    """Copy the control plane's in-node kubeconfig to tests/e2e/_kube/admin.conf.

    `/etc/kubernetes/admin.conf` is the file the compose rig mounts at
    /etc/kube/admin.conf and that `kube_targets` reads over SSH - exactly as a
    consumer reads /etc/rancher/k3s/k3s.yaml. Its `server:` is a DNS name that
    resolves on the `kind` network, and that name is in the apiserver cert's
    DNS SANs, so `choose_tls_server_name` returns it as an exact match and the
    tier exercises the clean, warning-free path.
    """
    del kind_cluster  # ordering only
    kube_dir = HERE / "_kube"
    kube_dir.mkdir(exist_ok=True)

    # The Docker daemon creates a missing bind-mount source directory as root,
    # and the compose rig declares `./_kube:/etc/kube:ro`. So on any machine
    # where compose has ever come up before this fixture ran, `_kube` already
    # exists owned by root: mkdir(exist_ok=True) succeeds silently and the write
    # below dies with a bare EACCES that names no cause. Ordering this fixture
    # ahead of compose-up avoids *creating* that state but cannot heal a machine
    # that already has it, so the condition is detected here and reported with
    # its remedy.
    if not os.access(kube_dir, os.W_OK):
        pytest.fail(
            f"{kube_dir} exists but is not writable by this user - it is almost "
            "certainly root-owned, created by the Docker daemon for the "
            "./_kube bind mount before this fixture ran. Remove it and re-run:\n"
            "  sudo rm -rf tests/e2e/_kube"
        )

    dest = kube_dir / "admin.conf"
    # The `try` covers the write, not just the yield. Previously the write sat
    # outside it, so a failure there left `_kube` behind with no teardown armed.
    try:
        dumped = subprocess.run(
            ["docker", "exec", CONTROL_PLANE, "cat", "/etc/kubernetes/admin.conf"],
            capture_output=True,
            text=True,
            check=True,
        )
        if f"server: https://{CONTROL_PLANE}:6443" not in dumped.stdout:
            pytest.fail(
                "in-node kubeconfig does not name the control plane by DNS; the "
                "forward target would be wrong. Got:\n" + dumped.stdout
            )
        dest.write_text(dumped.stdout)
        # 0644, not 0600: the unprivileged `tester` user inside sshd-kube reads it.
        os.chmod(dest, 0o644)
        yield dest
    finally:
        # ignore_errors so a teardown failure cannot mask a real one, but not
        # silently: a `_kube` that survives is exactly the sticky root-owned
        # state the guard above has to fail on next run.
        shutil.rmtree(kube_dir, ignore_errors=True)
        if kube_dir.exists():
            warnings.warn(
                f"could not remove {kube_dir}; it likely holds root-owned "
                "contents and will fail the next run's writability guard. "
                "Remove it with:\n  sudo rm -rf tests/e2e/_kube",
                stacklevel=1,
            )
