"""FileSpec + NodeInput.fetch_files validation.

Validates: FileSpec absolute-path constraints, fetch_files key/value
limits, and required default semantics.
Code: tunstrap/schemas.py
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.unit.conftest import make_node
from tunstrap.schemas import FileSpec, InputSchema, NodeInput

pytestmark = pytest.mark.unit


def test_filespec_required_defaults_true() -> None:
    """FileSpec.required defaults to True when omitted."""
    spec = FileSpec.model_validate({"path": "/etc/k3s/k3s.yaml"})
    assert spec.required is True


def test_filespec_required_explicit_false() -> None:
    """FileSpec.required honours an explicit False."""
    spec = FileSpec.model_validate({"path": "/etc/x", "required": False})
    assert spec.required is False


def test_filespec_rejects_relative_path() -> None:
    """Relative paths are rejected (must be absolute)."""
    with pytest.raises(ValidationError):
        FileSpec.model_validate({"path": "etc/x"})


def test_filespec_rejects_tilde() -> None:
    """Tilde-prefixed paths are rejected (no shell expansion)."""
    with pytest.raises(ValidationError):
        FileSpec.model_validate({"path": "~/.kube/config"})


def test_filespec_rejects_empty_path() -> None:
    """An empty path is rejected by the validator."""
    with pytest.raises(ValidationError):
        FileSpec.model_validate({"path": ""})


def test_filespec_rejects_too_long_path() -> None:
    """Paths exceeding the documented length cap are rejected."""
    with pytest.raises(ValidationError):
        FileSpec.model_validate({"path": "/" + "a" * 4100})


def test_filespec_rejects_extra_field() -> None:
    """FileSpec is closed (extra='forbid')."""
    with pytest.raises(ValidationError):
        FileSpec.model_validate({"path": "/x", "extra": 1})


def test_node_fetch_files_default_none() -> None:
    """NodeInput.fetch_files defaults to None when omitted."""
    node = NodeInput.model_validate(make_node())
    assert node.fetch_files is None


def test_node_fetch_files_rejects_empty_dict() -> None:
    """An empty fetch_files dict is rejected (use None instead)."""
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate({"nodes": {"a": make_node(fetch_files={})}})
    assert "fetch_files" in str(excinfo.value)


def test_node_fetch_files_rejects_too_many_entries() -> None:
    """fetch_files rejects more than 16 entries per node."""
    files = {f"k{i}": {"path": f"/etc/x{i}"} for i in range(17)}
    with pytest.raises(ValidationError):
        InputSchema.model_validate({"nodes": {"a": make_node(fetch_files=files)}})


def test_node_fetch_files_rejects_bad_key_chars() -> None:
    """fetch_files keys must match the documented identifier pattern."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate(
            {"nodes": {"a": make_node(fetch_files={"bad name": {"path": "/x"}})}}
        )


def test_node_fetch_files_rejects_too_long_key() -> None:
    """fetch_files keys exceeding the length cap are rejected."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate(
            {"nodes": {"a": make_node(fetch_files={"a" * 65: {"path": "/x"}})}}
        )


def test_node_fetch_files_happy_path() -> None:
    """A well-formed fetch_files block is parsed into FileSpec values."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": make_node(
                    fetch_files={
                        "kubeconfig": {"path": "/etc/rancher/k3s/k3s.yaml"},
                        "ca": {"path": "/etc/ssl/ca.pem", "required": False},
                    }
                )
            }
        }
    )
    fs = schema.nodes["a"].fetch_files
    assert fs is not None
    assert fs["kubeconfig"].required is True
    assert fs["ca"].required is False
