"""CLI start input validation.

Validates: tunstrap/cli.py start command rejects invalid stdin and
legacy fields with structured SchemaValidationError output.
Code: tunstrap/cli.py
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from tunstrap.cli import main

pytestmark = pytest.mark.unit


def test_start_rejects_invalid_json_with_exit_1() -> None:
    """Non-JSON stdin is reported as SchemaValidationError (exit 1)."""
    result = CliRunner().invoke(main, ["start"], input="not json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "SchemaValidationError"


def test_start_rejects_legacy_require_field() -> None:
    """The retired top-level `require` field is rejected by extra=forbid."""
    body = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            },
            "require": ["a"],
        }
    )
    result = CliRunner().invoke(main, ["start"], input=body)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "SchemaValidationError"
    assert "require" in json.dumps(payload["details"])


def test_start_field_validation_retains_location_and_message() -> None:
    """Stripping field input retains the location and message needed to fix it."""
    body = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "port": "not-a-port",
                    "user": "u",
                    "ssh_pkey": "valid-private-key",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )

    result = CliRunner().invoke(main, ["start"], input=body)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    error = payload["details"]["errors"][0]
    assert error["loc"] == ["nodes", "a", "port"]
    assert "valid integer" in error["msg"]


def test_start_model_validation_does_not_print_ssh_pkey() -> None:
    """A node-level validator must not expose its complete input on stdout."""
    secret = "MODEL-LEVEL-PRIVATE-KEY"
    body = json.dumps(
        {
            "nodes": {
                "a": {"host": "h", "user": "u", "ssh_pkey": secret},
            }
        }
    )

    result = CliRunner().invoke(main, ["start"], input=body)

    assert result.exit_code == 1
    assert secret not in result.output
    payload = json.loads(result.output)
    error = payload["details"]["errors"][0]
    assert error["loc"] == ["nodes", "a"]
    assert "node must define at least one" in error["msg"]


def test_start_model_validation_does_not_print_ssh_pkey_passphrase() -> None:
    """A node-level validator must not expose an SSH key passphrase on stdout."""
    secret = "MODEL-LEVEL-PRIVATE-KEY-PASSPHRASE"
    body = json.dumps(
        {
            "nodes": {
                "a": {"host": "h", "user": "u", "ssh_pkey_passphrase": secret},
            }
        }
    )

    result = CliRunner().invoke(main, ["start"], input=body)

    assert result.exit_code == 1
    assert secret not in result.output
    payload = json.loads(result.output)
    error = payload["details"]["errors"][0]
    assert error["loc"] == ["nodes", "a"]
    assert "node must define at least one" in error["msg"]


def test_start_nodes_validator_does_not_print_any_node_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nodes-wide validator must not expose secrets from valid sibling nodes."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    pkey_secret = "VALID-NODE-PRIVATE-KEY"
    password_secret = "ANOTHER-VALID-NODE-PASSWORD"
    body = json.dumps(
        {
            "nodes": {
                "key-node": {
                    "host": "key-host",
                    "user": "u",
                    "ssh_pkey": pkey_secret,
                    "remote_targets": {"p": "127.0.0.1:22"},
                },
                "password-node": {
                    "host": "password-host",
                    "user": "u",
                    "ssh_password": password_secret,
                    "remote_targets": {"p": "127.0.0.1:22"},
                },
                "unauthenticated-node": {
                    "host": "missing-auth-host",
                    "user": "u",
                    "remote_targets": {"p": "127.0.0.1:22"},
                },
            }
        }
    )

    result = CliRunner().invoke(main, ["start"], input=body)

    assert result.exit_code == 1
    assert pkey_secret not in result.output
    assert password_secret not in result.output
    payload = json.loads(result.output)
    error = payload["details"]["errors"][0]
    assert error["loc"] == ["nodes"]
    assert "unauthenticated-node" in error["msg"]


def test_start_nested_remote_target_validation_does_not_print_input() -> None:
    """Nested pydantic errors must not interpolate their invalid input into stdout."""
    secret = "NESTED-REMOTE-TARGET-PRIVATE-KEY"
    body = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "valid-password",
                    "remote_targets": {"p": {"host": "target", "port": 22, "ssh_pkey": secret}},
                }
            }
        }
    )

    result = CliRunner().invoke(main, ["start"], input=body)

    assert result.exit_code == 1
    assert secret not in result.output
    payload = json.loads(result.output)
    error = payload["details"]["errors"][0]
    assert error["loc"] == ["nodes", "a", "remote_targets"]
    assert "invalid dict form" in error["msg"]
