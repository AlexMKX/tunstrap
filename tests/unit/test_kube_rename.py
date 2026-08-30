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


# --- Structural-defect guards (issue #26) -------------------------------------
#
# rename_identities is a public function (exported via __all__) whose declared
# input is "the fetched kubeconfig ... untrusted input". It used to defend its
# structural assumptions with `assert`; under `python -O` those vanish and a
# DIRECT caller's malformed document degrades into a bare
# KeyError/TypeError/AttributeError, because the public contract would no
# longer hold. Each structural defect below must therefore raise KubeParseError
# so the contract is honoured for direct callers and `python -O` cannot erase
# the check.
#
# These seven raises are NOT reachable from run_kube_targets for the structural
# inputs: run_kube_targets always runs parse_kubeconfig on the same document
# first, and that dominates every case below (non-string current-context,
# absent context, non-mapping body, non-string cluster/user refs, missing
# cluster/user) before rename_identities sees the doc. They are defence-in-depth
# for direct/public callers. The only raise reachable from run_kube_targets is
# the reserved-namespace collision, whose daemon_error/exit-4 teardown path
# (the broad _worker._run guard turning one bad target into a whole-node
# teardown) is covered by test_pre_existing_identity_surfaces_as_per_target_warning
# in tests/unit/test_kube_run.py, not here.


def test_non_string_current_context_raises_kubeparseerror() -> None:
    """A non-string current-context is a typed KubeParseError, not an AssertionError.

    Under ``python -O`` the old ``assert isinstance(current, str)`` disappeared
    and ``_find_named(contexts, 42)`` returned None, so the failure surfaced as
    the missing-context branch instead of the real defect.
    """
    doc: dict[str, object] = {"current-context": 42, "contexts": [], "clusters": [], "users": []}
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    assert "not a string" in str(excinfo.value)


def test_current_context_missing_from_contexts_raises_kubeparseerror() -> None:
    """A current-context that names no entry in contexts raises KubeParseError."""
    doc: dict[str, object] = {
        "current-context": "ghost",
        "contexts": [{"name": "default", "context": {"cluster": "default", "user": "default"}}],
        "clusters": [],
        "users": [],
    }
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    assert "ghost" in str(excinfo.value)


def test_non_mapping_context_body_raises_kubeparseerror() -> None:
    """A context entry whose ``context`` is not a mapping raises KubeParseError."""
    doc: dict[str, object] = {
        "current-context": "default",
        "contexts": [{"name": "default", "context": "not-a-mapping"}],
        "clusters": [],
        "users": [],
    }
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    assert "default" in str(excinfo.value)


def test_non_string_cluster_reference_raises_kubeparseerror() -> None:
    """A context whose ``cluster`` ref is not a string raises KubeParseError."""
    doc: dict[str, object] = {
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": 7, "user": "default"}}],
        "clusters": [],
        "users": [],
    }
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    assert "cluster" in str(excinfo.value)
    assert "not a string" in str(excinfo.value)


def test_non_string_user_reference_raises_kubeparseerror() -> None:
    """A context whose ``user`` ref is not a string raises KubeParseError."""
    doc: dict[str, object] = {
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": "default", "user": 7}}],
        "clusters": [],
        "users": [],
    }
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    assert "user" in str(excinfo.value)
    assert "not a string" in str(excinfo.value)


def test_cluster_reference_not_in_clusters_raises_kubeparseerror() -> None:
    """A context pointing at a cluster absent from ``clusters`` raises KubeParseError.

    Also pins the all-rejections-leave-doc-unchanged invariant: the existence
    lookup was moved ahead of every mutation (it used to sit between the active
    context's name rewrite and its cluster/user name rewrite), so this rejection
    must not leave a half-renamed document.
    """
    doc: dict[str, object] = {
        "current-context": "default",
        "contexts": [
            {"name": "default", "context": {"cluster": "missing-cluster", "user": "default"}}
        ],
        "clusters": [{"name": "default", "cluster": {"server": "https://127.0.0.1:1"}}],
        "users": [{"name": "default", "user": {}}],
    }
    before = copy.deepcopy(doc)
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    assert "missing-cluster" in str(excinfo.value)
    assert doc == before, "rejection must leave the document unmutated"


def test_user_reference_not_in_users_raises_kubeparseerror() -> None:
    """A context pointing at a user absent from ``users`` raises KubeParseError.

    Same all-rejections-leave-doc-unchanged invariant as the cluster case: the
    existence lookup was relocated ahead of every mutation.
    """
    doc: dict[str, object] = {
        "current-context": "default",
        "contexts": [
            {"name": "default", "context": {"cluster": "default", "user": "missing-user"}}
        ],
        "clusters": [{"name": "default", "cluster": {"server": "https://127.0.0.1:1"}}],
        "users": [{"name": "default", "user": {}}],
    }
    before = copy.deepcopy(doc)
    with pytest.raises(KubeParseError) as excinfo:
        rename_identities(doc, "node", "kube")
    assert "missing-user" in str(excinfo.value)
    assert doc == before, "rejection must leave the document unmutated"
