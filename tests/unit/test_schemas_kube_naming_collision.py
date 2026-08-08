"""InputSchema rejects collisions in tunstrap-<node>-<target> names."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tunstrap.schemas import InputSchema

pytestmark = pytest.mark.unit


def test_naming_join_collision_across_nodes_is_rejected() -> None:
    """Different hyphenated pairs rendering one identity are rejected."""
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate(
            {
                "nodes": {
                    "a-b": {
                        "host": "h1",
                        "user": "u",
                        "ssh_password": "p",
                        "kube_targets": {"c": {"kubeconfig_path": "/etc/x.yaml"}},
                    },
                    "a": {
                        "host": "h2",
                        "user": "u",
                        "ssh_password": "p",
                        "kube_targets": {"b-c": {"kubeconfig_path": "/etc/y.yaml"}},
                    },
                }
            }
        )
    message = str(excinfo.value)
    assert "tunstrap-a-b-c" in message
    assert "a-b" in message and "c" in message
    assert "a" in message and "b-c" in message


def test_non_colliding_hyphenated_names_are_accepted() -> None:
    """Hyphens alone do not trigger the collision check."""
    InputSchema.model_validate(
        {
            "nodes": {
                "node-one": {
                    "host": "h1",
                    "user": "u",
                    "ssh_password": "p",
                    "kube_targets": {"kube-a": {"kubeconfig_path": "/etc/x.yaml"}},
                },
                "node-two": {
                    "host": "h2",
                    "user": "u",
                    "ssh_password": "p",
                    "kube_targets": {"kube-b": {"kubeconfig_path": "/etc/y.yaml"}},
                },
            }
        }
    )
