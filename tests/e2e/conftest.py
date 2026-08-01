"""E2E fixtures: a kind cluster plus one sshd node joined to kind's network.

Constants and helpers live in ``tests/e2e/rig.py``; this file holds fixtures
only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    require_tools("docker", "kind", "kubectl")

    # A crashed prior run can leave a half-configured cluster of this name, which
    # would silently change every result. Delete first, unconditionally.
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
    try:
        ready = kubectl_in_node("get", "nodes", "-o", "name")
        if ready.returncode != 0 or ready.stdout.strip() != f"node/{CONTROL_PLANE}":
            pytest.fail(
                "kind cluster came up but the in-node kubectl oracle does not work: "
                f"rc={ready.returncode} stdout={ready.stdout!r} stderr={ready.stderr!r}"
            )
        yield CLUSTER_NAME
    finally:
        subprocess.run(
            ["kind", "delete", "cluster", "--name", CLUSTER_NAME],
            check=False,
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
    dest = kube_dir / "admin.conf"
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
    try:
        yield dest
    finally:
        shutil.rmtree(kube_dir, ignore_errors=True)
