# Kube-targets Implementation Plan

> **Redaction/repoint note (2026-08-10):** Replaced ignored scratch-file paths
> with an accurate description of their untracked location; no plan step changed.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-contained "kube mode" to `tunstrap` so it reads a remote kubeconfig, forwards the apiserver port, probes the serving-cert SAN for `tls-server-name`, patches `server:`, and returns ready-to-use kubeconfig fields — eliminating the HCL-side reconstruction.

**Architecture:** A new `NodeInput.kube_targets` field drives a new `tunstrap/kube.py` module (parse current-context, resolve, probe SAN, patch via `ruamel.yaml`). `TunnelManager._start_one` calls it after forwards open. A new session-directory layer (`tunstrap/session.py`) hosts identity + optional materialized files under a well-known `tunnel-data/` subdir. CLI gains `start --session-dir` / `stop --session-dir`; legacy `stop --pid --token` is removed.

**Tech Stack:** Python 3.10+, pydantic 2.x, asyncssh (fork), click 8.x, `ruamel.yaml>=0.18,<0.19`, `cryptography` (already transitively available via integration tests; used for SAN parsing). Tests: pytest (`unit`/`integration` markers), pytest-asyncio (`asyncio_mode=auto`), mypy --strict, ruff, black, pylint (fail-under 9.0), vulture.

**Spec:** `docs/specs/2026-05-30-kube-targets-design.md`

---

## File Structure

**New files:**
- `tunstrap/kube.py` — kube-mode logic: parse current-context, choose SAN, patch kubeconfig. Pure-ish functions + one async orchestration entry. One responsibility: turn a fetched kubeconfig + a live SSH connection into a patched kubeconfig + extracted fields.
- `tunstrap/session.py` — session-directory lifecycle: resolve/validate `--session-dir`, create `tunnel-data/`, write identity, materialize files, cleanup (generated vs supplied).
- `tests/unit/test_schemas_kube.py` — `KubeTarget` + `NodeInput.kube_targets` validation.
- `tests/unit/test_kube_parse.py` — current-context parse + extraction + multi-context warning.
- `tests/unit/test_kube_san.py` — SAN selection + insecure_fallback branches.
- `tests/unit/test_kube_patch.py` — server/tls-server-name patch + byte-stability.
- `tests/unit/test_session_dir.py` — session-dir resolve/validate/cleanup.
- `tests/unit/test_output_kube.py` — `KubeTargetOutput` / `NodeOutput.kube_targets` / `OutputSchema.session_dir` shape + behavior.
- `tests/unit/fixtures/kube/` — kubeconfig fixtures (examples 1-3 from the spec).
- `tests/integration/test_kube_targets.py` — end-to-end forward+probe+patch, materialize, insecure_fallback.

**Modified files:**
- `tunstrap/schemas.py` — add `KubeTarget`, `NodeInput.kube_targets`, `DaemonOptions.materialize`, `KubeTargetOutput`, `NodeOutput.kube_targets`, `OutputSchema.session_dir`, `TunnelWarning` reuse.
- `tunstrap/manager.py` — call kube mode in `_start_one`; thread `kube_targets` + warnings into output; accept `session_dir` for materialization.
- `tunstrap/_worker.py` — resolve session dir, write identity into `tunnel-data/`, pass `session_dir` to manager + output, cleanup on exit.
- `tunstrap/daemon.py` — accept `session_dir` through spawn; pass to worker argv.
- `tunstrap/cli.py` — `start --session-dir`; rewrite `stop` to `--session-dir` only (remove `--pid/--token`).
- `tunstrap/identity.py` — allow identity files under a caller-supplied `tunnel-data/` dir (parameterize `_state_dir`).
- `pyproject.toml` — add `ruamel.yaml` + `cryptography` to dependencies.
- `README.md` — kube-mode section, opt-in materialization, host-key threat model, migration note.

---

## Phase 0 — Baseline

### Task 0.1: Record baseline test counts

**Files:**
- Record the baseline in the untracked artifacts directory.

- [ ] **Step 1: Run the unit suite and capture counts**

Run: `.venv/bin/pytest tests/unit -q`
Expected: a passing summary like `N passed`. Record the exact number.

- [ ] **Step 2: Run lint/type gates and capture status**

Run each and record PASS/FAIL:
```
.venv/bin/ruff format --check tunstrap
.venv/bin/ruff check tunstrap
.venv/bin/mypy --strict tunstrap
```
Expected: all clean on the untouched baseline.

- [ ] **Step 3: Write the baseline artifact**

Record the unit count, gate statuses, and date in the gitignored artifacts directory; do not attempt to commit that scratch file.

- [ ] **Step 4: No commit**

The artifact is untracked. Nothing to commit in this task.

---

## Phase 1 — Dependencies and input schema

### Task 1.1: Add runtime dependencies

**Files:**
- Modify: `pyproject.toml:9-13`

- [ ] **Step 1: Add the two new dependencies**

In `pyproject.toml`, change the `dependencies` array to add `ruamel.yaml` and `cryptography`:

```toml
dependencies = [
    "asyncssh @ git+https://github.com/AlexMKX/asyncssh.git@v2.23.0+forward-tracker.1",
    "pydantic>=2.13,<3",
    "click>=8.3,<9",
    "ruamel.yaml>=0.18,<0.19",
    "cryptography>=44,<46",
]
```

- [ ] **Step 2: Install into the dev venv**

Run: `.venv/bin/pip install -e ".[dev]"`
Expected: installs `ruamel.yaml` and `cryptography` without conflicts.

- [ ] **Step 3: Verify imports resolve**

Run: `.venv/bin/python -c "import ruamel.yaml; from cryptography import x509; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add ruamel.yaml and cryptography for kube mode"
```

### Task 1.2: RED — KubeTarget schema validation tests

**Files:**
- Create: `tests/unit/test_schemas_kube.py`

- [ ] **Step 1: Write the failing tests**

```python
"""KubeTarget + NodeInput.kube_targets validation.

Validates: KubeTarget path rules, default values, and kube_targets
key/value limits on NodeInput.
Code: tunstrap/schemas.py
Assertion: invalid paths/keys raise ValidationError; defaults resolve
to insecure_fallback=False and required=True.
Method: construct models via model_validate and assert resolved fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tunstrap.schemas import InputSchema, KubeTarget, NodeInput
from tests.unit.conftest import make_node

pytestmark = pytest.mark.unit


def test_kube_target_defaults() -> None:
    """KubeTarget defaults: insecure_fallback False, required True, tls hint None."""
    kt = KubeTarget.model_validate({"kubeconfig_path": "/etc/rancher/k3s/k3s.yaml"})
    assert kt.insecure_fallback is False
    assert kt.required is True
    assert kt.tls_server_name is None


def test_kube_target_rejects_relative_path() -> None:
    """A relative kubeconfig_path is rejected."""
    with pytest.raises(ValidationError):
        KubeTarget.model_validate({"kubeconfig_path": "etc/k3s.yaml"})


def test_kube_target_rejects_tilde_path() -> None:
    """A tilde-prefixed kubeconfig_path is rejected (no shell expansion)."""
    with pytest.raises(ValidationError):
        KubeTarget.model_validate({"kubeconfig_path": "~/.kube/config"})


def test_kube_target_rejects_extra_field() -> None:
    """KubeTarget is closed (extra='forbid')."""
    with pytest.raises(ValidationError):
        KubeTarget.model_validate({"kubeconfig_path": "/x", "bogus": 1})


def test_node_kube_targets_default_none() -> None:
    """NodeInput.kube_targets defaults to None when omitted."""
    node = NodeInput.model_validate(make_node())
    assert node.kube_targets is None


def test_node_kube_targets_rejects_empty_dict() -> None:
    """An empty kube_targets dict is rejected (omit instead)."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate({"nodes": {"a": make_node(kube_targets={})}})


def test_node_kube_targets_rejects_bad_key() -> None:
    """kube_targets keys must match the identifier pattern."""
    with pytest.raises(ValidationError):
        InputSchema.model_validate(
            {"nodes": {"a": make_node(kube_targets={"bad name": {"kubeconfig_path": "/x"}})}}
        )


def test_node_kube_targets_happy_path() -> None:
    """A well-formed kube_targets block parses into KubeTarget values."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": make_node(
                    kube_targets={"k3s": {"kubeconfig_path": "/etc/rancher/k3s/k3s.yaml"}}
                )
            }
        }
    )
    kt = schema.nodes["a"].kube_targets
    assert kt is not None
    assert kt["k3s"].kubeconfig_path == "/etc/rancher/k3s/k3s.yaml"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_schemas_kube.py -q`
Expected: FAIL — `ImportError: cannot import name 'KubeTarget'`.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/unit/test_schemas_kube.py
git commit -m "test: RED KubeTarget + kube_targets validation"
```

### Task 1.3: GREEN — KubeTarget model + NodeInput.kube_targets

**Files:**
- Modify: `tunstrap/schemas.py:100-185`

- [ ] **Step 1: Add the KubeTarget model after FileSpec**

Insert after the `FileSpec` class (before `RemoteTarget`):

```python
class KubeTarget(BaseModel):
    """One kubeconfig to fetch, forward, SAN-probe, and patch.

    Exactly one cluster is handled: the kubeconfig's current-context.
    `tls_server_name` overrides the SAN probe entirely. `insecure_fallback`
    governs what happens when no usable TLS name can be determined.
    """

    model_config = ConfigDict(extra="forbid")

    kubeconfig_path: str = Field(min_length=1, max_length=4096)
    tls_server_name: str | None = None
    insecure_fallback: bool = Field(
        default=False,
        description=(
            "If the SAN probe yields no usable name and no tls_server_name is "
            "given: True emits insecure-skip-tls-verify (drops CA) + a warning; "
            "False fails the target (subject to `required`)."
        ),
    )
    required: bool = Field(
        default=True,
        description="If False, this target's failure does not fail the node.",
    )

    @field_validator("kubeconfig_path")
    @classmethod
    def _validate_absolute(cls, value: str) -> str:
        if value.startswith("~"):
            raise ValueError("kubeconfig_path must be literal (no '~' expansion)")
        if not value.startswith("/"):
            raise ValueError("kubeconfig_path must be absolute (start with '/')")
        return value
```

- [ ] **Step 2: Add the kube_targets field + validator to NodeInput**

In `NodeInput`, add the field after `fetch_files`:

```python
    kube_targets: dict[str, KubeTarget] | None = None
