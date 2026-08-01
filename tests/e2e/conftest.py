"""E2E fixtures: a kind cluster plus one sshd node joined to kind's network.

Constants and helpers live in ``tests/e2e/rig.py``; this file holds fixtures
only.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from tests.e2e.rig import HERE, skip_or_fail


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
