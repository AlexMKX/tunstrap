"""Guard the kubectl/node-image version coupling the CI comment asserts.

`.github/workflows/test.yml` installs a kubectl whose version must match the
kindest/node image pinned as ``NODE_IMAGE`` in tests/e2e/rig.py - the workflow
comment at the kubectl step says so explicitly. Nothing enforced the coupling:
the kubectl pin carries a ``# renovate:`` annotation but the node image does
not, and even if it did, ``kubernetes/kubernetes`` and ``kindest/node`` are
different Renovate datasources that bump independently, so annotations cannot
make the two move together. The annotations are inert today anyway (org
Renovate autodiscovery is scoped to ``garuda-tunnel/*-internal``; this repo is
``AlexMKX/tunstrap`` - see the a410db3 CAVEAT).

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
    """
    rig = _RIG.read_text()
    node = _NODE_RE.search(rig)
    assert node is not None, (
        f"could not parse a kindest/node vX.Y.Z from {_RIG}; the NODE_IMAGE "
        f"assignment format has changed or moved"
    )

    workflow = _WORKFLOW.read_text()
    kubectl = _KUBECTL_RE.search(workflow)
    assert kubectl is not None, (
        f"could not parse a kubectl vX.Y.Z pin from {_WORKFLOW}; the "
        f"dl.k8s.io/release/vX.Y.Z/ URL pattern has changed or is gone"
    )

    assert kubectl.group(1) == node.group(1), (
        f"kubectl pin v{kubectl.group(1)} does not match NODE_IMAGE "
        f"(kindest/node v{node.group(1)} in {_RIG}). These must move together: "
        f"bump both the dl.k8s.io/release/vX.Y.Z/ URL in {_WORKFLOW} and "
        f"NODE_IMAGE in tests/e2e/rig.py, or the e2e cluster and its kubectl "
        f"will skew."
    )
