"""SSH private-key loading.

Validates: tunstrap/ssh.py::_load_client_keys accepts the
supported PEM key types, returns None when no key is configured, and
raises asyncssh.KeyImportError on garbage input.
Code: tunstrap/ssh.py
"""

from __future__ import annotations

import asyncssh
import pytest

from tests.unit.conftest import make_node
from tunstrap.schemas import InputSchema
from tunstrap.ssh import _load_client_keys

pytestmark = pytest.mark.unit


def _generate_pem(key_type: str) -> str:
    key = asyncssh.generate_private_key(key_type)
    pem = key.export_private_key()
    assert isinstance(pem, bytes)
    return pem.decode()


@pytest.mark.parametrize(
    "key_type",
    ["ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"],
)
def test_load_client_keys_accepts_supported_formats(key_type: str) -> None:
    """Each supported key type is loaded into exactly one client key."""
    pem = _generate_pem(key_type)
    schema = InputSchema.model_validate({"nodes": {"a": make_node(ssh_pkey=pem)}})
    keys = _load_client_keys(schema.nodes["a"])
    assert keys is not None
    assert len(keys) == 1


def test_load_client_keys_returns_none_when_pkey_absent() -> None:
    """Without ssh_pkey the loader returns None (password-only path)."""
    schema = InputSchema.model_validate({"nodes": {"a": make_node()}})
    assert _load_client_keys(schema.nodes["a"]) is None


def test_load_client_keys_rejects_garbage() -> None:
    """Invalid PEM data raises asyncssh.KeyImportError."""
    schema = InputSchema.model_validate({"nodes": {"a": make_node(ssh_pkey="not a key")}})
    with pytest.raises(asyncssh.KeyImportError):
        _load_client_keys(schema.nodes["a"])
