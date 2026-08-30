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


def test_include_input_false_is_load_bearing_for_a_non_dict_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolates ``include_input=False`` from the ``_scrub`` layer behind it.

    Two independent layers keep the PEM out of the error envelope, and the
    test above cannot tell them apart: its failure lands on ``port``, so
    pydantic's ``input`` is the whole node *dict*, which still has a literal
    ``ssh_pkey`` key -- and ``TunstrapError._scrub`` strips that by key name
    whether or not ``include_input=False`` was passed. Reverting the guard
    leaves that test green (verified).

    Here the failure lands *on* ``ssh_pkey`` and its input is a bare list, so
    there is no ``ssh_pkey`` key for ``_scrub`` to match on and the secret is
    just a string inside a list it copies verbatim. ``include_input=False`` is
    then the only thing standing between the PEM and stderr.

    Fails with the PEM in the rendered envelope if
    ``tunstrap/cli_input.py::build_single_node_schema`` drops its guard. The
    same shape would isolate the other error-rendering call sites; this pins
    the one whose docstring makes the claim.
    """
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nDEADBEEF\n"
    monkeypatch.setenv(
        VAR,
        json.dumps(
            {
                "nodes": {
                    "node": {
                        "user": "u",
                        "host": "h.example.net",
                        # A list where a string belongs: the error is reported
                        # against ssh_pkey itself, with the list as its input.
                        "ssh_pkey": [secret],
                        "remote_targets": {"p": "127.0.0.1:6443"},
                    }
                }
            }
        ),
    )
    with pytest.raises(SchemaValidationError) as excinfo:
        build_schema_from_env(VAR)

    # Guard the premise: if no error lands on ssh_pkey there is nothing for
    # include_input to leak, and the assertion below would be vacuous.
    locs = [error["loc"] for error in excinfo.value.details["errors"]]
    assert any("ssh_pkey" in loc for loc in locs), f"no error landed on ssh_pkey: {locs}"

    rendered = json.dumps(excinfo.value.to_error_output())
    assert "DEADBEEF" not in rendered, f"the private key reached the error envelope: {rendered}"
