# Kubeconfig-as-identity delivery (issue #15) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Ticket:** AlexMKX/tunstrap#15 — rework kube delivery: deterministic context
names + a unified, env-native output contract.

**Target branch:** `feature/run-env-io` (PR #13). Every task below assumes a
checkout of that branch as the working tree.

**Spec:** `docs/specs/2026-08-07-issue15-kube-identity-delivery-design.md`
**Decision history (ADR, entries 1-19):**
`docs/specs/2026-08-07-issue15-kube-identity-decisions.md` — all historical
rationale (alternatives considered, why each rule is shaped the way it is)
lives there; this plan states what to build, not how the design arrived at it.

**Spike reference implementation:** branch `variant/combined` in the read-only
scratch worktree `/home/alex/Projects/garuda/worktrees/tunstrap-issue15-spike`
(reviewed; 475/475 pre-existing unit tests pass plus one new regression test).
**Cherry-pick the `kube.py` rename change from it as-is. Do NOT cherry-pick its
`envrender.py` change:** the spike's env-export body is a naive superset export
and must be replaced by the conditional cardinality contract given in Task 3.
The spike covers the kube part only (Tasks 1-3); it prototypes nothing of the
unified output contract, materialization, or the scalar-channel removal (Tasks
4-6) — that is new code with no spike reference. Never commit from the spike
worktree. Do not re-derive the six OpenTofu findings or the provider-precedence
findings; both are already in `docs/artifacts/`
(`2026-08-07-issue15-provider-env-findings.md`,
`2026-08-07-issue15-spike-findings.md`).

**Tech stack:** Python 3.10+, Pydantic v2, Click, ruamel.yaml, pytest +
pytest-asyncio. Use `.venv/bin/{pytest,ruff,black,mypy,pylint,vulture}`.
Integration/e2e tiers need Docker (+ `kind`/`kubectl`/`tofu` for e2e) on
`PATH`; see `tests/README.md` for env flags.

**Standing discipline for this plan:** the blast-radius tables in Task 5 are
grep-driven, authoritative enumerations, not starting points. If you find
something a table missed, **re-run the greps at the stated scope** — do not
patch the single spot you found. Default any new enumeration to the widest
established scope for the same search term (`tunstrap/ tests/ docs/` ×
`--include='*.py' --include='*.md' --include='*.tf'`) and narrow only with an
explicit, stated reason.

---

## The contract this plan implements

**Core principle: content on disk, paths in env.** All content-bearing
artifacts (patched kubeconfigs, the unified manifest, fetched files) are
written under `<session_dir>/tunnel-data/` at mode `0600`; only locators
travel through the environment.

### Two delivery modes

1. **Kube: env-native, always plan-safe.** `run` exports
   `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` (plus `KUBECONFIG`) from its own
   process environment, and the consumer pins a **literal**
   `config_context = "tunstrap-<node>-<target>"` per provider alias. No
   Terraform variable and no `file()` read anywhere in the kube path, so a
   saved plan applies cleanly (findings #1/#3/#4).
2. **Everything else: the unified manifest file, located by
   `TUNSTRAP_OUTPUT_FILE`.** `run` (and `start --output env`) exports
   `TUNSTRAP_OUTPUT_FILE=<session_dir>/tunnel-data/output.json` as a plain
   process env var; the consumer reads it with
   `try(jsondecode(file(get_env("TUNSTRAP_OUTPUT_FILE"))), { nodes = {} })`.
   The session dir is ephemeral and freshly minted per invocation, and the
   file is deleted at teardown/`stop`, so this mode — and the `--output-var`
   bridge below — is **one-shot `plan && apply` within a single tunstrap
   invocation only**. No saved-plan reuse across a tunstrap restart, no
   locator exemption (finding #6 compares the variable's whole bound value).
   `--output-var NAME` (`TF_VAR_tunstrap`) survives only as a narrower
   fallback for bare `tofu`, which cannot call `get_env(...)`; it carries the
   same manifest under the same one-shot rule.

Never read the manifest through a Terraform `output` block: outputs freeze
silently at plan time (finding #2). Read it directly inside the
provider/`locals` block that consumes it. Binding live data to a *resource*
attribute (rather than a provider config block) produces `Error: Provider
produced inconsistent final plan` — provider-block placement is the only
supported shape (findings artifact, Q3).

### The unified manifest shape

```json
{
  "session": {
    "session_dir": "/run/tunstrap/abc123",
    "pid": 4711,
    "started_at": "2026-08-07T00:00:00Z",
    "warnings": []
  },
  "nodes": {
    "node1": {
      "ports": {"service1": "127.0.0.1:5432"},
      "kube": {
        "k3s": {
          "path": "/run/tunstrap/abc123/tunnel-data/node1-k3s",
          "context": "tunstrap-node1-k3s",
          "endpoint": "https://127.0.0.1:41111"
        }
      },
      "fetch_files": {
        "hosts": {"path": "/run/tunstrap/abc123/tunnel-data/node1-hosts", "size": 6, "sha256": "..."}
      }
    }
  }
}
```

- Exactly two reserved top-level keys, `session` and `nodes` (a flat root
  would let an operator-named node collide with them).
- **Ports**: a plain `"host:port"` string per target — no integer form.
- **Kube**: `{path, context, endpoint}` only — never credentials, never file
  content. `context` is the post-rename `tunstrap-<node>-<target>` name.
- **`fetch_files`**: `{path, size, sha256}` on success, `{error}` on failure —
  **never `content_b64`**. The daemon materializes fetched bytes to
  `tunnel-data/<node>-<fetchname>`.

### Env keys `run` injects

Three survivor scalars, unconditionally: `TUNSTRAP_SESSION_DIR`,
`TUNSTRAP_PID`, `TUNSTRAP_OUTPUT_FILE`. Plus the kube channel when any kube
target materialized. Every `TUNSTRAP_<TARGET>_*` key is gone, along with
`render_env`, `inject_scalars`, and `MultiNodeEnvUnsupported`. Multi-node
input no longer requires `--output-var`.

### Kube env-export cardinality (never the naive superset)

| Materialized kubeconfig files | Exported keys |
|---|---|
| 0 | *(nothing)* |
| exactly 1 | `KUBECONFIG` + `KUBE_CONFIG_PATH` |
| ≥ 2 | `KUBECONFIG` + `KUBE_CONFIG_PATHS` (**no** `KUBE_CONFIG_PATH`) |

Values are colon-joined (`:`) — comma silently degrades to `localhost:80`
(finding #5). `KUBE_CONFIG_PATH` wins over `KUBE_CONFIG_PATHS` in the
measured provider precedence, so exporting both once a second file exists
would silently hide every cluster but the first (ADR entry 3).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `tunstrap/kube.py` | New `rename_identities(doc, node, target) -> str`, sweeping all references incl. non-current contexts; one call site in `run_kube_targets` between `patch_view` and `dump_kubeconfig`. |
| `tunstrap/schemas.py` | New `InputSchema`-level `model_validator` rejecting colliding `tunstrap-<node>-<target>` pairs; new `UnifiedOutput`/`UnifiedSession`/`UnifiedNode`/`UnifiedKubeRef`/`UnifiedFetchRef` models; `FetchedFile` gains `path: str \| None = None`; `RunKubeTarget` deleted. |
| `tunstrap/envrender.py` | New `_kube_channel_keys(count)` + `render_kube_env(output)` (conditional cardinality contract); new `render_unified_output(output)`; `render_output_var`'s body rewritten to serialize the unified structure; `predicted_env_keys` rewritten (conservative); `render_env` deleted. |
| `tunstrap/exceptions.py` | `MultiNodeEnvUnsupported` and its `_EXIT_CODES` entry deleted. |
| `tunstrap/cli.py` | `_build_child_env`: unconditional `render_kube_env(output)` call, three survivor scalars, `suppress_kubeconfig` drops all three kube names; new `_materialized_output_path` + atomic-replace materialization writer in `run_command`'s success path; `start --output env` rebuilt on the same mapping; the pre-spawn multi-node-without-`--output-var` gate deleted; `inject_scalars` removed from the whole call chain. |
| `tunstrap/session.py` | Referenced; optionally gains a shared atomic-replace helper (temp file + `os.replace`) if `_write_file` can be refactored to be callable without a live `SessionDir`. |
| `tunstrap/` (daemon/worker materialization site) | New fetched-file materialization step writing `tunnel-data/<node>-<fetchname>` and setting `FetchedFile.path`. |
| `tests/unit/test_kube_rename.py` | New. `rename_identities` as a pure function. |
| `tests/unit/test_schemas_kube_naming_collision.py` | New. The join-collision validator. |
| `tests/unit/test_kube_identity_collision.py` | New. The mandatory k3s-style upstream-name collision regression test. |
| `tests/unit/test_kube_run.py` | Extend: `context_name`/`cluster_name` assert the renamed value. |
| `tests/unit/test_envrender.py` | Extend/rewrite: `render_kube_env` cardinality cases; conservative `predicted_env_keys` cases; the safety-envelope half of the anti-drift guard; `render_unified_output` shape tests; every `render_env`-dependent test deleted by name. |
| `tests/unit/test_cli_run_materialize.py` | New. Materialization writer tests. |
| `tests/unit/test_cli_run_output_var.py`, `test_cli_run_output_var_projection.py`, `test_cli_run.py`, `test_cli_run_input_env_scrub.py`, `test_cli_runner.py`, `test_cli_run_postspawn.py`, `test_exceptions.py`, `test_tofu_proxy.py`, `test_manager_fetch.py`, `test_output_schema.py` | Retargeted per Task 5's blast-radius tables. |
| `tests/integration/test_run_env_io.py`, `test_cli_modes.py` | Retargeted for the shape/scalar removal against the real console script. |
| `tests/e2e/module/main.tf`, `rig.py`, `test_tofu_providers.py`, `test_terragrunt_apply.py` | Retargeted to `nodes.*.kube.*.path` (Task 6). |
| `docs/recipe_terragrunt.md` | Pre-existing `connections.*` shape fixed; the fetched-files subsection rewritten; Mode A + Mode B consumer sections added. |

---

### Task 1: `rename_identities` + call site in `kube.py` + naming-collision check

**Files:**
- Modify: `tunstrap/kube.py`, `tunstrap/schemas.py`
- Test: `tests/unit/test_kube_rename.py` (new), `tests/unit/test_kube_run.py`
  (extend), `tests/unit/test_schemas_kube_naming_collision.py` (new)

- [ ] **Step 1: Write failing tests**

`tests/unit/test_kube_rename.py`:

```python
"""rename_identities: deterministic tunstrap-<node>-<target> identity rename.

Validates: the current-context's cluster/user/context are all renamed to the
same tunstrap-<node>-<target> string; non-current entries are untouched;
current-context itself is updated; the return value is that shared name.
Code: tunstrap/kube.py::rename_identities
Assertion: post-call doc state matches exactly, including the untouched
ignored entries; the returned name equals every renamed field.
Method: build a minimal ruamel-shaped dict (plain dicts are sufficient; the
function only calls .get/[]/isinstance) with two contexts, call the function,
inspect doc afterwards.
"""

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
        "users": [
            {"name": "default", "user": {}},
            {"name": "other-u", "user": {}},
        ],
    }


def test_renames_current_context_cluster_and_user_to_shared_name() -> None:
    """All three identity fields get the same tunstrap-<node>-<target> value."""
    doc = _doc()
    new_name = rename_identities(doc, "node-a", "kube")
    assert new_name == "tunstrap-node-a-kube"
    assert doc["current-context"] == "tunstrap-node-a-kube"
    ctx = doc["contexts"][0]
    assert ctx["name"] == "tunstrap-node-a-kube"
    assert ctx["context"]["cluster"] == "tunstrap-node-a-kube"
    assert ctx["context"]["user"] == "tunstrap-node-a-kube"
    assert doc["clusters"][0]["name"] == "tunstrap-node-a-kube"
    assert doc["users"][0]["name"] == "tunstrap-node-a-kube"


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
    """The exact k3s-style collision case: same input, different node -> different name."""
    assert rename_identities(_doc(), "a", "kube") != rename_identities(_doc(), "b", "kube")


def test_ignored_context_sharing_the_active_cluster_keeps_a_valid_reference() -> None:
    """A non-current context that references the SAME cluster/user the active
    triple uses must have that reference updated too, or it dangles -- naming
    a cluster/user that no longer exists anywhere in the document under its
    old name. The ignored context's own `name` is untouched (it is not
    renamed itself, only its cluster/user references are); only entries that
    neither ARE nor REFERENCE the active triple stay fully byte-stable."""
    doc: dict[str, object] = {
        "current-context": "default",
        "contexts": [
            {"name": "default", "context": {"cluster": "default", "user": "default"}},
            # Shares the SAME cluster/user as the active context, under a
            # different context name -- a legitimate, ordinary kubeconfig shape.
            {"name": "staging", "context": {"cluster": "default", "user": "default"}},
        ],
        "clusters": [{"name": "default", "cluster": {"server": "https://127.0.0.1:1"}}],
        "users": [{"name": "default", "user": {}}],
    }
    new_name = rename_identities(doc, "node-a", "kube")
    staging_ctx = doc["contexts"][1]
    assert staging_ctx["name"] == "staging"  # the ignored context's own name is untouched
    assert staging_ctx["context"]["cluster"] == new_name  # its reference is NOT left dangling
    assert staging_ctx["context"]["user"] == new_name
    # And the referenced entries genuinely exist under the new name.
    assert doc["clusters"][0]["name"] == new_name
    assert doc["users"][0]["name"] == new_name
```

Add to `tests/unit/test_kube_run.py` (extends the existing
`test_run_kube_target_success`):

```python
@pytest.mark.asyncio
async def test_run_kube_target_reports_renamed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """KubeTargetOutput.context_name/cluster_name are tunstrap-<node>-<target>, not upstream."""
    monkeypatch.setattr(
        "tunstrap.kube.sans_from_cert",
        lambda _der: (["dev-kube-1", "192.0.2.11"], []),
    )
    conn = _FakeConn((FIXTURES / "single_internal_ip.yaml").read_bytes())
    outputs, _, _ = await run_kube_targets(
        conn,
        {"k3s": KubeTarget.model_validate({"kubeconfig_path": "/etc/k3s.yaml"})},
        connect_timeout=5,
        probe=_probe_ok,
        node_name="edge",
    )
    out = outputs["k3s"]
    assert out.context_name == "tunstrap-edge-k3s"
    assert out.cluster_name == "tunstrap-edge-k3s"
```

- [ ] **Step 2: Run to verify failure**

`.venv/bin/pytest tests/unit/test_kube_rename.py tests/unit/test_kube_run.py -v`

Expected: `test_kube_rename.py` fails on import (`rename_identities` missing);
the new `test_kube_run.py` case fails because `context_name`/`cluster_name`
still equal the fixture's upstream `"production"`.

- [ ] **Step 3: Implement `rename_identities` in `kube.py`**

Add after `patch_view`, before `dump_kubeconfig` (cherry-pick from
`variant/combined`, `tunstrap/kube.py`, dropping the spike docstring's "V1c"
framing; `__all__` gains `"rename_identities"`):

```python
def rename_identities(doc: dict[str, object], node: str, target: str) -> str:
    """Rename the current-context's cluster/user/context to a deterministic name.

    ``tunstrap-<node>-<target>`` for cluster, user and context alike -- see
    docs/specs/2026-08-07-issue15-kube-identity-delivery-design.md. Operates on
    the raw parsed document alone; the current-context's own name is enough to
    find every entry that needs renaming.

    Every *other* context's cluster/user REFERENCES are also updated if they
    name the same cluster/user being renamed here -- a kubeconfig can
    legitimately have two contexts sharing one cluster or user entry, and
    leaving such a reference unrenamed while the entry it points at IS renamed
    would dangle it. Only entries that neither are, nor reference, the active
    triple are left untouched; other contexts' own `name` fields are never
    renamed, only their `cluster`/`user` reference fields when they match.

    Returns the new name (shared by cluster, user and context alike).
    """
    new_name = f"tunstrap-{node}-{target}"
    current = doc.get("current-context")
    assert isinstance(current, str)
    contexts_raw = doc.get("contexts")
    contexts: list[object] = contexts_raw if isinstance(contexts_raw, list) else []
    ctx_entry = _find_named(contexts, current)
    assert ctx_entry is not None
    ctx_body = ctx_entry["context"]
    assert isinstance(ctx_body, dict)
    old_cluster = ctx_body["cluster"]
    old_user = ctx_body["user"]
    assert isinstance(old_cluster, str)
    assert isinstance(old_user, str)

    ctx_entry["name"] = new_name
    ctx_body["cluster"] = new_name
    ctx_body["user"] = new_name

    cluster_entry = _find_named(doc.get("clusters") or [], old_cluster)
    assert cluster_entry is not None
    cluster_entry["name"] = new_name

    user_entry = _find_named(doc.get("users") or [], old_user)
    assert user_entry is not None
    user_entry["name"] = new_name

    doc["current-context"] = new_name

    # Sweep every OTHER context for a reference to the cluster/user entries
    # just renamed. That context's own `name` is not touched -- it is not
    # becoming the current context, only its dangling reference is fixed.
    for entry in contexts:
        if entry is ctx_entry or not isinstance(entry, dict):
            continue
        other_body = entry.get("context")
        if not isinstance(other_body, dict):
            continue
        if other_body.get("cluster") == old_cluster:
            other_body["cluster"] = new_name
        if other_body.get("user") == old_user:
            other_body["user"] = new_name

    return new_name
```

Wire the call site in `run_kube_targets` (replace the `patch_view` →
`dump_kubeconfig` → `KubeTargetOutput` block):

```python
        patch_view(view, local_port=local_port, tls_server_name=tls_name, insecure=insecure)
        assert isinstance(view.doc, dict)  # parse_kubeconfig guaranteed this
        new_identity = rename_identities(view.doc, node_name, name)
        patched = dump_kubeconfig(view)
        outputs[name] = KubeTargetOutput(
            cluster_name=new_identity,
            context_name=new_identity,
```

**Do not carry over the spike's `# type: ignore[arg-type]`** on this call —
`view.doc` is typed `object` on `KubeconfigView` (dataclass field,
`kube.py:56`) and `rename_identities` wants `dict[str, object]`; use the
explicit `assert isinstance(view.doc, dict)` above instead, matching the
pattern `patch_view` already uses two lines earlier (`kube.py:257`). This
keeps `mypy --strict` clean without a suppression. `dump_kubeconfig`'s
signature does not change — `patch_view` owns server-address patching.

- [ ] **Step 4: Run to verify pass**

`.venv/bin/pytest tests/unit/test_kube_rename.py tests/unit/test_kube_run.py tests/unit/test_kube_patch.py tests/unit/test_kube_parse.py tests/unit/test_kube_parse_invariants.py -v`

Expected: all pass. (The last three files are the ones the spike confirmed are
unaffected; this run is the regression check that it stayed true.)

- [ ] **Step 5: Commit**

```bash
git add tunstrap/kube.py tests/unit/test_kube_rename.py tests/unit/test_kube_run.py
git commit -m "feat(kube): rename current-context identity to tunstrap-<node>-<target> (#15)"
```

- [ ] **Step 6: Write the failing naming-collision test**

New file `tests/unit/test_schemas_kube_naming_collision.py`:

```python
"""tunstrap-<node>-<target> is NOT unique by construction.

_FETCH_FILES_KEY_RE (schemas.py:11) permits internal hyphens in node/target
identifiers, and the join itself uses a hyphen, so two DIFFERENT (node,
target) pairs can render the SAME string: (node="a-b", target="c") and
(node="a", target="b-c") both produce "tunstrap-a-b-c". This is a distinct
defect class from the k3s-style collision test (Task 2) -- that test proves
upstream kubeconfig names colliding is fixed by the rename; this test proves
tunstrap's OWN naming scheme does not collide with itself, independent of any
kubeconfig content at all.

Code: tunstrap/schemas.py (validator, InputSchema level)
Method: construct an InputSchema with exactly the a-b/c vs a/b-c pair and
assert validation rejects it, naming both colliding pairs.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tunstrap.schemas import InputSchema

pytestmark = pytest.mark.unit


def test_naming_join_collision_across_nodes_is_rejected() -> None:
    """(node='a-b', target='c') and (node='a', target='b-c') both join to
    tunstrap-a-b-c -- reject the whole payload, naming both colliding pairs."""
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate(
            {
                "nodes": {
                    "a-b": {
                        "host": "h1", "user": "u", "ssh_password": "p",
                        "kube_targets": {"c": {"kubeconfig_path": "/etc/x.yaml"}},
                    },
                    "a": {
                        "host": "h2", "user": "u", "ssh_password": "p",
                        "kube_targets": {"b-c": {"kubeconfig_path": "/etc/y.yaml"}},
                    },
                }
            }
        )
    message = str(excinfo.value)
    assert "tunstrap-a-b-c" in message
    assert "a-b" in message and "c" in message  # first colliding pair
    assert "a" in message and "b-c" in message  # second colliding pair


def test_non_colliding_hyphenated_names_are_accepted() -> None:
    """Anti-vacuity: hyphens alone don't trigger the check -- only an actual join collision does."""
    InputSchema.model_validate(
        {
            "nodes": {
                "node-one": {
                    "host": "h1", "user": "u", "ssh_password": "p",
                    "kube_targets": {"kube-a": {"kubeconfig_path": "/etc/x.yaml"}},
                },
                "node-two": {
                    "host": "h2", "user": "u", "ssh_password": "p",
                    "kube_targets": {"kube-b": {"kubeconfig_path": "/etc/y.yaml"}},
                },
            }
        }
    )
```

- [ ] **Step 7: Run to verify failure**

`.venv/bin/pytest tests/unit/test_schemas_kube_naming_collision.py -v`

Expected: FAIL — no such validator exists yet; the first test's payload is
wrongly accepted today.

- [ ] **Step 8: Implement the collision check in `schemas.py`**

Add an `InputSchema`-level `model_validator(mode="after")` alongside the
existing `_validate_auth` field validator (`schemas.py:278-289`) — this must
run at `InputSchema` level, not per-`NodeInput`, since the collision is
cross-node:

```python
@model_validator(mode="after")
def _validate_kube_identity_names_are_unique(self) -> InputSchema:
    """tunstrap-<node>-<target> is not unique by construction (hyphens are
    legal in both node and target names); reject a payload where two
    different (node, target) pairs join to the same rendered identity."""
    seen: dict[str, tuple[str, str]] = {}
    for node_name, node in self.nodes.items():
        for target_name in node.kube_targets or {}:
            joined = f"tunstrap-{node_name}-{target_name}"
            if joined in seen:
                other_node, other_target = seen[joined]
                raise ValueError(
                    f"kube identity name collision: ({node_name!r}, {target_name!r}) "
                    f"and ({other_node!r}, {other_target!r}) both render {joined!r}"
                )
            seen[joined] = (node_name, target_name)
    return self
```

Place it near `InputSchema`'s existing `_validate_auth` validator so both
cross-node checks live together. Do not move it onto `NodeInput` even though
`kube_targets` is a `NodeInput` field — single-node scope cannot see the
collision.

- [ ] **Step 9: Run to verify pass, then commit**

`.venv/bin/pytest tests/unit/test_schemas_kube_naming_collision.py tests/unit/test_schemas.py tests/unit/test_schemas_kube.py -v`

Expected: all pass.

```bash
git add tunstrap/schemas.py tests/unit/test_schemas_kube_naming_collision.py
git commit -m "feat(schemas): reject tunstrap-<node>-<target> naming collisions (#15)"
```

---

### Task 2: The mandatory collision regression test

**Files:**
- Create: `tests/unit/test_kube_identity_collision.py`

This is the trap the design doc's testing contract calls out by name: it must
land, unmodified in substance, regardless of how Task 1 was implemented.

- [ ] **Step 1: Copy the spike's prototype under a repo-convention file name**

Copy `tests/unit/test_issue15_context_collision.py` from the spike worktree
(`/home/alex/Projects/garuda/worktrees/tunstrap-issue15-spike`, branch
`variant/combined`) to `tests/unit/test_kube_identity_collision.py` in this
checkout — **only the file is renamed** (this repo's other test files never
carry an issue number, e.g. `test_kube_run.py`, `test_envrender.py`). **Keep
the test function name unchanged**,
`test_two_k3s_style_targets_get_distinct_deterministic_identities` — it is
already descriptive. Drop the module docstring's "spike" framing, replacing it
with a plain description; the content is otherwise correct verbatim (exact
source reproduced in full in
`docs/artifacts/2026-08-07-issue15-spike-findings.md`, "Part 3").

- [ ] **Step 2: Run to verify it is GREEN after Task 1**

`.venv/bin/pytest tests/unit/test_kube_identity_collision.py -v`

Expected: PASS. (It was confirmed RED against the unmodified branch and GREEN
under `variant/combined`; this run confirms the same holds against this
checkout's own Task 1 implementation.)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_kube_identity_collision.py
git commit -m "test(kube): regression test for the k3s upstream-name collision trap (#15)"
```

---

### Task 3: `render_kube_env` + the conditional env-export contract

**Files:**
- Modify: `tunstrap/envrender.py`
- Test: `tests/unit/test_envrender.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_envrender.py` (uses the existing `_kube_out` helper):

```python
def test_render_kube_env_zero_files_returns_empty() -> None:
    """No kube_targets anywhere -> no keys at all."""
    out = OutputSchema(
        connections={"h": NodeOutput(ports={"db": 1})},
        pid=1, session_dir="/s", started_at="now",
    )
    assert render_kube_env(out) == {}


def test_render_kube_env_one_file_sets_path_not_paths() -> None:
    """Exactly one materialized file: KUBECONFIG + KUBE_CONFIG_PATH, no _PATHS."""
    out = OutputSchema(
        connections={"h": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, "/s/k3s")})},
        pid=1, session_dir="/s", started_at="now",
    )
    env = render_kube_env(out)
    assert env == {"KUBECONFIG": "/s/k3s", "KUBE_CONFIG_PATH": "/s/k3s"}
    assert "KUBE_CONFIG_PATHS" not in env


def test_render_kube_env_two_files_sets_paths_not_path() -> None:
    """Two materialized files (could be one node, two targets, or two nodes):
    KUBECONFIG + KUBE_CONFIG_PATHS, no _PATH -- KUBE_CONFIG_PATH would win over
    KUBE_CONFIG_PATHS per the measured provider precedence and hide the second
    cluster."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, "/s/a-k3s")}),
            "b": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7001, "/s/b-k3s")}),
        },
        pid=1, session_dir="/s", started_at="now",
    )
    env = render_kube_env(out)
    assert env == {"KUBECONFIG": "/s/a-k3s:/s/b-k3s", "KUBE_CONFIG_PATHS": "/s/a-k3s:/s/b-k3s"}
    assert "KUBE_CONFIG_PATH" not in env


def test_predicted_env_keys_reserves_all_three_for_one_kube_target() -> None:
    """predicted_env_keys is a CONSERVATIVE predictor, not exact: it reserves
    all three kube names whenever ANY kube_targets are declared, regardless of
    exact count -- input cardinality can shrink by output time (an optional
    node/target can fail), so predicting the exact one-file branch here would
    under-reserve KUBE_CONFIG_PATHS for a schema that later, at runtime,
    actually produces >=2 files. render_kube_env's own export (tested above)
    stays exact -- only the predictor is conservative."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "node": {
                    "host": "h.example.net", "user": "u", "ssh_password": "p",
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                }
            }
        }
    )
    keys = predicted_env_keys(schema)
    assert {"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"} <= keys