```

And add a validator mirroring `_validate_fetch_files`:

```python
    @field_validator("kube_targets")
    @classmethod
    def _validate_kube_targets(
        cls, value: dict[str, KubeTarget] | None
    ) -> dict[str, KubeTarget] | None:
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("kube_targets: omit field instead of empty dict")
        if len(value) > 16:
            raise ValueError("kube_targets: at most 16 entries per node")
        for name in value:
            if len(name) > 64:
                raise ValueError(f"kube_targets key {name!r}: max 64 chars")
            if not _FETCH_FILES_KEY_RE.match(name):
                raise ValueError(
                    f"kube_targets key {name!r}: must match ^[a-zA-Z_][a-zA-Z0-9_-]*$"
                )
        return value
```

- [ ] **Step 3: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_schemas_kube.py -q`
Expected: PASS (8 passed).

- [ ] **Step 4: Run gates on the changed file**

Run:
```
.venv/bin/ruff format --check tunstrap/schemas.py
.venv/bin/ruff check tunstrap/schemas.py
.venv/bin/mypy --strict tunstrap/schemas.py
```
Expected: all clean.

- [ ] **Step 5: Commit**

```bash
git add tunstrap/schemas.py
git commit -m "feat(schemas): add KubeTarget and NodeInput.kube_targets"
```

### Task 1.4: GREEN — DaemonOptions.materialize

**Files:**
- Modify: `tunstrap/schemas.py:57-75`
- Create test inline in `tests/unit/test_schemas_kube.py`

- [ ] **Step 1: Write the failing test (append to test_schemas_kube.py)**

```python
def test_daemon_materialize_default_false() -> None:
    """DaemonOptions.materialize defaults to False."""
    schema = InputSchema.model_validate({"nodes": {"a": make_node()}})
    assert schema.daemon.materialize is False


def test_daemon_materialize_explicit_true() -> None:
    """DaemonOptions.materialize honours an explicit True."""
    schema = InputSchema.model_validate(
        {"nodes": {"a": make_node()}, "daemon": {"materialize": True}}
    )
    assert schema.daemon.materialize is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_schemas_kube.py -k materialize -q`
Expected: FAIL — `materialize` not a field / AttributeError.

- [ ] **Step 3: Add the field to DaemonOptions**

In `DaemonOptions`, after `auto_stop_idle_seconds`:

```python
    materialize: bool = Field(
        default=False,
        description=(
            "If True, fetched/patched files (e.g. kube_targets kubeconfig) are "
            "written mode 0600 into the session dir's tunnel-data/ and removed "
            "on stop/atexit. Default False keeps content off disk."
        ),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_schemas_kube.py -k materialize -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add tunstrap/schemas.py tests/unit/test_schemas_kube.py
git commit -m "feat(schemas): add DaemonOptions.materialize"
```

### Task 1.5: RED — output schema for kube_targets + session_dir

**Files:**
- Create: `tests/unit/test_output_kube.py`

- [ ] **Step 1: Write the failing tests**

```python
"""KubeTargetOutput / NodeOutput.kube_targets / OutputSchema.session_dir.

Validates: the output models carry the extracted kube fields and the
always-present session_dir.
Code: tunstrap/schemas.py
Assertion: a fully-populated KubeTargetOutput round-trips; NodeOutput
defaults kube_targets to {}; OutputSchema requires session_dir.
Method: construct models and assert field values / required errors.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tunstrap.schemas import (
    KubeTargetOutput,
    NodeOutput,
    OutputSchema,
)

pytestmark = pytest.mark.unit


def _kube_output(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cluster_name": "production",
        "context_name": "production",
        "local_port": 40123,
        "endpoint": "https://127.0.0.1:40123",
        "tls_server_name": "am.prod.kube.example.net",
        "certificate_authority_data": "Y2E=",
        "client_certificate_data": "Y2VydA==",
        "client_key_data": "a2V5",
        "content_b64": "a3ViZWNvbmZpZw==",
        "path": None,
    }
    base.update(overrides)
    return base


def test_kube_target_output_roundtrip() -> None:
    """A fully-populated KubeTargetOutput preserves all fields."""
    out = KubeTargetOutput.model_validate(_kube_output())
    assert out.endpoint == "https://127.0.0.1:40123"
    assert out.tls_server_name == "am.prod.kube.example.net"
    assert out.path is None


def test_kube_target_output_insecure_allows_empty_ca_and_null_tls() -> None:
    """Insecure fallback shape: empty CA and null tls_server_name are valid."""
    out = KubeTargetOutput.model_validate(
        _kube_output(certificate_authority_data="", tls_server_name=None)
    )
    assert out.certificate_authority_data == ""
    assert out.tls_server_name is None


def test_node_output_kube_targets_defaults_empty() -> None:
    """NodeOutput.kube_targets defaults to an empty dict."""
    node = NodeOutput.model_validate({"ports": {"p": 1}})
    assert node.kube_targets == {}


def test_output_schema_requires_session_dir() -> None:
    """OutputSchema.session_dir is required (always present)."""
    with pytest.raises(ValidationError):
        OutputSchema.model_validate(
            {"connections": {}, "pid": 1, "token": "t", "started_at": "now"}
        )


def test_output_schema_with_session_dir() -> None:
    """OutputSchema accepts a session_dir string."""
    schema = OutputSchema.model_validate(
        {
            "connections": {},
            "pid": 1,
            "token": "t",
            "started_at": "now",
            "session_dir": "/run/tunstrap/1",
        }
    )
    assert schema.session_dir == "/run/tunstrap/1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_output_kube.py -q`
Expected: FAIL — `ImportError: cannot import name 'KubeTargetOutput'`.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/unit/test_output_kube.py
git commit -m "test: RED kube output schema + session_dir"
```

### Task 1.6: GREEN — KubeTargetOutput, NodeOutput.kube_targets, OutputSchema.session_dir

**Files:**
- Modify: `tunstrap/schemas.py:230-258`

- [ ] **Step 1: Add KubeTargetOutput before NodeOutput**

```python
class KubeTargetOutput(BaseModel):
    """Extracted, ready-to-use fields for one kube_target's current-context cluster.

    `content_b64` is the full patched kubeconfig. `path` is set only when
    daemon.materialize is True. On insecure fallback,
    `certificate_authority_data` is "" and `tls_server_name` is null.
    """

    model_config = ConfigDict(extra="forbid")

    cluster_name: str
    context_name: str
    local_port: int
    endpoint: str
    tls_server_name: str | None
    certificate_authority_data: str
    client_certificate_data: str
    client_key_data: str
    content_b64: str
    path: str | None = None
```

- [ ] **Step 2: Add kube_targets to NodeOutput**

```python
class NodeOutput(BaseModel):
    """Per-node success payload: ports, fetched files, and kube targets."""

    model_config = ConfigDict(extra="forbid")

    ports: dict[str, int]
    fetch_files: dict[str, FetchedFile] = Field(default_factory=dict)
    kube_targets: dict[str, KubeTargetOutput] = Field(default_factory=dict)
```

- [ ] **Step 3: Add session_dir to OutputSchema**

In `OutputSchema`, add after `token`:

```python
    session_dir: str
```

(Place it before `started_at`; it is required, no default.)

- [ ] **Step 4: Run the output tests**

Run: `.venv/bin/pytest tests/unit/test_output_kube.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the full unit suite — expect known downstream breakage**

Run: `.venv/bin/pytest tests/unit -q`
Expected: the new tests pass, but tests constructing `OutputSchema` without `session_dir` (e.g. in `manager`/`daemon`/`cli` tests) now FAIL. This is expected and fixed in Phases 3-5. Note which tests fail.

- [ ] **Step 6: Run gates on the changed file**

Run:
```
.venv/bin/ruff format --check tunstrap/schemas.py
.venv/bin/ruff check tunstrap/schemas.py
.venv/bin/mypy --strict tunstrap/schemas.py
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add tunstrap/schemas.py
git commit -m "feat(schemas): add KubeTargetOutput, NodeOutput.kube_targets, session_dir"
```

---

## Phase 2 — Kube module: parse, SAN selection, patch

### Task 2.1: Create kubeconfig fixtures

**Files:**
- Create: `tests/unit/fixtures/kube/single_internal_ip.yaml`
- Create: `tests/unit/fixtures/kube/hostname_split_horizon.yaml`
- Create: `tests/unit/fixtures/kube/multi_context.yaml`

- [ ] **Step 1: Write fixture single_internal_ip.yaml (server before CA, comments, cluster name != kubernetes)**

```yaml
apiVersion: v1
clusters:
- cluster:
    #insecure-skip-tls-verify: true
    server: https://10.0.0.11:6443
    certificate-authority-data: Y2EtZGF0YQ==
  name: production
contexts:
- context: { cluster: production, namespace: production, user: production }
  name: production
current-context: production
kind: Config
preferences: {}
users:
- name: production
  user:
    client-certificate-data: Y2VydC1kYXRh
    client-key-data: a2V5LWRhdGE=
```

- [ ] **Step 2: Write fixture hostname_split_horizon.yaml (DNS server name)**

```yaml
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: Y2EtZGF0YQ==
    server: https://am.prod.kube.example.net:6443
  name: kubernetes
contexts:
- context: { cluster: kubernetes, user: kubernetes-admin }
  name: kubernetes-admin@kubernetes
current-context: kubernetes-admin@kubernetes
kind: Config
preferences: {}
users:
- name: kubernetes-admin
  user:
    client-certificate-data: Y2VydC1kYXRh
    client-key-data: a2V5LWRhdGE=
```

- [ ] **Step 3: Write fixture multi_context.yaml (two contexts, one cluster)**

```yaml
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: Y2EtZGF0YQ==
    server: https://10.0.0.40:6443
  name: kubernetes
contexts:
- context: { cluster: kubernetes, user: kubernetes-admin }
  name: kubernetes-admin@kubernetes
- context: { cluster: kubernetes, namespace: staging-am, user: kubernetes-admin }
  name: staging
current-context: staging
kind: Config
preferences: {}
users:
- name: kubernetes-admin
  user:
    client-certificate-data: Y2VydC1kYXRh
    client-key-data: a2V5LWRhdGE=
```

- [ ] **Step 4: Commit the fixtures**

```bash
git add tests/unit/fixtures/kube/
git commit -m "test(fixtures): kubeconfig examples for kube mode"
```

