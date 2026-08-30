"""Two parse_kubeconfig properties that look like dead weight and are not.

Regression: `parse_kubeconfig` was a single CC-23 function until it was split
into an orchestrator plus five section parsers (`_load_root`, `_context_refs`,
`_cluster_section`, `_user_section`, `_ignored_contexts`). The split was proven
behaviour-preserving by a throwaway 93-case characterization harness, but that
harness is not in the repo, so two properties it pinned were left unguarded.
Both survive a reading of the code that concludes they are redundant:

1. The `str()` calls on `cluster_name`/`user_name` are load-bearing. ruamel is
   loaded in round-trip mode, so a *quoted* YAML scalar comes back as a
   `ScalarString` subclass of `str`, not a `str`. `str(x)` therefore changes
   the field's runtime type, while `server=server` deliberately does not.
   "Both are already `str`, drop the call" is the natural cleanup and it is
   wrong. The measured consequence is a type change in `KubeconfigView`, which
   feeds `KubeTargetOutput` and the `--output-var` projection.

2. Validation order across the five helpers is observable. Every failure mode
   raises the same type (`KubeParseError`), so reordering two section calls, or
   the three `_string_field` extractions in the constructor call, changes only
   the *message* a caller sees for an input with more than one defect. Callers
   surface that message as the `kube_target` warning text.

Code: tunstrap/kube.py::parse_kubeconfig,
tunstrap/kube.py::_cluster_section, tunstrap/kube.py::_user_section,
tunstrap/kube.py::_string_field
Assertion: names the current context resolves to are plain `str` while the
scalars copied straight out of the document are not; and for an input with two
defects, the *first* check in source order is the one that reports. Both use
exact type identity / exact message equality, never `isinstance` or substring,
so a wrong-but-similar value cannot satisfy them.
Method: parse in-module byte literals (the quoting is the point, so it must be
visible at the assertion rather than hidden in a fixture file) and read the
runtime types back off the returned view, comparing against the raw scalars
still reachable through `view.doc`.
"""

from __future__ import annotations

import pytest

from tunstrap.kube import KubeParseError, parse_kubeconfig

pytestmark = pytest.mark.unit

# Every scalar double-quoted, so ruamel yields DoubleQuotedScalarString for all
# of them and the only thing that can flatten one back to `str` is an explicit
# coercion in parse_kubeconfig.
QUOTED_KUBECONFIG = (
    b"apiVersion: v1\n"
    b"kind: Config\n"
    b'current-context: "prod"\n'
    b"contexts:\n"
    b'- name: "prod"\n'
    b"  context:\n"
    b'    cluster: "c1"\n'
    b'    user: "u1"\n'
    b"clusters:\n"
    b'- name: "c1"\n'
    b"  cluster:\n"
    b'    server: "https://192.0.2.1:6443"\n'
    b'    certificate-authority-data: "Y0E="\n'
    b"users:\n"
    b'- name: "u1"\n'
    b"  user:\n"
    b'    client-certificate-data: "Y1I="\n'
)


def _kubeconfig(*, clusters: str, users: str) -> bytes:
    """A kubeconfig whose current context references cluster `c1` and user `u1`."""
    return (
        "apiVersion: v1\n"
        "kind: Config\n"
        "current-context: prod\n"
        "contexts:\n"
        "- name: prod\n"
        "  context: {cluster: c1, user: u1}\n"
        f"{clusters}"
        f"{users}"
    ).encode()


CLUSTER_OK = "clusters:\n- name: c1\n  cluster: {server: 'https://192.0.2.1:6443'}\n"
CLUSTER_ABSENT = "clusters: []\n"
CLUSTER_BAD_CA = (
    "clusters:\n- name: c1\n"
    "  cluster: {server: 'https://192.0.2.1:6443', certificate-authority-data: 7}\n"
)
USER_OK = "users:\n- name: u1\n  user: {}\n"
USER_ABSENT = "users: []\n"
USER_BAD_CERT = "users:\n- name: u1\n  user: {client-certificate-data: 7}\n"
USER_BAD_KEY = "users:\n- name: u1\n  user: {client-key-data: 7}\n"
USER_BAD_CERT_AND_KEY = (
    "users:\n- name: u1\n  user: {client-certificate-data: 7, client-key-data: 8}\n"
)