def test_predicted_env_keys_reserves_all_three_for_two_kube_targets_one_node() -> None:
    """Same conservative reservation for the >=2 case -- the point is that BOTH
    cardinalities reserve identically (all three), which is what makes the
    predictor a safe over-approximation rather than a second exact
    implementation of _kube_channel_keys."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "node": {
                    "host": "h.example.net", "user": "u", "ssh_password": "p",
                    "kube_targets": {
                        "a": {"kubeconfig_path": "/etc/a.yaml"},
                        "b": {"kubeconfig_path": "/etc/b.yaml"},
                    },
                }
            }
        }
    )
    keys = predicted_env_keys(schema)
    assert {"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"} <= keys
```

**Why the predictor is conservative (and why the guard is two-part).**
`predicted_env_keys` runs pre-spawn against *input* cardinality, but an
optional (`required: false`) node or kube target can fail without failing the
run, so *output* cardinality can be smaller than what was declared. Two kube
targets declared (which an exact predictor would map to the `≥2` branch,
`KUBE_CONFIG_PATHS` only) but one optional node fails at connect time → only
one file materializes → the real export uses the `==1` branch
(`KUBE_CONFIG_PATH`), which an exact predictor never reserved. A
`--output-var KUBE_CONFIG_PATH` would then pass the pre-spawn collision check
and be **silently overwritten** post-spawn. Hence: reserve **all three** kube
names whenever *any* `kube_targets` are declared. Over-reserving is the safe
direction (a false-positive usage error, cheap and visible) versus
under-reserving (a silent post-spawn collision). The two tests above are the
**formula half** of the anti-drift guard; the **safety-envelope half**
(`actual ⊆ predicted`, driven by a cardinality-shrink case) needs
`_build_child_env` to exist and is therefore added in Task 5. See ADR entry 16.

- [ ] **Step 2: Run to verify failure**

`.venv/bin/pytest tests/unit/test_envrender.py -v`

Expected: FAIL — `render_kube_env` missing; `predicted_env_keys` still uses
the unconditional `KUBECONFIG`-only rule.

- [ ] **Step 3: Implement in `envrender.py`**

Add the shared cardinality helper (used by `render_kube_env`, so the export
rule lives in exactly one place):

```python
def _kube_channel_keys(count: int) -> set[str]:
    """Names of the kube-channel env keys the conditional contract exports.

    0 files: nothing. Exactly 1: KUBECONFIG + KUBE_CONFIG_PATH. >=2:
    KUBECONFIG + KUBE_CONFIG_PATHS. KUBE_CONFIG_PATH and KUBE_CONFIG_PATHS are
    never both present -- KUBE_CONFIG_PATH wins over KUBE_CONFIG_PATHS per the
    measured OpenTofu kubernetes/helm provider precedence (docs/artifacts/
    2026-08-07-issue15-provider-env-findings.md), so exporting both once a
    second file exists would silently hide every cluster but the first.
    """
    if count == 0:
        return set()
    if count == 1:
        return {"KUBECONFIG", "KUBE_CONFIG_PATH"}
    return {"KUBECONFIG", "KUBE_CONFIG_PATHS"}


def render_kube_env(output: OutputSchema) -> dict[str, str]:
    """Build the node-count-agnostic kube channel: KUBECONFIG plus the
    OpenTofu-provider-facing var the conditional contract picks.

    This channel has no node dimension: it collects one materialized path per
    kube_target across every node, so it is safe to call for any node count.
    """
    kube_paths: list[str] = []
    for node in output.connections.values():
        for kname, target in node.kube_targets.items():
            if target.path is None:
                raise ValueError(f"kube target {kname!r} not materialized; cannot set KUBECONFIG")
            kube_paths.append(target.path)
    if not kube_paths:
        return {}
    joined = ":".join(kube_paths)
    return {key: joined for key in _kube_channel_keys(len(kube_paths))}
```

Replace `render_env`'s inline kube-path block so it delegates to
`render_kube_env` (`render_env` is still alive at this point — Task 5 deletes
it). **This also deletes the now-unused `kube_paths: list[str] = []`
accumulator declaration at `envrender.py:49`** — the block below never appends
to it (that accumulation moved into `render_kube_env`), and leaving the
declaration would fail `ruff check` (unused variable) at Task 7's gate:

```python
    for kname, target in node.kube_targets.items():
        base = _key(kname)
        if target.path is None:
            raise ValueError(f"kube target {kname!r} not materialized; cannot set KUBECONFIG")
        put(f"TUNSTRAP_{base}_KUBECONFIG", target.path)
        put(f"TUNSTRAP_{base}_ENDPOINT", target.endpoint)

    for key, value in render_kube_env(output).items():
        put(key, value)
    return env
```

Update `predicted_env_keys` to reserve conservatively. Unlike
`render_kube_env`'s own export (exact, because it runs *after* real
materialization and knows the true count), this runs pre-spawn against the
*input* schema:

```python
def predicted_env_keys(schema: InputSchema) -> set[str]:
    keys: set[str] = set()
    if len(schema.nodes) == 1:
        (node,) = schema.nodes.values()
        keys.update({"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID"})
        for tname in node.remote_targets:
            base = _key(tname)
            keys.update(
                {f"TUNSTRAP_{base}_HOST", f"TUNSTRAP_{base}_PORT", f"TUNSTRAP_{base}_ENDPOINT"}
            )
        for kname in node.kube_targets or {}:
            base = _key(kname)
            keys.update({f"TUNSTRAP_{base}_KUBECONFIG", f"TUNSTRAP_{base}_ENDPOINT"})
    if any(node.kube_targets for node in schema.nodes.values()):
        keys.update({"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"})
    return keys
```

Task 5 rewrites this function's body again — **not** to change the kube
cardinality rule (already final and conservative here), but because the whole
`TUNSTRAP_*` scalar half (`if len(schema.nodes) == 1:`) disappears with the
scalar channel. The conservative kube-reservation line survives that rewrite
verbatim.

Also correct the docstring claim at `envrender.py:83-93` that "multi-node
input injects no scalars at all, so the answer there is the empty set" — now
false for the kube-channel keys; it stays true only for `TUNSTRAP_*` scalars.

- [ ] **Step 4: Run to verify pass**

`.venv/bin/pytest tests/unit/test_envrender.py -v`

Expected: all pass, including every pre-existing case — single-node
`KUBECONFIG` behaviour is byte-identical for the one-kube-target case, since
`_kube_channel_keys(1)` includes `KUBECONFIG` exactly as the old
unconditional `put("KUBECONFIG", ...)` did.

- [ ] **Step 5: Commit**

```bash
git add tunstrap/envrender.py tests/unit/test_envrender.py
git commit -m "feat(envrender): multi-node kube channel + conditional KUBE_CONFIG_PATH(S) export (#15)"
```

---

### Task 4: The unified output contract — shape + `render_unified_output`

Pure-function work only: no `cli.py` wiring and no materialization yet (both
Task 5). This task makes the shape exist and be correctly built from an
`OutputSchema`.

**Files:**
- Modify: `tunstrap/schemas.py` (new models), `tunstrap/envrender.py`
  (`render_unified_output`, `render_output_var` body rewritten)
- Test: `tests/unit/test_envrender.py` (new cases)

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_envrender.py`:

```python
def test_render_unified_output_shape() -> None:
    """Ports become 'host:port' strings; kube becomes {path,context,endpoint}
    references; fetch_files becomes {path,size,sha256} -- NOT a content_b64
    passthrough; two reserved top-level keys."""
    out = OutputSchema(
        connections={
            "node1": NodeOutput(
                ports={"service1": 5432},
                kube_targets={
                    "k3s": _kube_out_full(
                        7000, "/s/tunnel-data/node1-k3s", context="tunstrap-node1-k3s"
                    )
                },
                fetch_files={
                    # .path is set here because materialization (Task 5) runs
                    # before render_unified_output ever sees this object -- the
                    # daemon writes the bytes and sets .path, exactly as it
                    # already does for KubeTargetOutput.path today.
                    "hosts": FetchedFile(
                        content_b64="aG9zdHM=", size=6, sha256="ab" * 32,
                        path="/s/tunnel-data/node1-hosts",
                    )
                },
            )
        },
        pid=42,
        session_dir="/s",
        started_at="2026-08-07T00:00:00Z",
    )
    unified = render_unified_output(out)
    assert unified["session"] == {
        "session_dir": "/s",
        "pid": 42,
        "started_at": "2026-08-07T00:00:00Z",
        "warnings": [],
    }
    node = unified["nodes"]["node1"]
    assert node["ports"] == {"service1": "127.0.0.1:5432"}
    assert node["kube"]["k3s"] == {
        "path": "/s/tunnel-data/node1-k3s",
        "context": "tunstrap-node1-k3s",
        "endpoint": "https://127.0.0.1:7000",
    }
    # {path, size, sha256} exactly -- no content_b64 in the projection.
    assert node["fetch_files"]["hosts"] == {
        "path": "/s/tunnel-data/node1-hosts", "size": 6, "sha256": "ab" * 32,
    }
    # Nothing that could carry raw content -- kube credentials AND fetched
    # file content_b64 -- ever appears anywhere in the shape.
    dumped = json.dumps(unified)
    for leaked in ("client_certificate_data", "client_key_data", "content_b64"):
        assert leaked not in dumped


def test_render_unified_output_multi_node() -> None:
    """Node dimension is a nested key: two nodes, two independent bodies."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(ports={"db": 1}),
            "b": NodeOutput(ports={"db": 2}),
        },
        pid=1, session_dir="/s", started_at="now",
    )
    unified = render_unified_output(out)
    assert set(unified["nodes"]) == {"a", "b"}
    assert unified["nodes"]["a"]["ports"]["db"] == "127.0.0.1:1"
    assert unified["nodes"]["b"]["ports"]["db"] == "127.0.0.1:2"


