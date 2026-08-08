"""OutputSchema / NodeOutput / FetchedFile shape.

Validates: FetchedFile XOR invariant (success XOR error), NodeOutput
defaults, OutputSchema JSON round-trip with fetched-files payload.
Code: tunstrap/schemas.py
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tunstrap.schemas import (
    FetchedFile,
    NodeOutput,
    OutputSchema,
    TunnelWarning,
)

pytestmark = pytest.mark.unit


def test_fetched_file_success_shape() -> None:
    """Success FetchedFile has content/size/sha256 and no error."""
    ff = FetchedFile(content_b64="YQ==", size=1, sha256="ca978112...")
    assert ff.error is None


def test_fetched_file_error_shape() -> None:
    """Error FetchedFile carries error code only; payload fields are None."""
    ff = FetchedFile(error="SSH_FX_NO_SUCH_FILE")
    assert ff.content_b64 is None
    assert ff.size is None
    assert ff.sha256 is None


def test_fetched_file_rejects_empty() -> None:
    """Empty FetchedFile (no branch populated) is rejected."""
    with pytest.raises(ValidationError):
        FetchedFile()


def test_fetched_file_rejects_both_branches() -> None:
    """A FetchedFile cannot have both success payload and an error label."""
    with pytest.raises(ValidationError):
        FetchedFile(content_b64="YQ==", size=1, sha256="x", error="x")


def test_fetched_file_path_defaults_none_and_is_not_part_of_the_xor() -> None:
    """path defaults to None and is set independently by materialization,
    mirroring KubeTargetOutput.path -- it is not part of the success/error xor."""
    ff = FetchedFile(content_b64="YQ==", size=1, sha256="ca978112...")
    assert ff.path is None
    materialized = ff.model_copy(update={"path": "/s/tunnel-data/a-kubeconfig"})
    assert materialized.path == "/s/tunnel-data/a-kubeconfig"


def test_node_output_default_fetch_files_empty() -> None:
    """NodeOutput.fetch_files defaults to an empty dict when omitted."""
    no = NodeOutput(ports={"p": 40000})
    assert no.fetch_files == {}


def test_output_schema_round_trip_with_fetch_files() -> None:
    """OutputSchema with fetched files round-trips via model_dump_json."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(
                ports={"p": 40000},
                fetch_files={
                    "kubeconfig": FetchedFile(
                        content_b64="YXBpVmVyc2lvbjogdjEK",
                        size=15,
                        sha256="abc123",
                    ),
                    "ca": FetchedFile(error="SSH_FX_NO_SUCH_FILE"),
                },
            )
        },
        pid=1,
        session_dir="/tmp/x",
        started_at="2026-05-19T10:00:00Z",
        warnings=[TunnelWarning(node="b", error="x")],
    )
    rebuilt = OutputSchema.model_validate_json(out.model_dump_json())
    assert rebuilt == out