def test_context_refs_are_flattened_but_copied_scalars_are_not() -> None:
    """`str()` on the context's cluster/user refs changes the type; server keeps its own."""
    view = parse_kubeconfig(QUOTED_KUBECONFIG)
    doc = view.doc
    assert isinstance(doc, dict)
    ctx_body = doc["contexts"][0]["context"]
    raw_cluster_ref = ctx_body["cluster"]
    raw_user_ref = ctx_body["user"]
    raw_server = doc["clusters"][0]["cluster"]["server"]

    # Premise: round-trip mode really does hand back str SUBCLASSES here. If
    # ruamel ever stops doing so this fails first, with a clear reason, rather
    # than making the real assertions below silently vacuous.
    assert type(raw_cluster_ref) is not str
    assert type(raw_user_ref) is not str
    assert type(raw_server) is not str

    # Flattened by the explicit str() coercions.
    assert type(view.cluster_name) is str
    assert type(view.user_name) is str

    # Not coerced: these are the document's own scalars, handed through as-is.
    assert type(view.server) is type(raw_server)
    assert type(view.context_name) is not str
    assert type(view.certificate_authority_data) is not str

    # The coercion must preserve the value, not merely the type.
    assert view.cluster_name == "c1"
    assert view.user_name == "u1"
    assert view.server == "https://192.0.2.1:6443"


@pytest.mark.parametrize(
    ("clusters", "users", "expected"),
    [
        pytest.param(
            CLUSTER_ABSENT,
            USER_ABSENT,
            "cluster 'c1' not found",
            id="cluster-resolved-before-user",
        ),
        pytest.param(
            CLUSTER_BAD_CA,
            USER_BAD_CERT,
            "'c1' certificate-authority-data must be a string, got int",
            id="ca-extracted-before-client-certificate",
        ),
        pytest.param(
            CLUSTER_OK,
            USER_BAD_CERT_AND_KEY,
            "'u1' client-certificate-data must be a string, got int",
            id="client-certificate-extracted-before-client-key",
        ),
    ],
)
def test_first_defect_in_source_order_is_the_one_reported(
    clusters: str, users: str, expected: str
) -> None:
    """With two defects present, the earlier check reports and the later one stays silent."""
    with pytest.raises(KubeParseError) as excinfo:
        parse_kubeconfig(_kubeconfig(clusters=clusters, users=users))
    assert str(excinfo.value) == expected


@pytest.mark.parametrize(
    ("clusters", "users", "expected"),
    [
        pytest.param(CLUSTER_OK, USER_ABSENT, "user 'u1' not found", id="user-missing"),
        pytest.param(
            CLUSTER_OK,
            USER_BAD_CERT,
            "'u1' client-certificate-data must be a string, got int",
            id="client-certificate-malformed",
        ),
        pytest.param(
            CLUSTER_OK,
            USER_BAD_KEY,
            "'u1' client-key-data must be a string, got int",
            id="client-key-malformed",
        ),
    ],
)
def test_the_later_check_of_each_ordered_pair_really_fires_on_its_own(
    clusters: str, users: str, expected: str
) -> None:
    """Positive control for the ordering table: each deferred check is real.

    Without this, deleting a later check outright would leave every ordering
    row above green -- the row would be satisfied by the earlier defect alone
    and could no longer distinguish "checked second" from "never checked".
    """
    with pytest.raises(KubeParseError) as excinfo:
        parse_kubeconfig(_kubeconfig(clusters=clusters, users=users))
    assert str(excinfo.value) == expected