def test_render_output_var_serializes_the_unified_shape() -> None:
    """render_output_var's return value decodes to the same shape render_unified_output builds."""
    out = OutputSchema(
        connections={"h": NodeOutput(ports={"db": 1})},
        pid=1, session_dir="/s", started_at="now",
    )
    decoded = json.loads(render_output_var(out))
    assert decoded == render_unified_output(out)
```

(`_kube_out_full` is a small extension of the file's existing `_kube_out`
helper that also accepts a `context` kwarg — add it alongside `_kube_out`; do
not change `_kube_out`'s signature, Task 3's tests still use it unchanged.)

- [ ] **Step 2: Run to verify failure**

`.venv/bin/pytest tests/unit/test_envrender.py -k "unified" -v`

Expected: FAIL — `render_unified_output` missing; `render_output_var` still
returns the old `RunKubeTarget`-projection shape.

- [ ] **Step 3: Implement**

Add models to `tunstrap/schemas.py` (placed there, not `envrender.py`,
matching the existing convention that `schemas.py` is the "Single source of
JSON shape" per its own module docstring, `schemas.py:1`):

```python
class UnifiedKubeRef(BaseModel):
    """Kube reference in the unified output: never credentials, never content."""

    model_config = ConfigDict(extra="forbid")

    path: str | None
    context: str
    endpoint: str


class UnifiedSession(BaseModel):
    """Session metadata block of the unified output."""

    model_config = ConfigDict(extra="forbid")

    session_dir: str
    pid: int
    started_at: str
    warnings: list[TunnelWarning] = Field(default_factory=list)


class UnifiedFetchRef(BaseModel):
    """Fetched-file reference in the unified output: never content_b64,
    mirroring UnifiedKubeRef's own credential/content narrowing. Success and
    error are mutually exclusive, matching FetchedFile's own xor -- but this
    model has no validator enforcing it, because render_unified_output (below)
    is the only place that constructs one, from an already-validated
    FetchedFile, per exactly the same explicit-keyword-construction pattern
    UnifiedKubeRef already uses instead of a second runtime check."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    size: int | None = None
    sha256: str | None = None
    error: str | None = None


class UnifiedNode(BaseModel):
    """One node's body in the unified output: ports, kube refs, fetch_files."""

    model_config = ConfigDict(extra="forbid")

    ports: dict[str, str] = Field(default_factory=dict)
    kube: dict[str, UnifiedKubeRef] = Field(default_factory=dict)
    fetch_files: dict[str, UnifiedFetchRef] = Field(default_factory=dict)


class UnifiedOutput(BaseModel):
    """The entire consumer-facing output: two reserved top-level keys."""

    model_config = ConfigDict(extra="forbid")

    session: UnifiedSession
    nodes: dict[str, UnifiedNode]
```

Add to `tunstrap/envrender.py`:

```python
def render_unified_output(output: OutputSchema) -> dict[str, Any]:
    """Build the unified, node-qualified structure (design doc, "Unified
    output contract"). Ports become 'host:port' strings; kube becomes
    {path, context, endpoint} references (never credentials, never content);
    fetch_files becomes {path, size, sha256} (or {error}) -- NOT a
    passthrough: content must not enter a Terraform variable or the
    materialized manifest, only its path/metadata may. Callers must ensure
    fetch_files entries are already materialized (.path set) before calling
    this -- see Task 5's fetched-file materialization step, which runs
    upstream of this function, the same ordering KubeTargetOutput.path
    already requires today.
    """
    nodes: dict[str, object] = {}
    for node_name, node in output.connections.items():
        kube = {
            kname: UnifiedKubeRef(
                path=target.path, context=target.context_name, endpoint=target.endpoint
            ).model_dump()
            for kname, target in node.kube_targets.items()
        }
        ports = {tname: f"127.0.0.1:{port}" for tname, port in node.ports.items()}
        fetch_files = {
            fname: (
                UnifiedFetchRef(error=f.error).model_dump(exclude_none=True)
                if f.error is not None
                else UnifiedFetchRef(path=f.path, size=f.size, sha256=f.sha256)
                    .model_dump(exclude_none=True)
            )
            for fname, f in node.fetch_files.items()
        }
        nodes[node_name] = UnifiedNode(
            ports=ports, kube=kube, fetch_files=fetch_files
        ).model_dump()
    session = UnifiedSession(
        session_dir=output.session_dir,
        pid=output.pid,
        started_at=output.started_at,
        warnings=output.warnings,
    ).model_dump(mode="json")
    return {"session": session, "nodes": nodes}
```

**`exclude_none=True`, deliberately**: without it a success entry would
serialize `{"path": ..., "size": ..., "sha256": ..., "error": null}` — a stray
`"error": null` in every successful fetch, not matching the contract's shape
(`{"path", "size", "sha256"}` exactly) or the error-branch shape (`{"error"}`
exactly). `UnifiedKubeRef` needs no such treatment: none of its three fields
is ever optional/`None` in a materialized `KubeTargetOutput`.

Replace `render_output_var`'s body (signature unchanged, `OutputSchema -> str`
— no `cli.py` call-site change needed):

```python
def render_output_var(output: OutputSchema) -> str:
    """Serialise the unified structure for ``--output-var``.

    Delivers the same content the materialized file carries (Task 5) -- see
    docs/specs/2026-08-07-issue15-kube-identity-delivery-design.md, "The
    unified output contract", for the delivery/stability contract governing
    which of the two a plan-safe consumer should actually bind to.
    """
    return json.dumps(render_unified_output(output), separators=(",", ":"))
```

Delete the old `RunKubeTarget`-based body (the `payload =
output.model_dump(mode="json")` / per-node `RunKubeTarget.model_validate`
loop) — fully replaced, not kept as a fallback.

**`RunKubeTarget` disposition:** now unused by `render_output_var`. Check with
`vulture` (Task 7) whether anything else still imports it; if not, delete the
class from `schemas.py` too — its whole purpose (an allow-list projection for
this exact channel) is now served by `UnifiedKubeRef`.

- [ ] **Step 4: Run to verify pass**

`.venv/bin/pytest tests/unit/test_envrender.py -v`

Expected: all pass. The **old** `render_output_var` shape tests in
`tests/unit/test_cli_run_output_var.py` (e.g.
`test_output_var_carries_the_whole_envelope_minus_kube_credentials`) now fail,
expectedly — they pin the old `connections.<node>.ports.<target>` (int) shape;
Task 5 retargets them alongside the rest of that file's changes. Do not fix
them here; note the expected failures and move on (one clean commit per
concern).

- [ ] **Step 5: Commit**

```bash
git add tunstrap/schemas.py tunstrap/envrender.py tests/unit/test_envrender.py
git commit -m "feat(envrender): unified node-qualified output contract, shape only (#15)"
```

---

### Task 5: Materialize the unified output; remove the scalar channel

**This is the big ripple task.** It does six things in one coherent change,
because they are the same edit site (`_build_child_env` and its callers) or
its direct sibling (the daemon-side materialization step):

(a) wires `render_unified_output`/`render_output_var` into `run` and adds
unconditional materialization of `output.json`;
(b) collapses the kube-channel call to unconditional (ADR entry 13);
(c) deletes `render_env`, `MultiNodeEnvUnsupported`, and `inject_scalars`;
(d) **re-scopes** (not deletes) the `predicted_env_keys` anti-drift guard so
it compares against `_build_child_env`'s actual output;
(e) retargets every pre-existing test, fixture, and shipped artifact this
removal breaks, across every tier — enumerated exhaustively below;
(f) materializes `fetch_files` content to `tunnel-data/<node>-<fetchname>` the
same way kube files already are, removing `content_b64` from every
consumer-facing projection.

**Read before starting.** The tables below are a full grep-driven enumeration
across `tunstrap/`, `tests/unit`, `tests/integration`, `tests/e2e`, and
`docs/`. **Treat them as complete; if you find something they missed, that is
a signal to re-run the greps, not to patch the one spot found.**

Re-derivable with (or equivalent):

```bash
grep -rn 'TUNSTRAP_[A-Z0-9_]*' tunstrap/ tests/ docs/ \
  --include='*.py' --include='*.md' --include='*.tf' \
  | grep -vE 'TUNSTRAP_SESSION_DIR|TUNSTRAP_PID|TUNSTRAP_OUTPUT_FILE|TUNSTRAP_INPUT|TUNSTRAP_E2E_REQUIRE_ALL|TUNSTRAP_TOKEN'
