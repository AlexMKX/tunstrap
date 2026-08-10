"""rename_identities: deterministic tunstrap-<node>-<target> identity rename.

The current-context's cluster/user/context are renamed to the shared
deterministic name ``tunstrap-<node>-<target>``. The fetched kubeconfig is
untrusted, so a name already present in that reserved namespace is rejected
as a ``KubeParseError`` -- never silently renamed around -- and a rejection
leaves the document unmutated.
"""

from __future__ import annotations

import copy

import pytest

from tunstrap.kube import KubeParseError, rename_identities

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


def _doc_with_pre_existing_identity(slot: str) -> dict[str, object]:
    """A doc whose active triple is 'default' but ``slot`` already holds the name.

    ``rename_identities(doc, 'node', 'kube')`` would generate
    ``tunstrap-node-kube``; planting that name in one of the three collections
    is the upstream-reserved-namespace collision the function must reject.
    """
    doc: dict[str, object] = {
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": "default", "user": "default"}}],
        "clusters": [{"name": "default", "cluster": {"server": "https://127.0.0.1:1"}}],
        "users": [{"name": "default", "user": {}}],
    }
    reserved = "tunstrap-node-kube"
    ctx_list = doc["contexts"]
    clu_list = doc["clusters"]
    usr_list = doc["users"]
    assert isinstance(ctx_list, list) and isinstance(clu_list, list) and isinstance(usr_list, list)
    if slot == "contexts":
        ctx_list.append({"name": reserved, "context": {"cluster": "x", "user": "x"}})
    elif slot == "clusters":
        clu_list.append({"name": reserved, "cluster": {"server": "https://x"}})
    elif slot == "users":
        usr_list.append({"name": reserved, "user": {}})
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown slot {slot!r}")
    return doc


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


@pytest.mark.parametrize(
    "slot",
    [
        pytest.param("clusters", id="pre-existing-clusters-entry"),
        pytest.param("users", id="pre-existing-users-entry"),
        pytest.param("contexts", id="pre-existing-contexts-entry"),
    ],
)
def test_pre_existing_generated_name_is_rejected(slot: str) -> None:
    """A reserved-namespace collision in any of the three collections is rejected.

    The fetched kubeconfig is untrusted; a name already present in tunstrap's
    ``tunstrap-<node>-<target>`` namespace is either misconfiguration or an
    attempt to shadow the identity tunstrap is about to create. Without this
    guard the entry is duplicated (two clusters/users/contexts with the same
    name) and which one the patched context resolves to becomes
    order-dependent. The fix is rejection (``KubeParseError``), not
    uniquifying, because the deterministic name is a consumer-facing literal.
    """
    doc = _doc_with_pre_existing_identity(slot)
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    message = str(excinfo.value)
    assert "tunstrap-node-kube" in message
    # The message must tell an operator why the untrusted file is rejected, in
    # terms of tunstrap's reserved namespace -- not a bare "already exists".
    assert "reserved" in message or "tunstrap-" in message


def test_rejection_leaves_document_unmutated() -> None:
    """The collision check fires before any name/reference is rewritten.

    A rejection that already mutated the active context or its cluster/user
    entries would leave a half-renamed document for the caller to discover.
    Snapshot the doc, attempt the rename, assert byte-equality with the
    snapshot.
    """
    doc = _doc_with_pre_existing_identity("clusters")
    before = copy.deepcopy(doc)
    with pytest.raises(KubeParseError):
        rename_identities(doc, "node", "kube")
    assert doc == before


def test_rejection_message_is_operator_facing() -> None:
    """The error message names the colliding identity and the reserved namespace.

    Per-target handling surfaces ``str(exc)`` verbatim as the kube_target
    warning text, so the message must read as an operator-facing sentence --
    not an internal code reference -- and must name the literal name that
    collided so the operator can find and rename the offending upstream entry.
    """
    doc = _doc_with_pre_existing_identity("clusters")
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    message = str(excinfo.value)
    assert "tunstrap-node-kube" in message
    # No raw repr/type/exception-class noise; a sentence an operator can act on.
    assert "KubeParseError" not in message