### Task 2.2: RED — kubeconfig parse + current-context extraction

**Files:**
- Create: `tests/unit/test_kube_parse.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Parse a kubeconfig and extract current-context cluster + user material.

Validates: KubeconfigView extracts server/CA/cert/key for the current
context; multi-context files yield an ignored-contexts warning; a
malformed kubeconfig raises KubeParseError.
Code: tunstrap/kube.py
Assertion: extracted fields match the fixtures; warnings list names the
ignored contexts; bad YAML raises KubeParseError (not a bare YAMLError).
Method: load fixtures from tests/unit/fixtures/kube and call parse_kubeconfig.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tunstrap.kube import KubeParseError, parse_kubeconfig

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kube"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_single_internal_ip() -> None:
    """current-context 'production' yields the internal-IP server + creds."""
    view = parse_kubeconfig(_read("single_internal_ip.yaml"))
    assert view.context_name == "production"
    assert view.cluster_name == "production"
    assert view.server == "https://10.0.0.11:6443"
    assert view.certificate_authority_data == "Y2EtZGF0YQ=="
    assert view.client_certificate_data == "Y2VydC1kYXRh"
    assert view.client_key_data == "a2V5LWRhdGE="
    assert view.ignored_contexts == []


def test_parse_hostname_server() -> None:
    """A hostname server is extracted verbatim from the current context."""
    view = parse_kubeconfig(_read("hostname_split_horizon.yaml"))
    assert view.cluster_name == "kubernetes"
    assert view.server == "https://am.prod.kube.example.net:6443"


def test_parse_multi_context_reports_ignored() -> None:
    """A multi-context file selects current-context and reports the others."""
    view = parse_kubeconfig(_read("multi_context.yaml"))
    assert view.context_name == "staging"
    assert view.server == "https://10.0.0.40:6443"
    assert "kubernetes-admin@kubernetes" in view.ignored_contexts


def test_parse_rejects_malformed_yaml() -> None:
    """Malformed YAML raises KubeParseError, not a bare YAMLError."""
    with pytest.raises(KubeParseError):
        parse_kubeconfig(b"clusters: [unterminated")


def test_parse_rejects_missing_current_context() -> None:
    """A kubeconfig without current-context raises KubeParseError."""
    with pytest.raises(KubeParseError):
        parse_kubeconfig(b"apiVersion: v1\nkind: Config\nclusters: []\n")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_kube_parse.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tunstrap.kube'`.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/unit/test_kube_parse.py
git commit -m "test: RED kubeconfig parse + current-context extraction"
```

### Task 2.3: GREEN — kube.py parse layer

**Files:**
- Create: `tunstrap/kube.py`

- [ ] **Step 1: Write the parse layer**

```python
"""Kube mode: parse a remote kubeconfig, choose a TLS server name, patch it.

One kube_target maps to exactly one cluster: the kubeconfig's
current-context. Other contexts/clusters are ignored and left byte-stable
in the patched output. The fetched kubeconfig is untrusted input: it is
parsed in ruamel round-trip/safe mode and parse failures become a typed
KubeParseError (never a daemon crash).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class KubeParseError(Exception):
    """A kubeconfig could not be parsed or lacked a usable current-context."""


@dataclass
class KubeconfigView:
    """Extracted current-context view plus the live parsed document.

    `doc` is the round-trip ruamel document used later for in-place patching
    (comments/key order preserved). The scalar fields are the extracted
    current-context cluster/user material.
    """

    doc: object
    context_name: str
    cluster_name: str
    user_name: str
    server: str
    certificate_authority_data: str
    client_certificate_data: str
    client_key_data: str
    ignored_contexts: list[str] = field(default_factory=list)


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    return y


def parse_kubeconfig(raw: bytes) -> KubeconfigView:
    """Parse raw kubeconfig bytes and extract the current-context view.

    Raises KubeParseError on malformed YAML, missing current-context, or an
    unresolvable cluster/user reference.
    """
    try:
        doc = _yaml().load(io.BytesIO(raw))
    except YAMLError as exc:
        raise KubeParseError(f"kubeconfig is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise KubeParseError("kubeconfig root is not a mapping")

    current = doc.get("current-context")
    if not current or not isinstance(current, str):
        raise KubeParseError("kubeconfig has no current-context")

    contexts = doc.get("contexts") or []
    ctx = _find_named(contexts, current)
    if ctx is None:
        raise KubeParseError(f"current-context {current!r} not found in contexts")
    ctx_body = ctx.get("context") or {}
    cluster_name = ctx_body.get("cluster")
    user_name = ctx_body.get("user")
    if not cluster_name or not user_name:
        raise KubeParseError(f"context {current!r} missing cluster or user")

    cluster = _find_named(doc.get("clusters") or [], cluster_name)
    if cluster is None:
        raise KubeParseError(f"cluster {cluster_name!r} not found")
    cluster_body = cluster.get("cluster") or {}
    server = cluster_body.get("server")
    if not server or not isinstance(server, str):
        raise KubeParseError(f"cluster {cluster_name!r} has no server")

    user = _find_named(doc.get("users") or [], user_name)
    if user is None:
        raise KubeParseError(f"user {user_name!r} not found")
    user_body = user.get("user") or {}

    ignored = [
        str(c.get("name"))
        for c in contexts
        if isinstance(c, dict) and c.get("name") != current
    ]

    return KubeconfigView(
        doc=doc,
        context_name=current,
        cluster_name=str(cluster_name),
        user_name=str(user_name),
        server=server,
        certificate_authority_data=str(cluster_body.get("certificate-authority-data") or ""),
        client_certificate_data=str(user_body.get("client-certificate-data") or ""),
        client_key_data=str(user_body.get("client-key-data") or ""),
        ignored_contexts=ignored,
    )


def _find_named(items: object, name: str) -> dict[str, object] | None:
    """Return the first list entry whose 'name' equals `name`, else None."""
    if not isinstance(items, list):
        return None
    for entry in items:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None
```

- [ ] **Step 2: Run the parse tests**

Run: `.venv/bin/pytest tests/unit/test_kube_parse.py -q`
Expected: PASS (5 passed).

- [ ] **Step 3: Run gates on the new module**

Run:
```
.venv/bin/ruff format --check tunstrap/kube.py
.venv/bin/ruff check tunstrap/kube.py
.venv/bin/mypy --strict tunstrap/kube.py
```
Expected: clean. (If mypy complains about ruamel types, add a `[[tool.mypy.overrides]]` for `ruamel.*` with `ignore_missing_imports = true` in `pyproject.toml` and re-run; commit that pyproject change with this task.)

- [ ] **Step 4: Commit**

```bash
git add tunstrap/kube.py pyproject.toml
git commit -m "feat(kube): parse kubeconfig + extract current-context view"
```

### Task 2.4: RED — SAN selection

**Files:**
- Create: `tests/unit/test_kube_san.py`

- [ ] **Step 1: Write the failing tests**

```python
"""TLS server-name selection from a certificate's SAN list.

