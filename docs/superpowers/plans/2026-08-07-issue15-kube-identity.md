# Kubeconfig-as-identity delivery (issue #15) Implementation Plan

> **Redaction/repoint note (2026-08-10):** Repointed provider evidence to its
> committed spec and replaced local/ignored references with accurate placeholders.

> **Status: historical record, NOT the executable plan.** This file carries the
> full 8-iteration decision history inline. Implementers execute
> `docs/superpowers/plans/2026-08-08-issue15-kube-identity-clean.md`
> (the consolidated final state); apply any future correction THERE, and treat
> this file as frozen.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revised (iteration 3): the unified-output-contract pivot.** Tasks 1-3 below
(kube identity rename, mandatory collision test, kube env conditional
contract) are **unchanged by the pivot** — U4, kube part stands as written,
cherry-pick from the spike exactly as before. **Task 4 is entirely new**
(unified output shape + `render_unified_output`, pure-function work). **Task 5
is entirely new and absorbs iteration 2's old Task 4** (multi-node kube
wiring): the old two-branch `_build_child_env` is superseded by one
unconditional `render_kube_env` call, per decision history entry 13, and
Task 5 additionally wires materialization and deletes `render_env`,
`MultiNodeEnvUnsupported`, and `inject_scalars` in the same commit, since all
three are the same edit site. The recipe task (renumbered Task 6, was Task 5)
gains new content; the gate-pass task is renumbered Task 7 (was Task 6) with
no content change beyond fixed cross-references.

**Revised (iteration 6): a three-model red-team review of the pivot found 12
consolidated findings, all addressed in place — task numbering is unchanged
from iteration 4/5, only task *content*.** Highlights: Task 1 gains a naming
collision check (R10) and a dangling-reference fix (R14); Task 3's
`predicted_env_keys` ships the **conservative** superset formula directly,
not the exact-cardinality one an earlier revision had it compute (R11 — no
"Task 5 fixes it again" deferral needed here, the correct formula lands the
first time); Task 5's materialization writer is a true atomic replace, not
`_write_file` reused as-is (R13), and gains a stdin-mode `--output env`
guard; Task 6's recipe is two explicit consumer modes (env-native kube /
unified-file convenience), not a single blended one, and drops the unsound
`var.tunstrap_session_dir` locator pattern entirely (R9, R12) — decision
history entries 14-18 carry the full reasoning for each.

**Revised (iteration 7): the user's confirmed post-red-team direction plus
one added constraint (R16), superseding parts of R9/R12/R13/R15 — task
numbering unchanged, only task content.** Core principle: **content on disk,
paths in env.** Delivery collapses from three modes to two — R9's mode 2
(the literal, caller-pinned `--session-dir` file) is retracted; the
`--output-var` bridge survives only for bare `tofu`. `TUNSTRAP_OUTPUT_FILE`
generalizes into the primary, env-carried locator for `run` (not just `start
--output env`), read via `get_env(...)`/`file()`, never a pinned path.
`fetch_files` content_b64 is removed from every consumer-facing channel: the
daemon materializes fetched bytes to `tunnel-data/<node>-<fetchname>`
(mirroring the kube precedent exactly) and projects `{path, size, sha256}`
instead — Task 5 gains this mechanism plus a dedicated `content_b64`
blast-radius enumeration. **User's constraint, encoded explicitly:** the
session dir stays mandatory *lifecycle* infrastructure (`daemon.pid`,
`session.lock`, `stop`/recovery) — only its role as a *consumer-facing*
locator is retracted, not the directory itself. See decision history entry
19 (new) and the correction annotations on entries 14 and 18.

**Goal:** Rename the materialized kubeconfig's cluster/user/context to
`tunstrap-<node>-<target>` (deterministic, per-target); make the `KUBECONFIG`
export multi-node-safe; export the OpenTofu-provider-facing env vars under the
conditional cardinality contract (never the naive superset) — all three
unchanged by the pivot. **New:** replace tunstrap's entire consumer-facing
output with one unified, node-qualified JSON structure, delivered as both an
`--output-var` value and a materialized session-dir file (materialization
primary); remove the `TUNSTRAP_<TARGET>_*` scalar channel,
`MultiNodeEnvUnsupported`, and `inject_scalars` entirely; document the
resulting recipe (kube-only guidance unchanged, unified-output guidance new).