grep -rn 'MultiNodeEnvUnsupported\|inject_scalars\|render_env(' tunstrap/ tests/ --include='*.py'
grep -rn 'connections\.' tests/ docs/ --include='*.py' --include='*.md' --include='*.tf'
grep -rn '\["connections"\]\|\.connections\[' tests/ --include='*.py'
grep -rn 'content_b64' tunstrap/ tests/ docs/ --include='*.py' --include='*.md' --include='*.tf'
```

#### Blast-radius table — unit tier (authoritative; every hit has a disposition)

| File:line | Old shape/symbol | Disposition |
|---|---|---|
| `test_cli_run.py:91` | `FakePopen.last_env["TUNSTRAP_DB_PORT"] == "5432"` | Retarget: assert `TUNSTRAP_SESSION_DIR`/`TUNSTRAP_PID`/`TUNSTRAP_OUTPUT_FILE` present, `TUNSTRAP_DB_PORT` absent. |
| `test_cli_run_input_env_scrub.py:156` | `env["TUNSTRAP_DB_PORT"] == "5432"`, "the injected scalars must survive the scrub" | Retarget: assert `TUNSTRAP_SESSION_DIR` survives the scrub instead; same docstring claim, different scalar. |
| `test_cli_run_input_env_scrub.py:174` | `json.loads(env[VAR])["pid"] == 99` | Retarget: `json.loads(env[VAR])["session"]["pid"] == 99` — `pid` moved under the unified structure's `session` key. |
| `test_cli_runner.py:392` (+docstring at ~360) | `"export TUNSTRAP_DB_PORT='5432'" in res.output` — the `start --output env` pin | **Fix the existing assertion**, not just "add a test": replace with the new three-survivors-plus-kube-channel export set; drop `TUNSTRAP_DB_PORT`/`TUNSTRAP_WEB_PORT`-style lines from any fixture the test builds. |
| `test_cli_run_postspawn.py:955,993` (`test_lone_optional_node_failure_keeps_its_own_exit_code`) | Asserts `error["error"] == "MultiNodeEnvUnsupported"` for a lone optional node's failure (`connections == {}` trips `render_env`'s `!= 1` guard) | Retarget completely; the new behaviour is the opposite: `_build_child_env` no longer branches on connection count, so this **succeeds** (exit 0). Rename to `test_lone_optional_node_failure_still_succeeds_with_only_a_warning`; assert exit 0, `session.warnings` (via `--output-var`) carries the "edge" failure, teardown ran exactly once. |
| `test_cli_run_output_var.py` (multiple) | See Step 2's per-test list | Retarget/delete per that list. |
| `test_cli_run_output_var_projection.py` (whole file) | `RunKubeTarget` import; `["connections"]["node"]["kube_targets"]["k3s"]`; `decoded["pid"]`/`["session_dir"]`/`["started_at"]`/`["connections"]["node"]["ports"]` | Security-critical (credential-scrubbing pin) — see the dedicated sub-section below, not a one-line note. |
| `test_envrender.py:4` | `from tunstrap.exceptions import MultiNodeEnvUnsupported` | Delete the import — `ruff` F401 once every user of it in this file is gone. |
| `test_envrender.py` (`render_env`-dependent) | `test_render_ports_and_session`, `test_render_kube_sets_kubeconfig`, `test_render_kube_not_materialized_raises`, `test_render_requires_single_node_zero`, `test_render_requires_single_node_two` | Delete all five — each asserts on `render_env`, which no longer exists. Do not add any new `render_env`-asserting test in Task 3 either; there is nothing left for one to pin. |
| `test_envrender.py::test_predicted_env_keys_matches_render_env` | Compares `predicted_env_keys` against `render_env`'s output | **Retarget, not delete** — see "Anti-drift guard" below. |
| `test_envrender.py` (predicted_env_keys shape) | `test_predicted_env_keys_no_kube_omits_kubeconfig`, `test_predicted_env_keys_multi_node_is_empty` | Delete — both pin the old per-target scalar enumeration / the "multi-node is empty" claim, false under the new unconditional `{session scalars} ∪ kube-channel` contract. Replaced by `test_predicted_env_keys_is_session_scalars_plus_kube_channel` and `test_predicted_env_keys_no_kube_is_just_the_three_survivors` (Step 2). |
| `test_exceptions.py:87-90` | `issubclass(MultiNodeEnvUnsupported, TunstrapError)` subclass test | Delete. |
| `test_exceptions.py:94-99` | Exit-code + envelope test constructing `MultiNodeEnvUnsupported(...)` | Delete. |
| `test_exceptions.py:107-114` | `_EXIT_CODES[MultiNodeEnvUnsupported] == 1` table test | Delete. All **three** cases named explicitly. |
| `test_tofu_proxy.py:375,386,397,399` | Docstrings framing the pop in terms of `inject_scalars`/`render_env` | Update docstrings only (mechanism note, not an assertion change) — both `test_tunnelled_suppresses_kubeconfig_in_child_env` and `test_tunnelled_drops_an_inherited_kubeconfig_in_the_multi_node_case`. |

#### Blast-radius table — integration tier

| File:line | Old shape/symbol | Disposition |
|---|---|---|
| `test_run_env_io.py:49-50` (`_PROBE_SINGLE`) | `os.environ["TUNSTRAP_WEB_PORT"]` | Retarget: probe reads `json.load(open(os.environ["TUNSTRAP_OUTPUT_FILE"]))["nodes"]["hub"]["ports"]["web"]` (a `"host:port"` string; `.rsplit(":", 1)[1]` for the port). |
| `test_run_env_io.py` `_PROBE_MULTI` | `envelope["connections"][name]["ports"]["web"]` | Retarget to `envelope["nodes"][name]["ports"]["web"]` (string, parsed as above). |
| `test_run_env_io.py` `_PROBE_MULTI` leak check | `k.startswith("TUNSTRAP_") and k != "TUNSTRAP_INPUT"` — now wrongly flags the three sanctioned survivors as leaks | Retarget: exclude `TUNSTRAP_SESSION_DIR`, `TUNSTRAP_PID`, `TUNSTRAP_OUTPUT_FILE` too. |
| `test_run_env_io.py:173-193` (`test_multi_node_without_output_var_is_exit_1`) | Asserts exit 1 + `MultiNodeEnvUnsupported`, `not session_dir.exists()` | **Retarget completely — the exact behaviour this task inverts.** Rename to `test_multi_node_without_output_var_now_succeeds`; assert exit 0, no `MultiNodeEnvUnsupported` anywhere in stderr (stderr may be empty), teardown ran. Materialization *content* is not re-verified here — that is `test_cli_run_materialize.py`'s job; this test's remaining job is confirming the real console script allows the case. |
| `test_cli_modes.py:111-138` | `start --output env`'s `TUNSTRAP_WEB_PORT`/`TUNSTRAP_WEB_ENDPOINT`; `run`'s child probe reading `os.environ['TUNSTRAP_WEB_PORT']` directly | Retarget both tests in this range: the `start --output env` test asserts `TUNSTRAP_SESSION_DIR`/`TUNSTRAP_OUTPUT_FILE` present, `TUNSTRAP_WEB_PORT`/`_ENDPOINT` absent, and derives the port via `json.load(open(env["TUNSTRAP_OUTPUT_FILE"]))["nodes"][...]["ports"]["web"]`; the `run` child probe (inline Python string) rewrites to read `TUNSTRAP_OUTPUT_FILE` the same way. |

#### Blast-radius table — e2e tier + shipped artifacts

In scope, not deferrable: the failure mode is **silent** — `try()` around
`jsondecode` swallows a shape mismatch into an empty `config_path`, and the
resulting error is a confusing provider message, not an obvious test failure.
**The edits themselves land in Task 6** (same textual migration as the recipe,
and the recipe↔module drift guard requires both to move together); they are
enumerated here because they are this task's blast radius.

| File:line | Old shape/symbol | Disposition |
|---|---|---|
| `tests/e2e/module/main.tf:27-28` | `try(jsondecode(var.tunstrap), { connections = {} })`; `local.tunnel.connections.node.kube_targets.k3s.path` | Retarget: `{ nodes = {} }`; `local.tunnel.nodes.node.kube.k3s.path`. Task 6. |
| `docs/recipe_terragrunt.md:287-288` | Same `tunnel`/`kubepath` locals, mirroring `main.tf` | Retarget identically. Task 6. |
| `docs/recipe_terragrunt.md:~329` | Prose: "`path` comes from... `connections.*.kube_targets.*.path`" | Retarget prose to `nodes.*.kube.*.path`. Task 6. |
| `docs/recipe_terragrunt.md:~407` | Prose: "the module picks the node out of `connections[<node>]`" | Retarget to `nodes[<node>]`; also correct the surrounding paragraph's claim that multi-node suppresses the scalar/`KUBECONFIG` channel — the kube channel is unconditional and the "TUNSTRAP_* env... not injected" framing is stale. Task 6. |
| `docs/recipe_terragrunt.md:~509` | "What is proven" section: `--output-var` → `TF_VAR_tunstrap` → `try(jsondecode(...))` → `config_path` chain | Mechanism description stays accurate; **verify only** once the two locals above change. |
| `tests/e2e/rig.py:171` | Docstring: "`module/main.tf` decodes `connections.node.kube_targets.k3s.path`" | Retarget docstring text to `nodes.node.kube.k3s.path`. |
| `tests/e2e/test_tofu_providers.py:154` | `envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]` | Retarget to `envelope["nodes"]["node"]["kube"]["k3s"]["path"]`. |
| `tests/e2e/test_tofu_providers.py:251-254` | Fake envelope literal `{"connections": {"node": {"ports": {}, "kube_targets": {"k3s": {...}}}}}` | Retarget to `{"nodes": {"node": {"ports": {}, "kube": {"k3s": {"path": ..., "context": ..., "endpoint": ...}}}}}` — align field names with `UnifiedKubeRef`, dropping any field beyond `path`/`context`/`endpoint` the old literal carried. |
| `tests/e2e/test_terragrunt_apply.py:339,425` | `envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]` (apply and tunnelled-output cases) | Retarget both to `envelope["nodes"]["node"]["kube"]["k3s"]["path"]`. |
| `tests/e2e/test_rig.py:278` | `envelope["connections"]["node"]["kube_targets"]["k3s"]` | **Out of scope, stated explicitly, not silently skipped:** this reads `tunstrap start`'s **raw stdout JSON** (`OutputSchema.model_dump_json()`-shaped), not the `--output-var`/materialized unified channel. Scope here is `run`'s consumer-facing channels plus `start --output env`; `start`'s default/`--output json` stdout is a separate contract for session-management tooling and is deliberately untouched. If a reviewer wants it unified too, that is a new decision. |
| `tests/e2e/test_recipe_terragrunt.py:259,322` | Recipe↔module drift guard (textual block comparison) | **Unaffected in mechanism.** The compared *content* changes automatically once `main.tf` and the recipe both move to the `nodes.*` shape. Task 6 must keep it green (run it as part of Task 6, not just Task 7). |

#### `test_cli_run_output_var_projection.py` — dedicated retarget (security-critical)

This file pins the credential-scrubbing property for the projected kube
reference — it must not be weakened while being reshaped. All four tests
retarget or delete, **not** left alone:

- `test_output_var_never_carries_kube_private_key_material` — retarget the
  shape lookup:
  `json.loads(env["TF_VAR_tunstrap"])["nodes"]["node"]["kube"]["k3s"]`
  instead of `["connections"]["node"]["kube_targets"]["k3s"]`. The absence
  assertions (`client_key_data`/`client_certificate_data`/`content_b64` not in
  `target`) are unaffected in spirit, but the field set is now smaller for a
  second reason too — see the next test.
- `test_output_var_keeps_every_field_the_consumer_chain_reads` — the
  anti-vacuity pair. **The expected dict shrinks further than credential
  removal alone**: `UnifiedKubeRef` carries exactly `{path, context,
  endpoint}` — `cluster_name`, `local_port`, `tls_server_name`, and
  `certificate_authority_data` (all present in the old `RunKubeTarget`
  projection, none of them credentials) are **also** gone, because the design
  narrows to references only. Retarget the expected dict to exactly
  `{"path": KUBE_PATH, "context": "probe-context", "endpoint":
  "https://127.0.0.1:41111"}`. This is a real, intentional narrowing beyond
  the credential fix — call it out in the retargeted test's docstring so a
  future reader does not mistake it for scope creep.
- `test_output_var_projection_leaves_the_rest_of_the_envelope_intact` —
  retarget: `decoded["pid"]` → `decoded["session"]["pid"]`,
  `decoded["session_dir"]` → `decoded["session"]["session_dir"]`,
  `decoded["started_at"]` → `decoded["session"]["started_at"]`,
  `decoded["connections"]["node"]["ports"]` →
  `decoded["nodes"]["node"]["ports"]` — **and note the value shape changed
  too**: `{"db": 5432}` (int) becomes `{"db": "127.0.0.1:5432"}` (string).
- `test_projection_is_an_allow_list_so_a_new_secret_field_cannot_leak` —
  **delete, not retarget.** It validated `RunKubeTarget.model_validate`
  directly, exercising `extra="ignore"`'s fail-closed behaviour against an
  untrusted dict. `RunKubeTarget` is deleted (Task 4); its replacement,
  `render_unified_output`, never calls `.model_validate()` on untrusted kube
  data at all — it constructs `UnifiedKubeRef(path=..., context=...,
  endpoint=...)` with three explicit keyword arguments, so a hypothetical
  field added to `KubeTargetOutput` later cannot leak through without someone
  editing that constructor call by hand. The allow-list property now holds
  **by construction**, and
  `test_output_var_keeps_every_field_the_consumer_chain_reads`'s exact-equality
  assertion already proves it end-to-end. **Confirm by re-reading
  `render_unified_output`'s body before deleting** — the property must
  actually hold, not just be asserted to hold by this note.

#### Fetched-file materialization + the `content_b64` enumeration

**New mechanism, same precedent as kube.** `FetchedFile` (`schemas.py:292-313`)
gains `path: str | None = None`, mirroring `KubeTargetOutput.path`
(`schemas.py:317-336`) exactly. Wherever kube materialization currently runs
daemon/worker-side (the same call site the design doc's "Materialization write
mechanism" section points at — **confirm the exact function before
implementing; do not assume it is `manager.py:start_all_and_build_output`
without checking**), add a parallel step: for each successful `FetchedFile` a
node's `fetch_files` produced, base64-decode `content_b64` and write the raw
bytes to `tunnel-data/<node>-<fetchname>` using the **same atomic-replace
primitive** as `output.json` (temp file + `O_EXCL` + `os.replace`, not
`_write_file`'s `O_TRUNC`), then set `.path`. A failed fetch (`.error` set)
materializes nothing. `content_b64` itself is **not** deleted from
`FetchedFile` — it stays internal plumbing, same as kube's own `content_b64`.
The projection is not a separate function: it is `render_unified_output`'s
`fetch_files` construction via `UnifiedFetchRef` (Task 4); this step is what
makes `.path` non-`None` by the time that function runs.

**Kube-internal `content_b64` hits — unaffected, listed to prove they were
checked, not missed:** `KubeTargetOutput.content_b64` (`schemas.py:335`) is a
different field entirely (the patched kubeconfig's own content, unrelated to
`fetch_files`). Every hit here reads or constructs *that* field:
`test_kube_run.py:111`, `test_envrender.py:20`, `test_output_kube.py:35`,
`test_tofu_proxy.py:351`, `test_kube_targets.py:91,147` (integration — reads
`start`'s raw stdout JSON, already out of scope per the carve-out), and the
**absence** assertions for kube's own `content_b64` in
`test_cli_run_output_var.py:256,281` and
`test_cli_run_output_var_projection.py:10,72,91,190,249` (these already
correctly assert kube's `content_b64` is *not* in the projected shape —
nothing to change).

**`fetch_files`-related — in scope, retarget:**

| File:line | Old shape/behaviour | Disposition |
|---|---|---|
| `test_manager_fetch.py:91` (`test_fetch_files_results_populate_node_output`) | Docstring "Fetcher results land in `NodeOutput.fetch_files` unchanged"; fixture `FetchedFile(content_b64="YQ==", size=1, sha256="ca97")`, no `path` | **False once materialization runs.** Retarget: assert `out.connections["a"].fetch_files["kubeconfig"].path` is set to the expected `tunnel-data/a-kubeconfig` location and its on-disk bytes match `base64.b64decode("YQ==")`; `content_b64` still present on the object (internal plumbing) but the test's point moves to `path`. Rename to drop "unchanged" from the docstring. |
| `test_fetcher_unit.py:101,111` | `fetcher.fetch_files()`'s own unit test, asserts `ff.content_b64` set on success | **Unaffected** — the SSH-fetch-to-memory layer, upstream of the new daemon-side materialization step; `fetcher.py` itself is not changed, only its caller gains a step after it. |
| `test_fetch_files.py:67,119,216` (integration) | `base64.b64decode(ff["content_b64"])` reading the raw `start` stdout envelope | **Unaffected in mechanism** (raw stdout stays the "complete envelope") **but verify against the correct channel**: if any of these three actually asserts against `--output-var`/materialized output rather than raw `start` JSON, that assertion retargets to read `ff["path"]` + a direct file read. Confirm which channel each of the three exercises before deciding no change is needed. |
| `test_fetch_security.py:49,52,69,87-89` (integration) | Proves fetched `content_b64` "appears on stdout only, never on stderr" — i.e. accepts it riding *some* channel | **Retarget the property proved, not just the assertion syntax.** Rewrite to assert (a) `content_b64`/the raw fetched bytes appear **nowhere** in `TF_VAR_tunstrap`, the materialized `output.json`, stdout, or stderr; (b) the file at the reported `path` exists, is mode `0600`, and its bytes match the source. This is a **stronger** security property than before — call that out in the retargeted docstring. |
| `test_cli_run_output_var.py:83` (`_RICH_PAYLOAD`) | `"fetch_files": {"hosts": {"content_b64": "aG9zdHM=", "size": 6, "sha256": "ab" * 32}}` | Retarget the fixture to `{"hosts": {"path": "/s/tunnel-data/node-hosts", "size": 6, "sha256": "ab" * 32}}`; any downstream assertion reading `fetch_files.hosts.content_b64` from the decoded var retargets to `.path`. |
| `test_output_schema.py:25,32,46,63` | `FetchedFile(content_b64=...)` construction, xor-validation tests | **Unaffected** — they test `FetchedFile`'s own `content_b64`/`error` xor, unchanged; only a new optional `path` field is added. Add one new case: `path` defaults to `None`, is not part of the xor, and can be set independently after construction (mirror `KubeTargetOutput.path`'s own coverage if a precedent test exists, rather than inventing a new assertion style). |

**`docs/` tier:**

| File:line | Old shape/behaviour | Disposition |
|---|---|---|
| `docs/recipe_terragrunt.md:344` | Kube-drop list: "and **drops** `client_key_data`... `content_b64`... `client_certificate_data`" | **Verify only, no rewrite.** States kube's `content_b64` is dropped from `TF_VAR_tunstrap`'s kube projection — still true. |
| `docs/recipe_terragrunt.md:361-364` | "`tunstrap start` is not affected: it writes the complete envelope to stdout... without `--materialize` its `content_b64` is the only way to obtain the kubeconfig at all." | **Verify only, no rewrite.** Matches the unchanged scope carve-out: `start`'s raw default JSON stdout is untouched, for kube and `fetch_files` alike. |
| `docs/recipe_terragrunt.md:366-388` (whole subsection, "### Fetched files are exported verbatim, not projected") | Argues the **opposite** of the shipped behaviour: "Every `fetch_files` entry keeps its `content_b64` whole"; "`FetchedFile` has no `path` (`schemas.py:292`), so dropping `content_b64` would be a silent, unrecoverable breakage"; "tunstrap fetches into the envelope (`content_b64`), not onto disk"; and a false premise that the materialize-then-drop end-state "is recorded in the spec's Out of scope" | **REWRITE — the whole subsection.** Replacement text in Task 6 Step 0b. |
| `tests/e2e/module/main.tf:13` | Comment: "the kube target's `client_key_data`, `client_certificate_data` and `content_b64` are dropped" | **Unaffected** (kube-only field) — already inside the region Task 6 Step 0 edits for the unrelated `connections.*`→`nodes.*` rename. |
| `docs/artifacts/charharness_start.py:33,42` | Fetch/kube fixtures using `content_b64` | **Out of scope, stated explicitly.** Its own docstring: "Not part of the test suite; lives in the gitignored artifacts dir." A local characterization harness, not shipped code, a test, or consumer documentation. |
| `docs/specs/2026-05-20-feature-fetch-files-design.md`, `docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md`, `docs/specs/2026-08-03-run-env-io-decision-history.md`, `docs/specs/2026-05-30-kube-targets-design.md`, `docs/superpowers/plans/2026-06-25-cli-run-modes.md`, `docs/superpowers/plans/2026-05-30-kube-targets.md` | Pre-#15 design/decision/plan documents for already-shipped tickets (#14 and earlier) | **Out of scope, historical record — cited, never edited.** Editing a completed ticket's own spec to match a later ticket's decision would falsify the record of what that ticket actually shipped. |
| `docs/artifacts/superseded/2026-07-30-owner-tracking-and-consumer-ergonomics-design.md` | — | **Out of scope** — already in a `superseded/` directory; self-evidently not live. |
| `docs/artifacts/2026-08-07-issue15-spike-findings.md:109,152,318,319` | Kube-only `content_b64` hits (patched-kubeconfig content in the collision-test prototype) | **Unaffected** (kube, not `fetch_files`) and a frozen historical spike snapshot. |

**Schema note:** `FetchedFile`'s xor validator (`schemas.py:303-314`) needs no
new logic for `path` — it is a plain optional field set post-construction by
the materialization step, the same relationship `KubeTargetOutput.path`
already has to that model's own required fields. Confirm against the actual
`KubeTargetOutput` definition before implementing, not assumed from this note.

**`predicted_env_keys`/anti-drift guard: checked, unaffected by the
fetched-file change.** `TUNSTRAP_OUTPUT_FILE` is one of the three
unconditional survivors `_build_child_env` injects and `predicted_env_keys`
reserves for **`run`**, not only for `start --output env`. `fetch_files`'s own
keys never touched env at all, so the guard's key set is untouched by the
materialization change — verified, not silently assumed.

#### Anti-drift guard — retargeted, not deleted, and two-part

**The guard is extended, never weakened.** After this task there are still
**two independent implementations** of "what keys will `run` inject":
`_build_child_env` (hardcodes `TUNSTRAP_SESSION_DIR`/`TUNSTRAP_PID`/
`TUNSTRAP_OUTPUT_FILE`, merges `render_kube_env`'s output) and
`predicted_env_keys` (Task 3's conservative formula). If these two silently
diverge, the pre-spawn `--output-var` collision check (`_validate_output_var`,
`cli.py:311-324` — confirm the exact line against the checked-out file)
under-rejects: a NAME that collides with a key `_build_child_env` actually
injects would sail through validation and then genuinely collide post-spawn.

The guard is **two independent tests**, because `predicted_env_keys` is
deliberately conservative rather than exact, so exact equality against the
actual export cannot hold in general (a schema with one kube target that
materializes cleanly predicts all three kube names while the actual export has
only two — correctly unequal):

1. **Formula test** (exact equality, unit-test style — proves the conservative
   formula itself is implemented correctly): the
   `test_predicted_env_keys_reserves_all_three_for_one_kube_target` /
   `..._two_kube_targets_one_node` pair already written in Task 3.
2. **Safety-envelope test** (subset — proves the conservative reservation
   still covers whatever *actually* gets injected, even when cardinality
   shrinks between input and output). A subset check here is *correct*, not a
   weakening, precisely because it is paired with (1):

```python
def test_predicted_env_keys_covers_actual_injected_keys_under_cardinality_shrink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety-envelope half of the two-part anti-drift guard: predicted must be
    a superset of actual, driven by the exact scenario that falsifies a
    predictor that got the conservatism backwards -- two kube targets
    DECLARED (one on an optional node that fails), only ONE materializes. A
    NAME colliding with a key _build_child_env actually injects, but which
    predicted_env_keys failed to reserve, would sail through the pre-spawn
    collision check and then genuinely collide post-spawn."""
    from tunstrap import cli as cli_mod
    from tunstrap.cli import _build_child_env

    # _build_child_env starts from dict(os.environ) (cli.py:394), so without
    # isolating it first, `set(actual)` is the whole ambient environment
    # (PATH, HOME, ...) and any comparison against it is meaningless in any
    # real process. Isolate BEFORE calling it, not after: subtracting
    # os.environ back out (`set(actual) - set(os.environ)`) is NOT an
    # acceptable substitute -- a key that is both inherited AND injected (an
    # operator-set KUBECONFIG, or a NAME matching --output-var) would be
    # subtracted away too, silently under-checking exactly the collision
    # this guard exists to catch.
    monkeypatch.setattr(cli_mod.os, "environ", {})

    # Input: two kube targets declared, on two nodes -- one optional and about
    # to fail. predicted_env_keys sees only this schema.
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": {
                    "host": "h1", "user": "u", "ssh_password": "p",
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                },
                "b": {
                    "host": "h2", "user": "u", "ssh_password": "p", "required": False,
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                },
            }
        }
    )
    # Output: node "b" failed (required: false), only node "a"'s kube target
    # actually materialized -- output cardinality (1) SHRANK below input
    # cardinality (2). This is the real _build_child_env sees post-spawn.
    out = OutputSchema(
        connections={
            "a": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, "/run/s/tunnel-data/k3s")}),
        },
        pid=1, session_dir="/run/s", started_at="now",
        warnings=[TunnelWarning(node="b", error="optional node refused the forward")],
    )
    actual = _build_child_env(out, output_var=None, input_env=None)
    # Subset, not equality: predicted (conservative, computed from input
    # cardinality 2) legitimately claims MORE than actual (exact, computed
    # from output cardinality 1) -- that asymmetry is the whole point.
    assert set(actual) <= predicted_env_keys(schema)
    # Anti-vacuity: KUBE_CONFIG_PATHS specifically must be in the prediction
    # even though it is NOT in the actual export (the >=2 branch never fires
    # here) -- this is the exact key an exact-cardinality predictor would
    # have wrongly omitted.
    assert "KUBE_CONFIG_PATHS" in predicted_env_keys(schema)
    assert "KUBE_CONFIG_PATHS" not in actual
```

Add it in `tests/unit/test_envrender.py` (not a new file), here in Task 5,
alongside `_build_child_env`'s own implementation — it is the piece that needs
both sides to exist simultaneously.

**Files:**
- Modify: `tunstrap/cli.py`, `tunstrap/envrender.py` (delete `render_env`),
  `tunstrap/exceptions.py` (delete `MultiNodeEnvUnsupported`),
  `tunstrap/schemas.py` (`FetchedFile.path`), the daemon/worker
  materialization site, optionally `tunstrap/session.py`
- Test (unit): `tests/unit/test_cli_run_output_var.py`,
  `test_cli_run_output_var_projection.py`, `test_cli_run_materialize.py`
  (new), `test_cli_run.py`, `test_cli_run_input_env_scrub.py`,
  `test_cli_runner.py`, `test_cli_run_postspawn.py`, `test_envrender.py`,
  `test_exceptions.py`, `test_tofu_proxy.py` (docstrings only),
  `test_manager_fetch.py`, `test_output_schema.py`
- Test (integration): `tests/integration/test_run_env_io.py`,
  `test_cli_modes.py`, `test_fetch_files.py` (verify channel),
  `test_fetch_security.py`
- Test/artifact (e2e): enumerated above, edited in Task 6

- [ ] **Step 1: Write failing tests**

New file `tests/unit/test_cli_run_materialize.py`:

```python
"""run's unified-output materialization: <session_dir>/tunnel-data/output.json.