Validates: prefer the original server host; else first DNS SAN; else
first IP SAN; empty SAN returns None. A non-exact match is flagged.
Code: tunstrap/kube.py
Assertion: choose_tls_server_name returns the documented preference and
a `fellback` flag indicating a non-exact match.
Method: call choose_tls_server_name with crafted SAN lists.
"""

from __future__ import annotations

import pytest

from tunstrap.kube import choose_tls_server_name

pytestmark = pytest.mark.unit


def test_prefers_original_host_when_in_san() -> None:
    """When the original server host is in SAN, it is chosen, no fallback."""
    name, fellback = choose_tls_server_name(
        original_host="am.prod.kube.example.net",
        dns_sans=["am.prod.kube.example.net", "kubernetes"],
        ip_sans=["10.0.0.40"],
    )
    assert name == "am.prod.kube.example.net"
    assert fellback is False


def test_falls_back_to_first_dns_san() -> None:
    """When the original host is absent, the first DNS SAN is chosen (fallback)."""
    name, fellback = choose_tls_server_name(
        original_host="127.0.0.1",
        dns_sans=["kubernetes", "kubernetes.default"],
        ip_sans=["10.0.0.40"],
    )
    assert name == "kubernetes"
    assert fellback is True


def test_falls_back_to_first_ip_san() -> None:
    """With no DNS SAN, the first IP SAN is chosen (fallback)."""
    name, fellback = choose_tls_server_name(
        original_host="127.0.0.1",
        dns_sans=[],
        ip_sans=["10.0.0.40", "127.0.0.1"],
    )
    assert name == "10.0.0.40"
    assert fellback is True


def test_empty_san_returns_none() -> None:
    """An empty SAN list returns None (caller decides insecure/fail)."""
    name, fellback = choose_tls_server_name(
        original_host="127.0.0.1", dns_sans=[], ip_sans=[]
    )
    assert name is None
    assert fellback is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_kube_san.py -q`
Expected: FAIL — `ImportError: cannot import name 'choose_tls_server_name'`.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/unit/test_kube_san.py
git commit -m "test: RED SAN selection"
```

### Task 2.5: GREEN — SAN selection + extraction helper

**Files:**
- Modify: `tunstrap/kube.py`

- [ ] **Step 1: Add the SAN helpers to kube.py**

Add the cryptography import at the top:

```python
from cryptography import x509
from cryptography.x509.oid import ExtensionOID
```

Append these functions:

```python
def _host_of(server: str) -> str:
    """Extract the host from an https URL (strip scheme, port, path)."""
    rest = server.split("://", 1)[-1]
    authority = rest.split("/", 1)[0]
    if authority.startswith("["):  # [ipv6]:port
        return authority[1 : authority.find("]")]
    return authority.rsplit(":", 1)[0] if ":" in authority else authority


def sans_from_cert(cert_der: bytes) -> tuple[list[str], list[str]]:
    """Return (dns_sans, ip_sans) parsed from a DER-encoded certificate.

    On any parse failure or absent SAN extension, returns ([], []).
    """
    try:
        cert = x509.load_der_x509_certificate(cert_der)
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        san = ext.value
        dns = list(san.get_values_for_type(x509.DNSName))
        ips = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
        return dns, ips
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # A malformed/absent SAN is not a daemon error: the caller treats an
        # empty result as "no usable name" and applies insecure_fallback policy.
        return [], []


def choose_tls_server_name(
    *,
    original_host: str,
    dns_sans: list[str],
    ip_sans: list[str],
) -> tuple[str | None, bool]:
    """Choose a tls-server-name; return (name, fellback).

    Preference: original host if present in SAN; else first DNS SAN; else
    first IP SAN; else None. `fellback` is True whenever the chosen name is
    not an exact match of `original_host` (including the None case).
    """
    if original_host in dns_sans or original_host in ip_sans:
        return original_host, False
    if dns_sans:
        return dns_sans[0], True
    if ip_sans:
        return ip_sans[0], True
    return None, True
```

- [ ] **Step 2: Run the SAN tests**

Run: `.venv/bin/pytest tests/unit/test_kube_san.py -q`
Expected: PASS (4 passed).

- [ ] **Step 3: Run gates**

Run:
```
.venv/bin/ruff check tunstrap/kube.py
.venv/bin/mypy --strict tunstrap/kube.py
```
Expected: clean. (If mypy needs a `cryptography` override, add it to pyproject and commit together.)

- [ ] **Step 4: Commit**

```bash
git add tunstrap/kube.py pyproject.toml
git commit -m "feat(kube): SAN parsing + tls-server-name selection"
```

### Task 2.6: RED — patch kubeconfig

**Files:**
- Create: `tests/unit/test_kube_patch.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Patch a kubeconfig: rewrite server + set tls-server-name (or insecure).

Validates: only the current-context cluster's server is rewritten;
tls-server-name is set; comments/key order are preserved; insecure mode
drops CA and sets insecure-skip-tls-verify.
Code: tunstrap/kube.py
Assertion: re-parsing the patched output shows the new server/tls fields;
the original comment line survives; untouched clusters keep their server.
Method: parse a fixture, patch it, dump, and re-parse to assert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tunstrap.kube import dump_kubeconfig, parse_kubeconfig, patch_view

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kube"


def test_patch_rewrites_server_and_sets_tls_name() -> None:
    """Secure patch sets the local endpoint and tls-server-name on the cluster."""
    view = parse_kubeconfig((FIXTURES / "single_internal_ip.yaml").read_bytes())
    patch_view(view, local_port=40123, tls_server_name="dev-kube-1", insecure=False)
    out = dump_kubeconfig(view)
    reparsed = parse_kubeconfig(out)
    assert reparsed.server == "https://127.0.0.1:40123"
    text = out.decode()
    assert "tls-server-name: dev-kube-1" in text
    assert "insecure-skip-tls-verify" not in text.replace("#insecure-skip-tls-verify", "")


def test_patch_preserves_comment() -> None:
    """The commented insecure line in the fixture survives the round-trip."""
    view = parse_kubeconfig((FIXTURES / "single_internal_ip.yaml").read_bytes())
    patch_view(view, local_port=1, tls_server_name="x", insecure=False)
    assert b"#insecure-skip-tls-verify: true" in dump_kubeconfig(view)


def test_patch_insecure_drops_ca_and_sets_skip_verify() -> None:
    """Insecure patch sets insecure-skip-tls-verify and removes CA data."""
    view = parse_kubeconfig((FIXTURES / "hostname_split_horizon.yaml").read_bytes())
    patch_view(view, local_port=55555, tls_server_name=None, insecure=True)
    text = dump_kubeconfig(view).decode()
    assert "insecure-skip-tls-verify: true" in text
    assert "certificate-authority-data" not in text
    assert "tls-server-name" not in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_kube_patch.py -q`
Expected: FAIL — `ImportError: cannot import name 'patch_view'`.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/unit/test_kube_patch.py
git commit -m "test: RED kubeconfig patch"
```

### Task 2.7: GREEN — patch + dump

**Files:**
- Modify: `tunstrap/kube.py`

- [ ] **Step 1: Add patch_view and dump_kubeconfig**

```python
def patch_view(
    view: KubeconfigView,
    *,
    local_port: int,
    tls_server_name: str | None,
    insecure: bool,
) -> None:
    """Patch the current-context cluster in-place on the ruamel doc.

    Rewrites `server:` to the local forwarded endpoint. On secure patch sets
    `tls-server-name`. On insecure patch sets `insecure-skip-tls-verify: true`
    and removes `certificate-authority-data`. Other clusters are untouched.
    """
    doc = view.doc
    assert isinstance(doc, dict)
    cluster = _find_named(doc.get("clusters") or [], view.cluster_name)
    assert cluster is not None  # parse_kubeconfig guaranteed this
    body = cluster["cluster"]
    body["server"] = f"https://127.0.0.1:{local_port}"
    if insecure:
        body["insecure-skip-tls-verify"] = True
        body.pop("certificate-authority-data", None)
        body.pop("tls-server-name", None)
    else:
        if tls_server_name is not None:
            body["tls-server-name"] = tls_server_name


def dump_kubeconfig(view: KubeconfigView) -> bytes:
    """Serialise the (patched) ruamel doc back to YAML bytes."""
    buf = io.BytesIO()
    _yaml().dump(view.doc, buf)
    return buf.getvalue()
```

- [ ] **Step 2: Run the patch tests**

Run: `.venv/bin/pytest tests/unit/test_kube_patch.py -q`
Expected: PASS (3 passed).

- [ ] **Step 3: Run gates**

Run:
```
.venv/bin/ruff check tunstrap/kube.py
.venv/bin/mypy --strict tunstrap/kube.py
```
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tunstrap/kube.py
git commit -m "feat(kube): patch server + tls-server-name / insecure"
```

---

## Phase 3 — Session directory + materialization

### Task 3.1: RED — session-dir resolve/validate/cleanup

**Files:**
- Create: `tests/unit/test_session_dir.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Session-directory resolution, tunnel-data creation, and cleanup.

Validates: a generated dir is removed wholesale; a supplied dir keeps
only tunnel-data removed; tunnel-data is 0700; an existing/symlinked
tunnel-data is rejected.
Code: tunstrap/session.py
Assertion: directory existence/mode after open/cleanup matches the rules;
SessionError is raised on a hostile tunnel-data.
Method: drive SessionDir against tmp_path with crafted preconditions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tunstrap.session import SessionDir, SessionError

pytestmark = pytest.mark.unit


def test_generated_dir_cleanup_removes_whole_dir(tmp_path: Path) -> None:
    """A daemon-generated session dir is removed entirely on cleanup."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    root = Path(sd.session_dir)
    assert (root / "tunnel-data").is_dir()
    sd.cleanup()
    assert not root.exists()


def test_supplied_dir_cleanup_keeps_dir(tmp_path: Path) -> None:
    """A supplied session dir keeps the dir; only tunnel-data is removed."""
    supplied = tmp_path / "work"
    supplied.mkdir()
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    assert (supplied / "tunnel-data").is_dir()
    sd.cleanup()
    assert supplied.exists()
    assert not (supplied / "tunnel-data").exists()


def test_tunnel_data_is_0700(tmp_path: Path) -> None:
    """tunnel-data is created with mode 0700."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    mode = stat.S_IMODE(os.stat(Path(sd.session_dir) / "tunnel-data").st_mode)
    assert mode == 0o700


def test_rejects_existing_tunnel_data(tmp_path: Path) -> None:
    """A pre-existing tunnel-data in a supplied dir is rejected (orphan/foreign)."""
    supplied = tmp_path / "work"
    (supplied / "tunnel-data").mkdir(parents=True)
    with pytest.raises(SessionError):
        SessionDir.create(supplied=str(supplied), base=tmp_path)


def test_rejects_symlink_tunnel_data(tmp_path: Path) -> None:
    """A symlinked tunnel-data is rejected (no symlink-following)."""
    supplied = tmp_path / "work"
    supplied.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (supplied / "tunnel-data").symlink_to(target)
    with pytest.raises(SessionError):
        SessionDir.create(supplied=str(supplied), base=tmp_path)


def test_write_identity_and_materialize(tmp_path: Path) -> None:
    """Identity files and a materialized file land in tunnel-data, mode 0600."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    sd.write_identity(pid=4321, token="tok")
    path = sd.materialize("hub-k3s", b"kubeconfig-bytes")
    data_dir = Path(sd.session_dir) / "tunnel-data"
    assert (data_dir / "daemon.pid").read_text().strip() == "4321"
    assert (data_dir / "token").read_text().strip() == "tok"
    assert Path(path).read_bytes() == b"kubeconfig-bytes"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_session_dir.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tunstrap.session'`.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/unit/test_session_dir.py
git commit -m "test: RED session-dir resolve/validate/cleanup"
```

### Task 3.2: GREEN — session.py

**Files:**
- Create: `tunstrap/session.py`

- [ ] **Step 1: Write session.py**

```python
"""Session directory: identity + optional materialized files under tunnel-data/.

The daemon always works inside a well-known `tunnel-data/` subdirectory of
the session dir. When the daemon generates the session dir itself, cleanup
removes the whole dir; when the caller supplies it, cleanup removes only
`tunnel-data/` (the caller's directory is never touched). `--session-dir`
is untrusted: an existing tunnel-data that is a symlink, a non-directory,
or not owned by the current user is rejected.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TUNNEL_DATA = "tunnel-data"


class SessionError(Exception):
    """The session dir or its tunnel-data subdir failed validation."""


class SessionDir:
    """Owns the tunnel-data/ subdir lifecycle for one daemon instance."""

    def __init__(self, *, session_dir: Path, generated: bool) -> None:
        """Store the resolved session dir and whether the daemon generated it."""
        self.session_dir = str(session_dir)
        self._root = session_dir
        self._generated = generated
        self._data = session_dir / _TUNNEL_DATA

    @classmethod
    def create(cls, *, supplied: str | None, base: Path | None = None) -> "SessionDir":
        """Resolve/validate the session dir and create tunnel-data/ (0700)."""
        if supplied is None:
            parent = base if base is not None else Path(tempfile.gettempdir())
            root = Path(tempfile.mkdtemp(prefix="tunstrap-", dir=parent))
            generated = True
        else:
            root = Path(supplied).resolve()
            if not root.is_absolute():
                raise SessionError("session dir must be absolute")
            root.mkdir(parents=True, exist_ok=True)
            generated = False

        data = root / _TUNNEL_DATA
        cls._validate_data_slot(data)
        data.mkdir(mode=0o700)
        return cls(session_dir=root, generated=generated)

    @staticmethod
    def _validate_data_slot(data: Path) -> None:
        """Reject a pre-existing tunnel-data that is unsafe to own."""
        if data.is_symlink():
            raise SessionError("tunnel-data is a symlink; refusing to follow")
        if data.exists():
            if not data.is_dir():
                raise SessionError("tunnel-data exists and is not a directory")
            if data.stat().st_uid != os.getuid():
                raise SessionError("tunnel-data exists and is not owned by this user")
            raise SessionError(
                "tunnel-data already exists (possible orphaned session); "
                "remove it before reusing this session dir"
            )

    def write_identity(self, *, pid: int, token: str) -> None:
        """Write daemon.pid and token (mode 0600) into tunnel-data/."""
        self._write_file("daemon.pid", f"{pid}\n".encode("ascii"))
        self._write_file("token", f"{token}\n".encode("ascii"))

    def materialize(self, name: str, content: bytes) -> str:
        """Write `content` to tunnel-data/<name> (mode 0600); return the path."""
        return self._write_file(name, content)

    def _write_file(self, name: str, content: bytes) -> str:
        path = self._data / name
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return str(path)

    def cleanup(self) -> None:
        """Remove tunnel-data/ (and the whole dir if we generated it). Best-effort."""
        if self._generated:
            shutil.rmtree(self._root, ignore_errors=True)
        else:
            shutil.rmtree(self._data, ignore_errors=True)

    @staticmethod
    def read_identity(session_dir: str) -> tuple[int, str]:
        """Read (pid, token) from a session dir's tunnel-data/. Raises SessionError."""
        data = Path(session_dir).resolve() / _TUNNEL_DATA
        try:
            pid = int((data / "daemon.pid").read_text().strip())
            token = (data / "token").read_text().strip()
        except (OSError, ValueError) as exc:
            raise SessionError(f"cannot read identity from {data}: {exc}") from exc
        return pid, token
