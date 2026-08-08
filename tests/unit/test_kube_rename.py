"""rename_identities: deterministic tunstrap-<node>-<target> identity rename."""

from __future__ import annotations

import pytest

from tunstrap.kube import rename_identities

pytestmark = pytest.mark.unit


def _doc() -> dict[str, object]:
    return {
        "current-context": "default",
        "contexts": [
            {"name": "default", "context": {"cluster": "default", "user": "default"}},
            {"name": "other", "context": {"cluster": "other-c", "user": "other-u"}},
        ],
        "clusters": [
            {"name": "default", "cluster": {"server": "https://127.0.0.1:1"}},
            {"name": "other-c", "cluster": {"server": "https://127.0.0.1:2"}},
        ],
        "users": [{"name": "default", "user": {}}, {"name": "other-u", "user": {}}],
    }


def test_renames_current_context_cluster_and_user_to_shared_name() -> None:
    """All three identity fields get the same tunstrap-<node>-<target> value."""
    doc = _doc()
    new_name = rename_identities(doc, "node-a", "kube")
    assert new_name == "tunstrap-node-a-kube"
    assert doc["current-context"] == new_name
    ctx = doc["contexts"][0]
    assert ctx["name"] == new_name
    assert ctx["context"]["cluster"] == new_name
    assert ctx["context"]["user"] == new_name
    assert doc["clusters"][0]["name"] == new_name
    assert doc["users"][0]["name"] == new_name


def test_ignored_entries_are_left_untouched() -> None:
    """Non-current context/cluster/user entries survive byte-stable."""
    doc = _doc()
    rename_identities(doc, "node-a", "kube")
    assert doc["contexts"][1] == {
        "name": "other",
        "context": {"cluster": "other-c", "user": "other-u"},
    }
    assert doc["clusters"][1]["name"] == "other-c"
    assert doc["users"][1]["name"] == "other-u"


def test_two_nodes_same_upstream_names_get_distinct_results() -> None:
    """The same input produces a different name for each node."""
    assert rename_identities(_doc(), "a", "kube") != rename_identities(_doc(), "b", "kube")


def test_ignored_context_sharing_active_cluster_keeps_valid_reference() -> None:
    """Shared cluster/user references in ignored contexts are updated."""
    doc: dict[str, object] = {
        "current-context": "default",
        "contexts": [
            {"name": "default", "context": {"cluster": "default", "user": "default"}},
            {"name": "staging", "context": {"cluster": "default", "user": "default"}},
        ],
        "clusters": [{"name": "default", "cluster": {"server": "https://127.0.0.1:1"}}],
        "users": [{"name": "default", "user": {}}],
    }
    new_name = rename_identities(doc, "node-a", "kube")
    staging = doc["contexts"][1]
    assert staging["name"] == "staging"
    assert staging["context"]["cluster"] == new_name
    assert staging["context"]["user"] == new_name