Validates: run always writes the unified structure to a deterministic path,
mode 0600, regardless of --output-var or node count; the file's content
equals render_unified_output's output for the same OutputSchema.
Code: tunstrap/cli.py (materialization call site)
Method: CliRunner + spawn_daemon/Popen/_teardown_run monkeypatched, as in
test_cli_run_output_var.py; read the file back after invoke().
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.unit.conftest import cleaning_teardown
from tunstrap import cli as cli_mod
from tunstrap.cli import main
from tunstrap.envrender import render_unified_output
from tunstrap.schemas import OutputSchema

pytestmark = pytest.mark.unit


def test_run_materializes_output_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-node run writes tunnel-data/output.json, mode 0600, matching content."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    payload = {
        "connections": {"h": {"ports": {"db": 5432}, "fetch_files": {}, "kube_targets": {}}},
        "pid": 99, "session_dir": str(session_dir), "started_at": "2026-08-07T00:00:00Z",
    }
    monkeypatch.setattr(
        cli_mod, "spawn_daemon",
        lambda schema, session_dir=None, *, input_env=None: {"kind": "success", "payload": payload},
    )
    monkeypatch.setattr(cli_mod.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(cli_mod, "_teardown_run", cleaning_teardown)
    monkeypatch.setenv("TUNSTRAP_INPUT", json.dumps({"nodes": {"node": {
        "host": "h", "user": "u", "ssh_password": "p", "remote_targets": {"db": "127.0.0.1:5432"},
    }}}))
    result = CliRunner().invoke(
        main, ["run", "--input-env", "TUNSTRAP_INPUT", "--", "true"]
    )
    assert result.exit_code == 0, result.stderr
    materialized = session_dir / "tunnel-data" / "output.json"
    assert materialized.exists()
    assert stat.S_IMODE(materialized.stat().st_mode) == 0o600
    out = OutputSchema.model_validate(payload)
    assert json.loads(materialized.read_text()) == render_unified_output(out)
    assert _FakePopen.last_env is not None
    assert _FakePopen.last_env["TUNSTRAP_OUTPUT_FILE"] == str(materialized)


class _FakePopen:
    last_env: dict[str, str] | None = None
    returncode = 0

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        _FakePopen.last_env = env

    def wait(self) -> int:
        return 0

    def send_signal(self, _signum: int) -> None:
        pass
```

Add to `tests/unit/test_cli_run_output_var.py`:

```python
def test_multi_node_run_succeeds_without_output_var(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """Multi-node input with NO --output-var succeeds: materialization covers
    multi-node unconditionally, so the opt-in gate has nothing left to force."""
    survivor_a = {"ports": {}, "fetch_files": {}, "kube_targets": {"k3s": _RICH_KUBE}}
    other_kube = dict(_RICH_KUBE, path="/s/tunnel-data/node-b-k3s")
    survivor_b = {"ports": {}, "fetch_files": {}, "kube_targets": {"k3s": other_kube}}
    spawn[0](
        {
            "kind": "success",
            "payload": {
                "connections": {"a": survivor_a, "b": survivor_b},
                "pid": 99, "session_dir": "/s", "started_at": "2026-08-07T00:00:00Z",
            },
        }
    )
    monkeypatch.setenv(VAR, _payload({"a": _node(), "b": _node()}))
    result = CliRunner().invoke(main, ["run", "--input-env", VAR, "--", "true"])
    assert result.exit_code == 0, result.stderr
    assert FakePopen.last_env is not None
    joined = "/s/tunnel-data/node-k3s:/s/tunnel-data/node-b-k3s"
    assert FakePopen.last_env["KUBECONFIG"] == joined
    assert FakePopen.last_env["KUBE_CONFIG_PATHS"] == joined
    assert "KUBE_CONFIG_PATH" not in FakePopen.last_env
    assert FakePopen.last_env["TUNSTRAP_SESSION_DIR"] == "/s"
    assert FakePopen.last_env["TUNSTRAP_PID"] == "99"
    assert FakePopen.last_env["TUNSTRAP_OUTPUT_FILE"] == "/s/tunnel-data/output.json"


def test_suppress_kubeconfig_drops_all_three_kube_env_names(
    monkeypatch: pytest.MonkeyPatch, spawn: list[Any]
) -> None:
    """suppress_kubeconfig (the tunstrap_tofu proxy's guard) must drop
    KUBE_CONFIG_PATH/_PATHS too, not just KUBECONFIG -- those are the names
    the providers actually read (ADR entry 7)."""
    from tunstrap.cli import _build_child_env
    from tunstrap.schemas import OutputSchema

    out = OutputSchema.model_validate(
        {
            "connections": {"h": {"ports": {}, "kube_targets": {"k3s": _RICH_KUBE}}},
            "pid": 1, "session_dir": "/s", "started_at": "now",
        }
    )
    env = _build_child_env(out, output_var=None, input_env=None, suppress_kubeconfig=True)
    assert "KUBECONFIG" not in env
    assert "KUBE_CONFIG_PATH" not in env
    assert "KUBE_CONFIG_PATHS" not in env
```

Also add the safety-envelope anti-drift test given in full above, to
`tests/unit/test_envrender.py`.

- [ ] **Step 2: Retarget every pre-existing test pinning the removed machinery**

**`tests/unit/test_cli_run_output_var.py`** — this file's whole premise (the
scalar/`--output-var` interaction) partly no longer exists:

- `test_collision_with_injected_scalar_is_usage_error` — pins `--output-var
  TUNSTRAP_DB_PORT` colliding with an injected scalar. `TUNSTRAP_DB_PORT` is
  never injected now, so the collision cannot occur. **Delete this test** —
  there is no equivalent behaviour to assert.
- `test_non_colliding_tunstrap_prefixed_name_is_accepted` — rename to
  `test_tunstrap_prefixed_output_var_name_is_accepted` and drop the "only some
  are protected" framing from the docstring; its only remaining job is
  confirming `--output-var TUNSTRAP_ANYTHING` is not rejected just for the
  prefix.
- `test_multi_node_without_output_var_is_exit_1_pre_spawn` — **delete**;
  superseded by Step 1's `test_multi_node_run_succeeds_without_output_var`,
  not retargetable (the assertion is the literal opposite).
- `test_multi_node_with_output_var_reaches_spawn` — still valid in spirit;
  update the docstring only (it describes removed `render_env` behaviour). The
  assertions are unaffected — it never inspects env content.
- `test_output_var_carries_the_whole_envelope_minus_kube_credentials` —
  **retarget in place**: rename to
  `test_output_var_carries_the_unified_structure_minus_kube_credentials`,
  replace the expected-shape assertions with the unified shape
  (`nodes.node.ports.db == "127.0.0.1:5432"`, `nodes.node.kube.k3s == {"path":
  ..., "context": ..., "endpoint": ...}`,
  `nodes.node.fetch_files.hosts.sha256 == ...`), and keep the
  credential-absence assertions (`client_certificate_data`/`client_key_data`/
  `content_b64` must still not appear anywhere in the decoded payload) — that
  property is unchanged, only the container shape is.
- `test_single_node_keeps_scalars_alongside_output_var` — **delete**; no
  scalars survive to keep "alongside" anything except the three survivors,
  already covered by `test_child_env_without_output_var_is_unchanged`.
- `test_multi_node_injects_output_var_and_no_scalars` — retarget: update the
  body to decode `render_unified_output`'s shape (`nodes` keyed by `"a"`/`"b"`)
  instead of the old `OutputSchema.connections` shape; keep the `leaked`
  scalar-absence assertion (still a real guard against a regression that
  reintroduces target-scoped scalars).
- `test_multi_node_suppresses_scalars_but_exports_kube_channel` — retarget:
  its `leaked = [...TUNSTRAP_...]; assert leaked == []` assertion no longer
  describes a real guard (the kube channel is unconditional by construction,
  so there is nothing left to falsify). Rename to
  `test_optional_node_failure_does_not_affect_kube_channel_or_unified_output`
  and rewrite the body to assert: the kube channel still fires for the one
  surviving connection (`KUBECONFIG`/`KUBE_CONFIG_PATH` present), and the
  unified structure (if `--output-var` given) reflects only the surviving node
  (`"b"` absent from `nodes`, its failure visible in `session.warnings`). Drop
  the `leaked` assertion entirely — asserting the absence of something nothing
  produces is a tautology, not a guard.
- `test_child_env_without_output_var_is_unchanged` — retarget: the expected
  `injected` dict shrinks to exactly `{"TUNSTRAP_SESSION_DIR": "/s",
  "TUNSTRAP_PID": "99", "TUNSTRAP_OUTPUT_FILE": "/s/tunnel-data/output.json"}`
  (drop `TUNSTRAP_DB_HOST`/`_PORT`/`_ENDPOINT`; this fixture's node has no
  kube_targets, so no kube keys either). Docstring: "the three survivors,
  session metadata only." The existing `injected` filter
  (`k.startswith(("TUNSTRAP_", "KUBECONFIG"))`) is already broad enough to
  catch `TUNSTRAP_OUTPUT_FILE` — only the expected dict changes.

**`tests/unit/test_envrender.py`** — delete every `render_env`-specific test
**by name**: `test_render_ports_and_session`,
`test_render_kube_sets_kubeconfig`, `test_render_kube_not_materialized_raises`,
`test_render_requires_single_node_zero`,
`test_render_requires_single_node_two`. Also delete the now-unused
module-level `from tunstrap.exceptions import MultiNodeEnvUnsupported`
(`test_envrender.py:4` — `ruff` F401), plus
`test_predicted_env_keys_no_kube_omits_kubeconfig` and
`test_predicted_env_keys_multi_node_is_empty`.

**Do NOT delete `test_predicted_env_keys_matches_render_env`** — retarget it in
place to `test_predicted_env_keys_covers_actual_injected_keys_under_
cardinality_shrink` (the safety-envelope half, code given in full above). Two
independent implementations of the injected-key set still exist; the guard
still has a job.

Replace the two deleted "shape" tests with:

```python
def test_predicted_env_keys_is_session_scalars_plus_kube_channel() -> None:
    """predicted_env_keys collapses to the three survivors + the CONSERVATIVE
    kube channel (all three names, not just the branch this input's exact
    declared cardinality would hit) -- there is no other injected key left,
    and the formula does not vary by exact count."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": {
                    "host": "h", "user": "u", "ssh_password": "p",
                    "kube_targets": {"k3s": {"kubeconfig_path": "/etc/k3s.yaml"}},
                },
                "b": {
                    "host": "h2", "user": "u", "ssh_password": "p",
                    "kube_targets": {"k4s": {"kubeconfig_path": "/etc/k4s.yaml"}},
                },
            }
        }
    )
    assert predicted_env_keys(schema) == {
        "TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE",
        "KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS",
    }


def test_predicted_env_keys_no_kube_is_just_the_three_survivors() -> None:
    schema = InputSchema.model_validate(
        {"nodes": {"a": {"host": "h", "user": "u", "ssh_password": "p",
                          "remote_targets": {"db": "127.0.0.1:1"}}}}
    )
    assert predicted_env_keys(schema) == {
        "TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE",
    }
```

`format_exports`'s own test (`test_format_exports_quotes_safely`) is
unaffected — it takes a plain `dict[str, str]`, not an `OutputSchema`.

**`tests/unit/test_exceptions.py`**: delete **all three**
`MultiNodeEnvUnsupported` cases by name: the subclass check (`:87-90`), the
exit-code + envelope test (`:94-99`), and the `_EXIT_CODES` table test
(`:107-114`).

**`tests/unit/test_tofu_proxy.py`**: the two `suppress_kubeconfig`-related
tests (`test_tunnelled_suppresses_kubeconfig_in_child_env`,
`test_tunnelled_drops_an_inherited_kubeconfig_in_the_multi_node_case`) keep
their assertions unchanged (`KUBECONFIG` still must not leak) but their
docstrings currently frame the pop in single-node-vs-multi-node /
`inject_scalars` terms — update both to drop that framing entirely; one
unconditional pop covers every case.

**`tests/unit/test_manager_fetch.py`, `tests/unit/test_output_schema.py`**:
per the fetched-file table above.

- [ ] **Step 3: Run to verify failure**

`.venv/bin/pytest tests/unit/test_cli_run_output_var.py tests/unit/test_cli_run_output_var_projection.py tests/unit/test_cli_run_materialize.py tests/unit/test_cli_run.py tests/unit/test_cli_run_input_env_scrub.py tests/unit/test_cli_runner.py tests/unit/test_cli_run_postspawn.py tests/unit/test_envrender.py tests/unit/test_exceptions.py -v`

Expected: FAIL across the board — `render_env`/`MultiNodeEnvUnsupported`/
`inject_scalars` still exist and behave the old way; `_build_child_env` still
requires `inject_scalars` and injects only two survivors, not three; no
materialization call site exists yet; `start --output env` still emits
per-target scalars.

- [ ] **Step 4: Implement**

`tunstrap/exceptions.py`: delete the `MultiNodeEnvUnsupported` class and its
`_EXIT_CODES` entry.

`tunstrap/envrender.py`: delete `render_env` in its entirety, and the
now-unused `from tunstrap.exceptions import MultiNodeEnvUnsupported` import
(`envrender.py:13`). Rewrite `predicted_env_keys`:

```python
def predicted_env_keys(schema: InputSchema) -> set[str]:
    """Env keys ``run`` will inject for this *input* schema, unconditional on
    node count: the three session scalars, plus -- conservatively, not per the
    exact _kube_channel_keys(count) branch -- all three kube names whenever
    any node declares kube_targets at all. Input cardinality can shrink by
    output time (an optional node/target can fail without failing the run), so
    predicting the exact branch would under-reserve; see the "Anti-drift
    guard" section for the cardinality-shrink case this guards against. Used
    pre-spawn to reject a colliding --output-var NAME before a daemon exists.
    """
    keys = {"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE"}
    if any(node.kube_targets for node in schema.nodes.values()):
        keys |= {"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"}
    return keys
```

This keeps Task 3's conservative kube rule verbatim and only drops the scalar
half's `if len(schema.nodes) == 1:` per-target block. **Do not reintroduce
`_kube_channel_keys(total_kube)` (the exact per-count branch) here** — that
would silently make the predictor exact again and reopen the under-reservation
hole.

`tunstrap/schemas.py`: add `path: str | None = None` to `FetchedFile`.

`tunstrap/session.py`: confirm `SessionDir._write_file`'s exact signature
(`session.py:132`) before item 4 below. **It is not a drop-in reuse** —
`_write_file` is mode-fixed-at-creation but not atomic (`O_TRUNC`, no rename
step), while materialization needs true atomicity too.

**Daemon/worker side:** add the fetched-file materialization step described in
"Fetched-file materialization" above, at the same site kube materialization
already runs (confirm the function by reading the code).

`tunstrap/cli.py`:

1. **Remove the `inject_scalars` parameter from all four places that thread
   it:**
   - `_build_child_env` (`cli.py:365-372`, parameter declaration) — remove the
     parameter and its `if inject_scalars:` branch (`cli.py:399`).
   - `_run_child` (`cli.py:466-474`, parameter; `cli.py:486`, passed through
     to `_build_child_env`).
   - `_supervise_child` (`cli.py:513-521`, parameter; `cli.py:543`, passed
     through to `_run_child`).
   - `run_command` (`cli.py:648`, `inject_scalars = len(schema.nodes) == 1` —
     delete the line entirely; `cli.py:702`, the keyword argument passed to
     `_supervise_child` — delete it from the call).

   Confirm all four sites against the checked-out file rather than trusting
   these line numbers verbatim — they may have shifted once Tasks 1-4 land.
2. Remove the `cli.py:640` pre-spawn block:
   ```python
   if output_var is None and len(schema.nodes) != 1:
       raise MultiNodeEnvUnsupported(...)
   ```
   entirely — multi-node without `--output-var` is no longer rejected.
3. Rewrite `_build_child_env`:

```python
def _build_child_env(
    output: OutputSchema,
    *,
    output_var: str | None,
    input_env: str | None,
    suppress_kubeconfig: bool = False,
) -> dict[str, str]:
    child_env = dict(os.environ)
    if input_env is not None:
        child_env.pop(input_env, None)
    child_env["TUNSTRAP_SESSION_DIR"] = output.session_dir
    child_env["TUNSTRAP_PID"] = str(output.pid)
    child_env["TUNSTRAP_OUTPUT_FILE"] = _materialized_output_path(output.session_dir)
    child_env.update(render_kube_env(output))
    if suppress_kubeconfig:
        for key in ("KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"):
            child_env.pop(key, None)
    if output_var is not None:
        child_env[output_var] = render_output_var(output)
    return child_env


def _materialized_output_path(session_dir: str) -> str:
    """The deterministic path the materialization writer writes to; shared so
    _build_child_env's TUNSTRAP_OUTPUT_FILE and the actual writer never
    independently compute a different path for the same file."""
    return str(Path(session_dir) / "tunnel-data" / "output.json")
```

   No branch, no `inject_scalars` parameter anywhere in the call chain.
4. **Materialization writer — a true atomic replace, not write-then-chmod and
   not `O_TRUNC` alone.** `SessionDir._write_file`'s real property is
   **mode-fixed-at-creation** (`session.py:132`, `os.open(path, O_CREAT |
   O_WRONLY | O_TRUNC, 0o600)`, no separate `chmod`) — **not** atomic:
   `O_TRUNC` overwrites in place, visible mid-write. `Path.write_text()` +
   `.chmod(0o600)` is worse still (a real, umask-dependent `0644` window).
   This write needs *both* mode-fixed-at-creation *and* true atomicity, for
   three reasons: (1) **torn-read prevention** if the process is killed
   mid-write — a truncated file at the final path is indistinguishable from a
   valid short one to a naive reader, while `os.replace` guarantees only a
   complete old or complete new file is ever observable; (2)
   **defense-in-depth** against any future change that reintroduces a
   stable/reusable path; (3) the fetched-file writer shares this exact
   primitive, so one atomic-replace primitive is reasoned about once, not
   twice. Use a temp file + rename:

```python
    materialized_path = _materialized_output_path(output.session_dir)
    tunnel_data_dir = Path(materialized_path).parent
    tunnel_data_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tunnel_data_dir / f".output.json.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    try:
        os.write(fd, render_output_var(output).encode())
    finally:
        os.close(fd)
    os.replace(tmp_path, materialized_path)
```

   `O_EXCL` on the temp file guards against a colliding temp name (the mode is
   already fixed at creation); `os.replace` is the atomic step. If
   `SessionDir._write_file` can be refactored into something callable without
   a live `SessionDir` instance (this writer runs in the CLI **parent**
   process, `run_command`, which holds no `SessionDir` — kube materialization
   happens daemon/worker-side, inside the process that does own one), factor
   the primitive above into a small shared helper in `session.py` that both
   call sites use; otherwise replicate it in `cli.py` as shown. **Do not
   describe this as "reusing `_write_file`" if the code is not actually
   shared** — the temp-file + `os.replace` step is new work `_write_file` does
   not do.

   Place the call in `run_command`'s success path — the same place
   `_build_child_env` is already called, **inside the `try` that owns
   teardown** (the "cleanup must own the whole post-spawn window" invariant
   from `docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md` applies:
   this write is new work in that protected window) — unconditionally,
   regardless of `--output-var` and regardless of node count. **Confirm the
   exact `run_command` call site by reading the checked-out `cli.py`**; line
   citations for this function have drifted before, so re-resolve rather than
   trusting one.
5. **`start_command`'s `--output env` mode** (`cli.py:204-206`,
   `sys.stdout.write(format_exports(render_env(out)))`) is the **other**
   caller of `render_env` — deleting the function without touching this call
   site breaks `start` outright (`NameError`), not just a stale test. `start`
   also now materializes under `--output env` (only): it already forces
   `daemon.materialize` there via `cli.py:191`'s
   `force_materialize=(output_fmt == "env")`, so the kube files already land
   on disk; extend that to write `output.json` through the same
   `_materialized_output_path`/atomic-write helper from item 4. Update the
   branch to build the same three-survivors-plus-kube-channel mapping
   `_build_child_env` uses:
   ```python
   if kind == "success" and output_fmt == "env":
       out = OutputSchema.model_validate(message["payload"])
       _write_materialized_output(out)  # same helper as item 4
       env = {
           "TUNSTRAP_SESSION_DIR": out.session_dir,
           "TUNSTRAP_PID": str(out.pid),
           "TUNSTRAP_OUTPUT_FILE": _materialized_output_path(out.session_dir),
       }
       env.update(render_kube_env(out))
       sys.stdout.write(format_exports(env))
   ```
   Fix the existing test pinning this mode (`test_cli_runner.py:392`) in place
   — do not add a new test alongside a stale one.

   **Stdin-mode guard — a real reachable failure, not a theoretical one.**
   `--output env` forces `daemon.materialize = True` only for **flag mode**
   (`build_flag_schema`'s `force_materialize=(output_fmt == "env")`,
   `cli.py:191`); a **stdin**-supplied payload's own `daemon.materialize` is
   the caller's explicit statement and `_pick_start_input_schema` leaves it
   alone (`cli.py:160-174`). A stdin payload declaring `kube_targets` with
   `materialize: false` under `--output env` therefore reaches the now
   unconditional `render_kube_env(out)` call with `target.path is None`, which
   raises a bare `ValueError` — an ugly traceback, not a typed error. **Fix
   before wiring the unconditional call:** **choose (a)** — force
   `daemon.materialize = True` for the stdin path too when `output_fmt ==
   "env"`, matching flag mode's own precedent (smallest change, consistent:
   `--output env` needs materialized kube paths regardless of input channel).
   The alternative, (b) catching `ValueError` around the `render_kube_env`
   call and re-raising as a typed `TunstrapError` subclass, is acceptable only
   if a reviewer specifically wants materialization to stay an operator
   opt-out even under `--output env`. Add a unit test: stdin payload,
   `daemon.materialize: false`, `kube_targets` declared, `--output env` →
   exit 0 with the kube path materialized (option (a)), or a typed error
   (option (b)) — never a bare `ValueError` traceback.

- [ ] **Step 5: Run to verify pass, then the full unit suite**

`.venv/bin/pytest tests/unit/test_cli_run_output_var.py tests/unit/test_cli_run_output_var_projection.py tests/unit/test_cli_run_materialize.py tests/unit/test_cli_run.py tests/unit/test_cli_run_input_env_scrub.py tests/unit/test_cli_runner.py tests/unit/test_cli_run_postspawn.py tests/unit/test_envrender.py tests/unit/test_exceptions.py tests/unit/test_tofu_proxy.py tests/unit/test_manager_fetch.py tests/unit/test_output_schema.py -v`

Expected: **all pass, and only after every row of the blast-radius tables has
actually been applied.** A partial pass with a handful of still-red tests
means a table row was skipped, not that the row was optional.

`.venv/bin/pytest tests/unit -q`

Expected: full pass. Read the actual count; do not compare it against any
number recorded in this plan or the spike findings — both predate this task's
deletions and retargets.

- [ ] **Step 6: Commit**

```bash
git add tunstrap/cli.py tunstrap/envrender.py tunstrap/exceptions.py tunstrap/schemas.py \
  tunstrap/session.py \
  tests/unit/test_cli_run_output_var.py tests/unit/test_cli_run_output_var_projection.py \
  tests/unit/test_cli_run_materialize.py tests/unit/test_cli_run.py \
  tests/unit/test_cli_run_input_env_scrub.py tests/unit/test_cli_runner.py \
  tests/unit/test_cli_run_postspawn.py tests/unit/test_envrender.py \
  tests/unit/test_exceptions.py tests/unit/test_tofu_proxy.py \
  tests/unit/test_manager_fetch.py tests/unit/test_output_schema.py
git commit -m "feat: unified output materialization; remove TUNSTRAP_* scalars, MultiNodeEnvUnsupported, inject_scalars (#15)"
```

**Note:** include `tunstrap/session.py` only if Step 4 item 4 added a shared
atomic-write helper there; also `git add` the daemon/worker module that gained
the fetched-file materialization step.

- [ ] **Step 7: Integration retargets**

The blast-radius table's integration rows are their own step, not folded into
Task 7's gate pass — they are behavioural retargets (TDD-shaped: they can fail
against the old code and must pass against the new), not verification only.

**Files:** `tests/integration/test_run_env_io.py`,
`tests/integration/test_cli_modes.py`, plus the `test_fetch_files.py` /
`test_fetch_security.py` dispositions from the fetched-file table.

Apply every integration row: `_PROBE_SINGLE`/`_PROBE_MULTI` read
`TUNSTRAP_OUTPUT_FILE` instead of `TUNSTRAP_WEB_PORT`; the "no scalar leak"
check excludes the three sanctioned survivors;
`test_multi_node_without_output_var_is_exit_1` is renamed and inverted to
`test_multi_node_without_output_var_now_succeeds`; `test_cli_modes.py`'s
`start --output env` test and `run`'s child probe both retarget to
`TUNSTRAP_OUTPUT_FILE`. Run (requires Docker, per `tests/README.md`):

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/integration -m integration -q -k "run_env_io or cli_modes or fetch"
```

Expected: FAIL before the retargets (old assertions against new behaviour),
PASS after. Commit:

```bash
git add tests/integration/test_run_env_io.py tests/integration/test_cli_modes.py \
  tests/integration/test_fetch_files.py tests/integration/test_fetch_security.py
git commit -m "test(integration): retarget env-shape assertions for the unified output contract (#15)"
```

---

### Task 6: Recipe documentation + e2e artifact shape migration

**This task carries the e2e-tier and shipped-recipe rows of Task 5's
blast-radius tables** — they land here, not in Task 5, because they are the
same textual shape migration as the new recipe content, and
`test_recipe_terragrunt.py`'s drift guard requires the recipe and
`tests/e2e/module/main.tf` to move together or it fails by design.

**Files:**
- Modify: `docs/recipe_terragrunt.md`, `tests/e2e/module/main.tf`,
  `tests/e2e/rig.py`, `tests/e2e/test_tofu_providers.py`,
  `tests/e2e/test_terragrunt_apply.py`

- [ ] **Step 0: Fix the recipe's pre-existing `connections.*` shape (before adding new content)**

The recipe already contains working HCL/prose in the old shape. Fix these **in
place** before Steps 1-2 add anything new, so the document is never left in a
self-contradictory state (old shape in one section, new shape in another):

- `docs/recipe_terragrunt.md:287-288` — the `tunnel`/`kubepath` locals:
  `try(jsondecode(var.tunstrap), { connections = {} })` →
  `try(jsondecode(var.tunstrap), { nodes = {} })`;
  `local.tunnel.connections.node.kube_targets.k3s.path` →
  `local.tunnel.nodes.node.kube.k3s.path`.
- `docs/recipe_terragrunt.md:~329` — prose point 3, "`path` comes from the
  materialized file... `connections.*.kube_targets.*.path`" → `nodes.*.kube.*.path`.
- `docs/recipe_terragrunt.md:~407` — "The input variable is scrubbed" section:
  "the module picks the node out of `connections[<node>]`" → `nodes[<node>]`;
  also correct the surrounding paragraph's claim that multi-node input
  suppresses the scalar/`KUBECONFIG` channel entirely — the kube channel is
  unconditional now; only the `TUNSTRAP_<TARGET>_*` scalars, which no longer
  exist as a concept, were ever suppressed for multi-node.
- `docs/recipe_terragrunt.md:~509` — "What is proven" section: **verify only**,
  no shape-specific text to change (the `--output-var` → `TF_VAR_tunstrap` →
  `jsondecode` → `config_path` chain description stays accurate once the two
  locals above change).

**`tests/e2e/module/main.tf:27-28`** — the exact chain the e2e tier proves,
mirroring the recipe: `try(jsondecode(var.tunstrap), { connections = {} })` →
`{ nodes = {} }`; `local.tunnel.connections.node.kube_targets.k3s.path` →
`local.tunnel.nodes.node.kube.k3s.path`. Update the module's own header
comment (`main.tf:1-9`, "The exact chain this tier exists to prove") to match.

**`tests/e2e/rig.py:171`** — docstring: "`module/main.tf` decodes
`connections.node.kube_targets.k3s.path`" → `nodes.node.kube.k3s.path`.

**`tests/e2e/test_tofu_providers.py:154`** —
`envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]` →
`envelope["nodes"]["node"]["kube"]["k3s"]["path"]`.

**`tests/e2e/test_tofu_providers.py:251-254`** — the fake envelope dict literal
(`"connections": {"node": {"ports": {}, "kube_targets": {"k3s": {...}}}}`) →
`{"nodes": {"node": {"ports": {}, "kube": {"k3s": {"path": ..., "context":
..., "endpoint": ...}}}}}`, aligning field names with `UnifiedKubeRef` (drop
any field beyond `path`/`context`/`endpoint` the old literal carried — this
fixture only needs enough to drive its dead-cluster negative-control
scenario).

**`tests/e2e/test_terragrunt_apply.py:339,425`** —
`envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]` (apply and
tunnelled-output cases) → `envelope["nodes"]["node"]["kube"]["k3s"]["path"]`
at both sites.

**Not touched, disposition recorded** (restated here where a reader would
otherwise expect to find them fixed): `tests/e2e/test_rig.py:278` reads
`start`'s raw stdout JSON, out of scope; `tests/e2e/test_recipe_terragrunt.py`'s
drift guard needs no code change — its compared content updates automatically
once the steps above land.

- [ ] **Step 0b: Rewrite `docs/recipe_terragrunt.md:366-388` — the "Fetched files are exported verbatim, not projected" subsection**

This shipped subsection currently argues the **opposite** of the shipped
behaviour (fetch content stays whole in the envelope, `FetchedFile` has no
`path`, dropping `content_b64` would be "a silent, unrecoverable breakage").
Fix in place, same "before Steps 1-2 add anything new" discipline as Step 0.
Replace the entire subsection (heading through the final paragraph ending
"...is recorded in the spec's 'Out of scope'.") with:

> ### Fetched files are materialized, not carried in the envelope
>
> The projection above (kube) and this one (`fetch_files`) follow the same
> rule: `run` materializes content to disk under the session dir's
> `tunnel-data/`, mode `0600`, and the consumer-facing envelope carries only a
> reference to it. Each `fetch_files` entry becomes `{path, size, sha256}` on
> success, `{error}` on failure — never `content_b64`.
>
> `FetchedFile` **has a `path`** (`schemas.py`, extended for this ticket), so
> the lossless on-disk alternative exists, the same way it already existed for
> kube.
>
> **The plan-file-persistence risk is resolved as a class, not documented
> around**: since fetched content never enters `TF_VAR_tunstrap` or the
> materialized file at all, `--fetch`ing a secret cannot land it in a saved
> Terraform plan file through this channel. Read the file directly at
> `fetch_files.<name>.path` if you need its contents.

Also check the one adjacent sentence this rewrite does not itself replace: the
"One other free-form string rides this channel unprojected: `warnings[*].error`"
paragraph immediately after (line ~390) stays accurate as written —
`warnings[*].error` is unrelated to `fetch_files` — but confirm after the edit
that "One other" still reads correctly given the preceding subsection no
longer describes `fetch_files` as unprojected. Reword the transition if it no
longer parses; do not leave a dangling "other."

Verify lines 344 and 361-364 (the kube-drop list and the `start` carve-out,
immediately above this subsection) need **no** edit — both state kube-specific
`content_b64` facts that remain true.

Run (requires `kind`/`tofu`/`kubectl`/Docker, per `tests/README.md` — optional
locally, mandatory before merge per Task 7 Step 4):

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/e2e -m e2e -q
```

Commit this shape-migration half separately from the new recipe content below,
so a reviewer sees "shape rename, no behaviour change" and "new recipe
content" as two independently reviewable diffs:

```bash
git add tests/e2e/module/main.tf tests/e2e/rig.py tests/e2e/test_tofu_providers.py \
  tests/e2e/test_terragrunt_apply.py
git commit -m "test(e2e): retarget to the unified output nodes.*.kube.*.path shape (#15)"
```

- [ ] **Step 1: Add Mode A — env-native kube**

Add a new section (placement: after the existing provider-config example, so
it reads as "and here is the identity-delivery contract that example depends
on") titled around **Mode A: env-native kube (satisfies the ticket's strict
"nothing live enters Terraform" contract)**:

1. `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` from tunstrap's own process
   environment (no `var.`-bound value, no file read in HCL at all for kube)
   **plus a literal `config_context = "tunstrap-<node>-<target>"` per provider
   alias** — a **two-alias worked HCL example**, citing findings #3 and #5 by
   number:
   ```hcl
   provider "kubernetes" {
     alias          = "node1_k3s"
     config_context = "tunstrap-node1-k3s"  # literal -- never derived from var.tunstrap
   }
   provider "kubernetes" {
     alias          = "node2_k3s"
     config_context = "tunstrap-node2-k3s"
   }
   ```
2. Explicit warning: never derive `config_context`'s value from `var.tunstrap`
   or any decoded data — literal only, matching the deterministic naming
   scheme exactly.
3. A short "measured facts a consumer needs" list, restated (not re-derived)
   from **all six** of the ticket's findings plus this design's provider
   findings — all six, do not miscount:
   - **#1** — provider configuration **is** re-evaluated at apply.
   - **#2** — outputs **freeze silently** — the worst failure mode, name it as
     such.
   - **#3** — per-alias `config_context` works with an env-supplied kubeconfig
     path (Mode A's own basis, shown in item 1's example).
   - **#4** — plan-safe end to end, measured live: plan with one set of ports,
     mutate only the kubeconfig, apply the *saved* plan → the alias uses the
     mutated value, zero plan-variable mismatch.
   - **#5** — `KUBE_CONFIG_PATHS` is colon-separated (comma silently falls
     back to `localhost:80`).
   - **#6** — a live value bound to a `var.` **does** trip "Mismatch between
     input and plan variable value" on a saved plan (Mode B's one-shot rule
     rests on this).
   - A live value bound to a **resource attribute** (not a provider config
     block) produces `Error: Provider produced inconsistent final plan` — cite
     `docs/artifacts/2026-08-07-issue15-provider-env-findings.md`'s Q3 result,
     and show provider-block placement as the only supported shape in both
     Mode A and Mode B.
4. A one-line pointer to the deterministic naming scheme
   (`tunstrap-<node>-<target>`) and why it matters for anyone piping the
   materialized kubeconfig into `kubectl --context` directly instead of
   through a provider.

- [ ] **Step 2: Add Mode B — unified-file convenience**

Immediately after Step 1's section (same document — a real consumer may use
Mode A for kube and Mode B for ports in the same module), add a section titled
around **Mode B: unified-file convenience (ports + kube references; does NOT
satisfy the ticket's strict contract — state this plainly)**. **No literal,
operator-pinned path and no `var.`-derived locator anywhere in this section:**

5. **The shape**, with a worked HCL example using the env-carried
   `TUNSTRAP_OUTPUT_FILE` locator via Terragrunt's `get_env(...)` — no
   `--session-dir` precondition and no operator-agreed path, because the
   session dir is ephemeral unconditionally:
   ```hcl
   locals {
     tunnel = try(
       jsondecode(file(get_env("TUNSTRAP_OUTPUT_FILE"))),
       { nodes = {} },
     )
   }

   provider "kubernetes" {
     config_path = local.tunnel.nodes.node1.kube.k3s.path
   }
   ```
   read directly inside the `locals` block that feeds the provider config —
   never through an `output`, per finding #2.
6. **Ports lose their integer form** (`"host:port"` string) — show the
   extraction idiom explicitly:
   ```hcl
   locals {
     service1_port = split(":", local.tunnel.nodes.node1.ports.service1)[1]
   }
   ```
7. **The stability contract**, restated plainly and matching the design doc's
   "Stability contract" word-for-word on the load-bearing claims: **both** Mode
   B forms — item 5's `TUNSTRAP_OUTPUT_FILE` form and the `--output-var`
   (`var.tunstrap`) form — are **one-shot `plan && apply` only**, with no
   saved-plan reuse across a tunstrap restart for either and no locator
   exemption of any kind (the check compares the variable's whole value; the
   file itself is deleted at teardown alongside the rest of `tunnel-data/`).
   State it as plainly as the design doc does: *"Neither Mode B form survives
   a tunstrap restart. If you need a saved plan to apply cleanly against fresh
   ports or fetched-file content, re-run plan in the same tunstrap
   invocation."* Cite findings #1, #2 and #6 by number.
8. **The `jsondecode`-not-JavaScript note**, one sentence: consumption is via
   HCL's `jsondecode`; there is no JS runtime anywhere in this stack (ADR
   entry 12).
9. **Fetched files:** state *"Fetched file content never enters a Terraform
   variable or plan file — only its path, size, and checksum do. Read the file
   itself at `fetch_files.<name>.path` if you need its contents."* Do not
   carry any warning framed around fetched content riding this channel — it
   does not.

Match the existing file's structure (numbered/lettered subsections, HCL code
fences, "Measured Terragrunt facts"-style attribution footers) — read the
file's current shape before writing; do not introduce a new prose style.

- [ ] **Step 3: Cross-check against the artifacts, the design doc, and the drift guard**

Confirm every measured fact restated in both new sections matches
`docs/artifacts/2026-08-07-issue15-provider-env-findings.md`, the ticket's own
six findings, and the design doc's "Stability contract" subsection verbatim —
no rewording that could drift from the source transcripts or introduce a
second, subtly different phrasing of the same rule. This is a manual
read-through, not a test.

Then run the recipe↔module drift guard, which must stay green through both
Step 0's shape migration and this step's new content (it fails loudly, by
design, if the two documents disagree on a shared HCL block):

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/e2e/test_recipe_terragrunt.py -m e2e -q
```

- [ ] **Step 4: Commit**

```bash
git add docs/recipe_terragrunt.md
git commit -m "docs(recipe): kubeconfig-as-identity delivery + unified output + stability contract (#15)"
```

---

### Task 7: Full gate pass

**Files:** none (verification only).

- [ ] **Step 1: Style/type/lint gates**

```bash
.venv/bin/black --check .
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pylint tunstrap/
.venv/bin/vulture tunstrap/
.venv/bin/mypy --strict tunstrap
```

Expected: all clean. `vulture` has no whitelist file to update
(`vulture_whitelist.py` was removed; `min_confidence = 80` in
`pyproject.toml`). If `rename_identities`/`render_kube_env`/
`render_unified_output` get flagged as unused, a call site is missing — that
is not a case for a suppression. Conversely, if `RunKubeTarget`, `render_env`,
or `MultiNodeEnvUnsupported` are still importable from anywhere,
`vulture`/`ruff` catching them is the signal Task 5's deletions were
incomplete. `pylint`'s `fail-under = 9.0` gate applies to the whole
`tunstrap/` package score, not per-file.

- [ ] **Step 2: Unit suite**

```bash
.venv/bin/pytest tests/unit -q
```

Expected: full pass. **Do not compare the count against any number recorded in
this plan or the spike findings** — this work deletes a meaningful number of
pre-existing tests while adding others. Run it and read the real number.

- [ ] **Step 3: Integration suite**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/integration -m integration -q
```

Expected: full pass. This tier **does** change: Task 5 Step 7 retargets
`test_run_env_io.py`, `test_cli_modes.py`, and the fetch tests for the same
shape/scalar removal as the unit tier, and this is where those retargets are
proven against the *real* console script and a real docker rig rather than
`CliRunner`. If this step is reached with those retargets not yet landed it
will fail, correctly — that failure is Task 5 Step 7 being incomplete, not a
flake to route around.

- [ ] **Step 4: e2e suite**

This tier **does** change too: Task 6 moves `tests/e2e/module/main.tf`,
`rig.py`, `test_tofu_providers.py`, and `test_terragrunt_apply.py` to the
`nodes.*.kube.*.path` shape — real code changes this tier must pass against,
not a no-op regression check.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/e2e -m e2e -q
```

Expected: full pass, including `test_recipe_terragrunt.py`'s drift guard
(already run once in Task 6 Step 3; the full tier here is the final
confirmation nothing else regressed).

**Separately, and optional:** the design doc's "`e2e` coverage — optional,
with rationale" describes a *different* piece of work — an e2e-level collision
test proving the kube identity rename (rewriting two kind kubeconfigs to a
shared identity before feeding them to `tunstrap start`/`run`). No task here
adds it by default, because Task 2's unit-level regression test already
exercises that defect precisely. If a reviewer wants it, it is an extension of
Task 2, not part of this step. Do not conflate the two: Task 6's e2e shape
migration is mandatory and verified by this step; the collision-specific e2e
coverage is optional.

- [ ] **Step 5: Final commit / PR**

No further commit needed if Tasks 1-6 committed cleanly and the gates pass on
the resulting tree. Open or update PR #13 against `feature/run-env-io` per the
ticket's stated target; **do not merge** (org rule — curate and validate,
leave the merge decision to the human reviewer).

---

## Coverage checklist

**Kube part (Tasks 1-3):**

| Requirement | Where |
|---|---|
| Ticket work item 1 — patch identity names (cluster + user + context) | Task 1 |
| Rename scope: the active current-context triple only (ADR entry 5) | Task 1 (`rename_identities`, "ignored entries" test) |
| Every reference to the renamed cluster/user swept, incl. non-current contexts | Task 1 (shared-reference test + implementation sweep) |
| Naming scheme `tunstrap-<node>-<target>`, no configurable prefix (ADR entry 4) | Task 1 |
| Naming-join collision rejected at validation time (ADR entry 15) | Task 1 Steps 6-9 (`a-b`/`c` vs `a`/`b-c`) |
| `patch_view` owns server-address patching; `dump_kubeconfig` signature unchanged | Task 1 Step 3 |
| Mandatory unit-level k3s collision regression test | Task 2 |
| Ticket work item 2 — multi-node kube channel | Task 3 (`render_kube_env`, node-count-agnostic) |
| Ticket work item 3 — export provider-facing vars per the conditional cardinality contract, never the naive superset (ADR entry 3) | Task 3 (`_kube_channel_keys`) |
| Conservative `predicted_env_keys` + two-part anti-drift guard (ADR entry 16) | Task 3 (formula half) + Task 5 (safety-envelope half) |
| `suppress_kubeconfig` drops all three kube env names (ADR entry 7) | Task 5 (`_build_child_env`) |

**Unified output contract (Tasks 4-6):**

| Requirement | Where |
|---|---|
| Unified node-qualified contract replaces the flat scalar channel (ADR entry 10) | Task 4 (shape) + Task 5 (scalar deletion) |
| Materialization is the primary delivery; `--output-var` is the bare-`tofu` bridge (ADR entry 11) | Task 5 Step 4 (unconditional write) |
| Scalar channel removed, not narrowed: `render_env`, `inject_scalars`, `MultiNodeEnvUnsupported` all deleted (ADR entry 13) | Task 5 |
| Kube side unchanged; the unified structure carries only kube *references* | Task 4 (`UnifiedKubeRef`, `extra="forbid"`, `{path, context, endpoint}` only) + Task 5's projection-file retarget |
| Content on disk, paths in env — incl. fetched files (ADR entry 19) | Task 5 (fetched-file materialization, `UnifiedFetchRef`) |
| Atomic-replace writer, not `O_TRUNC`, not write-then-chmod (ADR entry 17) | Task 5 Step 4 item 4, shared with the fetched-file writer |
| Consumer-side transformation via `jsondecode` (ADR entry 12) | Task 6 Step 2 item 8 |
| Breaking deliberately, no compatibility shim (ADR entry 8) | Tasks 5-6 across every tier |
| Recipe carries both consumer modes + the measured facts + the stability contract | Task 6 Steps 0, 0b, 1, 2 |

**Types:** `rename_identities(dict[str, object], str, str) -> str`;
`render_kube_env(OutputSchema) -> dict[str, str]`;
`_kube_channel_keys(int) -> set[str]`;
`render_unified_output(OutputSchema) -> dict[str, Any]`;
`render_output_var(OutputSchema) -> str` (signature unchanged, body rewritten);
`predicted_env_keys(InputSchema) -> set[str]` (return type unchanged, body
simplified); `_build_child_env(output, *, output_var, input_env,
suppress_kubeconfig=False) -> dict[str, str]` (**no `inject_scalars`
parameter** anywhere in the chain); `_materialized_output_path(str) -> str`
(shared between the env-var value and the writer so the two cannot drift).

**Deliberately left to the implementer to resolve against the live tree, not
gaps:** the exact `run_command` call site for the materialization write (Task
5 Step 4 item 4 — re-resolve by reading the function; line citations for it
have drifted before); whether `SessionDir._write_file` can be refactored into
a shared atomic-replace helper or the primitive is replicated in `cli.py`
(both paths specified); and the exact daemon/worker function that owns kube
materialization, where the fetched-file step attaches (confirm by reading, do
not assume).