```

- [ ] **Step 2: Run the session tests**

Run: `.venv/bin/pytest tests/unit/test_session_dir.py -q`
Expected: PASS (6 passed).

- [ ] **Step 3: Run gates**

Run:
```
.venv/bin/ruff format --check tunstrap/session.py
.venv/bin/ruff check tunstrap/session.py
.venv/bin/mypy --strict tunstrap/session.py
```
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tunstrap/session.py
git commit -m "feat(session): tunnel-data session dir lifecycle"
```

---

## Phase 4 — Kube orchestration + manager wiring

### Task 4.1: RED — run_kube_targets orchestration (forward + probe + patch)

**Files:**
- Create: `tests/unit/test_kube_run.py`

This tests the orchestration with the asyncssh connection and the SAN probe
stubbed, so it stays a unit test (no real SSH). The function under test is
`run_kube_targets(conn, kube_targets, *, connect_timeout, probe)` where
`probe` is an injectable async callable returning DER cert bytes.

- [ ] **Step 1: Write the failing tests**

```python
"""Orchestrate one node's kube_targets: forward, probe SAN, patch, extract.

Validates: a successful kube_target yields a KubeTargetOutput with the
local endpoint, chosen tls_server_name, and patched content; a required
target whose fetch fails is reported as a required failure.
Code: tunstrap/kube.py::run_kube_targets
Assertion: returned outputs carry the local port + tls name; warnings
include the non-exact-SAN note; required failures are listed.
Method: drive run_kube_targets with a fake connection + injected probe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tunstrap.kube import run_kube_targets
from tunstrap.schemas import KubeTarget

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kube"


class _FakeListener:
    def __init__(self, port: int) -> None:
        self._port = port

    def get_port(self) -> int:
        return self._port

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _FakeConn:
    """Stubs the two asyncssh calls run_kube_targets uses: sftp + forward."""

    def __init__(self, file_bytes: bytes) -> None:
        self._file_bytes = file_bytes

    def start_sftp_client(self) -> Any:
        conn = self

        class _CM:
            async def __aenter__(self) -> Any:
                class _Sftp:
                    async def stat(self, _path: str) -> Any:
                        class _S:
                            size = len(conn._file_bytes)

                        return _S()

                    def open(self, _path: str, _mode: str) -> Any:
                        data = conn._file_bytes

                        class _FH:
                            async def __aenter__(self) -> Any:
                                class _R:
                                    async def read(self, _n: int) -> bytes:
                                        return data

                                return _R()

                            async def __aexit__(self, *_a: Any) -> None:
                                return None

                        return _FH()

                return _Sftp()

            async def __aexit__(self, *_a: Any) -> None:
                return None

        return _CM()

    async def forward_local_port(self, *_a: Any, **_k: Any) -> _FakeListener:
        return _FakeListener(40123)


async def _probe_ok(_host: str, _port: int) -> bytes:
    # Minimal: return a sentinel; sans_from_cert returns ([],[]) for junk, so
    # use a probe that bypasses cert parsing by patching choose via monkeypatch.
    return b"DERCERT"


@pytest.mark.asyncio
async def test_run_kube_target_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy kube_target yields a patched output with the local endpoint."""
    monkeypatch.setattr(
        "tunstrap.kube.sans_from_cert",
        lambda _der: (["dev-kube-1", "10.0.0.11"], []),
    )
    conn = _FakeConn((FIXTURES / "single_internal_ip.yaml").read_bytes())
    outputs, required_failures, warnings = await run_kube_targets(
        conn,
        {"k3s": KubeTarget.model_validate({"kubeconfig_path": "/etc/k3s.yaml"})},
        connect_timeout=5,
        probe=_probe_ok,
    )
    assert required_failures == []
    out = outputs["k3s"]
    assert out.endpoint == "https://127.0.0.1:40123"
    assert out.tls_server_name in {"dev-kube-1", "10.0.0.11"}
    assert out.local_port == 40123
    assert out.content_b64  # non-empty patched kubeconfig
```

> Note: `run_kube_targets` does NOT materialize or probe local TCP; it forwards,
> fetches via SFTP, probes the apiserver cert (via injected `probe`), selects the
> SAN, patches, and returns. Listener lifecycle (keeping forwards open) is owned
> by the manager, which receives the listeners alongside the outputs — see Task 4.2
> for the exact return contract the manager consumes.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_kube_run.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_kube_targets'`.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/unit/test_kube_run.py
git commit -m "test: RED run_kube_targets orchestration"
```

### Task 4.2: GREEN — run_kube_targets

**Files:**
- Modify: `tunstrap/kube.py`

- [ ] **Step 1: Add imports + result dataclass + orchestration**

Add at the top of kube.py:

```python
import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncssh

from tunstrap.schemas import KubeTarget, KubeTargetOutput, TunnelWarning
```

Define the probe type:

```python
ProbeFn = Callable[[str, int], Awaitable[bytes]]
```

Add the orchestration entry. It fetches each kubeconfig over SFTP, forwards the
apiserver, probes the cert, selects the SAN, patches, and builds the output:

```python
async def run_kube_targets(
    conn: "asyncssh.SSHClientConnection",
    kube_targets: dict[str, KubeTarget],
    *,
    connect_timeout: int,
    probe: ProbeFn,
    node_name: str = "",
) -> tuple[dict[str, KubeTargetOutput], list[str], list[TunnelWarning]]:
    """Process every kube_target for a node; return (outputs, required_failures, warnings).

    The local-forward listeners opened here are owned by the live SSH
    connection (`conn`) and stay open for as long as the connection does; the
    manager keeps `conn` in its `_NodeRuntime` and closes it on `stop_all`, so
    the forwards do not need to be returned separately. Each target is
    independent: a failure on one (subject to its `required`) does not abort the
    others.
    """
    outputs: dict[str, KubeTargetOutput] = {}
    required_failures: list[str] = []
    warnings: list[TunnelWarning] = []

    for name, target in kube_targets.items():
        try:
            raw = await asyncio.wait_for(
                _fetch_one(conn, target.kubeconfig_path), timeout=connect_timeout
            )
            view = parse_kubeconfig(raw)
        except (KubeParseError, OSError, asyncio.TimeoutError) as exc:
            warnings.append(TunnelWarning(node=node_name, error=f"kube_target {name}: {exc}"))
            if target.required:
                required_failures.append(name)
            continue

        for ignored in view.ignored_contexts:
            warnings.append(
                TunnelWarning(
                    node=node_name,
                    error=f"kube_target {name}: ignored context {ignored!r}",
                    skipped=False,
                )
            )

        host, port = _split_host_port(view.server)
        listener = await conn.forward_local_port("127.0.0.1", 0, host, port)
        local_port = listener.get_port()

        tls_name, insecure = await _resolve_tls(
            target=target, view=view, host=host, local_port=local_port,
            probe=probe, node_name=node_name, name=name, warnings=warnings,
        )
        if tls_name is None and not insecure:
            required_failures.append(name) if target.required else None
            warnings.append(
                TunnelWarning(node=node_name, error=f"kube_target {name}: no usable TLS name")
            )
            listener.close()
            await listener.wait_closed()
            continue

        patch_view(view, local_port=local_port, tls_server_name=tls_name, insecure=insecure)
        patched = dump_kubeconfig(view)
        outputs[name] = KubeTargetOutput(
            cluster_name=view.cluster_name,
            context_name=view.context_name,
            local_port=local_port,
            endpoint=f"https://127.0.0.1:{local_port}",
            tls_server_name=None if insecure else tls_name,
            certificate_authority_data="" if insecure else view.certificate_authority_data,
            client_certificate_data=view.client_certificate_data,
            client_key_data=view.client_key_data,
            content_b64=base64.b64encode(patched).decode("ascii"),
            path=None,
        )
    return outputs, required_failures, warnings


def _split_host_port(server: str) -> tuple[str, int]:
    host = _host_of(server)
    rest = server.split("://", 1)[-1].split("/", 1)[0]
    port_part = rest.rsplit(":", 1)[-1] if ":" in rest and not rest.endswith("]") else "443"
    return host, int(port_part)


async def _fetch_one(conn: "asyncssh.SSHClientConnection", path: str) -> bytes:
    """Read a single small file over SFTP (1 MiB cap), return raw bytes."""
    async with conn.start_sftp_client() as sftp:
        stat = await sftp.stat(path)
        if stat.size is not None and stat.size > (1 << 20):
            raise OSError("kubeconfig exceeds 1 MiB cap")
        async with sftp.open(path, "rb") as fh:
            data = await fh.read((1 << 20) + 1)
    raw = data if isinstance(data, bytes) else data.encode()
    if len(raw) > (1 << 20):
        raise OSError("kubeconfig exceeds 1 MiB cap")
    return raw