**Spec:** `docs/specs/2026-08-07-issue15-kube-identity-delivery-design.md`.
**Decision history:** `docs/specs/2026-08-07-issue15-kube-identity-decisions.md`
(entries 10-13 are the pivot; entry 9 is marked superseded, not deleted).
**Reference implementation:** `variant/combined` in the scratch worktree
`<spike-worktree>` — reviewed,
475/475 pre-existing unit tests pass plus one new regression test (476/476).
**Cherry-pick the `kube.py` rename change from it as-is; the `envrender.py`
change needs its body replaced** — the spike's Axis 3 prototype (superset
export) is superseded by the conditional contract below (design doc §"Env-
export contract", decision-history #3). **Scope note, iteration 3:** the
spike predates the pivot and covers the kube part only (Tasks 1-3 below);
nothing in it prototypes the unified output contract, materialization, or the
scalar-channel removal (Tasks 5-6) — those are new code with no spike
reference to cherry-pick from. Do not re-derive the six OpenTofu findings or
the provider-precedence findings; the latter is now committed in the provider-precedence spec.

**Target branch:** `feature/run-env-io` (PR #13). Every task below assumes a
checkout of that branch (not the spike worktree) as the working tree; the
spike worktree is a read-only reference, never committed from directly.

**Tech stack:** Python 3.10+, Pydantic v2, Click, ruamel.yaml, pytest +
pytest-asyncio. Use `.venv/bin/{pytest,ruff,black,mypy,pylint,vulture}`.
Integration/e2e tiers need Docker (+ `kind`/`kubectl`/`tofu` for e2e) on
`PATH`; see `tests/README.md` for env flags.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `tunstrap/kube.py` | New `rename_identities(doc, node, target) -> str`; one call site in `run_kube_targets` between `patch_view` and `dump_kubeconfig`. **[unchanged by the pivot]** |
| `tunstrap/envrender.py` | New `render_kube_env(output) -> dict[str,str]` (conditional cardinality contract, **unchanged by the pivot**) + a shared `_kube_channel_keys(count)` helper reused by `predicted_env_keys`. **[PIVOT]** New `render_unified_output(output) -> dict[str, Any]` (the unified structure) and `UnifiedOutput`/`UnifiedNode`/`UnifiedKubeRef` models (or equivalent — see Task 5); `render_output_var`'s body rewritten to serialize the unified structure instead of the old `RunKubeTarget` projection; `render_env` and `predicted_env_keys`' scalar half **deleted**. |
| `tunstrap/schemas.py` | **[PIVOT]** New `UnifiedOutput`/`UnifiedNode`/`UnifiedKubeRef` Pydantic models (or placed in `envrender.py` — Task 5 picks one and states why). |
| `tunstrap/exceptions.py` | **[PIVOT]** `MultiNodeEnvUnsupported` and its `_EXIT_CODES` entry **deleted**. |
| `tunstrap/cli.py` | `_build_child_env`: **[PIVOT, supersedes iteration 2's two-branch design]** unconditional `render_kube_env(output)` call (no `inject_scalars` branch — the branch and the flag are both deleted); extend `suppress_kubeconfig` to drop all three kube env names (unchanged by the pivot); **[PIVOT]** new unconditional materialization write for `run`; `cli.py:640`'s pre-spawn multi-node-without-`--output-var` gate **deleted**. |
| `tests/unit/test_kube_rename.py` | New. Direct unit tests for `rename_identities` as a pure function. **[unchanged by the pivot]** |
| `tests/unit/test_kube_identity_collision.py` | New. The mandatory k3s-style collision regression test (promoted from the spike's throwaway `test_issue15_context_collision.py`, renamed to match this repo's non-issue-numbered test naming convention). **[unchanged by the pivot]** |
| `tests/unit/test_kube_run.py` | Extend: `context_name`/`cluster_name` now assert the renamed value, not just "non-empty output". **[unchanged by the pivot]** |
| `tests/unit/test_envrender.py` | Extend: `render_kube_env` cardinality cases (0/1/≥2 files, unchanged by the pivot, Task 3); `predicted_env_keys` cardinality cases (rewritten scope, **Task 5**). **[PIVOT]** New: `render_unified_output` shape tests (Task 4); every `render_env`-dependent test deleted by name, incl. the Task-3-era `test_render_kube_env_works_for_multi_node_while_render_env_still_rejects`; anti-drift guard **retargeted, not deleted** (Task 5). |
| `tests/unit/test_cli_run_output_var.py` | Extend/rewrite: multi-node `run` with kube_targets gets the kube channel and the unified output in the child env, **with or without** `--output-var`; `suppress_kubeconfig` drops all three kube names; **[PIVOT]** every `TUNSTRAP_<TARGET>_*`-scalar or `MultiNodeEnvUnsupported`-exit-code assertion deleted or rewritten (Task 5). |
| `tests/unit/test_cli_run_output_var_projection.py` | **[PIVOT]** Retargeted, not left alone (missed twice before iteration 4) — the credential-scrubbing pin for the kube reference; shape moves to `nodes.*.kube.*`, expected field set narrows to `UnifiedKubeRef`'s three fields, one test deleted outright (Task 5). |
| `tests/unit/test_cli_run_materialize.py` | **[PIVOT]** New. Materialization writer tests (session-dir file, mode, content, `TUNSTRAP_OUTPUT_FILE`) (Task 5). |
| `tests/unit/test_cli_run.py`, `test_cli_run_input_env_scrub.py`, `test_cli_runner.py`, `test_cli_run_postspawn.py` | **[PIVOT]** Each retargets one pre-existing `TUNSTRAP_<TARGET>_*` or `MultiNodeEnvUnsupported` assertion — full list in Task 5's blast-radius table (Task 5). |
| `tests/unit/test_exceptions.py`, `tests/unit/test_tofu_proxy.py` | **[PIVOT]** Three `MultiNodeEnvUnsupported` cases deleted; two docstrings updated to drop `inject_scalars` framing (Task 5). |
| `tunstrap/session.py` | **[PIVOT, R13]** Referenced, not necessarily modified — `_write_file`'s mode-fixed-at-creation property (not "atomic": no rename step) is either factored into a shared atomic-replace (temp file + `os.replace`) helper both kube materialization and `output.json` use, or the atomic-replace primitive is replicated inline in `cli.py` if that refactor isn't clean (Task 5). |
| `tunstrap/schemas.py` (naming collision) | **[PIVOT, R10]** New `InputSchema`-level `model_validator` rejecting a payload where two `(node, target)` pairs render the same `tunstrap-<node>-<target>` string (Task 1). |
| `tests/integration/test_run_env_io.py`, `test_cli_modes.py` | **[PIVOT]** Retargeted for the same shape/scalar removal, proven against the real console script (Task 5 Step 7). |
| `tests/e2e/module/main.tf`, `rig.py`, `test_tofu_providers.py`, `test_terragrunt_apply.py` | **[PIVOT]** Retargeted to `nodes.*.kube.*.path` (Task 6 Step 0) — mandatory, not the optional collision-specific e2e coverage. |
| `docs/recipe_terragrunt.md` | Recipe section per the design doc's "Documentation" item — kube-only content unchanged **except its pre-existing `connections.*` shape, fixed in Task 6 Step 0 before new content is added**; **[PIVOT]** new unified-output-consumption + stability-contract + jsondecode-note content (Task 6 Steps 1-2). |

---

### Task 1: `rename_identities` + call site in `kube.py` + naming collision check

**Files:**
- Modify: `tunstrap/kube.py` (rename_identities, R14 dangling-reference fix),
  `tunstrap/schemas.py` (new naming-collision validator, R10)
- Test: `tests/unit/test_kube_rename.py` (new), `tests/unit/test_kube_run.py`
  (extend), `tests/unit/test_schemas_kube.py` or a new
  `tests/unit/test_schemas_kube_naming_collision.py` (R10's collision test)

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
    """[R14] A non-current context that references the SAME cluster/user the
    active triple uses must have that reference updated too, or it dangles --
    naming a cluster/user that no longer exists anywhere in the document
    under its old name. The ignored context's own `name` is untouched (it is
    not renamed itself, only its cluster/user references are); only entries
    that neither ARE nor REFERENCE the active triple stay fully byte-stable."""
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
`variant/combined`, `tunstrap/kube.py`, with the docstring's "V1c" spike
framing dropped — that context belongs in the decision history, not the
shipped code — and `__all__` gains `"rename_identities"`):

```python
def rename_identities(doc: dict[str, object], node: str, target: str) -> str:
    """Rename the current-context's cluster/user/context to a deterministic name.

    ``tunstrap-<node>-<target>`` for cluster, user and context alike — see
    docs/specs/2026-08-07-issue15-kube-identity-delivery-design.md. Operates on
    the raw parsed document alone; the current-context's own name is enough to
    find every entry that needs renaming.

    [R14] Every *other* context's cluster/user REFERENCES are also updated if
    they name the same cluster/user being renamed here -- a kubeconfig can
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

    # [R14] Sweep every OTHER context for a reference to the cluster/user
    # entries just renamed. This context's own `name` is not touched -- it is
    # not becoming the current context, only its dangling reference is fixed.
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
`view.doc` is typed `object` on `KubeconfigView` (dataclass field, `kube.py:56`)
and `rename_identities` wants `dict[str, object]`; use the explicit
`assert isinstance(view.doc, dict)` above instead, matching the pattern
`patch_view` already uses two lines earlier (`kube.py:257`) — this keeps
`mypy --strict` clean without a suppression.

- [ ] **Step 4: Run to verify pass**

`.venv/bin/pytest tests/unit/test_kube_rename.py tests/unit/test_kube_run.py tests/unit/test_kube_patch.py tests/unit/test_kube_parse.py tests/unit/test_kube_parse_invariants.py -v`
Expected: all pass. (The last three files are the ones the spike confirmed
are unaffected — this run is the regression check that confirms it stayed
true after cherry-picking, not a re-derivation.)

- [ ] **Step 5: Commit**

```bash
git add tunstrap/kube.py tests/unit/test_kube_rename.py tests/unit/test_kube_run.py
git commit -m "feat(kube): rename current-context identity to tunstrap-<node>-<target> (#15)"
```

- [ ] **Step 6: [R10] Write the failing naming-collision test**

New file `tests/unit/test_schemas_kube_naming_collision.py`:

```python
"""[R10] tunstrap-<node>-<target> is NOT unique by construction.

_FETCH_FILES_KEY_RE (schemas.py:11) permits internal hyphens in node/target
identifiers, and the join itself uses a hyphen, so two DIFFERENT (node,
target) pairs can render the SAME string: (node="a-b", target="c") and
(node="a", target="b-c") both produce "tunstrap-a-b-c". This is a distinct
defect class from the mandatory k3s-style collision test (Task 2) -- that
test proves upstream kubeconfig names colliding is fixed by the rename; this
test proves tunstrap's OWN naming scheme does not collide with itself,
independent of any kubeconfig content at all.

Code: tunstrap/schemas.py (new validator, InputSchema level)
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
Expected: FAIL — no such validator exists yet; both tests currently pass
`InputSchema.model_validate` without complaint (the first test's payload is
wrongly accepted today).

- [ ] **Step 8: Implement the collision check in `schemas.py`**

Add an `InputSchema`-level `model_validator(mode="after")` (alongside the
existing `_validate_auth` field validator, `schemas.py:278-289`) — this must
run at the `InputSchema` level, not per-`NodeInput`, since the collision is
cross-node:

```python
@model_validator(mode="after")
def _validate_kube_identity_names_are_unique(self) -> InputSchema:
    """[R10] tunstrap-<node>-<target> is not unique by construction (hyphens
    are legal in both node and target names); reject a payload where two
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

Place this near `InputSchema`'s existing `_validate_auth` validator so both
cross-node checks live together. Note this validator is intentionally at
`InputSchema` level (has access to every node), not on `NodeInput`
(single-node scope, cannot see the collision) — do not move it there even
though `kube_targets` is a `NodeInput` field.

- [ ] **Step 9: Run to verify pass, then commit**

`.venv/bin/pytest tests/unit/test_schemas_kube_naming_collision.py tests/unit/test_schemas.py tests/unit/test_schemas_kube.py -v`
Expected: all pass.

```bash
git add tunstrap/schemas.py tests/unit/test_schemas_kube_naming_collision.py
git commit -m "feat(schemas): reject tunstrap-<node>-<target> naming collisions (#15, R10)"
```

---

### Task 2: The mandatory collision regression test

**Files:**
- Create: `tests/unit/test_kube_identity_collision.py`

This is the trap the design doc's testing contract calls out by name: it must
land, unmodified in substance, regardless of how Task 1 was implemented.

- [ ] **Step 1: Copy the spike's prototype under a repo-convention file name**

Copy `tests/unit/test_issue15_context_collision.py` from the spike worktree
(` <spike-worktree>`, branch
`variant/combined`) to `tests/unit/test_kube_identity_collision.py` in this
checkout — **only the file is renamed** (this repo's other test files never
carry an issue number, e.g. `test_kube_run.py`, `test_envrender.py`). **Keep
the test function name unchanged**,
`test_two_k3s_style_targets_get_distinct_deterministic_identities` — it is
already descriptive and needs no rename. Drop the module docstring's "spike"
framing, replacing it with a plain description (content is otherwise correct
verbatim; see the untracked spike's Part 3 for the exact source).

- [ ] **Step 2: Run to verify it is GREEN after Task 1**

`.venv/bin/pytest tests/unit/test_kube_identity_collision.py -v`
Expected: PASS. (It was confirmed RED against the unmodified branch and GREEN
under `variant/combined` during the spike; this run confirms the same is true
against this checkout's own Task 1 implementation, not a re-derivation of the
spike's own finding.)

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


def test_render_kube_env_works_for_multi_node_while_render_env_still_rejects() -> None:
    """The exact split render_env's own docstring claims: kube channel is
    node-count-agnostic, scalar channel is not."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, "/s/a-k3s")}),
            "b": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7001, "/s/b-k3s")}),
        },
        pid=1, session_dir="/s", started_at="now",
    )
    assert render_kube_env(out)  # does not raise
    with pytest.raises(MultiNodeEnvUnsupported):
        render_env(out)


def test_predicted_env_keys_reserves_all_three_for_one_kube_target() -> None:
    """[R11] predicted_env_keys is a CONSERVATIVE predictor, not exact: it
    reserves all three kube names whenever ANY kube_targets are declared,
    regardless of exact count -- input cardinality can shrink by output time
    (an optional node/target can fail), so predicting the exact one-file
    branch here would under-reserve KUBE_CONFIG_PATHS for a schema that
    later, at runtime, actually produces >=2 files. render_kube_env's own
    export (tested above) stays exact -- only the predictor is conservative."""
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
    """[R11] Same conservative reservation for the >=2 case -- the point is
    that BOTH cardinalities reserve identically (all three), which is what
    makes the predictor a safe over-approximation rather than a second exact
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

**[R11] The anti-drift guard is two-part, not a single equality against
`render_kube_env`/`render_env`** — a red-team finding, logic-verified, shows
the naive "predict the exact cardinality branch" design under-reserves:
`predicted_env_keys` runs pre-spawn against *input* cardinality, but an
optional (`required: false`) node or kube target can fail without failing
the run, so *output* cardinality can be smaller than what was declared. Two
kube targets declared (predicting the `≥2` branch, `KUBE_CONFIG_PATHS` only)
but one optional node fails at connect time → only one file actually
materializes → the real export uses the `==1` branch (`KUBE_CONFIG_PATH`) —
which the exact predictor never reserved. A `--output-var KUBE_CONFIG_PATH`
would then pass the pre-spawn collision check and be **silently
overwritten** by the real export. Fix: `predicted_env_keys` reserves **all
three** kube names whenever *any* `kube_targets` are declared, not the exact
per-count subset (implemented below); this is deliberately a superset of
what usually gets injected, and over-reserving is the safe direction (a
false-positive usage error, cheap and visible) versus under-reserving
(a silent post-spawn collision). The anti-drift concern this guard encodes
is this codebase's own discipline (spike/design review process), not a
concern stated by the ticket itself — an earlier revision of this note
misattributed it to "the ticket's own review process," corrected here. Add
both new tests above (formula-correctness style, not drift-guard style —
see decision history entry 16 for the full two-part-guard design); the
drift-guard half (`actual ⊆ predicted`, driven by a cardinality-shrink case)
is added in Task 5 once `_build_child_env` exists to compute "actual" from.

- [ ] **Step 2: Run to verify failure**

`.venv/bin/pytest tests/unit/test_envrender.py -v`
Expected: FAIL — `render_kube_env` missing; `predicted_env_keys` still uses
the unconditional `KUBECONFIG`-only rule.

- [ ] **Step 3: Implement in `envrender.py`**

Add a shared cardinality helper (used by both `render_kube_env` and
`predicted_env_keys`, so the two cannot independently drift on this rule —
this is stronger than the spike's structure, which computed the export dict
inline in `render_kube_env` with no shared helper):

```python
def _kube_channel_keys(count: int) -> set[str]:
    """Names of the kube-channel env keys the conditional contract exports.

    0 files: nothing. Exactly 1: KUBECONFIG + KUBE_CONFIG_PATH. >=2:
    KUBECONFIG + KUBE_CONFIG_PATHS. KUBE_CONFIG_PATH and KUBE_CONFIG_PATHS are
    never both present -- KUBE_CONFIG_PATH wins over KUBE_CONFIG_PATHS per the
    measured OpenTofu kubernetes/helm provider precedence (docs/specs/
    2026-08-10-issue15-provider-env-precedence.md), so exporting both once a
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

    Unlike the ``TUNSTRAP_<TARGET>_*`` scalars, this channel has no node
    dimension: it collects one materialized path per kube_target across every
    node (not just a single one), so it is safe to call for any node count.
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

Replace `render_env`'s inline kube-path block (unchanged single-node contract,
delegating to `render_kube_env` — **note:** `render_env` itself is still
alive at this point in the plan; Task 3 lands before Task 5 deletes it
entirely, so this edit is a normal in-place change here, not a preview of the
deletion). **This also deletes the now-unused `kube_paths: list[str] = []`
accumulator declaration at `envrender.py:49`** — the block below never
appends to it (that accumulation moved into `render_kube_env` above), and
leaving the declaration in place would fail `ruff check` (unused variable) at
Task 7's gate:

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

**[R11] Update `predicted_env_keys` to reserve conservatively, not exactly.**
Unlike `render_kube_env`'s own export (which correctly uses
`_kube_channel_keys(exact_count)` because it runs *after* real materialization
and knows the true count), `predicted_env_keys` runs pre-spawn against the
*input* schema, before any node has connected — so it must not assume the
declared cardinality will survive to output time. Whenever *any* node
declares `kube_targets` at all, reserve **all three** kube names
unconditionally, regardless of exact count:

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

**Note, still valid:** Task 5 replaces this function's body again — not to
fix the cardinality logic (already correct here, conservative from the
start, so no re-fix needed), but because the entire `TUNSTRAP_*` scalar
half this version still computes (the `if len(schema.nodes) == 1:` block)
is deleted once the scalar channel itself is removed. The conservative
kube-reservation line above is what Task 5's later rewrite keeps unchanged
(it only adds the three survivor scalars in its place of the deleted
per-target block) — see Task 5 Step 4.

Note the docstring's "Multi-node input injects no scalars at all, so the
answer there is the empty set" claim (`envrender.py:83-93`, pre-change) is now
**false** for the kube-channel keys specifically and must be corrected in the
same edit — it stays true only for the `TUNSTRAP_*` scalar keys.

- [ ] **Step 4: Run to verify pass**

`.venv/bin/pytest tests/unit/test_envrender.py -v`
Expected: all pass, including every pre-existing case (single-node
`KUBECONFIG` behaviour is byte-identical to before this task for the
one-kube-target case, since `_kube_channel_keys(1)` includes `KUBECONFIG`
exactly as the old unconditional `put("KUBECONFIG", ...)` did).

- [ ] **Step 5: Commit**

```bash
git add tunstrap/envrender.py tests/unit/test_envrender.py
git commit -m "feat(envrender): multi-node kube channel + conditional KUBE_CONFIG_PATH(S) export (#15)"
```

---

### Task 4: The unified output contract — shape + `render_unified_output` [PIVOT, new]

Pure-function work only: no `cli.py` wiring yet (Task 5), no materialization
yet (Task 5). This task makes the shape exist and be correctly built from an
`OutputSchema`; Task 5 makes anything call it.

**Files:**
- Modify: `tunstrap/schemas.py` (new models), `tunstrap/envrender.py` (new
  `render_unified_output`, `render_output_var` body rewritten)
- Test: `tests/unit/test_envrender.py` (new cases), `tests/unit/test_schemas.py`
  or a new `tests/unit/test_schemas_unified.py` (model tests)

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_envrender.py`:

```python
def test_render_unified_output_shape() -> None:
    """[R16] Ports become 'host:port' strings; kube becomes
    {path,context,endpoint} references; fetch_files becomes
    {path,size,sha256} -- NOT a content_b64 passthrough, corrected from an
    earlier revision of this test that asserted the opposite ("content_b64
    IS allowed via fetch_files"); two reserved top-level keys."""
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
                    # [R16] .path is set here because materialization (Task 5)
                    # runs before render_unified_output ever sees this object --
                    # the daemon writes the bytes and sets .path, exactly as it
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
    # [R16] {path, size, sha256} exactly -- no content_b64 in the projection.
    assert node["fetch_files"]["hosts"] == {
        "path": "/s/tunnel-data/node1-hosts", "size": 6, "sha256": "ab" * 32,
    }
    # Nothing that could carry raw content -- kube credentials AND fetched
    # file content_b64 -- ever appears anywhere in the shape. [R16] content_b64
    # joins this leak check; it is no longer a sanctioned exception.
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
helper that also accepts a `context` kwarg — add it alongside `_kube_out`,
do not change `_kube_out`'s existing signature, since Task 3's tests still
use it unchanged.)

- [ ] **Step 2: Run to verify failure**

`.venv/bin/pytest tests/unit/test_envrender.py -k "unified" -v`
Expected: FAIL — `render_unified_output` missing; `render_output_var` still
returns the old `RunKubeTarget`-projection shape.

- [ ] **Step 3: Implement**

Add models to `tunstrap/schemas.py` (placed there, not `envrender.py`,
matching the existing convention that `schemas.py` is "Single source of JSON
shape" per its own module docstring, `schemas.py:1`):

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
    """[R16] Fetched-file reference in the unified output: never content_b64,
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
    {path, context, endpoint} references (never credentials, never content,
    per RunKubeTarget's pre-existing allow-list — this reshapes, not
    reprojects). [R16] fetch_files becomes {path, size, sha256} (or
    {error}) -- NOT a passthrough. An earlier revision of this function (and
    this docstring) carried forward the pre-#15 design's decision to let
    fetch_files ride unprojected, including content_b64; R16 retracts that
    for the same reason U4 already narrowed kube: content must not enter a
    Terraform variable or the materialized file, only its path/metadata may.
    Callers must ensure fetch_files entries are already materialized (.path
    set) before calling this -- see Task 5's fetched-file materialization
    step, which runs upstream of this function, the same ordering
    KubeTargetOutput.path already requires today.
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

**`exclude_none=True`, deliberately**: without it, a success entry would
serialize `{"path": ..., "size": ..., "sha256": ..., "error": null}` — a
stray `"error": null` in every successful fetch, not matching the design
doc's shape (`{"path", "size", "sha256"}` exactly, no fourth key) or the
error-branch shape (`{"error"}` exactly, not `{"path": null, "size": null,
"sha256": null, "error": ...}`). This mirrors why `UnifiedKubeRef` does not
need the same treatment: none of its three fields is ever optional/`None`
in a materialized `KubeTargetOutput`.

Replace `render_output_var`'s body (keep the signature, `OutputSchema -> str`
— no `cli.py` call-site change needed):

```python
def render_output_var(output: OutputSchema) -> str:
    """Serialise the unified structure for ``--output-var``.

    Delivers the same content the materialized file carries (Task 5) — see
    docs/specs/2026-08-07-issue15-kube-identity-delivery-design.md, "The
    unified output contract", for the delivery/stability contract governing
    which of the two a plan-safe consumer should actually bind to a resource.
    """
    return json.dumps(render_unified_output(output), separators=(",", ":"))
```

Delete the old `RunKubeTarget`-based body (the `payload = output.model_dump
(mode="json")` / per-node `RunKubeTarget.model_validate` loop) — it is fully
replaced, not kept as a fallback.

**`RunKubeTarget` disposition:** now unused by `render_output_var`. Check with
`vulture` (Task 7) whether anything else still imports it; if not, delete the
class from `schemas.py` too — its whole purpose (an allow-list projection for
this exact channel) is now served by `UnifiedKubeRef`, and keeping an unused
allow-list model around is exactly the kind of drift a reviewer would flag on
sight.

- [ ] **Step 4: Run to verify pass**

`.venv/bin/pytest tests/unit/test_envrender.py -v`
Expected: all pass. The **old** `render_output_var` shape tests in
`tests/unit/test_cli_run_output_var.py` (e.g.
`test_output_var_carries_the_whole_envelope_minus_kube_credentials`) now
fail, expectedly — they pin the old `connections.<node>.ports.<target>`
(int) shape; Task 5 retargets them alongside the rest of that file's
scalar-removal changes. Do not fix them here; note the expected failures and
move on, matching this plan's own established discipline of one clean
commit per concern rather than a half-finished retarget.

- [ ] **Step 5: Commit**

```bash
git add tunstrap/schemas.py tunstrap/envrender.py tests/unit/test_envrender.py
git commit -m "feat(envrender): unified node-qualified output contract, shape only (#15)"
```

---

### Task 5: Materialize the unified output; remove the scalar channel [PIVOT, new + rewrite; R16, iteration 7 adds fetch-file materialization]

**This is the big ripple task.** It does six things in one coherent change,
because they are the same edit site (`_build_child_env` and its callers) or
its direct sibling (the daemon-side materialization step):
(a) wires `render_unified_output`/`render_output_var` into `run` and adds
unconditional materialization (using the repo's existing secure-write
primitive, not a write-then-chmod sequence); (b) collapses the kube-channel
call to unconditional (supersedes iteration 2's two-branch design, decision
history entry 13); (c) deletes `render_env`, `MultiNodeEnvUnsupported`, and
`inject_scalars`; (d) **re-scopes** (not deletes) the `predicted_env_keys`
anti-drift guard to compare against `_build_child_env`'s actual output, since
`render_env` is no longer there to compare against; (e) retargets every
pre-existing test, fixture, and shipped artifact this removal breaks, across
every tier — enumerated exhaustively below, not case-by-case; (f) **[R16,
new]** materializes `fetch_files` content to `tunnel-data/<node>-<fetchname>`
the same way kube files already are, removing `content_b64` from every
consumer-facing projection — see "Fetched-file materialization" below, its
own dedicated blast-radius enumeration.

**Iteration-4 note, read before starting this task.** The first three review
rounds each caught this task's blast radius incompletely, one spot at a time
— a test here, a stale reference there, an entire test *file*
(`test_cli_run_output_var_projection.py`) missed twice, integration and e2e
fixtures never looked at. The table below is a full grep-driven enumeration
across `tunstrap/`, `tests/unit`, `tests/integration`, `tests/e2e`, and
`docs/`, done once, systemically, specifically so this task cannot be landed
piecemeal again. **Do not treat this table as a starting point to extend by
inspection — treat it as complete; if the implementer finds something it
missed, that is itself a signal to re-run the greps below, not to patch the
one spot found.**

Re-derivable with (or equivalent):

```bash
grep -rn 'TUNSTRAP_[A-Z0-9_]*' tunstrap/ tests/ docs/ \
  --include='*.py' --include='*.md' --include='*.tf' \
  | grep -vE 'TUNSTRAP_SESSION_DIR|TUNSTRAP_PID|TUNSTRAP_OUTPUT_FILE|TUNSTRAP_INPUT|TUNSTRAP_E2E_REQUIRE_ALL|TUNSTRAP_TOKEN'
grep -rn 'MultiNodeEnvUnsupported\|inject_scalars\|render_env(' tunstrap/ tests/ --include='*.py'
grep -rn 'connections\.' tests/ docs/ --include='*.py' --include='*.md' --include='*.tf'
grep -rn '\["connections"\]\|\.connections\[' tests/ --include='*.py'
```

### Blast-radius table (authoritative; every hit below has a disposition)

**Unit tier:**

| File:line | Old shape/symbol | Disposition |
|---|---|---|
| `test_cli_run.py:91` | `FakePopen.last_env["TUNSTRAP_DB_PORT"] == "5432"` | Retarget: assert `TUNSTRAP_SESSION_DIR`/`TUNSTRAP_PID`/`TUNSTRAP_OUTPUT_FILE` present, `TUNSTRAP_DB_PORT` absent. |
| `test_cli_run_input_env_scrub.py:156` | `env["TUNSTRAP_DB_PORT"] == "5432"`, "the injected scalars must survive the scrub" | Retarget: assert `TUNSTRAP_SESSION_DIR` survives the scrub instead; same docstring claim, different scalar. |
| `test_cli_run_input_env_scrub.py:174` (found beyond the drill's list) | `json.loads(env[VAR])["pid"] == 99` | Retarget: `json.loads(env[VAR])["session"]["pid"] == 99` — `pid` moved under the unified structure's `session` key. |
| `test_cli_runner.py:392` (+docstring at ~360) | `"export TUNSTRAP_DB_PORT='5432'" in res.output` — existing `start --output env` pin | **Fix the existing assertion**, not just "add a test": replace with the new three-survivors-plus-kube-channel export set; drop `TUNSTRAP_DB_PORT`/`TUNSTRAP_WEB_PORT`-style lines from any fixture the test builds. |
| `test_cli_run_postspawn.py:955,993` (`test_lone_optional_node_failure_keeps_its_own_exit_code`) | Asserts `error["error"] == "MultiNodeEnvUnsupported"` for a lone optional node's failure (`connections == {}` trips `render_env`'s `!= 1` guard today) | Retarget completely, new behaviour is the opposite: `_build_child_env` no longer branches on connection count at all, so this now **succeeds** (exit 0). Rename to `test_lone_optional_node_failure_still_succeeds_with_only_a_warning`; assert exit 0, `session.warnings` (via `--output-var`) carries the "edge" failure, teardown ran exactly once. |
| `test_cli_run_output_var.py` (multiple) | See Task 5 Step 2's existing per-test list below — **unchanged by this iteration's fix**, already correct from iteration 3. | Retarget/delete per the existing list (kept). |
| `test_cli_run_output_var_projection.py` (**whole file, missed in iterations 1-3**) | `RunKubeTarget` import; `["connections"]["node"]["kube_targets"]["k3s"]`; `decoded["pid"]`/`["session_dir"]`/`["started_at"]`/`["connections"]["node"]["ports"]` | See dedicated sub-section below — this is a security-critical file (credential-scrubbing pin) and needs care, not a one-line note. |
| `test_envrender.py:4` | `from tunstrap.exceptions import MultiNodeEnvUnsupported` | Delete the import — `ruff` F401 once every user of it in this file is gone. |
| `test_envrender.py` (Task-3-era, `render_env`-dependent) | `test_render_ports_and_session`, `test_render_kube_sets_kubeconfig`, `test_render_kube_not_materialized_raises`, `test_render_requires_single_node_zero`, `test_render_requires_single_node_two`, **and explicitly `test_render_kube_env_works_for_multi_node_while_render_env_still_rejects`** (added by Task 3 itself, plan line ~360 — the general clause below missed naming this one by name in earlier iterations) | Delete all six — each asserts on `render_env`, which no longer exists. |
| `test_envrender.py::test_predicted_env_keys_matches_render_env` | Compares `predicted_env_keys` against `render_env`'s output | **Retarget, not delete** (the major fix this iteration exists to make) — see "Anti-drift guard" sub-section below. |
| `test_envrender.py` (predicted_env_keys shape) | `test_predicted_env_keys_no_kube_omits_kubeconfig`, `test_predicted_env_keys_multi_node_is_empty` | Delete — both pin the old per-target scalar enumeration / the "multi-node is empty" claim, which is false under the new unconditional `{session scalars} ∪ kube-channel` contract; superseded by Task 5's own new `test_predicted_env_keys_is_session_scalars_plus_kube_channel` (below, update its expected set to include `TUNSTRAP_OUTPUT_FILE`) and `test_predicted_env_keys_no_kube_is_just_the_two_survivors` (rename: three survivors now). |
| `test_exceptions.py:87-90` | `test_multinode_env_unsupported_is_a_tunstrap_error`-shaped subclass test | Delete. |
| `test_exceptions.py:94-99` | Exit-code + envelope test constructing `MultiNodeEnvUnsupported(...)` | Delete. |
| `test_exceptions.py:107-114` | `_EXIT_CODES[MultiNodeEnvUnsupported] == 1` table test | Delete. All **three** cases named explicitly — "whatever case pins the exit code" undercounted them twice already. |
| `test_tofu_proxy.py:375,386,397,399` | Docstrings framing the pop in terms of `inject_scalars`/`render_env` | Update docstrings only (mechanism note, not an assertion change) — both `test_tunnelled_suppresses_kubeconfig_in_child_env` and `test_tunnelled_drops_an_inherited_kubeconfig_in_the_multi_node_case`; already flagged in Task 5's earlier draft, kept here for completeness of the table. |

**Integration tier (Task 7 Step 3's "no changes needed" claim was false — corrected here and in Task 7):**

| File:line | Old shape/symbol | Disposition |
|---|---|---|
| `test_run_env_io.py:49-50` (`_PROBE_SINGLE`) | `os.environ["TUNSTRAP_WEB_PORT"]` | Retarget: probe reads `json.load(open(os.environ["TUNSTRAP_OUTPUT_FILE"]))["nodes"]["hub"]["ports"]["web"]` (a `"host:port"` string; `.rsplit(":", 1)[1]` for the port) instead. |
| `test_run_env_io.py` `_PROBE_MULTI` (same region, `envelope["connections"]`) | `envelope["connections"][name]["ports"]["web"]` | Retarget to `envelope["nodes"][name]["ports"]["web"]` (string, parse as above). |
| `test_run_env_io.py` `_PROBE_MULTI` leak check | `k.startswith("TUNSTRAP_") and k != "TUNSTRAP_INPUT"` → now **wrongly** flags the three sanctioned survivors as leaks | Retarget: exclude `TUNSTRAP_SESSION_DIR`, `TUNSTRAP_PID`, `TUNSTRAP_OUTPUT_FILE` too. |
| `test_run_env_io.py:173-193` (`test_multi_node_without_output_var_is_exit_1`) | Asserts exit 1 + `MultiNodeEnvUnsupported`, `not session_dir.exists()` | **Retarget completely — the exact behaviour this task inverts.** Rename to `test_multi_node_without_output_var_now_succeeds`; assert exit 0, no `MultiNodeEnvUnsupported` anywhere in stderr (stderr may be empty), teardown ran. Materialization's *content* is not re-verified here — that is `test_cli_run_materialize.py`'s job at unit level; this integration test's remaining job is confirming the real console script also allows the case, not re-proving the file's shape. |
| `test_cli_modes.py:111-138` | `start --output env`'s `TUNSTRAP_WEB_PORT`/`TUNSTRAP_WEB_ENDPOINT`; `run`'s child probe reading `os.environ['TUNSTRAP_WEB_PORT']` directly | Retarget both (two separate tests in this range): the `start --output env` test asserts `TUNSTRAP_SESSION_DIR`/`TUNSTRAP_OUTPUT_FILE` present, `TUNSTRAP_WEB_PORT`/`_ENDPOINT` absent, and derives the port via `json.load(open(env["TUNSTRAP_OUTPUT_FILE"]))["nodes"][...]["ports"]["web"]`; the `run` child probe (inline Python string) rewrites to read `TUNSTRAP_OUTPUT_FILE` the same way instead of `TUNSTRAP_WEB_PORT` directly. |

**E2E tier + shipped artifacts (in scope — this ships with the work; deferring is not an option, per the ruling, because the failure mode is silent: `try()` around `jsondecode` swallows the shape mismatch into an empty `config_path` and the resulting error is a confusing provider message, not an obvious test failure):**

| File:line | Old shape/symbol | Disposition |
|---|---|---|
| `tests/e2e/module/main.tf:27-28` | `try(jsondecode(var.tunstrap), { connections = {} })`; `local.tunnel.connections.node.kube_targets.k3s.path` | Retarget: `{ nodes = {} }`; `local.tunnel.nodes.node.kube.k3s.path`. Task 6 (extended). |
| `docs/recipe_terragrunt.md:287-288` | Same `tunnel`/`kubepath` locals, mirroring `main.tf` | Retarget identically. Task 6. |
| `docs/recipe_terragrunt.md:~329` | Prose: "`path` comes from... `connections.*.kube_targets.*.path`" | Retarget prose to `nodes.*.kube.*.path`. Task 6. |
| `docs/recipe_terragrunt.md:~407` | Prose: "the module picks the node out of `connections[<node>]`" | Retarget to `nodes[<node>]`; also correct the surrounding paragraph's claim that multi-node suppresses the scalar/`KUBECONFIG` channel — under the pivot the kube channel is unconditional and the "TUNSTRAP_* env... not injected" framing is stale. Task 6. |
| `docs/recipe_terragrunt.md:~509` | "What is proven" section, `--output-var` → `TF_VAR_tunstrap` → `try(jsondecode(...))` → `config_path` chain description | Mechanism description stays accurate; no shape-specific text to fix beyond confirming it still reads correctly once the two locals above change. Verify only. |
| `tests/e2e/rig.py:171` | Docstring: "`module/main.tf` decodes `connections.node.kube_targets.k3s.path`" | Retarget docstring text to `nodes.node.kube.k3s.path`. |
| `tests/e2e/test_tofu_providers.py:154` | `envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]` | Retarget to `envelope["nodes"]["node"]["kube"]["k3s"]["path"]`. |
| `tests/e2e/test_tofu_providers.py:251-254` | Fake envelope dict literal: `{"connections": {"node": {"ports": {}, "kube_targets": {"k3s": {...}}}}}` | Retarget the literal to `{"nodes": {"node": {"ports": {}, "kube": {"k3s": {"path": ..., "context": ..., "endpoint": ...}}}}}` — align field names with `UnifiedKubeRef` (drop any fields beyond `path`/`context`/`endpoint` the old literal happened to include). |
| `tests/e2e/test_terragrunt_apply.py:339,425` | `envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]` (×2, apply and tunnelled-output cases) | Retarget both to `envelope["nodes"]["node"]["kube"]["k3s"]["path"]`. |
| `tests/e2e/test_rig.py:278` | `envelope["connections"]["node"]["kube_targets"]["k3s"]` | **Unaffected, disposition = out of scope, stated explicitly, not silently skipped:** this reads `tunstrap start`'s **raw stdout JSON** (`OutputSchema.model_dump_json()`-shaped), not the `--output-var`/materialized unified channel. The pivot's scope is `run`'s consumer-facing channels (`--output-var`, materialization) and `start --output env`; `start`'s default/`--output json` stdout — documented since the pre-#15 design as "the complete envelope," a separate contract for session-management tooling, not consumer transformation — is deliberately untouched. Judgment call, recorded here since it narrows the blast radius meaningfully; if a reviewer wants `start`'s raw JSON unified too, that is a new decision, not an oversight. |
| `tests/e2e/test_recipe_terragrunt.py:259,322` | Recipe↔module drift guard (textual block comparison) | **Unaffected in mechanism.** The guard's compared *content* changes automatically once `main.tf` and the recipe are both updated to the `nodes.*` shape in Task 6 — no separate code change to the guard itself. Task 6's own steps must keep it green (run it as part of Task 6's verification, not just Task 7's). |

### `test_cli_run_output_var_projection.py` — dedicated retarget (security-critical, missed twice before this iteration)

This file pins the credential-scrubbing property for the projected kube
reference — it must not be weakened while being reshaped. All four tests
retarget or delete, **not** left alone:

- `test_output_var_never_carries_kube_private_key_material` — retarget the
  shape lookup: `json.loads(env["TF_VAR_tunstrap"])["nodes"]["node"]["kube"]["k3s"]`
  instead of `["connections"]["node"]["kube_targets"]["k3s"]`. The
  absence assertions (`client_key_data`/`client_certificate_data`/
  `content_b64` not in `target`) are unaffected in spirit, but note **the
  field set is now smaller than before for a different reason too** — see
  the next test.
- `test_output_var_keeps_every_field_the_consumer_chain_reads` — the
  anti-vacuity pair. **The expected dict shrinks further than credential
  removal alone**: `UnifiedKubeRef` carries exactly `{path, context,
  endpoint}` (Task 4's model) — `cluster_name`, `local_port`,
  `tls_server_name`, and `certificate_authority_data` (all present in the old
  `RunKubeTarget` projection, none of them credentials) are **also** gone
  under the unified shape, because the design narrows to references only
  (design doc, U4). Retarget the expected dict to exactly
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
  **delete, not retarget.** This test validated `RunKubeTarget.model_validate`
  directly, exercising `extra="ignore"`'s fail-closed behaviour against an
  untrusted dict. `RunKubeTarget` is deleted (Task 4's disposition); its
  replacement, `render_unified_output`, never calls `.model_validate()` on
  untrusted kube data at all — it constructs `UnifiedKubeRef(path=...,
  context=..., endpoint=...)` with three explicit keyword arguments, so a
  hypothetical field added to `KubeTargetOutput` later cannot leak through
  without someone editing that constructor call by hand. The allow-list
  property now holds **by construction**, not by validating against a model,
  so there is nothing left for a `model_validate`-shaped test to exercise
  differently from `test_output_var_keeps_every_field_the_consumer_chain_
  reads`'s own exact-equality assertion, which already proves the same
  property end-to-end. Confirm this by re-reading `render_unified_output`'s
  body (Task 4) before deleting — the property must actually hold, not just
  be asserted to hold by this note.

### Fetched-file materialization + `content_b64` blast-radius enumeration [R16, new]

**New mechanism, same precedent as kube.** `FetchedFile` (`schemas.py:292-313`)
gains `path: str | None = None`, mirroring `KubeTargetOutput.path`
(`schemas.py:317-336`) exactly. Wherever kube materialization currently runs
daemon/worker-side (the same call site the "Materialization write mechanism"
design-doc section and this task's `output.json` writer both point at —
confirm the exact function before implementing, do not assume it is
`manager.py:start_all_and_build_output` without checking), add a parallel
step: for each successful `FetchedFile` a node's `fetch_files` produced,
base64-decode `content_b64` and write the raw bytes to
`tunnel-data/<node>-<fetchname>` using the **same atomic-replace primitive**
as `output.json` (temp file + `O_EXCL` + `os.replace`, not `_write_file`'s
`O_TRUNC` — see "Materialization write mechanism," design doc), then set
`.path`. A failed fetch (`.error` set) materializes nothing. `content_b64`
itself is **not** deleted from `FetchedFile` — it stays internal plumbing,
same as kube's own `content_b64`. The projection itself (`{path, size,
sha256}`/`{error}`, no `content_b64`) is **not** a separate function here —
it is `render_unified_output`'s own `fetch_files` construction via the new
`UnifiedFetchRef` model, given in full in Task 4's `render_unified_output`
body (above); this materialization step is what makes `.path` non-`None` by
the time that function runs, the same ordering `KubeTargetOutput.path`
already requires and Task 4's own docstring now states explicitly.

**`content_b64` grep enumeration, repo-wide, every hit dispositioned** (per
this plan's established discipline — a table, not a promise to look later).
**[R16, iteration 8 — methodology correction.]** An earlier revision of this
enumeration ran the grep as `tunstrap/ tests/ --include='*.py'` only,
dropping the `docs/` tier and `--include='*.md'`/`'*.tf'` that iteration 4
established for the *original* blast-radius table (top of Task 5) and that
this R16-specific enumeration should have inherited rather than narrowing.
Consequence: `docs/recipe_terragrunt.md`'s own shipped "Fetched files are
exported verbatim, not projected" subsection — which argues the *opposite*
of R16 in prose — went unfound by a whole review round. Re-run at the
established scope, not the narrowed one:

```bash
grep -rn 'content_b64' tunstrap/ tests/ docs/ --include='*.py' --include='*.md' --include='*.tf'
```

**Kube-internal — unaffected by R16, listed to prove they were checked, not
missed:** `KubeTargetOutput.content_b64` (`schemas.py:335`) is a different
field entirely (the patched kubeconfig's own content, unrelated to
`fetch_files`) and is untouched by this ruling. Every hit below reads or
constructs *that* field, not `FetchedFile`'s: `test_kube_run.py:111`,
`test_envrender.py:20`, `test_output_kube.py:35`, `test_tofu_proxy.py:351`,
`test_kube_targets.py:91,147` (integration — reads `start`'s raw stdout JSON,
already out of scope per the existing carve-out), and the **absence**
assertions for kube's own `content_b64` in
`test_cli_run_output_var.py:256,281` and
`test_cli_run_output_var_projection.py:10,72,91,190,249` (these already
correctly assert kube's `content_b64` is *not* in the projected shape —
nothing to change).

**`fetch_files`-related — in scope, retarget:**

| File:line | Old shape/behaviour | Disposition |
|---|---|---|
| `test_manager_fetch.py:91` (`test_fetch_files_results_populate_node_output`) | Docstring "Fetcher results land in `NodeOutput.fetch_files` unchanged"; fixture `FetchedFile(content_b64="YQ==", size=1, sha256="ca97")`, no `path` | **False under R16** — a materialization step now runs after the fetch. Retarget: assert `out.connections["a"].fetch_files["kubeconfig"].path` is set to the expected `tunnel-data/a-kubeconfig` location and its on-disk bytes match `base64.b64decode("YQ==")`; `content_b64` still present on the object (internal plumbing, unchanged) but the test's point moves to `path`. Rename to drop "unchanged" from the docstring. |
| `test_fetcher_unit.py:101,111` | `fetcher.fetch_files()`'s own unit test, asserts `ff.content_b64` set on success | **Unaffected** — this is the SSH-fetch-to-memory layer, upstream of the new daemon-side materialization step; `fetcher.py` itself is not changed by R16, only its caller gains a new step after it. |
| `test_fetch_files.py:67,119,216` (integration) | `base64.b64decode(ff["content_b64"])` reading the raw `start` stdout envelope | **Unaffected in mechanism** (raw stdout stays the "complete envelope," existing carve-out) **but verify against the correct channel**: if any of these three actually assert against `--output-var`/materialized output rather than raw `start` JSON, that specific assertion retargets to read `ff["path"]` + a direct file read instead — confirm which channel each of the three actually exercises before deciding no change is needed; do not assume all three are raw-stdout without checking. |
| `test_fetch_security.py:49,52,69,87-89` (integration) | Proves fetched content_b64 "appears on stdout only, never on stderr" — i.e. accepts it riding *some* channel, checks which | **Retarget the property proved, not just the assertion syntax.** R16 makes a stronger claim possible: fetched content should appear **nowhere** in `--output-var`/the materialized manifest, only in the `0600` on-disk file. Rewrite to assert (a) `content_b64`/the raw fetched bytes do not appear anywhere in `TF_VAR_tunstrap`, the materialized `output.json`, stdout, or stderr; (b) the file at the reported `path` exists, is mode `0600`, and its bytes match the source. This is a **stronger** security property than the test proved before, not a weaker one — call that out in the retargeted test's docstring. |
| `test_cli_run_output_var.py:83` (`_RICH_PAYLOAD`) | `"fetch_files": {"hosts": {"content_b64": "aG9zdHM=", "size": 6, "sha256": "ab" * 32}}` | Retarget the fixture to `{"hosts": {"path": "/s/tunnel-data/node-hosts", "size": 6, "sha256": "ab" * 32}}` — this fixture feeds `test_output_var_keeps_every_field_the_consumer_chain_reads`-style field-preservation tests (the R16 instruction's explicit callout); any downstream assertion reading `fetch_files.hosts.content_b64` from the decoded var retargets to `.path`. |

**`docs/` tier — the rows the narrowed grep missed, added here:**

| File:line | Old shape/behaviour | Disposition |
|---|---|---|
| `docs/recipe_terragrunt.md:344` | Kube-drop list: "and **drops** `client_key_data`... `content_b64`... `client_certificate_data`" | **Verify only, no rewrite.** This states kube's `content_b64` is dropped from `TF_VAR_tunstrap`'s kube projection — still true under R16 (unrelated field, U4's narrowing was already in force). Confirmed accurate as written. |
| `docs/recipe_terragrunt.md:361-364` | "`tunstrap start` is not affected: it writes the complete envelope to stdout... without `--materialize` its `content_b64` is the only way to obtain the kubeconfig at all." | **Verify only, no rewrite.** Matches the existing, unchanged scope carve-out (design doc, "Compatibility") — `start`'s raw default JSON stdout is untouched by R16, for kube and (per "Fetched-file materialization," design doc) for `fetch_files` alike. Confirmed accurate. |
| `docs/recipe_terragrunt.md:366-388` (whole subsection, "### Fetched files are exported verbatim, not projected") | Argues the **opposite** of R16: "Every `fetch_files` entry keeps its `content_b64` whole"; "`FetchedFile` has no `path` (`schemas.py:292`), so dropping `content_b64` would be a silent, unrecoverable breakage"; "tunstrap fetches into the envelope (`content_b64`), not onto disk"; the materialize-then-drop end-state "is recorded in the spec's Out of scope" (false — the issue15 design's "Out of scope" does not list it; this premise is now simply wrong, not aspirational) | **REWRITE — the whole subsection.** See Task 6's new step, below, for the replacement text. This is the finding the narrowed grep missed. |
| `tests/e2e/module/main.tf:13` | Comment: "the kube target's `client_key_data`, `client_certificate_data` and `content_b64` are dropped" | **Unaffected by R16** (kube-only, unrelated field) — already inside the region Task 6 Step 0's existing shape-migration row for this file covers for the unrelated `connections.*`→`nodes.*` rename; no R16-specific change. |
| Untracked characterization harness | Fetch/kube fixtures using `content_b64` | **Out of scope, stated explicitly.** It is not part of the test suite, shipped code, or consumer documentation. |
| `docs/specs/2026-05-20-feature-fetch-files-design.md`, `docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md`, `docs/specs/2026-08-03-run-env-io-decision-history.md`, `docs/specs/2026-05-30-kube-targets-design.md`, `docs/superpowers/plans/2026-06-25-cli-run-modes.md`, `docs/superpowers/plans/2026-05-30-kube-targets.md` | Pre-#15 design/decision/plan documents for already-shipped tickets (#14 and earlier), predating this ticket by weeks | **Out of scope, historical record — cited, never edited**, matching this plan's own established treatment of the pre-#15 decision history everywhere else in this document (e.g. "the pre-#15 decision history's `fetch_files[*].content_b64` entry," cited by name, never rewritten). Editing a completed ticket's own historical spec to match a later ticket's decision would falsify the historical record of what that ticket actually shipped. |
| Untracked superseded owner-tracking design | — | **Out of scope** — historical scratch material, not live. |
| Untracked issue #15 spike notes | Kube-only `content_b64` hits (patched-kubeconfig content in the collision-test prototype) | **Unaffected by R16** (kube, not `fetch_files`) and a frozen historical spike snapshot. |
| `test_output_schema.py:25,32,46,63` | `FetchedFile(content_b64=...)` construction, xor-validation tests | **Unaffected** — these test `FetchedFile`'s own model validation (`content_b64`/`error` xor), which is unchanged; only a new optional `path` field is added, not a change to this xor. Add one new case: `path` defaults to `None`, is not part of the xor, and can be set independently after construction (mirrors `KubeTargetOutput.path`'s own test coverage, if any — check for a precedent test to mirror rather than inventing a new assertion style). |

**Schema note:** `FetchedFile`'s xor validator (`schemas.py:303-314`) does not
need new logic for `path` — it is a plain optional field set post-construction
by the new materialization step, the same relationship `KubeTargetOutput.path`
already has to that model's own required fields. Confirm this against the
actual `KubeTargetOutput` definition before implementing, not assumed from
this note alone.

**`predicted_env_keys`/anti-drift guard: checked, unaffected by R16.**
`TUNSTRAP_OUTPUT_FILE` was already one of the three unconditional survivors
`_build_child_env` injects and `predicted_env_keys` reserves for **`run`**,
not only for `start --output env` (see "The scalar channel is removed,"
design doc, and this task's own `predicted_env_keys` rewrite above) — R16's
"generalize `TUNSTRAP_OUTPUT_FILE` to `run`" instruction describes its
*role* changing (from a secondary convenience scalar to the *primary*
consumer-facing locator, now that R9's mode 2 is gone), not its *export
set membership*, which was already unconditional. No change to
`predicted_env_keys`'s formula, `_build_child_env`'s injection, or either
half of the two-part anti-drift guard (R11) is needed for R16 — verified,
not silently assumed. `fetch_files`'s own keys never touched env at all
(pre- or post-R16), so the guard's key set is untouched by the materialization
change above too.

### Anti-drift guard — retargeted, not deleted, and now two-part (R11)

**Standing ruling R1: the guard is extended, never weakened. An earlier
revision of this task deleted `test_predicted_env_keys_matches_render_env`
on the false premise that only one implementation of the injected-key set
remained after this task's rewrite. That premise is wrong**: after Task 5,
there are still **two independent implementations** of "what keys will `run`
inject" — `_build_child_env` (hardcodes `TUNSTRAP_SESSION_DIR`/
`TUNSTRAP_PID`/`TUNSTRAP_OUTPUT_FILE`, merges `render_kube_env`'s output) and
`predicted_env_keys` (Task 3's conservative formula: the three survivors,
plus all three kube names whenever any `kube_targets` are declared). If
these two silently diverge, the pre-spawn `--output-var` collision check
(`_validate_output_var`, `cli.py:311-324` — confirm the exact line against
the checked-out file) under-rejects: a NAME that collides with a key
`_build_child_env` actually injects would sail through validation and then
genuinely collide post-spawn.

**[R11 — a second, iteration-6 correction to this same guard.]** A *later*
revision retargeted the guard to a single `predicted_env_keys(schema) ==
set(actual)` full-equality assertion. That was only valid while
`predicted_env_keys` computed the *exact* cardinality-conditional key set.
Task 3 now makes `predicted_env_keys` deliberately **conservative** (reserves
all three kube names whenever any `kube_targets` exist, regardless of exact
count), so **exact equality can no longer hold in general** — a schema with
exactly one kube target that materializes cleanly now predicts all three
kube names (conservative) while the actual export has only two (`KUBECONFIG`
+ `KUBE_CONFIG_PATH`, the exact `==1` branch) — genuinely, correctly unequal.
**The guard splits into two independent tests:**

1. **Formula test** (exact equality, unit-test style — proves the
   *conservative formula itself* is implemented correctly; this is the
   already-written `test_predicted_env_keys_reserves_all_three_for_one_
   kube_target` / `..._two_kube_targets_one_node` pair from Task 3, not
   repeated here).
2. **Safety-envelope test** (subset, the actual anti-drift guard — proves
   the conservative reservation still covers whatever *actually* gets
   injected, even when cardinality shrinks between input and output):

```python
def test_predicted_env_keys_covers_actual_injected_keys_under_cardinality_shrink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[R11] Safety-envelope half of the two-part anti-drift guard: predicted
    must be a superset of actual, driven by the exact scenario that falsifies
    a predictor that got the conservatism backwards -- two kube targets
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

This test needs `_build_child_env` (Task 5), so it is added here, in Task 5,
alongside `_build_child_env`'s own implementation — not in Task 3, where
`predicted_env_keys`'s formula lands but `_build_child_env` does not yet
exist. Task 3's own two formula tests (above) are sufficient at that point;
this safety-envelope test is the piece that specifically needs both sides to
exist simultaneously.

**Files** (updated for the full blast radius; the earlier draft of this task
covered only the first three rows):
- Modify: `tunstrap/cli.py`, `tunstrap/envrender.py` (delete `render_env`,
  retarget the anti-drift guard's sibling code), `tunstrap/exceptions.py`
  (delete `MultiNodeEnvUnsupported`)
- Test (unit): `tests/unit/test_cli_run_output_var.py`,
  `tests/unit/test_cli_run_output_var_projection.py`,
  `tests/unit/test_cli_run_materialize.py` (new),
  `tests/unit/test_cli_run.py`, `tests/unit/test_cli_run_input_env_scrub.py`,
  `tests/unit/test_cli_runner.py`, `tests/unit/test_cli_run_postspawn.py`,
  `tests/unit/test_envrender.py`, `tests/unit/test_exceptions.py`,
  `tests/unit/test_tofu_proxy.py` (docstrings only)
- Test (integration): `tests/integration/test_run_env_io.py`,
  `tests/integration/test_cli_modes.py`
- Test/artifact (e2e, if the e2e tier is exercised — Task 6 owns the actual
  edits since they land alongside the recipe, but they are enumerated here
  because they are this task's blast radius, not new scope):
  `tests/e2e/module/main.tf`, `tests/e2e/rig.py`,
  `tests/e2e/test_tofu_providers.py`, `tests/e2e/test_terragrunt_apply.py`

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
    """Multi-node input with NO --output-var now succeeds -- was exit 1
    MultiNodeEnvUnsupported before this task; materialization covers
    multi-node unconditionally so the opt-in gate has nothing left to force."""
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
    KUBE_CONFIG_PATH/_PATHS too, not just KUBECONFIG -- see decision history
    #7: those are the names the providers actually read."""
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

- [ ] **Step 2: Retarget every pre-existing test pinning the removed machinery**

**`tests/unit/test_cli_run_output_var.py`** — this file's whole premise (the
scalar/`--output-var` interaction) partly no longer exists. Retarget or
delete:

- `test_collision_with_injected_scalar_is_usage_error` — pins
  `--output-var TUNSTRAP_DB_PORT` colliding with an injected scalar.
  `TUNSTRAP_DB_PORT` is no longer ever injected (no scalars), so this
  collision can no longer occur. **Delete this test**, not retarget — there
  is no equivalent behaviour to assert once the collision class it tested is
  gone.
- `test_non_colliding_tunstrap_prefixed_name_is_accepted` — asserts a
  `TUNSTRAP_`-prefixed `--output-var` NAME is accepted because only *some*
  `TUNSTRAP_` keys are protected. Retarget: rename to
  `test_tunstrap_prefixed_output_var_name_is_accepted` and drop the "only
  some are protected" framing from the docstring — under the new contract no
  `TUNSTRAP_<TARGET>_*` key exists to be protected from at all; the test's
  only remaining job is confirming `--output-var TUNSTRAP_ANYTHING` is not
  specially rejected just for the prefix.
- `test_multi_node_without_output_var_is_exit_1_pre_spawn` — pins the exact
  behaviour Step 1's new `test_multi_node_run_succeeds_without_output_var`
  inverts. **Delete this test**; it is superseded by the new one, not
  retargetable (the assertion is the literal opposite).
- `test_multi_node_with_output_var_reaches_spawn` — still valid in spirit
  (multi-node + `--output-var` reaches `spawn_daemon`) but its docstring
  ("until it lands render_env would still reject a two-node envelope
  post-spawn") describes removed code. Update the docstring only; the
  assertions are unaffected (it never inspects env content, only that
  `spawn_daemon` was reached).
- `test_output_var_carries_the_whole_envelope_minus_kube_credentials` — pins
  the **old** shape (`connections.<node>.ports.<target>` as an int,
  `RunKubeTarget`'s exact field set). **Retarget in place**: rename to
  `test_output_var_carries_the_unified_structure_minus_kube_credentials`,
  replace `_RICH_PAYLOAD`'s expected-shape assertions with the unified
  shape's (`nodes.node.ports.db == "127.0.0.1:5432"`,
  `nodes.node.kube.k3s == {"path": ..., "context": ..., "endpoint": ...}`,
  `nodes.node.fetch_files.hosts.sha256 == ...`), keep the credential-absence
  assertions (`client_certificate_data`/`client_key_data`/`content_b64` still
  must not appear anywhere in the decoded payload) — that property is
  unchanged, only the container shape is.
- `test_single_node_keeps_scalars_alongside_output_var` — pins
  `TUNSTRAP_DB_PORT`/`TUNSTRAP_DB_ENDPOINT` in the child env. **Delete**; no
  scalars survive to keep "alongside" anything except
  `TUNSTRAP_SESSION_DIR`/`TUNSTRAP_PID`, already covered by
  `test_child_env_without_output_var_is_unchanged` below.
- `test_multi_node_injects_output_var_and_no_scalars` — retarget: the
  "no scalars" half is now trivially true (nothing produces them), so the
  test's remaining job is confirming the unified structure carries both
  nodes correctly; update its body to decode `render_unified_output`'s shape
  (`nodes` keyed by `"a"`/`"b"`) instead of the old `OutputSchema.connections`
  shape, keep the `leaked` scalar-absence assertion (still a real guard
  against a regression that reintroduces target-scoped scalars).
- `test_multi_node_suppression_uses_input_count` (**already retargeted once**,
  in iteration 2, to `test_multi_node_suppresses_scalars_but_exports_kube_channel`)
  — retarget **again**: its `leaked = [...TUNSTRAP_...]; assert leaked == []`
  assertion no longer describes a real guard (there is no
  `inject_scalars`/input-node-count decision left to get wrong — the kube
  channel is unconditional by construction after this task, so there is
  nothing left to falsify). Rename to
  `test_optional_node_failure_does_not_affect_kube_channel_or_unified_output`
  and rewrite the body to assert: the kube channel still fires for the one
  surviving connection (`KUBECONFIG`/`KUBE_CONFIG_PATH` present), and the
  unified structure (if `--output-var` given) reflects only the surviving
  node (`"b"` absent from `nodes`, its failure visible in
  `session.warnings`). Drop the `leaked` assertion entirely — nothing
  produces `TUNSTRAP_`-prefixed target scalars anymore, so asserting their
  absence is now asserting a tautology, not a guard.
- `test_child_env_without_output_var_is_unchanged` — retarget: the expected
  `injected` dict shrinks to exactly `{"TUNSTRAP_SESSION_DIR": "/s",
  "TUNSTRAP_PID": "99", "TUNSTRAP_OUTPUT_FILE": "/s/tunnel-data/output.json"}`
  (drop `TUNSTRAP_DB_HOST`/`_PORT`/`_ENDPOINT` from the expected dict; this
  node has no kube_targets in its fixture, so no kube keys are expected
  either). Docstring updated to say "the three survivors, session metadata
  only." Widen the `injected` filter (`k.startswith(("TUNSTRAP_",
  "KUBECONFIG"))`) is already broad enough to catch `TUNSTRAP_OUTPUT_FILE`
  automatically — no filter change needed, only the expected dict.

**`tests/unit/test_envrender.py`** — delete every `render_env`-specific test
**by name** (the blast-radius table above lists these; restated here as the
concrete instruction): `test_render_ports_and_session`,
`test_render_kube_sets_kubeconfig`, `test_render_kube_not_materialized_raises`,
`test_render_requires_single_node_zero`, `test_render_requires_single_node_two`,
and **`test_render_kube_env_works_for_multi_node_while_render_env_still_rejects`**
(added by Task 3 itself — do not miss this one, it was undercounted in an
earlier revision of this plan). Also delete the now-unused module-level
`from tunstrap.exceptions import MultiNodeEnvUnsupported` import
(`test_envrender.py:4` — `ruff` F401 once nothing in the file uses it) and
`test_predicted_env_keys_no_kube_omits_kubeconfig`,
`test_predicted_env_keys_multi_node_is_empty` (both pin the old per-target
enumeration / the old "multi-node predicts nothing" claim, superseded below).

**Do NOT delete `test_predicted_env_keys_matches_render_env`.** An earlier
revision of this plan deleted it on the false premise that only one
implementation of the injected-key set remained after this task — false, see
"Anti-drift guard — retargeted, not deleted, and now two-part (R11)" above,
which is the actual, correct disposition and supersedes this paragraph if the
two ever disagree. **[R11]** Retarget it in place to
`test_predicted_env_keys_covers_actual_injected_keys_under_cardinality_
shrink` (the safety-envelope half; code given in full above) — not to a
single full-equality test named `..._matches_actual_injected_keys`, which was
this same paragraph's own iteration-4/5 name and is stale now that
`predicted_env_keys` is conservative rather than exact (see R11). Add it
here, in `test_envrender.py`, not as a new file.

Replace the two deleted "shape" tests with:

```python
def test_predicted_env_keys_is_session_scalars_plus_kube_channel() -> None:
    """[R11] predicted_env_keys collapses to the three survivors + the
    CONSERVATIVE kube channel (all three names, not just the >=2 branch that
    this input's exact declared cardinality would exactly hit) -- there is no
    other injected key left, and the formula does not vary by exact count."""
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

**`tests/unit/test_exceptions.py`**: delete **all three** `MultiNodeEnvUnsupported`
cases by name, not "whatever case pins the exit code" (an earlier revision of
this plan undercounted these twice): the subclass check (`:87-90`,
`issubclass(MultiNodeEnvUnsupported, TunstrapError)`), the exit-code +
envelope test (`:94-99`, constructs an instance and checks
`to_error_output()["error"]`), and the `_EXIT_CODES` table test (`:107-114`,
`_EXIT_CODES[MultiNodeEnvUnsupported] == 1`).

**`tests/unit/test_tofu_proxy.py`**: the two `suppress_kubeconfig`-related
tests (`test_tunnelled_suppresses_kubeconfig_in_child_env`,
`test_tunnelled_drops_an_inherited_kubeconfig_in_the_multi_node_case`) keep
their assertions unchanged (still correct: `KUBECONFIG` still must not leak)
but their fixtures currently rely on single-node-vs-multi-node framing in
their docstrings ("For single-node the post-injection pop already removes
KUBECONFIG... For multi-node... inject_scalars=False") — update both
docstrings to drop the `inject_scalars` framing entirely (there is no
branch left to describe; one unconditional pop covers every case, as
iteration 2's side note already anticipated).

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
(`envrender.py:13`, mirroring the same fix in `test_envrender.py:4`). Rewrite
`predicted_env_keys`:

```python
def predicted_env_keys(schema: InputSchema) -> set[str]:
    """Env keys ``run`` will inject for this *input* schema, unconditional on
    node count: the three session scalars, plus -- [R11] conservatively, not
    per the exact _kube_channel_keys(count) branch -- all three kube names
    whenever any node declares kube_targets at all. Input cardinality can
    shrink by output time (an optional node/target can fail without failing
    the run), so predicting the exact branch would under-reserve; see the
    "Anti-drift guard" section for the cardinality-shrink case this guards
    against. Used pre-spawn to reject a colliding --output-var NAME before a
    daemon exists.
    """
    keys = {"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE"}
    if any(node.kube_targets for node in schema.nodes.values()):
        keys |= {"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"}
    return keys
```

This is the same conservative rule Task 3 already gave `predicted_env_keys`
(above, in Task 3 Step 4) — Task 5 does not re-derive it, it only drops the
scalar half's `if len(schema.nodes) == 1:` per-target block per that task's
own "Note, still valid" callout. An earlier draft of this Task 5 rewrite
re-introduced `_kube_channel_keys(total_kube)` (the exact per-count branch)
here by mistake, which would have silently reverted the R11 fix for every
caller that hits this later body instead of Task 3's; fixed in place.

`tunstrap/session.py`: confirm `SessionDir._write_file`'s exact signature
(`session.py:132` per the design doc's citation) before Step 4 item 4 below.
**[R13] It is not a drop-in reuse** — `_write_file` is mode-fixed-at-creation
but not atomic (`O_TRUNC`, no rename step), while materialization needs true
atomicity too (temp file + `os.replace`); check whether `_write_file` can be
refactored into a shared atomic-replace helper both call sites use, or
whether the primitive is replicated inline in `cli.py` instead — see item 4.

`tunstrap/cli.py`:

1. **Remove the `inject_scalars` parameter from all three places that thread
   it, named explicitly (an earlier revision of this plan hedged with
   "whichever of these two names is correct" — both exist, and a third does
   too):**
   - `_build_child_env` (`cli.py:365-372`, the parameter declaration) —
     remove the parameter and its `if inject_scalars:` branch (`cli.py:399`).
   - `_run_child` (`cli.py:466-474`, parameter; `cli.py:486`, passed through
     to `_build_child_env`).
   - `_supervise_child` (`cli.py:513-521`, parameter; `cli.py:543`, passed
     through to `_run_child`).
   - `run_command` (`cli.py:648`, `inject_scalars = len(schema.nodes) == 1`
     — delete the line entirely; `cli.py:702`, the keyword argument passed
     to `_supervise_child` — delete it from the call).
   Confirm all four sites against the checked-out file rather than trusting
   these line numbers verbatim — they are a reading of the pre-iteration-4
   tree and may have shifted by the time Tasks 1-4 land ahead of this one.
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
    """The deterministic path materialize_output writes to; shared so
    _build_child_env's TUNSTRAP_OUTPUT_FILE and the actual writer never
    independently compute a different path for the same file."""
    return str(Path(session_dir) / "tunnel-data" / "output.json")
```

   No branch, no `inject_scalars` parameter anywhere in the call chain.
4. **[R13] Materialization writer — true atomic replace, not
   write-then-chmod, and not `O_TRUNC` alone.** `SessionDir._write_file`'s
   real property is **mode-fixed-at-creation** (`session.py:132`,
   `os.open(path, O_CREAT | O_WRONLY | O_TRUNC, 0o600)`, no separate
   `chmod`) — **not** "atomic" in the sense that matters here: `O_TRUNC`
   overwrites the file *in place*, visible mid-write to a concurrent reader.
   `Path.write_text()` + `.chmod(0o600)` is even worse (a real,
   umask-dependent `0644` window before `chmod` closes it). Neither is
   sufficient on its own: this write needs *both* mode-fixed-at-creation
   *and* true atomicity. **[R16, iteration 8 — rationale re-grounded, not
   just retargeted.]** An earlier revision justified the atomicity
   requirement by a `file()` call in a consumer's HCL racing a `run` restart
   rewriting the *same pinned path* — that race is retired under R16 (design
   doc, "Delivery," mode 2 is gone; `TUNSTRAP_OUTPUT_FILE` names a fresh
   per-invocation path, written before the child spawns, so nothing reads it
   concurrently with this write today). The requirement **stays** — strictly
   safer than `O_TRUNC`, costs nothing — on grounds that do not depend on
   that retired race: (1) torn-read prevention if this process is killed
   mid-write (a truncated file at the final path is indistinguishable from a
   valid short one to a naive reader; `os.replace` guarantees only a complete
   old or complete new file is ever observable); (2) defense-in-depth against
   any future change that reintroduces a stable/reusable path; (3) the
   fetched-file materialization writer (Task 5's "Fetched-file
   materialization" subsection, below) shares this exact primitive, so one
   atomic-replace helper is reasoned about once, not twice. Use a temp file +
   rename:

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

   `O_EXCL` on the temp file guards against a colliding temp name (the mode
   is already fixed at creation, same as the existing primitive); `os.replace`
   is the atomic step — a single filesystem rename, so a reader can never
   observe a partial write. If `SessionDir._write_file` can be refactored
   into something callable without a live `SessionDir` instance (this
   writer runs in the CLI **parent** process, `run_command`, which holds no
   `SessionDir` — kube materialization happens daemon/worker-side, inside
   the process that does own one), factor the atomic-replace primitive
   above into a small shared helper in `session.py` both call sites use;
   otherwise replicate it in `cli.py` as shown — do not describe this as
   "reusing `_write_file`" if the code is not actually shared, since the
   temp-file + `os.replace` step is new work `_write_file` does not
   currently do at all.
   
   Placed in `run_command`'s success path — the same place `_build_child_env`
   is already called, inside the `try` that owns teardown (design spec
   `2026-07-31-run-env-io-and-tofu-proxy-design.md`'s "Cleanup must own the
   whole post-spawn window" invariant applies here too: writing this file is
   new work in that same protected window, so it must go inside the existing
   `try`, not before it) — unconditionally (regardless of `--output-var`,
   regardless of node count). **Confirm the exact `run_command` call site
   against the checked-out `cli.py`** — the pre-#15 design's line numbers for
   this function have already drifted once (`cli.py:302-308` in that design's
   own text vs. later citations in this plan at `cli.py:640`/`cli.py:648`),
   so re-resolve by reading the function, not by trusting a stale citation.
5. **`start_command`'s `--output env` mode** (`cli.py:204-206`,
   `sys.stdout.write(format_exports(render_env(out)))`) is the **other**
   caller of `render_env` — deleting the function without touching this call
   site breaks `start` outright (`NameError`), not just a stale test. `start`
   also now materializes under `--output env` (only) — mirroring `run`'s
   conditional materialization: it already
   forces `daemon.materialize` under `--output env`, per `cli.py:191`'s
   `force_materialize=(output_fmt == "env")`, so the kube files already land
   on disk; extend that to also write `output.json` via the same
   `_materialized_output_path`/secure-write helper from item 4). Update the
   `--output env` branch to build the same three-survivors-plus-kube-channel
   mapping `_build_child_env` now uses:
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
   Fix the existing test pinning this mode (`test_cli_runner.py:392`, see the
   blast-radius table) in place — do not just add a new test alongside a
   stale one.

   **[R13] Stdin-mode guard — a real reachable failure, not a theoretical
   one.** `--output env` forces `daemon.materialize = True` only for **flag
   mode** (`build_flag_schema`'s `force_materialize=(output_fmt == "env")`,
   `cli.py:191`); a **stdin**-supplied payload's own `daemon.materialize` is
   the caller's explicit statement and `_pick_start_input_schema` leaves it
   alone (`cli.py:160-174`, docstring: *"a stdin payload's daemon.materialize
   is the caller's own statement and is left alone"*). A stdin payload that
   declares `kube_targets` with `materialize: false` under `--output env`
   therefore reaches the now-unconditional `render_kube_env(out)` call with
   `target.path is None` for that target, which raises a bare `ValueError` —
   an ugly traceback, not a typed error. **Fix, before wiring the unconditional
   call above:** either (a) force `daemon.materialize = True` for the stdin
   path too when `output_fmt == "env"`, matching flag mode's own precedent
   (simplest, and consistent — `--output env` needs materialized kube paths
   regardless of input channel), or (b) catch `ValueError` around the
   `render_kube_env` call in this branch and re-raise as a typed
   `TunstrapError` subclass with a clear message. **Choose (a)** unless a
   reviewer specifically wants materialization to stay an operator opt-out
   even under `--output env` — it is the smaller change and matches the
   existing flag-mode precedent exactly. Add a unit test: stdin payload,
   `daemon.materialize: false`, `kube_targets` declared, `--output env` →
   either exit 0 with the kube path materialized (if (a)), or a typed error
   (if (b)) — never a bare `ValueError` traceback.

- [ ] **Step 5: Run to verify pass, then the full suite**

`.venv/bin/pytest tests/unit/test_cli_run_output_var.py tests/unit/test_cli_run_output_var_projection.py tests/unit/test_cli_run_materialize.py tests/unit/test_cli_run.py tests/unit/test_cli_run_input_env_scrub.py tests/unit/test_cli_runner.py tests/unit/test_cli_run_postspawn.py tests/unit/test_envrender.py tests/unit/test_exceptions.py tests/unit/test_tofu_proxy.py -v`
Expected: **all pass, and only after every row of the blast-radius table
above has actually been applied** — this is not "full pass" as a hope, it is
"full pass" as the definition of this step being done; a partial pass with a
handful of still-red tests means a table row was skipped, not that the row
was optional.

`.venv/bin/pytest tests/unit -q`
Expected: full pass. Read the actual count; do not compare against any
number recorded in this plan or the spike findings — both predate this
task's deletions and retargets.

- [ ] **Step 6: Commit**

```bash
git add tunstrap/cli.py tunstrap/envrender.py tunstrap/exceptions.py tunstrap/session.py \
  tests/unit/test_cli_run_output_var.py tests/unit/test_cli_run_output_var_projection.py \
  tests/unit/test_cli_run_materialize.py tests/unit/test_cli_run.py \
  tests/unit/test_cli_run_input_env_scrub.py tests/unit/test_cli_runner.py \
  tests/unit/test_cli_run_postspawn.py tests/unit/test_envrender.py \
  tests/unit/test_exceptions.py tests/unit/test_tofu_proxy.py
git commit -m "feat: unified output materialization; remove TUNSTRAP_* scalars, MultiNodeEnvUnsupported, inject_scalars (#15)"
```

**Note:** `tunstrap/session.py` is only in this commit if Step 4 item 4 added
a small shared secure-write helper there; omit it if `SessionDir._write_file`
was reusable as-is.

- [ ] **Step 7: Integration retargets**

The blast-radius table's integration rows are their own step, not folded
into Task 7's gate pass — they are behavioural retargets (TDD-shaped: they
can fail against the old code and must pass against the new), not a
verification-only pass.

**Files:**
- Test: `tests/integration/test_run_env_io.py`, `tests/integration/test_cli_modes.py`

Apply every integration row from the blast-radius table above:
`_PROBE_SINGLE`/`_PROBE_MULTI` read `TUNSTRAP_OUTPUT_FILE` instead of
`TUNSTRAP_WEB_PORT`; the "no scalar leak" check excludes the three sanctioned
survivors; `test_multi_node_without_output_var_is_exit_1` is renamed and
inverted to `test_multi_node_without_output_var_now_succeeds`;
`test_cli_modes.py`'s `start --output env` test and `run`'s child probe both
retarget to `TUNSTRAP_OUTPUT_FILE`. Run (requires Docker, per
`tests/README.md`):

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/integration -m integration -q -k "run_env_io or cli_modes"
```

Expected: FAIL before the retargets (old assertions against new behaviour),
PASS after. Commit:

```bash
git add tests/integration/test_run_env_io.py tests/integration/test_cli_modes.py
git commit -m "test(integration): retarget env-shape assertions for the unified output contract (#15)"
```

---

### Task 6: Recipe documentation + e2e artifact shape migration (work item 4, extended by the pivot)

**This task also carries the e2e-tier and shipped-recipe rows of Task 5's
blast-radius table** — they land here, not in Task 5, because they are the
same textual shape migration as the new recipe content this task writes, and
`test_recipe_terragrunt.py`'s drift guard requires the recipe and
`tests/e2e/module/main.tf` to move together or it fails by design.

**Files:**
- Modify: `docs/recipe_terragrunt.md`, `tests/e2e/module/main.tf`,
  `tests/e2e/rig.py`, `tests/e2e/test_tofu_providers.py`,
  `tests/e2e/test_terragrunt_apply.py`

- [ ] **Step 0: Fix the recipe's pre-existing `connections.*` shape (before adding new content)**

The recipe already contains working HCL/prose in the old shape, predating
this pivot — fix these **in place** before Step 1/2 add anything new, so the
document is never left in a self-contradictory state (old shape in one
section, new shape in another):

- `docs/recipe_terragrunt.md:287-288` — the `tunnel`/`kubepath` locals:
  `try(jsondecode(var.tunstrap), { connections = {} })` →
  `try(jsondecode(var.tunstrap), { nodes = {} })`;
  `local.tunnel.connections.node.kube_targets.k3s.path` →
  `local.tunnel.nodes.node.kube.k3s.path`.
- `docs/recipe_terragrunt.md:~329` — prose point 3, "`path` comes from the
  materialized file... `connections.*.kube_targets.*.path`" → retarget to
  `nodes.*.kube.*.path`.
- `docs/recipe_terragrunt.md:~407` — "The input variable is scrubbed"
  section: "the module picks the node out of `connections[<node>]`" →
  `nodes[<node>]`; also correct the surrounding paragraph's claim that
  multi-node input suppresses the scalar/`KUBECONFIG` channel entirely — that
  was true pre-pivot and is false now (the kube channel is unconditional;
  only the `TUNSTRAP_<TARGET>_*` scalars, which no longer exist as a concept,
  were ever suppressed for multi-node).
- `docs/recipe_terragrunt.md:~509` — "What is proven" section: verify only,
  no shape-specific text to change (the `--output-var` → `TF_VAR_tunstrap` →
  `jsondecode` → `config_path` chain description stays accurate once the two
  locals above change).

**`tests/e2e/module/main.tf:27-28`** — the exact chain the e2e tier proves,
mirroring the recipe: `try(jsondecode(var.tunstrap), { connections = {} })`
→ `{ nodes = {} }`; `local.tunnel.connections.node.kube_targets.k3s.path` →
`local.tunnel.nodes.node.kube.k3s.path`. Update the module's own header
comment (`main.tf:1-9`, "The exact chain this tier exists to prove") to match.

**`tests/e2e/rig.py:171`** — docstring: "`module/main.tf` decodes
`connections.node.kube_targets.k3s.path`" → retarget to `nodes.node.kube.
k3s.path`.

**`tests/e2e/test_tofu_providers.py:154`** —
`envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]` →
`envelope["nodes"]["node"]["kube"]["k3s"]["path"]`.

**`tests/e2e/test_tofu_providers.py:251-254`** — the fake envelope dict
literal (`"connections": {"node": {"ports": {}, "kube_targets": {"k3s":
{...}}}}}`) → retarget to `{"nodes": {"node": {"ports": {}, "kube": {"k3s":
{"path": ..., "context": ..., "endpoint": ...}}}}}`, aligning the literal's
field names with `UnifiedKubeRef` (drop any field beyond `path`/`context`/
`endpoint` the old literal happened to carry — this fixture only needs
enough to exercise the dead-cluster negative-control scenario it drives).

**`tests/e2e/test_terragrunt_apply.py:339,425`** —
`envelope["connections"]["node"]["kube_targets"]["k3s"]["path"]` (apply and
tunnelled-output cases) → `envelope["nodes"]["node"]["kube"]["k3s"]["path"]`
at both sites.

**Not touched, disposition recorded (from Task 5's table, restated for
completeness at the point where a reader would otherwise expect to find
them fixed):** `tests/e2e/test_rig.py:278` reads `start`'s raw stdout JSON,
which is out of the pivot's scope (design doc judgment call); `tests/e2e/
test_recipe_terragrunt.py`'s drift guard needs no code change — its compared
content updates automatically once the steps above land.

- [ ] **Step 0b: [R16, iteration 8, new] Rewrite `docs/recipe_terragrunt.md:366-388`
  — the "Fetched files are exported verbatim, not projected" subsection**

Found by the widened `content_b64` grep (Task 5's enumeration, "docs tier"
rows) — this shipped subsection currently argues the **opposite** of R16's
shipped behaviour (fetch content stays whole in the envelope, `FetchedFile`
has no `path`, dropping `content_b64` would be "a silent, unrecoverable
breakage"). Fix in place, same "before Step 1/2 add anything new" discipline
as Step 0 above — this is pre-existing content, not new content Step 2 adds.
Replace the entire subsection (heading through the final paragraph ending
"...is recorded in the spec's 'Out of scope'.") with:

> ### Fetched files are materialized, not carried in the envelope
>
> The projection above (kube) and this one (`fetch_files`) now follow the
> same rule: `run` materializes content to disk under the session dir's
> `tunnel-data/`, mode `0600`, and the consumer-facing envelope carries only
> a reference to it. Each `fetch_files` entry becomes `{path, size, sha256}`
> on success, `{error}` on failure — never `content_b64`.
>
> This supersedes the asymmetry an earlier revision of this document
> described: `FetchedFile` **now has a `path`** (`schemas.py`, extended for
> this ticket), so the "dropping `content_b64` would be a silent,
> unrecoverable breakage" premise that justified keeping content in the
> envelope no longer holds — the lossless on-disk alternative that argument
> said was missing now exists, the same way it already existed for kube.
>
> **The plan-file-persistence risk this asymmetry existed to warn about is
> resolved as a class, not documented around**: since fetched content never
> enters `TF_VAR_tunstrap` or the materialized file at all, `--fetch`ing a
> secret no longer risks it landing in a saved Terraform plan file through
> this channel. Read the file directly at `fetch_files.<name>.path` if you
> need its contents.

Also update the one adjacent sentence this rewrite does not itself replace:
the "One other free-form string rides this channel unprojected:
`warnings[*].error`" paragraph immediately after (line ~390) stays accurate
as written — `warnings[*].error` is unrelated to `fetch_files` — but confirm
after the edit that "One other" still reads correctly given the preceding
subsection no longer describes `fetch_files` as unprojected at all (the
prior subsection's own "unprojected" framing is what "other" was
contrasting against); reword the transition if it no longer parses, do not
leave a dangling "other."

Verify lines 344 and 361-364 (the kube-drop list and the `start` carve-out,
immediately above this subsection) need **no** edit — both already state
kube-specific `content_b64` facts unaffected by R16, confirmed in Task 5's
enumeration table.

Run (requires `kind`/`tofu`/`kubectl`/Docker, per `tests/README.md` —
optional locally, mandatory before merge per Task 7 Step 4):

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/e2e -m e2e -q
```

Commit this shape-migration half separately from the new recipe content
below, so a reviewer can see "shape rename, no behaviour change" and "new
recipe content" as two distinct, independently reviewable diffs:

```bash
git add tests/e2e/module/main.tf tests/e2e/rig.py tests/e2e/test_tofu_providers.py \
  tests/e2e/test_terragrunt_apply.py
git commit -m "test(e2e): retarget to the unified output nodes.*.kube.*.path shape (#15)"
```

- [ ] **Step 1: Add Mode A — env-native kube [R12, rewritten from "kube-only recipe"]**

Add a new section (placement: after the existing provider-config example, so
it reads as "and here is the identity-delivery contract that example
depends on") titled around **Mode A: env-native kube (satisfies the
ticket's strict "nothing live enters Terraform" contract)**, per the design
doc's "Documentation" section, Mode A:

1. `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` from tunstrap's own process
   environment (no `var.`-bound value, no file read in HCL at all for kube)
   **plus a literal `config_context = "tunstrap-<node>-<target>"` per
   provider alias** — a **two-alias worked HCL example**, citing findings #3
   and #5 by number:
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
2. Explicit warning: never derive `config_context`'s value from
   `var.tunstrap` or any decoded data — literal only, matching the
   deterministic naming scheme exactly.
3. A short "measured facts a consumer needs" list, restated (not
   re-derived) from **all six** of the ticket's own findings and this
   design's own provider findings — an earlier revision of this list, despite
   its own header claiming all six, cited only four; do not repeat that
   miscount:
   - **#1** — provider configuration **is** re-evaluated at apply.
   - **#2** — outputs **freeze silently** — the worst failure mode, name it
     as such.
   - **#3** — per-alias `config_context` works with an env-supplied
     kubeconfig path (Mode A's own basis, shown in item 1's example).
   - **#4** — plan-safe end to end, measured live: plan with one set of
     ports, mutate only the kubeconfig, apply the *saved* plan → the alias
     uses the mutated value, zero plan-variable mismatch — the e2e-level
     confirmation Mode A's env-native path really is plan-safe.
   - **#5** — `KUBE_CONFIG_PATHS` is colon-separated (comma silently falls
     back to `localhost:80`).
   - **#6** — a live value bound to a `var.` **does** trip "Mismatch between
     input and plan variable value" on a saved plan (Mode B's one-shot rule,
     below, rests on this).
   - A live value bound to a **resource attribute** (not a provider config
     block) produces `Error: Provider produced inconsistent final plan` —
     cite the committed provider-precedence spec's Q3
     result, and show the provider-block placement as the only supported
     shape in both Mode A and Mode B.
4. A one-line pointer to the deterministic naming scheme
   (`tunstrap-<node>-<target>`) and why it matters for anyone piping the
   materialized kubeconfig into `kubectl --context` directly instead of
   through a provider.

- [ ] **Step 2: Add Mode B — unified-file convenience [R16, iteration 7 — rewritten again, R12's version retracted]**

Immediately after Step 1's section (same document — a real consumer may use
Mode A for kube and Mode B for ports in the same module), add a section
titled around **Mode B: unified-file convenience (ports + kube references;
does NOT satisfy the ticket's strict contract — state this plainly)**, per
the design doc's "Documentation" section, Mode B (iteration 7 text). **[R16]
No literal, operator-pinned path and no `var.tunstrap_session_dir`/
variable-derived locator anywhere in this section** — two earlier revisions
of this recipe each used one of those two unsound patterns in turn (a
locator built from `var.tunstrap`; then a literal pinned `--session-dir`
path); both are retracted, not adapted, per decision history entry 19 (which
itself supersedes entry 14's pinned-path decision):

5. **The shape**, with a worked HCL example using the env-carried
   `TUNSTRAP_OUTPUT_FILE` locator via Terragrunt's `get_env(...)` — no
   `--session-dir` precondition, no operator-agreed path, because the
   session dir stays ephemeral unconditionally (design doc, "Session dir:
   ephemeral, but not optional"):
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
   never through an `output`, per the stability contract's finding-#2
   warning.
6. **Ports lose their integer form** (`"host:port"` string) — show the
   extraction idiom explicitly:
   ```hcl
   locals {
     service1_port = split(":", local.tunnel.nodes.node1.ports.service1)[1]
   }
   ```
7. **[R16] The stability contract**, restated plainly and matching the
   design doc's "Stability contract" word-for-word on the load-bearing
   claims: **both** Mode B forms — item 5's `TUNSTRAP_OUTPUT_FILE` form and
   the `--output-var` (`var.tunstrap`) form — are **one-shot `plan && apply`
   only**, no saved-plan reuse across a tunstrap restart for either, no
   locator exemption of any kind (the check compares the variable's whole
   value; the file itself is deleted at teardown alongside the rest of
   `tunnel-data/`). An earlier revision of this item claimed item 5's form
   was unconditionally plan-safe given a `--session-dir` precondition — that
   precondition and the plan-safety it bought are both retracted; there is
   no remaining path-pinning mechanism. State this as plainly as the design
   doc does: *"Neither Mode B form survives a tunstrap restart. If you need
   a saved plan to apply cleanly against fresh ports or fetched-file content,
   re-run plan in the same tunstrap invocation."* Cite findings #1, #2 and #6
   by number.
8. **The `jsondecode`-not-JavaScript note** (U5), one sentence: consumption
   is via HCL's `jsondecode`; there is no JS runtime anywhere in this stack.
9. **[R16] The `fetch_files` warning is retracted, not carried forward.**
   Two earlier revisions of this recipe disagreed on whether to keep this
   warning (one dropped it, the next restored it while reshaping the
   payload); iteration 7 resolves it as a class instead of restating it:
   fetched content no longer rides `--output-var` or the materialized file at
   all — only `{path, size, sha256}` does (design doc, "Fetched-file
   materialization" and "Compatibility"). State instead: *"Fetched file
   content never enters a Terraform variable or plan file — only its path,
   size, and checksum do. Read the file itself at `fetch_files.<name>.path`
   if you need its contents."*

Match the existing file's structure (numbered/lettered subsections, HCL code
fences, "Measured Terragrunt facts"-style attribution footers) — read the
file's current shape before writing, do not introduce a new prose style.

- [ ] **Step 3: Cross-check against the two artifacts, the design doc, and the drift guard**

Confirm every measured fact restated in both new sections matches the committed
provider-precedence spec, the ticket's
own six findings, and the design doc's "Stability contract" subsection
verbatim — no rewording that could drift from the source transcripts or
introduce a second, subtly different phrasing of the same rule. This is a
manual read-through, not a test.

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
`pyproject.toml` — if `rename_identities`/`render_kube_env`/
`render_unified_output` get flagged as unused, that means a call site is
missing, not that a suppression is needed; conversely if `RunKubeTarget`,
`render_env`, or `MultiNodeEnvUnsupported` are still importable from
anywhere, `vulture`/`ruff` catching them as unused is the signal Task 5's
deletions were incomplete). `pylint`'s `fail-under = 9.0` gate applies to
the whole `tunstrap/` package score, not per-file.

- [ ] **Step 2: Unit suite**

```bash
.venv/bin/pytest tests/unit -q
```

Expected: full pass. **Do not compare the count against any number recorded
in this plan, the spike findings, or earlier iterations of this plan** — the
pivot deletes a meaningful number of pre-existing tests (Task 5's retarget
list) while adding others; both this plan's own earlier "475+N" guidance and
the spike's 476 are stale baselines from before the scalar-channel removal.
Run it and read the real number.

- [ ] **Step 3: Integration suite**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/integration -m integration -q
```

Expected: full pass — **this claim was false in an earlier revision of this
plan** ("no changes needed — this ticket touches no integration fixtures"),
corrected here: Task 5 Step 7 retargets `test_run_env_io.py` and
`test_cli_modes.py` for the exact same shape/scalar removal as the unit
tier, and this is the tier that proves those retargets hold against the
*real* console script and a real docker rig, not just `CliRunner`. If this
step is reached with those retargets not yet landed, it will fail, correctly
— that failure is not a flake to route around, it is Task 5 Step 7 being
incomplete.

- [ ] **Step 4: e2e suite**

**This claim was also false in an earlier revision of this plan** ("run the
tier unmodified as a regression check only"): Task 6 changes
`tests/e2e/module/main.tf`, `rig.py`, `test_tofu_providers.py`, and
`test_terragrunt_apply.py` to the `nodes.*.kube.*.path` shape — those are
real code changes this tier must pass against, not a no-op regression check.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/e2e -m e2e -q
```

Expected: full pass, including `test_recipe_terragrunt.py`'s drift guard
(already run once in Task 6 Step 3; running the full tier here is the final
confirmation nothing else regressed).

**Separately, and still optional:** the design doc's "`e2e` coverage —
optional, with rationale" describes a *different* piece of work — an
e2e-level collision test proving the kube identity rename, which no task in
this plan adds by default because Task 2's unit-level regression test
already exercises that specific defect precisely. If a reviewer chose to add
it anyway, it would be an extension of Task 2 (rewriting two kind
kubeconfigs to a shared identity before feeding them to `tunstrap start`/
`run`, per the design doc), not part of this step. Do not conflate the two:
Task 6's shape-migration e2e changes are mandatory and verified by this
step; the collision-specific e2e coverage is optional and, if added, is
Task 2's concern, verified the same way this step already verifies the rest
of the tier.

- [ ] **Step 5: Final commit / PR**

No further commit needed if Tasks 1-6 already committed cleanly and gates
pass on the resulting tree. Open or update PR #13 against `feature/run-env-io`
per the ticket's stated target; do not merge (org rule, per prior Phase-A
rulings on this repo — curate and validate, leave the merge decision to the
human reviewer).

---

## Self-Review

**Spec coverage — kube part (Tasks 1-3, unchanged by the pivot):**
- Ticket work item 1 (patch identity names, all three, cluster+user+context)
  → Task 1. ✓
- Ticket work item 2 (multi-node kube channel) → Task 3. ✓ (its original
  "`MultiNodeEnvUnsupported` narrows to scalars" framing is itself
  superseded by the pivot — see below, the class is removed entirely, not
  narrowed.)
- Ticket work item 3 (export provider-facing variables) → Task 3, per the
  conditional contract (R1), **not** the ticket's own placeholder "superset"
  suggestion — corrected in light of the provider findings that arrived after
  the ticket was written. ✓
- R1 (conditional, not superset; anti-drift guard extended for both
  cardinalities) → Task 3 builds `render_kube_env`'s exact cardinality
  contract; Task 5 re-scopes the guard's *other side* (`predicted_env_keys`
  vs. `_build_child_env`, once `render_env` is deleted) — but note
  `predicted_env_keys` **itself** ships its final, conservative formula
  directly in Task 3 (R11, iteration 6), not deferred to Task 5. ✓
- R3 (active-triple-only rename scope) → Task 1 (`rename_identities`'s scope,
  `test_kube_rename.py`'s "ignored entries" case). ✓
- R4 (naming scheme, no configurable prefix) → Task 1. ✓
- R6 (correction: `patch_view` owns server-address patching, not
  `dump_kubeconfig`) → encoded in Task 1's implementation guidance (no
  `dump_kubeconfig` signature change) and in the design doc directly. ✓
- R7 (mandatory unit collision test; e2e optional with rationale) → Task 2
  (mandatory) + Task 7 Step 4's "separately, and still optional" paragraph
  (explicit rationale for skipping the collision-specific e2e coverage by
  default, disentangled from Task 6's now-mandatory e2e shape migration —
  the two were conflated in an earlier revision of this plan; iteration 4
  keeps them clearly distinct). ✓
- Not covered by any ruling, found and closed here: `suppress_kubeconfig`
  must drop all three kube env names once `KUBE_CONFIG_PATH`/`_PATHS` become
  real exported channels → wired in Task 5's `_build_child_env` rewrite
  (originally Task 4 in iteration 2; the mechanism moved when the pivot
  merged kube-channel wiring into the same edit as scalar removal). ✓ (see
  decision history #7.)

**Spec coverage — the pivot (U1-U6, Tasks 4-6):**
- U1 (unified node-qualified output contract replaces flat scalars) → Task 4
  (shape + `render_unified_output`) + Task 5 (scalar deletion). Decision
  history entry 10. ✓
- U2 (delivery: var AND materialization, materialization primary) → Task 5
  Step 4 (materialization write, unconditional). Decision history entry 11. ✓
- U3 (scalar channel deprecated/removed, not "stays single-node"; disposition
  of `render_env`/`predicted_env_keys`/`MultiNodeEnvUnsupported` worked out)
  → Task 5 in full, **now backed by a grep-driven blast-radius table**
  (iteration 4) covering unit, integration, e2e and the recipe doc — every
  deletion and retarget is enumerated by file:line with a stated disposition,
  not reconstructed from a prose list. Decision history entries 10 and 13. ✓
- U4 (kube part unchanged; unified structure carries only kube references)
  → Task 1-3 untouched; Task 4's `UnifiedKubeRef` shape enforces
  reference-only fields at the model level (`extra="forbid"`, no credential
  field exists to leak, and — confirmed in Task 5's dedicated retarget of
  `test_cli_run_output_var_projection.py` — the field set is narrower than
  the pre-#15 `RunKubeTarget` projection by design, not by omission: `path`/
  `context`/`endpoint` only). ✓
- U5 (consumer-side transformation via jsondecode; "через js" recorded as an
  assumption) → design doc "Consumer-side transformation"; Task 6 Step 2's
  recipe content states the same interpretation; decision history entry 12
  records it as its own decision. ✓
- U6 (reconciliation: ticket's "nothing live enters Terraform" holds for kube,
  superseded for ports by materialization-primary + stability contract) →
  design doc's dedicated reconciliation subsection (present in both the
  problem framing and the unified-output section) + decision history entry
  11's "U6 reconciliation" paragraph, both present per the DoD's explicit
  requirement that this appear in *both* documents. ✓
- Sarge's ruling this iteration (`inject_scalars` gate semantics change:
  unified output emitted regardless of node count) → design doc's rewritten
  "`cli.py` wiring is in scope" subsection + Task 5's unconditional
  `_build_child_env`; decision history entry 13. ✓
- R5 (breaking deliberately) → extended by the pivot to cover the scalar
  removal and `MultiNodeEnvUnsupported` deletion too, not just the kube
  rename — design doc "Compatibility" section, pivot-tagged bullets. ✓
- R8 (recipe carries the three conditions + the four measured facts) →
  Task 6 Step 0 (fixes the recipe's own **pre-existing** `connections.*`
  shape so it does not contradict the new content) + Step 1 (kube part,
  unchanged) + Step 2 (pivot: shape, stability contract, jsondecode note). ✓
- Iteration-2 ruling (kube channel fires on `kube_targets` presence, not node
  count) → **superseded, not re-satisfied by a new mechanism**: under the
  pivot there is no `inject_scalars` branch left to satisfy the ruling
  *against*, so it holds trivially (Task 5's unconditional
  `render_kube_env` call). The pre-existing test this ruling first
  contradicted (originally `test_multi_node_suppression_uses_input_count`,
  retargeted once in iteration 2 to
  `test_multi_node_suppresses_scalars_but_exports_kube_channel`) is
  retargeted a **second** time in Task 5 Step 2, to
  `test_optional_node_failure_does_not_affect_kube_channel_or_unified_output`,
  because its remaining `leaked == []` assertion stopped describing a real
  guard once there was nothing left to leak from. Both retargets are
  recorded by name in decision history (entry 9, marked superseded; entry
  13, the current disposition). ✓

**Placeholder scan:** No task defers its own code to "TBD" with one
explicitly-flagged exception: Task 5 Step 4's materialization call site asks
the implementer to "confirm the exact `run_command` call site against the
checked-out `cli.py`" rather than citing a line number, because this plan's
own earlier line citations for that function have already drifted once
across iterations (noted inline, Task 5 Step 4) — re-resolving against the
live file is explicitly instructed, not a gap. Every other task either
cherry-picks concrete, reviewed spike code (Task 1's function body, Task 3's
`render_kube_env` skeleton before the cardinality-helper refactor) or
specifies exact replacement logic inline (Task 3's `_kube_channel_keys`,
Task 4's model definitions, Task 5's `_build_child_env` rewrite, Task 5's
retargeted anti-drift guard given in full). Task 6 is documentation plus the
e2e shape migration, both scoped to concrete file:line targets from the
blast-radius table. Task 7 Step 4 is scoped precisely (mandatory e2e shape
migration vs. optional collision coverage, disentangled — iteration 4).

**Type consistency:** `rename_identities(dict[str,object], str, str) -> str`;
`render_kube_env(OutputSchema) -> dict[str,str]`;
`_kube_channel_keys(int) -> set[str]`; `render_unified_output(OutputSchema) ->
dict[str, Any]`; `render_output_var(OutputSchema) -> str` (signature
unchanged, body rewritten); `predicted_env_keys(InputSchema) -> set[str]`
(return type unchanged, body simplified, now three-survivor-aware);
`_build_child_env(output, *, output_var, input_env, suppress_kubeconfig=False)
-> dict[str,str]` (**`inject_scalars` parameter removed** — every one of the
three call sites that threaded it, `_build_child_env`/`_run_child`/
`_supervise_child`/`run_command`, updated in the same task, Task 5, named
individually rather than hedged); `_materialized_output_path(str) -> str`
(new, shared between the env-var value and the writer so the two paths
cannot independently drift). ✓

**Under-specified for implementation, flagged rather than silently resolved:**
only the exact `run_command` call-site line for the materialization write
(Task 5 Step 4, noted inline — re-resolve against the live file rather than
trust a citation that has already drifted once) and whether `SessionDir.
_write_file` is directly reusable for `output.json` or needs a small sibling
helper (Task 5 Step 4 item 4, both paths given). `start`'s `--output env`
mode, which also called the now-deleted `render_env` and would otherwise
break outright, is explicitly resolved in Task 5 Step 4 item 5
(three-survivors-plus-kube-channel, matching `_build_child_env`'s own new
shape, plus materialization) — not left as an open question.

**Iteration-4 summary — the systemic fix this revision exists to make:** a
full grep-driven enumeration (unit + integration + e2e + docs, commands given
at the top of Task 5) replaced three rounds of case-by-case patching. The
anti-drift guard (`test_predicted_env_keys_matches_render_env`) is retargeted,
not deleted — the earlier deletion was itself a drill-caught defect in this
plan, corrected here and cross-referenced from the design doc and decision
history entry 13 so the three documents cannot silently re-diverge on this
point again.

**Iteration-5 correction:** the iteration-4 retargeted guard literal (at that
point, a single `predicted_env_keys(schema) == set(actual)` full-equality
assertion) was itself defective — `_build_child_env` starts from
`dict(os.environ)`, so the comparison was unconditionally False against the
real ambient environment of any test process. Fixed by isolating `os.environ`
to `{}` via `monkeypatch.setattr(cli_mod.os, "environ", {})` before calling
`_build_child_env`, with an explicit warning against subtracting `os.environ`
back out after the call instead (silently under-checks a key that is both
inherited and injected).

**Iteration-6 correction (R11) — the single-equality guard itself is now
split into two, per the "Anti-drift guard" section above; read the
iteration-5 warning below in that light, not as still forbidding a subset
check outright.** Iteration-5's warning against "relaxing the assertion to a
subset check" applied to the **exact-cardinality** predictor that existed at
the time — a subset check on top of an exact predictor really would have
been a pure relaxation, hiding a real regression. R11 changes what
`predicted_env_keys` computes (deliberately conservative, not exact), which
changes what "correct" means for the comparison: the safety-envelope half of
the now-two-part guard (Task 5, "Anti-drift guard — retargeted, not deleted,
and now two-part") **is** a subset check, `set(actual) <=
predicted_env_keys(schema)`, and that is *correct*, not a weakening — it is
paired with a separate, still-full-equality formula test (Task 3) that
guards the conservative formula's own correctness. The ambient-environment
isolation fix from iteration 5 carries forward unchanged into the
safety-envelope test's literal (given in full in Task 5, above).

**Iteration-7 summary (R16) — supersedes parts of R9/R12/R13/R15, does not
touch R1/R10/R11/R14.** The user's confirmed post-red-team direction plus
one added constraint: delivery collapses from R9's three modes to two
(mode 2, the literal-pinned-`--session-dir` file, is retracted —
`TUNSTRAP_OUTPUT_FILE` becomes the primary env-carried locator instead);
`fetch_files` content_b64 is removed from every consumer-facing channel
(materialized to disk, `{path, size, sha256}` projected instead, mirroring
the kube precedent R13's atomic-replace primitive already established);
R15's re-adoption of #14 fix 1 is retracted (fix 4 survives, reshaped). The
user's own constraint — the session dir stays mandatory lifecycle
infrastructure regardless — is encoded as its own design-doc subsection, not
folded silently into the stability contract where it could be missed. R1
(anti-drift guard), R10 (naming collision), R11 (conservative predictor),
and R14 (dangling context reference) are all independently verified
unaffected by this iteration — checked explicitly (see "`predicted_env_keys`/
anti-drift guard: checked, unaffected by R16," Task 5), not assumed safe by
omission. Decision history entry 19 (new) records the full
context/alternatives/decision/consequences; entries 14 and 18 carry
correction annotations rather than being rewritten in place, matching this
document's own established annotate-don't-rewrite discipline.

**Self-Review updates, iteration 7:**
- U2 ("delivery: var AND materialization, materialization primary") — still
  holds for the *mechanism* (materialization primary, var secondary); the
  *locator* for the materialized side changes from R9's caller-pinned path to
  R16's env-carried `TUNSTRAP_OUTPUT_FILE`. Task 5 (mechanism) + Task 6 Step 2
  (recipe) updated; decision history entry 19 records the correction.
- U6 (reconciliation) — the "ports: genuine third option" bullet is corrected
  in place (design doc, "Reconciliation," R16-tagged); the option itself
  (`file()` at a locatable path) survives, its plan-safety-across-restart
  property does not.
- R8 (recipe carries the conditions + measured facts) — Task 6 Step 2's Mode
  B content is rewritten a second time (R16), not just re-worded; the
  measured-facts list itself (six ticket findings) is unchanged, only which
  delivery mechanism each fact is cited in support of.
- R9/R12/R13/R15 — explicitly **not** re-litigated from scratch; R16 is
  scoped to exactly what changed (delivery mode count, the fetch_files
  projection, the fix-1 re-adoption), stated as corrections layered on top,
  per the ticket's own instruction for this iteration.

**Iteration-8 note — a methodology regression in iteration 7's own
enumeration, found by drill review and corrected here.** Iteration 7's
`content_b64` blast-radius grep (Task 5, "Fetched-file materialization")
was run as `tunstrap/ tests/ --include='*.py'` — narrower than the
`tunstrap/ tests/ docs/ --include='*.py' --include='*.md' --include='*.tf'`
scope iteration 4 established for the *original* blast-radius table at the
top of Task 5, and that this R16-specific enumeration should have inherited
by default rather than re-deriving from scratch. The missed `docs/` tier
cost a whole shipped subsection (`docs/recipe_terragrunt.md:366-388`)
arguing the opposite of what R16 ships — not caught until this iteration's
drill pass. **Standing instruction for any future grep-driven enumeration in
this plan: default to the widest previously-established scope for the same
search term (`docs/` + `.md`/`.tf` included) and narrow only with an
explicit, stated reason, never by silently reusing a shorter command from a
different, earlier context.** Fixed in place: the grep command (Task 5),
the enumeration table (new "docs tier" rows, Task 5), and a new Task 6 Step
0b carrying the actual rewrite. R13's atomic-replace rationale was also
re-grounded this iteration (design doc, two locations; plan, Task 5's
materialization-writer step) — the requirement itself did not change, only
its justification, which had come to rest on a race (`file()` racing a `run`
restart against a pinned path) that R16 itself had already retired one
iteration earlier without anyone circling back to the sentences that cited
it.
