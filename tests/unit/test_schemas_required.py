"""Per-node required policy + retired top-level require field.

Validates: NodeInput.required defaults to True; the legacy `require`
field on InputSchema is rejected by extra=forbid.
Code: tunstrap/schemas.py
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.unit.conftest import make_node
from tunstrap.schemas import InputSchema

pytestmark = pytest.mark.unit


def test_node_required_defaults_to_true() -> None:
    """NodeInput.required defaults to True when omitted."""
    schema = InputSchema.model_validate({"nodes": {"a": make_node()}})
    assert schema.nodes["a"].required is True


def test_node_required_explicit_false() -> None:
    """NodeInput.required honours an explicit False."""
    schema = InputSchema.model_validate({"nodes": {"a": make_node(required=False)}})
    assert schema.nodes["a"].required is False


def test_input_schema_rejects_legacy_require_field() -> None:
    """The retired top-level `require` string field is rejected."""
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate({"nodes": {"a": make_node()}, "require": "*"})
    assert "require" in str(excinfo.value)


def test_input_schema_rejects_legacy_require_list() -> None:
    """The retired top-level `require` list field is rejected."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate({"nodes": {"a": make_node()}, "require": ["a"]})


def test_mixed_required_round_trip() -> None:
    """A mixed required/optional schema round-trips through model_dump."""
    payload = {
        "nodes": {
            "a": make_node(),
            "b": make_node(required=False),
        }
    }
    schema = InputSchema.model_validate(payload)
    rebuilt = InputSchema.model_validate(schema.model_dump())
    assert rebuilt.nodes["a"].required is True
    assert rebuilt.nodes["b"].required is False