async def _resolve_tls(  # pylint: disable=too-many-arguments
    *,
    target: KubeTarget,
    view: KubeconfigView,
    host: str,
    local_port: int,
    probe: ProbeFn,
    node_name: str,
    name: str,
    warnings: list[TunnelWarning],
) -> tuple[str | None, bool]:
    """Determine (tls_server_name, insecure) for one target."""
    del view  # reserved for future per-view hints
    if target.tls_server_name is not None:
        return target.tls_server_name, False
    cert_der = await probe("127.0.0.1", local_port)
    dns_sans, ip_sans = sans_from_cert(cert_der)
    chosen, fellback = choose_tls_server_name(
        original_host=host, dns_sans=dns_sans, ip_sans=ip_sans
    )
    if chosen is None:
        if target.insecure_fallback:
            warnings.append(
                TunnelWarning(
                    node=node_name,
                    error=f"kube_target {name}: TLS verification disabled (insecure_fallback)",
                    skipped=False,
                )
            )
            return None, True
        return None, False
    if fellback:
        warnings.append(
            TunnelWarning(
                node=node_name,
                error=f"kube_target {name}: tls-server-name fell back to {chosen!r}",
                skipped=False,
            )
        )
    return chosen, False
```

- [ ] **Step 2: Run the orchestration test**

Run: `.venv/bin/pytest tests/unit/test_kube_run.py -q`
Expected: PASS (1 passed).

- [ ] **Step 3: Run gates**

Run:
```
.venv/bin/ruff format --check tunstrap/kube.py
.venv/bin/ruff check tunstrap/kube.py
.venv/bin/mypy --strict tunstrap/kube.py
```
Expected: clean. (If pylint flags `too-many-arguments` on `_resolve_tls`, the inline disable is already present; if `run_kube_targets` trips a similar check, add a matching inline disable with a one-line reason.)

- [ ] **Step 4: Commit**

```bash
git add tunstrap/kube.py
git commit -m "feat(kube): run_kube_targets orchestration (fetch/forward/probe/patch)"
```

### Task 4.3: GREEN — real SAN probe over the forwarded port

**Files:**
- Modify: `tunstrap/kube.py`

The unit tests inject `probe`; the production default does a TLS handshake to
the local forwarded port and returns the peer's DER cert. No unit test drives
the real socket (covered by integration Task 7.x); this task adds the default
and a thin signature test.

- [ ] **Step 1: Add a test asserting the default probe exists and is callable**

Append to `tests/unit/test_kube_run.py`:

```python
def test_default_probe_is_callable() -> None:
    """A default TLS probe is exported for production use."""
    from tunstrap.kube import default_san_probe

    assert callable(default_san_probe)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_kube_run.py::test_default_probe_is_callable -q`
Expected: FAIL — `ImportError: cannot import name 'default_san_probe'`.

- [ ] **Step 3: Add the default probe to kube.py**

```python
import ssl as _ssl


async def default_san_probe(host: str, port: int) -> bytes:
    """TLS-handshake to host:port and return the peer certificate in DER form.

    Verification is disabled for the handshake itself (we only want the cert
    to read its SAN); the resulting tls-server-name is what re-enables real
    verification for the client. Runs the blocking socket work in a thread.
    """

    def _connect() -> bytes:
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        import socket as _socket

        with _socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=None) as tls:
                der = tls.getpeercert(binary_form=True)
        if der is None:
            raise OSError("no peer certificate presented")
        return der

    return await asyncio.to_thread(_connect)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_kube_run.py -q`
Expected: PASS.

- [ ] **Step 5: Run gates**

Run:
```
.venv/bin/ruff check tunstrap/kube.py
.venv/bin/mypy --strict tunstrap/kube.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add tunstrap/kube.py tests/unit/test_kube_run.py
git commit -m "feat(kube): default TLS SAN probe over forwarded port"
```

### Task 4.4: GREEN — wire kube_targets into TunnelManager

**Files:**
- Modify: `tunstrap/manager.py`

- [ ] **Step 1: Extend imports and _NodeRuntime**

Add to the schemas import in manager.py:

```python
from tunstrap.schemas import (
    ErrorOutput,
    FetchedFile,
    InputSchema,
    KubeTargetOutput,
    NodeOutput,
    OutputSchema,
    TunnelWarning,
)
from tunstrap.kube import run_kube_targets
```

Add fields to `_NodeRuntime`:

```python
    kube_targets: dict[str, KubeTargetOutput] = field(default_factory=dict)
    kube_warnings: list[TunnelWarning] = field(default_factory=list)
```

- [ ] **Step 2: Accept session_dir + materialize in the constructor**

Change `TunnelManager.__init__` to accept an optional session writer:

```python
    def __init__(self, schema: InputSchema, session=None) -> None:  # type: ignore[no-untyped-def]
        """Store the parsed input schema and optional SessionDir for materialize."""
        self._schema = schema
        self._session = session
        self._runtimes: list[_NodeRuntime] = []
        self.activity_tracker = ActivityTracker()
```

> The `session` parameter is a `tunstrap.session.SessionDir | None`. It is
> kept untyped-import-light here (manager already imports many modules); annotate
> with a `TYPE_CHECKING` import of `SessionDir` if mypy requires it.

- [ ] **Step 3: Call kube mode in _start_one (after fetch_files)**

Insert this block in `_start_one`, after the `fetch_files` block and before
`runtime.success = True`:

```python
        if node.kube_targets:
            try:
                kube_out, kube_required, kube_warn = await run_kube_targets(
                    runtime.conn,
                    node.kube_targets,
                    connect_timeout=node.ssh_options.connect_timeout,
                    probe=__import__(
                        "tunstrap.kube", fromlist=["default_san_probe"]
                    ).default_san_probe,
                    node_name=name,
                )
            except _NODE_STARTUP_ERRORS as exc:
                runtime.error = str(exc)
                await close_transport(runtime.conn, runtime.listeners)
                runtime.conn = None
                runtime.listeners = []
                return runtime
            runtime.kube_targets = kube_out
            runtime.kube_warnings = kube_warn
            if self._session is not None:
                for kname, kout in kube_out.items():
                    import base64

                    path = self._session.materialize(
                        f"{name}-{kname}", base64.b64decode(kout.content_b64)
                    )
                    runtime.kube_targets[kname] = kout.model_copy(update={"path": path})
            if kube_required:
                runtime.error = f"required kube_targets failed: {kube_required}"
                await close_transport(runtime.conn, runtime.listeners)
                runtime.conn = None
                runtime.listeners = []
                return runtime
```

> Replace the `__import__(...)` indirection with a top-level
> `from tunstrap.kube import default_san_probe` import if it passes lint;
> the indirection only exists to avoid an import cycle if one appears. Prefer the
> direct import; fall back to the indirection only if `ruff`/`pylint` reports a cycle.

- [ ] **Step 4: Include kube_targets + warnings in the output**

In `start_all_and_build_output`, change the `connections` comprehension to
include kube_targets, and extend `warnings`:

```python
        connections: dict[str, NodeOutput] = {
            r.name: NodeOutput(
                ports=r.ports,
                fetch_files=r.fetched_files,
                kube_targets=r.kube_targets,
            )
            for r in results
            if r.success
        }
        warnings = [
            TunnelWarning(node=r.name, error=r.error or "unknown error")
            for r in results
            if not r.success and not self._schema.nodes[r.name].required
        ]
        for r in results:
            if r.success:
                warnings.extend(r.kube_warnings)
```

- [ ] **Step 5: Thread session_dir into OutputSchema**

`start_all_and_build_output` must now set `session_dir`. Add a parameter:

```python
    async def start_all_and_build_output(
        self,
        *,
        pid: int,
        token: str,
        session_dir: str,
    ) -> OutputSchema | ErrorOutput:
```

and pass it into the `OutputSchema(...)` constructor:

```python
        return OutputSchema(
            connections=connections,
            pid=pid,
            token=token,
            session_dir=session_dir,
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            warnings=warnings,
        )
```

- [ ] **Step 6: Run manager-touching unit tests**

Run: `.venv/bin/pytest tests/unit -q -k "manager or daemon or ipc or output"`
Expected: tests that call `start_all_and_build_output` will FAIL until callers
pass `session_dir` (fixed in Phase 5). Confirm the failures are only the
`session_dir`-arg signature ones, not logic regressions in the new code.

- [ ] **Step 7: Run gates on manager.py**

Run:
```
.venv/bin/ruff check tunstrap/manager.py
.venv/bin/mypy --strict tunstrap/manager.py
```
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add tunstrap/manager.py
git commit -m "feat(manager): wire kube_targets + session_dir into output"
```

---

## Phase 5 — Daemon / worker / identity / CLI wiring

### Task 5.1: GREEN — identity under a caller-supplied directory

**Files:**
- Modify: `tunstrap/identity.py`

Identity currently lives in `_state_dir()` (`~/.local/state/tunstrap`). The
session model puts `daemon.pid`+`token` under `<session-dir>/tunnel-data/`. The
flock-based identity check must consult that same directory when a session dir
is in play. We keep `_state_dir()` as the default but let the worker pass the
session's `tunnel-data` path.

- [ ] **Step 1: Add a test for directory parametrization**

Append to a new `tests/unit/test_identity_dir.py`:

```python
"""verify_token honours an explicit state directory.

Validates: a live flock + matching pid in a given directory yields match;
a foreign pid yields mismatch.
Code: tunstrap/identity.py
Assertion: verify_token(..., state_dir=...) reads the lockfile from that dir.
Method: create a lockfile held by the current process via flock and check.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from tunstrap.identity import IdentityCheckResult, verify_token

pytestmark = pytest.mark.unit


def test_verify_token_uses_explicit_dir(tmp_path: Path) -> None:
    """A held lock with a matching pid in `state_dir` resolves to match."""
    token = "tok"
    lock = tmp_path / f"{token}.lock"
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, f"{os.getpid()}\n".encode())
    try:
        result = verify_token(os.getpid(), token, state_dir=tmp_path)
        assert result == IdentityCheckResult.match
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_identity_dir.py -q`
Expected: FAIL — `verify_token()` got an unexpected keyword argument `state_dir`.

- [ ] **Step 3: Parametrize verify_token**

Change `verify_token` signature and body in identity.py:

