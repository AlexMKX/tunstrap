"""Integration fixtures: docker compose lifecycle, SSH keypair, sample files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def started_daemons() -> list[str]:
    """Mutable list of session_dir strings produced by successful start calls."""
    return []


@pytest.fixture(scope="session")
def ssh_keypair() -> tuple[str, str]:
    keys_dir = HERE / "_keys"
    keys_dir.mkdir(exist_ok=True)
    priv_path = keys_dir / "id_test"
    pub_path = keys_dir / "id_test.pub"
    if not priv_path.exists():
        # Paramiko 4 dropped Ed25519Key.generate; use cryptography directly.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        priv_obj = Ed25519PrivateKey.generate()
        with priv_path.open("w") as fh:
            fh.write(
                priv_obj.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.OpenSSH,
                    encryption_algorithm=serialization.NoEncryption(),
                ).decode()
            )
        os.chmod(priv_path, 0o600)
        pub = (
            priv_obj.public_key()
            .public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )
        with pub_path.open("w") as fh:
            fh.write(pub + " test\n")
    return priv_path.read_text(), pub_path.read_text()


@pytest.fixture(scope="session")
def tunstrap_it_dir() -> Path:
    """Create /tmp/tunstrap-it/ with mode 0o1777 before any docker mount.

    Docker bind-mount on a missing host path creates it as root:0o755, which
    then prevents the in-runner test process from writing fixture files.
    Pre-creating the directory with sticky world-writable mode is the same
    pattern /tmp itself uses and works on every Linux runner.
    """
    root = Path("/tmp/tunstrap-it")
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o1777)
    return root


@pytest.fixture(scope="session")
def ssh_test_cluster(
    ssh_keypair: tuple[str, str],
    tunstrap_it_dir: Path,
) -> Iterator[dict[str, Any]]:
    del tunstrap_it_dir  # forces ordering only
    if sys.platform != "linux":
        pytest.skip("integration tests require Linux + Docker")
    compose_file = HERE / "docker-compose.yml"
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d", "--wait"],
        check=True,
    )
    try:
        services = ["sshd-a", "sshd-b", "sshd-c"]
        ports: dict[str, int] = {}
        for service in services:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "port", service, "2222"],
                capture_output=True,
                text=True,
                check=True,
            )
            host, port = result.stdout.strip().rsplit(":", 1)
            ports[service] = int(port)
        # Bastion service for cross-host forward tests.
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "port", "sshd-bastion", "2222"],
            capture_output=True,
            text=True,
            check=True,
        )
        _host, bastion_port_str = result.stdout.strip().rsplit(":", 1)
        bastion_port = int(bastion_port_str)

        # Wait for HTTP identity servers (no healthcheck, --wait does not cover them).
        import time

        for target_alias in ("target-1", "target-2"):
            for attempt in range(30):
                probe = subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(compose_file),
                        "exec",
                        "-T",
                        "sshd-bastion",
                        "nc",
                        "-z",
                        target_alias,
                        "80",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if probe.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                pytest.fail(f"HTTP identity server for {target_alias}:80 never came up")

        priv_pem, _pub = ssh_keypair
        yield {
            "ports": ports,
            "private_pem": priv_pem,
            "user": "tester",
            "host": "127.0.0.1",
            "bastion_port": bastion_port,
            "target_aliases": ["target-1", "target-2"],
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down", "-v"],
            check=False,
        )


@pytest.fixture(scope="session")
def prepared_files(tunstrap_it_dir: Path) -> dict[str, Path]:
    """Populate /tmp/tunstrap-it/ with fixtures bind-mounted into sshd containers."""
    root = tunstrap_it_dir

    kubeconfig = root / "kubeconfig"
    kubeconfig.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- name: test\n"
        "  cluster:\n"
        "    server: https://127.0.0.1:6443\n"
        "users: []\n"
        "contexts: []\n"
        'current-context: ""\n'
    )

    big = root / "big.bin"
    if not big.exists() or big.stat().st_size != 2 * (1 << 20):
        with big.open("wb") as fh:
            fh.write(os.urandom(2 * (1 << 20)))

    no_perm = root / "no-perm.txt"
    # ensure the path is writable across runs (in case last run left mode 000)
    if no_perm.exists():
        os.chmod(no_perm, 0o600)
    no_perm.write_text("secret\n")

    # Kube fixtures for kube_target integration tests (Phase 7).
    # `kube_k3s` points at the fake apiserver with valid SAN.
    # `kube_nosan` points at the SAN-less apiserver — exercises insecure_fallback.
    kube_k3s = root / "kube_k3s"
    kube_k3s.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- name: default\n"
        "  cluster:\n"
        "    server: https://fake-apiserver:6443\n"
        "    certificate-authority-data: Y2EtZGF0YQ==\n"
        "contexts:\n"
        "- name: default\n"
        "  context: { cluster: default, user: default }\n"
        "current-context: default\n"
        "users:\n"
        "- name: default\n"
        "  user:\n"
        "    client-certificate-data: Y2VydA==\n"
        "    client-key-data: a2V5\n"
    )

    kube_nosan = root / "kube_nosan"
    kube_nosan.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- name: default\n"
        "  cluster:\n"
        "    server: https://fake-apiserver-nosan:6443\n"
        "    certificate-authority-data: Y2EtZGF0YQ==\n"
        "contexts:\n"
        "- name: default\n"
        "  context: { cluster: default, user: default }\n"
        "current-context: default\n"
        "users:\n"
        "- name: default\n"
        "  user:\n"
        "    client-certificate-data: Y2VydA==\n"
        "    client-key-data: a2V5\n"
    )

    os.chmod(kubeconfig, 0o644)
    os.chmod(big, 0o644)
    os.chmod(no_perm, 0o000)
    os.chmod(kube_k3s, 0o644)
    os.chmod(kube_nosan, 0o644)

    return {
        "kubeconfig": kubeconfig,
        "big": big,
        "no_perm": no_perm,
        "kube_k3s": kube_k3s,
        "kube_nosan": kube_nosan,
    }


def tunstrap_start(stdin_payload: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run(
        ["tunstrap", "start"],
        input=json.dumps(stdin_payload),
        text=True,
        capture_output=True,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "json": json.loads(completed.stdout) if completed.stdout.strip() else None,
    }
