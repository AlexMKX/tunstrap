"""InputSchema from an environment variable (`run --input-env VAR`).

Validates: build_schema_from_env parses and validates exactly like start's
stdin path, and turns every failure into a SchemaValidationError (exit 1)
whose details never carry the offending input.
Code: tunstrap/cli_input.py
Assertion: the happy path equals the stdin-parsed equivalent; each failure
mode raises SchemaValidationError with the documented details keys and no
ssh_pkey anywhere in the serialised error.
Method: monkeypatch.setenv plus direct calls; no CLI, no daemon.
"""

from __future__ import annotations

import json

import pytest

from tunstrap.cli_input import build_schema_from_env
from tunstrap.exceptions import SchemaValidationError
from tunstrap.schemas import InputSchema

pytestmark = pytest.mark.unit

VAR = "TUNSTRAP_INPUT_TEST"

_NODE = {
    "host": "h.example.net",
    "user": "u",
    "ssh_password": "p",
    "remote_targets": {"db": "127.0.0.1:5432"},
}


def test_valid_single_node_equals_stdin_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env path yields the same InputSchema the stdin path would."""
    raw = json.dumps({"nodes": {"node": _NODE}})
    monkeypatch.setenv(VAR, raw)
    assert build_schema_from_env(VAR) == InputSchema.model_validate(json.loads(raw))


def test_valid_multi_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-node payloads parse; run's own gate decides what to do with them."""
    monkeypatch.setenv(VAR, json.dumps({"nodes": {"a": _NODE, "b": dict(_NODE)}}))
    schema = build_schema_from_env(VAR)
    assert sorted(schema.nodes) == ["a", "b"]


@pytest.mark.parametrize("value", [None, "", "   \n\t "])
def test_unset_empty_or_whitespace(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Unset, empty and whitespace-only are the same failure, and name the var."""
    if value is None:
        monkeypatch.delenv(VAR, raising=False)
    else:
        monkeypatch.setenv(VAR, value)
    with pytest.raises(SchemaValidationError) as excinfo:
        build_schema_from_env(VAR)
    assert excinfo.value.details == {"var": VAR}


def test_malformed_json_reports_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON content reports the decoder's byte position, like stdin does."""
    monkeypatch.setenv(VAR, "{invalid")
    with pytest.raises(SchemaValidationError) as excinfo:
        build_schema_from_env(VAR)
    assert excinfo.value.details["var"] == VAR
    assert isinstance(excinfo.value.details["position"], int)


def test_schema_invalid_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid JSON that is not an InputSchema reports pydantic's errors list."""
    monkeypatch.setenv(VAR, json.dumps({"nodes": {"node": {"host": "h"}}}))
    with pytest.raises(SchemaValidationError) as excinfo:
        build_schema_from_env(VAR)
    assert excinfo.value.details["var"] == VAR
    assert excinfo.value.details["errors"], "pydantic errors must be surfaced"


def test_schema_error_never_echoes_the_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed node must not echo ssh_pkey back through the error envelope."""
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nDEADBEEF\n"
    monkeypatch.setenv(
        VAR,
        json.dumps({"nodes": {"node": {"user": "u", "ssh_pkey": secret, "port": "not-an-int"}}}),
    )
    with pytest.raises(SchemaValidationError) as excinfo:
        build_schema_from_env(VAR)
    assert "DEADBEEF" not in json.dumps(excinfo.value.to_error_output())