```python
def verify_token(pid: int, token: str, state_dir: Path | None = None) -> IdentityCheckResult:
    """Return whether ``pid`` is alive and owns the identity lock for ``token``."""
    if not _process_exists(pid):
        return IdentityCheckResult.not_found

    base = state_dir if state_dir is not None else _state_dir()
    lock_path = base / f"{token}.lock"
    if not lock_path.is_file():
        return IdentityCheckResult.not_found

    return _check_lock(lock_path, pid)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_identity_dir.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Run gates**

Run:
```
.venv/bin/ruff check tunstrap/identity.py
.venv/bin/mypy --strict tunstrap/identity.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add tunstrap/identity.py tests/unit/test_identity_dir.py
git commit -m "feat(identity): allow verify_token state_dir override"
```

### Task 5.2: GREEN — daemon spawn passes session_dir

**Files:**
- Modify: `tunstrap/daemon.py`

- [ ] **Step 1: Add session_dir param to spawn_daemon and worker argv**

Change `spawn_daemon` signature:

```python
def spawn_daemon(schema: InputSchema, session_dir: str | None = None) -> dict[str, Any]:
```

Add the argv entry (after `--token`):

```python
                f"--token={runtime_token}",
                *( [f"--session-dir={session_dir}"] if session_dir is not None else [] ),
```

- [ ] **Step 2: Run daemon unit tests**

Run: `.venv/bin/pytest tests/unit -q -k daemon`
Expected: existing daemon tests still pass (param is optional). Note any
`session_dir` output failures — fixed in Task 5.3.

- [ ] **Step 3: Run gates**

Run:
```
.venv/bin/ruff check tunstrap/daemon.py
.venv/bin/mypy --strict tunstrap/daemon.py
```
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tunstrap/daemon.py
git commit -m "feat(daemon): pass session_dir through to the worker"
```

### Task 5.3: GREEN — worker creates session dir, materializes, cleans up

**Files:**
- Modify: `tunstrap/_worker.py`

- [ ] **Step 1: Parse --session-dir and create the SessionDir**

Add to `_parse_args`:

```python
    parser.add_argument("--session-dir", default=None)
```

Add the import:

```python
from tunstrap.session import SessionDir
```

- [ ] **Step 2: Replace the lock-based identity with the session dir**

In `_run`, after schema is read and before building the manager, create the
session dir, write identity into `tunnel-data/`, and acquire the identity lock
inside that directory. Replace the prior `_acquire_identity_lock(token)` call in
`main()` with a session-scoped flow:

- Create `session = SessionDir.create(supplied=args.session_dir)`.
- Write `session.write_identity(pid=os.getpid(), token=args.token)`.
- Acquire the flock lockfile in `Path(session.session_dir) / "tunnel-data"`
  (pass that dir to a parametrized `_acquire_identity_lock(token, state_dir)`).
- Pass `session` to `TunnelManager(schema, session=session)` when
  `schema.daemon.materialize` is True, else `TunnelManager(schema)`.
- Call `start_all_and_build_output(pid=..., token=..., session_dir=session.session_dir)`.
- On every exit path (`required_failure`, `daemon_error`, normal shutdown),
  call `session.cleanup()` in addition to releasing the lock.

Concretely, change `_acquire_identity_lock` to accept a directory:

```python
def _acquire_identity_lock(token: str, state_dir: Path) -> int:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / f"{token}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise DaemonError("identity lock already held — token collision", {}) from exc
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    os.fsync(fd)
    return fd
```

And in `main()`, create the session BEFORE acquiring the lock, then pass the
`tunnel-data` dir:

```python
def main(argv: list[str] | None = None) -> None:
    """Worker entry: create session dir, lock identity, run loop, clean up, exit."""
    args = _parse_args(argv)
    try:
        session = SessionDir.create(supplied=args.session_dir)
        data_dir = Path(session.session_dir) / "tunnel-data"
        lock_fd = _acquire_identity_lock(args.token, data_dir)
        session.write_identity(pid=os.getpid(), token=args.token)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        _report_pre_run_failure(args.ipc_fd, exc)
        os._exit(4)
    rc = asyncio.run(_run(args, lock_fd, session))
    os._exit(rc)
```

Update `_run` to accept `session` and pass `session_dir`/cleanup. Replace each
`_release_identity_lock(lock_fd, args.token)` call with:

```python
        _release_identity_lock(lock_fd, args.token, Path(session.session_dir) / "tunnel-data")
        session.cleanup()
```

and update `_release_identity_lock` to take the directory:

```python
def _release_identity_lock(lock_fd: int, token: str, state_dir: Path) -> None:
    try:
        (state_dir / f"{token}.lock").unlink()
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass
```

Update the manager construction and call:

```python
    manager = TunnelManager(schema, session=session if schema.daemon.materialize else None)
    ...
    result = await manager.start_all_and_build_output(
        pid=os.getpid(), token=args.token, session_dir=session.session_dir
    )
```

Add the `Path` import at the top: `from pathlib import Path`.

- [ ] **Step 3: Run the worker-dependent unit tests**

Run: `.venv/bin/pytest tests/unit -q`
Expected: previously-failing `session_dir` signature tests now PASS; the full
unit suite is green except any tests still asserting the old lock location
(update those tests in this step to point at the session `tunnel-data`).

- [ ] **Step 4: Run gates**

Run:
```
.venv/bin/ruff check tunstrap/_worker.py
.venv/bin/mypy --strict tunstrap/_worker.py
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tunstrap/_worker.py tests/unit
git commit -m "feat(worker): session-dir identity + materialize + cleanup"
```

### Task 5.4: GREEN — CLI start --session-dir; stop rewritten to --session-dir

**Files:**
- Modify: `tunstrap/cli.py`

- [ ] **Step 1: Add --session-dir to start_command and pass it to spawn_daemon**

```python
@main.command("start")
@click.option("--session-dir", "session_dir", default=None)
def start_command(session_dir: str | None) -> None:
    """Read JSON from stdin, open tunnels, daemonize, print mapping JSON."""
    try:
        raw = sys.stdin.read()
        ...
        message = spawn_daemon(schema, session_dir=session_dir)
        ...
```

(Keep the rest of `start_command` unchanged.)

- [ ] **Step 2: Rewrite stop_command to --session-dir only**

Replace the entire `stop_command` with:

```python
@main.command("stop")
@click.option("--session-dir", "session_dir", required=True)
@click.option("--grace-seconds", type=int, default=10, show_default=True)
def stop_command(session_dir: str, grace_seconds: int) -> None:
    """Stop the daemon recorded under <session-dir>/tunnel-data and clean it up."""
    from pathlib import Path

    from tunstrap.session import SessionError

    try:
        pid, token = SessionDir.read_identity(session_dir)
    except SessionError as exc:
        sys.stdout.write(json.dumps({"stopped": False, "reason": str(exc)}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(0)
    data_dir = Path(session_dir).resolve() / "tunnel-data"
    _kill_with_identity(pid, token, grace_seconds, force=True, state_dir=data_dir)
    # Remove tunnel-data (and the generated dir if applicable) best-effort.
    SessionDir.cleanup_path(session_dir)
```

- [ ] **Step 3: Thread state_dir through _kill_with_identity and _is_alive**

Add `state_dir: Path | None = None` to `_kill_with_identity` and pass it to the
two `verify_token(pid, token)` calls as `verify_token(pid, token, state_dir)`.
Do the same for `status_command` via `_is_alive(pid, token, state_dir)`.

- [ ] **Step 4: Add SessionDir.cleanup_path classmethod**

