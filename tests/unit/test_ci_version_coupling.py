"""Guard the kubectl/node-image version coupling the CI comment asserts.

`.github/workflows/test.yml` installs a kubectl whose version must match the
kindest/node image pinned as ``NODE_IMAGE`` in tests/e2e/rig.py - the workflow
comment at the kubectl step says so explicitly. Nothing enforced the coupling:
the kubectl pin carries a ``# renovate:`` annotation but the node image does
not, and even if it did, ``kubernetes/kubernetes`` and ``kindest/node`` are
different Renovate datasources that bump independently, so annotations cannot
make the two move together. The annotations are inert today anyway: this repo
is outside the organization's Renovate autodiscovery scope, so nothing acts on
the ``# renovate:`` comments here regardless of how carefully they are kept
in sync.

A test is therefore the only enforcement that works regardless of enrolment.
Lives in the unit tier (not e2e) on purpose: the e2e job ``needs: unit``, so a
divergence fails the unit job and skips cluster setup rather than surfacing as
a confusing kubectl/cluster skew mid-run. Needs no cluster - two file reads.
It reads rig.py as TEXT rather than importing it, so the unit tier stays
decoupled from the e2e package's code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RIG = REPO_ROOT / "tests" / "e2e" / "rig.py"
_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

# kindest/node:vX.Y.Z -> the Kubernetes version baked into the node image.
_NODE_RE = re.compile(r'NODE_IMAGE\s*=\s*"kindest/node:v(\d+\.\d+)\.\d+"')
# dl.k8s.io/release/vX.Y.Z/ -> the kubectl the CI installs.
_KUBECTL_RE = re.compile(r"dl\.k8s\.io/release/v(\d+\.\d+)\.\d+")


def test_kubectl_pin_matches_node_image_pin() -> None:
    """The CI kubectl minor version matches the kind node image's.

    Asserted at the minor (X.Y) level: kubectl/cluster skew policy tolerates
    +/-1 minor, but the recipe's intent is an exact match, and the divergence
    that must not pass silently is a minor skew - e.g. a Renovate bump of
    kubectl 1.34 -> 1.35 while the node stays 1.34. Patch differences
    (1.34.0 vs 1.34.1) are benign - kubectl and kindest/node release patches
    independently - so an exact-version assertion would false-positive on those.

    Fails-when-broken verbatim red recorded in the task report: node held at
    1.34, kubectl pin temporarily raised to 1.35.

    ``re.search`` binds to the *first* ``dl.k8s.io`` URL in the workflow. With
    one kubectl-install step that is fine, but a second step (an arm64
    runner, a macOS job, ...) would leave the guard checking only the first
    match while the second drifts unwatched - silently green. ``findall`` plus
    an exactly-one assertion turns that into a loud, named failure instead. A
    full YAML parse is deliberately not used here: it would let the test
    reason over `jobs`/`steps` structurally, but this guard only needs to
    reject a *second matching URL string*, which a plain-text scan already
    catches at a fraction of the cost.
    """
    rig = _RIG.read_text()
    node = _NODE_RE.search(rig)
    assert node is not None, (
        f"could not parse a kindest/node vX.Y.Z from {_RIG}; the NODE_IMAGE "
        f"assignment format has changed or moved"
    )

    workflow = _WORKFLOW.read_text()
    kubectl_matches = _KUBECTL_RE.findall(workflow)
    assert len(kubectl_matches) == 1, (
        f"expected exactly one dl.k8s.io/release/vX.Y.Z/ URL in {_WORKFLOW}, "
        f"found {len(kubectl_matches)}: {kubectl_matches}. A second kubectl "
        f"install step means this guard is only watching one of them - name "
        f"the new step explicitly or extend this test to cover it, rather "
        f"than letting the second URL drift unchecked."
    )
    kubectl_version = kubectl_matches[0]

    assert kubectl_version == node.group(1), (
        f"kubectl pin v{kubectl_version} does not match NODE_IMAGE "
        f"(kindest/node v{node.group(1)} in {_RIG}). These must move together: "
        f"bump both the dl.k8s.io/release/vX.Y.Z/ URL in {_WORKFLOW} and "
        f"NODE_IMAGE in tests/e2e/rig.py, or the e2e cluster and its kubectl "
        f"will skew."
    )