In `tunstrap/session.py`, add a stop-side helper that removes tunnel-data
without an instance (stop does not know if the dir was generated, so it removes
only tunnel-data — the safe subset; a generated dir's empty shell is harmless):

```python
    @classmethod
    def cleanup_path(cls, session_dir: str) -> None:
        """Remove <session_dir>/tunnel-data best-effort (stop-side cleanup)."""
        data = Path(session_dir).resolve() / _TUNNEL_DATA
        shutil.rmtree(data, ignore_errors=True)
```

- [ ] **Step 5: Update the stop import block**

Ensure cli.py imports `SessionDir`:

```python
from tunstrap.session import SessionDir
```

- [ ] **Step 6: Update CLI unit tests**

Update `tests/unit/test_cli_start_validation.py` and any stop tests to the new
interfaces (start gains `--session-dir`; stop uses `--session-dir`, no
`--pid/--token`). For stop, point a test session dir at a tmp `tunnel-data`
with `daemon.pid`/`token` files and assert the JSON result shape.

- [ ] **Step 7: Run CLI unit tests**

Run: `.venv/bin/pytest tests/unit -q -k cli`
Expected: PASS.

- [ ] **Step 8: Run gates**

Run:
```
.venv/bin/ruff check tunstrap/cli.py tunstrap/session.py
.venv/bin/mypy --strict tunstrap/cli.py tunstrap/session.py
```
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add tunstrap/cli.py tunstrap/session.py tests/unit
git commit -m "feat(cli): start --session-dir; stop via --session-dir (remove legacy)"
```

---

## Phase 6 — Full unit sweep + gates

### Task 6.1: Green the whole unit suite + all gates

**Files:**
- Modify: any unit test still referencing removed/renamed interfaces.

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/pytest tests/unit -q`
Expected: all green. Fix any remaining tests that still assume the old
`OutputSchema` (no `session_dir`), the old `stop --pid --token`, or the old
identity location. Each fix: adjust the test to the new shape (do not weaken
assertions).

- [ ] **Step 2: Run all gates across the package**

Run:
```
.venv/bin/ruff format --check tunstrap tests
.venv/bin/ruff check tunstrap tests
.venv/bin/mypy --strict tunstrap
.venv/bin/pylint tunstrap
.venv/bin/vulture tunstrap vulture_whitelist.py
```
Expected: ruff/black clean; mypy clean; pylint ≥ 9.0; vulture reports nothing
new. If vulture flags a genuinely unused symbol introduced by this work, remove
it; if it flags an intentionally-public API, add it to `vulture_whitelist.py`.

- [ ] **Step 3: Commit any test/format fixups**

```bash
git add -A
git commit -m "test: green full unit suite for kube mode; lint/type clean"
```

---

## Phase 7 — Integration

### Task 7.1: Add a fake-apiserver TLS service to the compose stack

**Files:**
- Modify: `tests/integration/docker-compose.yml`
- Modify: `tests/integration/conftest.py`

- [ ] **Step 1: Add a TLS service that serves a cert with a known SAN**

Add a service to `docker-compose.yml` that listens on `:6443` with a TLS cert
whose SAN includes a known DNS name (e.g. `dev-kube-1`) and the container's IP.
A minimal approach: an `nginx`/`openssl s_server` container that presents a
self-signed cert generated at build with `-addext "subjectAltName=DNS:dev-kube-1"`.
Mount a generated `k3s.yaml` whose `server:` points at that service's
in-network address (`https://fake-apiserver:6443`), reachable over the sshd
forward.

```yaml
  fake-apiserver:
    image: alpine:3.20
    command: >
      sh -c "apk add --no-cache openssl &&
      openssl req -x509 -newkey rsa:2048 -nodes -days 1
        -subj '/CN=dev-kube-1'
        -addext 'subjectAltName=DNS:dev-kube-1,IP:127.0.0.1'
        -keyout /tmp/k.pem -out /tmp/c.pem &&
      openssl s_server -accept 6443 -cert /tmp/c.pem -key /tmp/k.pem -quiet"
    networks: [default]
```

- [ ] **Step 2: Add a kubeconfig fixture pointing at the fake apiserver**

In `prepared_files` (conftest.py), add a `kube_k3s` file with:
```
server: https://fake-apiserver:6443
current-context: default
```
plus a single cluster/context/user named `default` with dummy b64 CA/cert/key.
Bind-mount it into the sshd containers like the existing `kubeconfig`.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/docker-compose.yml tests/integration/conftest.py
git commit -m "test(it): fake apiserver TLS service + kube fixture"
```

### Task 7.2: Integration test — end-to-end kube_target

**Files:**
- Create: `tests/integration/test_kube_targets.py`

- [ ] **Step 1: Write the end-to-end test**

```python
"""End-to-end kube_targets over a real sshd forward + fake apiserver.

Validates: start with a kube_target produces a patched kubeconfig whose
server points at the local forwarded port and whose tls-server-name is the
probed SAN; materialize writes the file; stop cleans up the session dir.
Code: tunstrap kube mode (kube.py, manager.py, _worker.py, cli.py)
Assertion: output.connections[node].kube_targets.k3s.endpoint is local;
tls_server_name == 'dev-kube-1'; materialized path exists then is removed.
Method: drive `tunstrap start`/`stop` subprocesses against compose.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import tunstrap_start

pytestmark = [pytest.mark.integration]


def test_kube_target_end_to_end(ssh_test_cluster: dict[str, Any]) -> None:
    """A kube_target yields a locally-usable, SAN-correct, patched kubeconfig."""
    session_dir = tempfile.mkdtemp(prefix="gt-kube-it-")
    payload = {
        "nodes": {
            "node": {
                "host": ssh_test_cluster["host"],
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": ssh_test_cluster["user"],
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"keep": "127.0.0.1:22"},
                "kube_targets": {"k3s": {"kubeconfig_path": "/data/kube_k3s"}},
            }
        },
        "daemon": {"materialize": True, "auto_stop_idle_seconds": 60},
    }
    result = subprocess.run(
        ["tunstrap", "start", "--session-dir", session_dir],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    kt = out["connections"]["node"]["kube_targets"]["k3s"]
    assert kt["endpoint"].startswith("https://127.0.0.1:")
    assert kt["tls_server_name"] == "dev-kube-1"
    patched = base64.b64decode(kt["content_b64"]).decode()
    assert "127.0.0.1" in patched
    assert "tls-server-name: dev-kube-1" in patched
    assert kt["path"] is not None and Path(kt["path"]).is_file()

    stop = subprocess.run(
        ["tunstrap", "stop", "--session-dir", session_dir],
        text=True,
        capture_output=True,
    )
    assert stop.returncode == 0, stop.stderr
    assert not (Path(session_dir) / "tunnel-data").exists()
```

> The `tunstrap_start` helper imported above is reused only for shape
> reference; this test invokes `start` directly to pass `--session-dir`. Keep the
> import only if a helper call is added; otherwise drop it to satisfy vulture/ruff.

- [ ] **Step 2: Run the integration test**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/integration/test_kube_targets.py -m integration -q`
Expected: PASS. (Requires Docker; the PATH prepend makes the bare `tunstrap`
in conftest resolve to the venv entry point.)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_kube_targets.py
git commit -m "test(it): end-to-end kube_target forward+probe+patch+materialize"
```

### Task 7.3: Integration test — insecure_fallback

**Files:**
- Modify: `tests/integration/test_kube_targets.py`

- [ ] **Step 1: Add a SAN-less apiserver service + fixture**

Add `fake-apiserver-nosan` to compose serving a cert generated WITHOUT
`-addext subjectAltName` (CN only, empty SAN), and a `kube_nosan` fixture whose
`server:` points at it.

- [ ] **Step 2: Write the test**

```python
def test_kube_target_insecure_fallback(ssh_test_cluster: dict[str, Any]) -> None:
    """A SAN-less cert with insecure_fallback=True yields insecure-skip-tls-verify."""
    session_dir = tempfile.mkdtemp(prefix="gt-kube-it-")
    payload = {
        "nodes": {
            "node": {
                "host": ssh_test_cluster["host"],
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": ssh_test_cluster["user"],
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"keep": "127.0.0.1:22"},
                "kube_targets": {
                    "k3s": {"kubeconfig_path": "/data/kube_nosan", "insecure_fallback": True}
                },
            }
        },
        "daemon": {"auto_stop_idle_seconds": 60},
    }
    result = subprocess.run(
        ["tunstrap", "start", "--session-dir", session_dir],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    kt = out["connections"]["node"]["kube_targets"]["k3s"]
    assert kt["tls_server_name"] is None
    assert kt["certificate_authority_data"] == ""
    patched = base64.b64decode(kt["content_b64"]).decode()
    assert "insecure-skip-tls-verify: true" in patched
    assert any("insecure_fallback" in w["error"] for w in out["warnings"])
    subprocess.run(
        ["tunstrap", "stop", "--session-dir", session_dir],
        text=True, capture_output=True, check=False,
    )
```

- [ ] **Step 3: Run it**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/integration/test_kube_targets.py -m integration -q`
Expected: PASS (both integration tests).

- [ ] **Step 4: Commit**

```bash
git add tests/integration
git commit -m "test(it): insecure_fallback path for SAN-less apiserver"
```

---

## Phase 8 — Documentation

### Task 8.1: README — kube mode, materialization, host-key, migration

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Kube mode" section**

After the "Input reference" section, add a "Kube mode (`kube_targets`)" section
documenting: the `kube_targets` field and `KubeTarget` fields
(`kubeconfig_path`, `tls_server_name`, `insecure_fallback`, `required`), the
one-cluster/current-context rule, SAN-probe behavior + selection preference, and
the `connections[node].kube_targets[name]` output fields. Use `example.net`
domains only.

- [ ] **Step 2: Add an "On-disk materialization" subsection**

Document `daemon.materialize` (default False), the `tunnel-data/` layout, the
`path` output field, and that materialized files (incl. private keys) are mode
0600 and removed on stop/atexit.

- [ ] **Step 3: Rewrite the security "content never to disk" note**

State the default guarantee is unchanged; materialization is opt-in. Add the
host-key threat-model paragraph from the spec (disposable/trusted hosts;
pinning is future; do not use kube mode on untrusted networks).

- [ ] **Step 4: Add a migration note for removed `stop --pid --token`**

Document that `stop` now takes `--session-dir`, and that `start` accepts an
optional `--session-dir`; the `session_dir` field is always in the output.

- [ ] **Step 5: Verify no real domains/IPs slipped in**

Run: `.venv/bin/python - <<'PY'`-style check or `rg -n "gfn\.team|[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" README.md` and confirm only `example.net`/loopback `127.0.0.1` appear.
Expected: no real domains; only `example.net` and `127.0.0.1`.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: README kube mode, materialization, host-key, migration"
```

---

## Phase 9 — Final verification + PR

### Task 9.1: Full verification sweep

**Files:**
- Create/update the untracked baseline record with final counts.

- [ ] **Step 1: Run the entire test + gate matrix**

Run:
```
.venv/bin/ruff format --check tunstrap tests
.venv/bin/ruff check tunstrap tests
.venv/bin/mypy --strict tunstrap
.venv/bin/pylint tunstrap
.venv/bin/vulture tunstrap vulture_whitelist.py
.venv/bin/pytest tests/unit -q
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/integration -m integration -q
```
Expected: all green; pylint ≥ 9.0; coverage (if run with `--cov`) ≥ 80%.

- [ ] **Step 2: Record final counts in the artifact**

Append final unit + integration counts and gate statuses to the baseline
artifact (untracked; do not commit).

- [ ] **Step 3: Sanity-grep for leftover legacy references**

Run: `rg -n "stop --pid|--token|\.kube\.|gfn\.team" tunstrap README.md docs/specs`
Expected: no production references to removed `stop --pid/--token`, no old
`.kube.` output key, no real domain. (Hits inside migration notes that
explicitly describe the removal are acceptable.)

### Task 9.2: Push + open PR

- [ ] **Step 1: Confirm branch + clean tree**

Run: `git status --short && git log --oneline -15`
Expected: clean tree on `feature/kube-targets`; the commit chain matches the
phases above.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin feature/kube-targets
gh pr create --fill --title "feat: kube_targets self-contained kubeconfig forwarding"
```
Expected: PR URL returned. Reference the spec
`docs/specs/2026-05-30-kube-targets-design.md` in the PR body.

---

## Notes for the implementer

- **TDD discipline:** every feature task is RED (failing test) → GREEN (minimal
  code) → gates → commit. Do not write implementation before its test fails.
- **Async tests** rely on `asyncio_mode = auto` (already in pyproject); mark
  coroutine tests with `@pytest.mark.asyncio` only where shown.
- **Integration PATH:** `tests/integration/conftest.py` invokes a bare
  `tunstrap`; always prepend the venv: `PATH="$PWD/.venv/bin:$PATH"`.
- **mypy + third-party stubs:** if `ruamel.yaml` or `cryptography` lack stubs,
  add a `[[tool.mypy.overrides]]` block with `ignore_missing_imports = true` and
  commit it alongside the first task that imports them.
- **Secrets:** never log decoded file bytes or private keys; `_scrub` in
  `exceptions.py` already strips secret keys from error details — keep new error
  paths routed through the typed exceptions.
