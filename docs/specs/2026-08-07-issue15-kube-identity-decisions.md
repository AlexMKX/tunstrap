# Decision history: kubeconfig-as-identity delivery (issue #15)

- Date: 2026-08-07 (revised same day, iteration 3: entries 10-13 record the
  unified-output-contract pivot; entry 9 is marked superseded rather than
  rewritten — an ADR is a history, decisions get superseded in place, not
  erased)
- Companion design doc (kept as-is, not superseded):
  `docs/specs/2026-08-07-issue15-kube-identity-delivery-design.md`.
- Companion evidence, not repeated here: `docs/artifacts/2026-08-07-issue15-
  spike-findings.md` (implementation spike, six prototype variants against the
  full unit suite) and `docs/artifacts/2026-08-07-issue15-provider-env-
  findings.md` (live-probed OpenTofu provider precedence).
- Ticket: [AlexMKX/tunstrap#15](https://github.com/AlexMKX/tunstrap/issues/15),
  a handoff superseding most of #14.

This document is one entry per decision: context, alternatives considered
(with the spike's own measured numbers where an alternative was actually
prototyped, not just discussed), the decision, and its consequences. It does
not restate the design doc's contract prose — see the companion doc for that.

## 1. Rename placement: standalone `rename_identities(doc, node, target)`

**Context.** The materialized kubeconfig's cluster/user/context identities
need renaming to `tunstrap-<node>-<target>` before serialization. Three
placements were prototyped in the spike, each as an isolated branch against
the same `feature/run-env-io` base, each run against the full 475-test unit
suite.

**Alternatives considered** (spike findings, "Part 2"):

| Placement | Diff size | Unit tests broken | Note |
|---|---|---|---|
| V1a — inline in `run_kube_targets`, mutating `KubeconfigView` in place before `patch_view` | +28/-0 | 0/475 | Minimal, but only testable through the SSH-orchestration fakes `run_kube_targets` needs |
| V1b — inside `dump_kubeconfig` (optional `node`/`target` kwargs, mutates `view` as a side effect of dumping) | +35/-3 | 0/475 (kwargs kept optional; making them required — closer to a "the serializer owns identity" design — would break `test_kube_patch.py`'s three no-rename calls, a real but trivial pin) | Couples "serialize to bytes" with "mutate identity"; also the shape the ticket's own imprecise phrasing about `dump_kubeconfig` invited — see the design doc's correction note |
| **V1c — standalone `rename_identities(doc, node, target) -> str`, no `KubeconfigView` dependency** | +42/-2 | 0/475 | **Chosen** |

**Decision.** V1c. It operates on the raw parsed `dict` alone (resolves
`current-context` itself), so it is unit-testable with a bare fixture dict —
no `KubeconfigView`, no `run_kube_targets`, no SSH fakes required to exercise
it in isolation. It keeps `dump_kubeconfig` a pure serializer (matches the
design doc's correction that server-address patching is `patch_view`'s job,
not the serializer's — extending the serializer's responsibility further in
the opposite direction, per V1b, would be the wrong direction to move it in).
It also matches the ticket's own proposed signature verbatim.

**Consequences.** One new top-level function + `__all__` export in `kube.py`,
one call site in `run_kube_targets` between `patch_view` and
`dump_kubeconfig`. `KubeTargetOutput` is built from the function's return
value (the shared new name) instead of `view.cluster_name`/`view.context_name`
post-parse. No signature change to `dump_kubeconfig`, `patch_view`, or
`parse_kubeconfig`.

## 2. `render_kube_env` split out of `render_env`

**Context.** `render_env`'s single node-count guard (`envrender.py:26-30`)
covered three unrelated things: the node-ambiguous `TUNSTRAP_<TARGET>_*`
scalars, the node-ambiguous per-kube-target `TUNSTRAP_<KUBE>_*` scalars, and
the `KUBECONFIG` colon-joined list, which is not node-ambiguous — it is
already a path list and `render_env` already colon-joins it.

**Alternatives considered.** The only alternative discussed (not prototyped
separately, since it is the null option) was leaving `render_env` as one
function and adding a `multi_node: bool` escape-hatch parameter that skips the
guard for callers that only want the kube lines. Rejected without prototyping:
it reintroduces exactly the "one function doing two things gated by a flag"
shape the split exists to remove, and it would make the scalar-channel
guarantee ("still requires exactly one node") a run-time parameter instead of
an structural property of which function you call.

**Decision.** Extract `render_kube_env(output) -> dict[str, str]`, callable
for any node count including zero and multi-node. `render_env` keeps its
existing single-node contract unchanged and delegates its kube-line
construction to `render_kube_env` for the single-node case. **[Editorial fix,
iteration 6]** Single-node output is **not** byte-identical to before the
split, as this entry originally claimed — precisely: `KUBECONFIG`'s *value*
(the colon-joined path list) is unchanged, but the conditional cardinality
contract (entry 3, below) this same split enables also *grows the key set*
for the single-file case (adding `KUBE_CONFIG_PATH` alongside `KUBECONFIG`,
which pre-split `render_env` never exported). The design doc carries the
same correction.

**Evidence.** Spike Axis 2: +30/-5 lines in `envrender.py`, 0/475 unit tests
broken (pure, behavior-preserving refactor for the single-node case), and
manually verified live that `render_kube_env` correctly colon-joins across
**two** nodes' kube targets with `len(connections) == 2`, while `render_env`
on the same input still raises `MultiNodeEnvUnsupported` (spike findings,
"Part 2", Axis 2 row and the inline verification transcript).

**Consequences.** `MultiNodeEnvUnsupported`'s scope narrows from "the whole
kube-carrying export" to "the scalar channel only" — see decision 8
(breaking-change policy) for why this is accepted rather than treated as a
regression. `cli.py` needs a new call site to actually invoke
`render_kube_env` for the multi-node case — see decision 6.

**[Superseded in part by entry 13, iteration 3.]** `render_kube_env` itself
— the function this entry extracts — is **not** superseded and ships
unchanged (kube part, U4). What *is* superseded: `render_env`, the function
it was extracted *from*, is later deleted in its entirety once the
unified-output pivot removes the scalar channel it existed to produce (entry
10). `MultiNodeEnvUnsupported` accordingly narrows all the way to zero raise
sites and is removed too (entry 13), rather than staying narrowed-to-scalars
as this entry originally concluded.

## 3. Env export: conditional cardinality, not the superset

**Context.** The ticket's work item 3 asked to prototype exporting
`KUBE_CONFIG_PATH` and `KUBE_CONFIG_PATHS` alongside `KUBECONFIG` as a
superset, explicitly deferring the final choice to a parallel
provider-verification effort ("Whether they also honour plain `KUBECONFIG` is
being verified by another agent in parallel").

**Alternatives considered.**

- **Superset, always** (spike Axis 3, as literally prototyped): export all
  three unconditionally. Evidence: +10/-2 lines, exactly one test break —
  `test_predicted_env_keys_matches_render_env` — and only when
  `predicted_env_keys` isn't updated in the same commit (confirmed by
  deliberately reverting only that half and re-running; 1 failure, clear diff,
  0 once both changed together). **Rejected once the provider findings landed
  post-spike**: `docs/artifacts/2026-08-07-issue15-provider-env-findings.md`
  shows `KUBE_CONFIG_PATH` wins over `KUBE_CONFIG_PATHS` when both are set,
  live-confirmed for both `hashicorp/kubernetes` v2.38.0 and
  `hashicorp/helm` v2.17.0. Exporting both unconditionally the instant a
  *second* kube target is materialized would silently shadow every cluster but
  the one named by `KUBE_CONFIG_PATH` — the collision-class failure this whole
  design exists to prevent, recreated one layer down in the env contract
  itself.
- **`KUBE_CONFIG_PATH` + `KUBECONFIG` only, drop `KUBE_CONFIG_PATHS`
  entirely**: considered and rejected, because it has no way to express more
  than one materialized file to the providers at all — a real regression for
  any consumer with two or more kube targets on one or more nodes, which the
  multi-node kube channel (decision 2) exists specifically to support.

**Decision.** Conditional on the number of materialized kubeconfig files
(one per kube target, summed across the whole envelope):

- 0 files → nothing exported.
- exactly 1 file → `KUBECONFIG` + `KUBE_CONFIG_PATH`, both pointing at the one
  file; `KUBE_CONFIG_PATHS` **not** exported.
- ≥ 2 files → `KUBECONFIG` + `KUBE_CONFIG_PATHS`, both the same colon-joined
  list; `KUBE_CONFIG_PATH` **must not** be exported (it would win over the
  list per the measured provider precedence, hiding every cluster but the
  first).

**Consequences.** `predicted_env_keys` must model the *same* cardinality
condition — not just "kube_targets is non-empty" as it does today — and the
anti-drift guard test is extended with cases for both cardinalities. This is
strictly more surface than the superset would have needed (two branches
instead of one flat set), but it is the only shape that does not have a
silent-shadowing failure mode once the provider precedence fact is known.

**[Superseded in part by entry 16, iteration 6 (R11).]** The claim that
`predicted_env_keys` should model "the *same*" (i.e. exact) cardinality
condition is wrong for `predicted_env_keys` specifically — it must instead
*over-approximate* conservatively, because it runs pre-spawn against input
cardinality, which can shrink by the time output cardinality is known (an
optional node/target can fail). `render_kube_env`'s own export logic (the
actual, output-side computation this entry establishes) is unaffected and
stays exact — only the *predictor* becomes conservative. See entry 16.

## 4. Naming scheme: `tunstrap-<node>-<target>`, no configurable prefix

**Context.** The identity strings need a scheme that is unique across nodes
and targets without operator configuration.

**Alternatives considered.** A configurable prefix (e.g. `--context-prefix`)
was the natural next reach — it would let two independent tunstrap
invocations avoid colliding even if `node`/`target` names happened to repeat
across them. Not prototyped: rejected on the org rule cited directly in the
ticket ("avoid excessive configurability") before implementation, on the
grounds that the node name already solves the concrete scenario a prefix
would be reached for (merging two separate tunstrap runs whose `node` dict
keys already have to differ or the merge was already ill-defined for other
reasons).

**Decision.** Fixed `tunstrap-<node>-<target>` for cluster, user and context
alike (one shared name across all three, not three independently-derived
strings) — no prefix option, no per-field naming variation.

**Consequences.** Uniqueness is a corollary of `NodeInput`/`kube_targets`
dict-key validation (`_validate_identifier_key`, `schemas.py:14-22`) rather
than a property this feature has to separately maintain — no new uniqueness
check is needed anywhere in the rename path. A future request for a
configurable prefix is a new decision, not an oversight in this one.

**[Superseded in part by entry 15, iteration 6 (R10).]** "No new uniqueness
check is needed anywhere in the rename path" is **false**: dict-key
validation only proves `node` and `target` are each individually valid
identifiers, not that the hyphen-joined `tunstrap-<node>-<target>` string is
unique across different `(node, target)` pairs — `_FETCH_FILES_KEY_RE`
permits internal hyphens, so `(node="a-b", target="c")` and `(node="a",
target="b-c")` both join to `tunstrap-a-b-c`. A real validation-time
collision check is added; see entry 15 for the alternatives considered and
why detection (not structural prevention) was chosen.

## 5. Rename scope: the active current-context triple only

**[Corrected iteration 6 (R14) — read in conjunction with the note at the end
of this entry, not in isolation.]**

**Context.** A materialized kubeconfig may contain more than the
current-context's cluster/user/context — `ignored_contexts` (the *collection*
of skipped-context names) is computed in `parse_kubeconfig` at
`kube.py:179-183`; the *warning* for each is actually logged where that list
is consumed, in `run_kube_targets` at `kube.py:330-337`, not at the
collection site itself — an earlier revision of this entry (and the design
doc) cited the wrong function for the warning.

**Alternatives considered.** Renaming (or pruning) every context/cluster/user
in the document, not just the current one, was considered and rejected for
this change. Reasons: (a) it changes the module's pre-existing, documented,
tested contract that non-current entries are left byte-stable
(`kube.py:1-8`), which is out of this ticket's stated scope; (b) the practical
collision surface is the current-context triple, since k3s and kind — the two
shapes this codebase actually targets — both ship single-context
kubeconfigs; (c) it is real additional work (deciding prune vs. rename for
entries no consumer will ever reach through `current-context`) that the
ticket's own "one cluster per kube_target" framing does not ask for.

**Decision.** Rename only the current-context's cluster, user and context
entries, **but update every reference to them, including references from
ignored (non-current) contexts** — corrected, see below. Every entry neither
part of nor referencing the active triple stays untouched.

**Consequences.** Accepted, documented residual risk: two materialized files
whose *non-current* contexts happen to collide are not protected by this
change. If that ever becomes a real incident rather than a theoretical one,
the fix is pruning ignored entries at materialization time — a separate,
larger change, explicitly out of scope here (see the design doc's "Out of
scope" section).

**[Corrected, iteration 6 — R14.]** An earlier revision of this decision's
"Decision" line read "Leave every other entry in the document untouched,"
full stop. That is incomplete in a way that produces a real defect: a
kubeconfig can legitimately have a *non-current* (ignored) context whose own
`context.cluster`/`context.user` reference the **same** cluster/user entry
the current context also uses (two contexts sharing one cluster with
different users is an ordinary shape). If the shared cluster/user entry is
renamed but the ignored context's reference to it is left pointing at the
*old* name, that reference now dangles — it names an entry that no longer
exists anywhere in the document under that name, which is strictly worse
than the pre-rename state. The rename must therefore walk **every** reference
to the renamed cluster/user, not just the current context's own — "leave
every other entry untouched" is now read as "leave every entry that neither
*is* nor *references* the active triple untouched," a narrower and correct
claim. See the design doc's "Rename scope" section (iteration 6) and the
plan's Task 1 for the concrete fix and its regression test.

## 6. `cli.py` wiring is in scope for this ticket

**Context.** The spike's own open question asked whether wiring `run`'s
`_build_child_env` to actually call the new multi-node-safe `render_kube_env`
was part of this ticket or a follow-up, since the spike itself only proved the
export *function* works multi-node — it never wired a call site, to keep the
spike's diff isolated to `envrender.py`/`kube.py`.

**Decision (ruling, not re-litigated here).** In scope. Without it, a
multi-node `run` invocation with kube targets never actually emits
`KUBECONFIG`/`KUBE_CONFIG_PATH(S)` for the child process — the multi-node kube
channel would be dead code, reachable only by unit tests calling
`render_kube_env` directly, never by an actual `tunstrap run` invocation.

**Consequences.** `_build_child_env` (`cli.py:365-407`) needs a second,
independent call — `render_kube_env(output)` merged into `child_env` whenever
any node carries `kube_targets`, regardless of the existing `inject_scalars`
gate. This is new code beyond what the spike prototyped; see the
implementation plan for the concrete change and its test.

## 7. `suppress_kubeconfig` extends to all three exported kube env vars

**Context.** Not raised by the ticket or by any ruling — found while
specifying the env-export contract (decision 3). `tunstrap_tofu` sets
`suppress_kubeconfig=True` so a broken `TF_VAR_tunstrap` → `config_path` chain
fails loudly rather than silently reaching the cluster through a
still-present `KUBECONFIG` (`tofu_proxy.py:138-155`, `cli.py:388-392`). **[Editorial
fix, iteration 6 — narrowed]** The provider findings (decision 3's evidence)
show that guard has been inert **only for the two providers' own Go
configuration chain** — neither provider's `initializeConfiguration()`/
`newKubeConfig()` ever read plain `KUBECONFIG`. An earlier revision of this
entry said "inert all along" without that qualifier, which overstated the
claim: the same suppression was, and remains, load-bearing for a *different*
audience the whole time — `tofu`'s children include `local-exec`
provisioners and `external` data sources that can shell out to the
`kubectl`/`helm` **CLIs** directly, both of which do honour plain
`KUBECONFIG`. It becomes genuinely load-bearing **for the provider-native
chain specifically, for the first time**, the moment this design starts
exporting `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS`, which the providers do
read.

**Alternatives considered.** Leaving `suppress_kubeconfig` as-is (dropping
only `KUBECONFIG`) was the default until this was noticed; not viable once
stated plainly, since it would silently defeat the one property the proxy's
own docstring claims to guarantee.

**Decision.** `suppress_kubeconfig` drops all three names —
`KUBECONFIG`, `KUBE_CONFIG_PATH`, `KUBE_CONFIG_PATHS` — both inherited and
injected, whenever set.

**Consequences.** One small, mechanical change to `_build_child_env`
(`cli.py:394-404`); no change to `tunstrap_tofu`'s own call site
(`suppress_kubeconfig=True` already requests "suppress the kube env", its
meaning just becomes complete). See the plan for the concrete diff and its
test (`tests/unit/test_cli_run_output_var.py` or a new focused test asserting
all three names are absent from the child env under
`suppress_kubeconfig=True` with a multi-file payload).

## 8. Breaking-change policy: deliberate, no compatibility shim

**Context.** The rename changes the value of `KubeTargetOutput.context_name`/
`.cluster_name` for every consumer of the materialized kubeconfig or the
`--output-var` payload; narrowing `MultiNodeEnvUnsupported`'s scope changes a
documented, tested exception contract.

**Alternatives considered.** A compatibility flag (e.g.
`daemon.rename_kube_identities: bool`, defaulting to the old behaviour) was
considered and rejected without prototyping. The org rule the ticket itself
cites is explicit: no backward compatibility unless instructed. A flag would
also mean two code paths to test and maintain for a rename whose only
consumers are documented, internal (the recipe this design also ships) and
not yet depended on by any released version.

**Decision.** Breaking, deliberately, with no flag and no fallback:

- Upstream context/cluster/user names in the materialized kubeconfig always
  change to `tunstrap-<node>-<target>`.
- `KubeTargetOutput.context_name`/`.cluster_name` always report the new
  names.
- `MultiNodeEnvUnsupported`'s contract narrows to the scalar channel only,
  matching what its own docstring already claimed.

**Consequences.** Any external consumer relying on the upstream cluster's own
context name surviving verbatim through tunstrap breaks with this change,
with no opt-out. This is accepted per the org rule and stated explicitly here
so it is not mistaken for an oversight during review.

**[Partially superseded by entry 13, iteration 3.]** The rename bullets (first
two) stand unchanged. The third bullet — "`MultiNodeEnvUnsupported`'s contract
narrows to the scalar channel only" — is superseded: entry 13 removes the
class entirely rather than leaving it narrowed. The **policy** this entry
establishes (breaking, deliberately, no compatibility shim) is what entry 13
applies to justify the further deletion; the specific narrowing outcome is
what changed, not the policy behind it.

## 9. Kube channel fires independently of node count — a pre-existing test's contract is deliberately inverted

**[Superseded by entry 13, iteration 3 — kept below verbatim as the historical
record, not rewritten.]** This entry's *conclusion* (kube channel fires on
`kube_targets` presence, not node count) still holds and is in fact easier to
satisfy under the pivot. What is superseded is the *mechanism* it specified
to get there — the two-branch `_build_child_env` (`inject_scalars=True` →
`render_env` delegating to `render_kube_env`; `inject_scalars=False` →
`render_kube_env` directly) — because `render_env` and `inject_scalars` are
both removed by the pivot (entries 10/13), leaving one unconditional
`render_kube_env(output)` call with no branch at all. The test this entry
retargets is retargeted *again* under the pivot, for a different reason (the
scalar-leak assertion it still carried no longer makes sense once scalars do
not exist as a concept to leak) — see entry 13 and the plan's **Task 5 Step 2**
(corrected pointer, iteration 4: the cli.py wiring and scalar-removal work
this entry's mechanism affects both live in Task 5, not Task 4, under the
plan's iteration-3 task renumbering — Task 4 is the unified-output shape
task, a pure-function step with no `_build_child_env` changes at all).

**Context.** Decision 6 established that `cli.py` wiring for the multi-node
kube channel is in scope. Working out that wiring's exact trigger condition
(§6's own text, and the design doc's "`cli.py` wiring is in scope" section)
surfaced a sharper rule than "fires when `inject_scalars` is false": **the
kube channel fires whenever `kube_targets` are present in the output, full
stop — independent of node count and independent of why `inject_scalars`
happens to be false.** `inject_scalars` decides which function computes the
keys (`render_env`, which delegates, vs. `render_kube_env` directly); it never
decides whether they get computed.

This directly contradicts a **pre-existing, deliberately-written** test:
`tests/unit/test_cli_run_output_var.py::test_multi_node_suppression_uses_input_count`
(added under the pre-#15 `run` env I/O design) asserts, among other things,
`"KUBECONFIG" not in FakePopen.last_env` for a two-input-node run whose one
surviving output connection carries a materialized kube target. That
assertion encoded the *old* contract — "multi-node input ⇒ no kube env at
all" — which decisions 2 and 6 above deliberately supersede.

**Alternatives considered.** Leaving the old assertion in place and adding a
node-count exception to the new rule (e.g. "kube channel fires on
`kube_targets` presence, except when the *input* had more than one node and
only one survived") was considered and rejected: it reintroduces exactly the
kind of node-count-conditional kube-channel logic this whole design exists to
remove, for the sole purpose of keeping one old assertion green, and it would
leave the multi-node kube channel just as reachable as before, since a
survivor-of-multiple-optional-nodes shape is not distinguishable from a
"real" multi-node output at the type level.

**Decision.** The old assertion is wrong under the new contract and is
retargeted, not preserved: `test_multi_node_suppression_uses_input_count` is
renamed to `test_multi_node_suppresses_scalars_but_exports_kube_channel` and
its `KUBECONFIG`-absence assertion is flipped to a `KUBECONFIG`/
`KUBE_CONFIG_PATH`-presence assertion (exactly one file survives in this
test's payload). The test's other half — that the `TUNSTRAP_*` scalars stay
suppressed, decided by the *input* node count and not `len(out.connections)`
— is unchanged and remains the one place that half of the contract is
falsifiable; only the kube-channel half of the old assertion was ever wrong
under the new design. See the plan's iteration-2 Task 4 Step 2 for this
retarget as it landed at the time (historical citation, not re-resolved here
— the pivot's iteration-3 task renumbering moved the equivalent *area* of
work, and the *second* retarget this test undergoes, to the current plan's
Task 5; see entry 13 and its own corrected pointer above).

**Consequences.** Anyone reading `git blame` on that test past this point
sees a deliberate contract inversion, not a silent weakening — which is why
it is recorded here by name rather than only in the plan's own commit
message. No other test in the suite encoded the old "no kube env for
multi-node, at all" contract (confirmed by grep across `tests/unit/` for
`KUBECONFIG` assertions during this revision), so this is the only retarget
this decision requires.

---

## Iteration 3: the unified-output-contract pivot

Entries 10-13 record a user-directed design pivot, not a discovery made while
implementing entries 1-9. Where a ruling is given rather than derived, that is
stated plainly rather than reverse-engineered into a false "alternatives
considered."

## 10. Unified node-qualified output contract replaces the flat scalar channel

**Context.** The pre-pivot design (entries 1-9) kept the `TUNSTRAP_<TARGET>_*`
scalar channel for single-node output and added a parallel, node-agnostic
kube channel (`render_kube_env`) alongside it. The user's pivot rejects that
two-channel shape entirely: the *entire* consumer-facing output — ports, kube
references, session metadata — becomes one unified, node-qualified JSON
structure, replacing the scalar channel outright rather than extending it.

**Alternatives considered.**

- **Extend the scalars with a node dimension** (e.g.
  `TUNSTRAP_<NODE>_<TARGET>_PORT`, or a separate `TUNSTRAP_NODES` listing
  key). Rejected by the user's own reasoning, encoded here rather than
  re-derived: this is two mechanisms doing the same job — a flat `KEY=VALUE`
  scalar space and a structured JSON value both trying to represent a
  hierarchy — when the domain has one natural, unambiguous representation
  (nested keys) that scalars cannot express without inventing a second
  encoding scheme layered on top of environment-variable naming rules
  (`_key()`'s `[^A-Z0-9]` → `_` sanitisation already loses information for
  non-trivial names; stacking a node segment on top compounds that). Scalars
  are also the wrong abstraction for the general case: `fetch_files` content
  and multi-field kube references (`path`/`context`/`endpoint`) do not fit a
  single scalar value at all — the pre-pivot design already routed those
  through the structured `--output-var` channel instead, which is the
  existing proof that the domain wants a structure, not more scalars.
- **Status quo: keep both the scalar channel (single-node) and the separate
  kube channel (node-agnostic), unified only within `--output-var`'s own
  existing projection.** This is entries 1-9's actual shipped design.
  Rejected by the pivot: it leaves three channels (scalars, kube env,
  `--output-var`) each with a different node-count contract, which is exactly
  the kind of multi-mechanism-for-one-need shape the first alternative was
  also rejected for, just already partially built rather than newly proposed.

**Decision.** One unified structure (design doc, "The unified output
contract", shape sketch) replaces the scalar channel outright.
`TUNSTRAP_<TARGET>_*` and the per-kube-target `TUNSTRAP_<KUBE>_*` scalars are
deleted, not deprecated-with-a-flag (decision 8's breaking-change policy
extends to this too — see entry 13). `TUNSTRAP_SESSION_DIR`/`TUNSTRAP_PID`
survive as two of three non-target-scoped scalars, kept for a real
bootstrapping need (locating the payload from a shell context that has not
parsed anything), not for backward compatibility. **[iteration 4 addition]**
A **third** survivor, `TUNSTRAP_OUTPUT_FILE` (the materialized JSON path),
was added while tracing `render_env`'s deletion through `start --output env`
— dropping to only two survivors leaves that mode with no way to tell a
plain-`remote_targets` shell consumer where a forwarded port landed, a real
functional regression the design doc's "The scalar channel is removed"
section now records as a judgment call, not a silent narrowing.

**Consequences.** `render_env` (the scalar-producing function) is deleted.
`render_output_var`'s internals change to build the new shape (signature
unchanged: still `OutputSchema -> str`). A new `render_unified_output`
function/model pair is needed (design doc shape). Every test asserting a
`TUNSTRAP_<TARGET>_*` key breaks and must be deleted or retargeted, not
patched around — see the plan.

## 11. Materialization-primary + var-as-convenience + explicit stability contract

**[Corrected in part by entry 14, iteration 6 (R9) — read together, not in
isolation.]** This entry's core conclusion — materialization is primary,
both channels ship, the reasoning cited to findings #1/#2/#6 — stands. What
is corrected: the specific **mechanism** this entry originally described for
the var form ("the var is a locator and convenience path... the var only to
locate the file") is retracted by entry 14 as unsound (a three-model
red-team review found the locator pattern does not actually buy plan-safety
— see entry 14's alternatives-considered for the full reasoning). Read this
entry's "Decision"/"Consequences" below as describing the *what* (two
channels, materialization primary); read entry 14 for the corrected *how*
(three independent modes, no locator).

**Context.** U2/U6: the unified structure is delivered both as the
`--output-var` value and as a materialized JSON file, and the user decided
materialization is primary. This directly touches ticket #15's own framing
("connection data should stop travelling through Terraform input variables")
and the pre-pivot recipe's "no connection data in input variables" condition,
because the var form of the unified structure **does** carry live connection
data (host:port strings) — an honest tension, not a technicality.

**Alternatives considered.**

- **Var-only delivery** (the pre-pivot `--output-var` shape, just reshaped):
  rejected by the user's explicit ruling (U2) that materialization is
  primary, on the strength of findings #1 and #6 (below) — a var-only design
  has no plan-safe path for a consumer who reuses a saved plan across a
  tunstrap restart.
- **Materialization-only delivery** (drop `--output-var` entirely): would
  satisfy the ticket's stricter "nothing live enters Terraform" framing most
  completely, and was the closest fit to ticket #15's own words. Rejected:
  U2 explicitly keeps the var form ("delivered BOTH as... AND materialized"),
  and dropping it removes the only channel through which HCL can discover
  *anything* without already knowing a session path — Terraform config can
  only see env vars that are `TF_VAR_*`-mapped or read by a provider's own
  Go code (like `KUBE_CONFIG_PATH`); a plain shell env var pointing at a file
  path is invisible to HCL entirely unless also injected as a `TF_VAR_*`.
  Materialization-only would need a *different* bootstrap mechanism the user
  did not ask for.

**Decision.** Both channels ship, materialization primary:

- `--output-var NAME` still injects the full unified JSON as a string (var
  form, convenience/bootstrap).
- `run` unconditionally materializes the same JSON to
  `<session_dir>/tunnel-data/output.json` (new; not gated by `--output-var`).
- **Reasoning, cited to the ticket's own findings, restated in full here per
  the instruction that this ADR entry carry it:**
  - **Finding #1** (provider configuration IS re-evaluated at apply — a
    `file()` read inside a provider block picks up a post-plan change) is
    *why* the materialized file is plan-safe: reading it via `file()` inside
    the provider config block re-reads current content at apply time.
  - **Finding #6** (binding a connection value to a `var.` trips "Mismatch
    between input and plan variable value" on a saved plan — the ticket's own
    negative control) is *why* the var form is not plan-safe: its decoded
    values are frozen into the plan at `plan` time, and a tunstrap restart
    before `apply` risks the mismatch.
  - **Finding #2** (outputs freeze silently — `file()` through an output
    returns the plan-time value at apply, no error) belongs in the stability
    contract text, not just the reasoning here, because it is the trap that
    defeats finding #1's plan-safety guarantee if the materialized file is
    read through an intermediate `output` rather than directly at the point
    of use — see the design doc's "Stability contract" subsection, which
    states this as an explicit consumer-facing rule, not just an internal
    rationale.

**U6 reconciliation, recorded here as the decision's own rationale (also
stated in the design doc for the consumer-facing read).** **[Corrected,
iteration 6 — R12: an earlier revision of this paragraph scoped the
reconciliation too narrowly, by kind of *data* rather than kind of
*channel*.]** Ticket #15's "nothing live enters Terraform" holds in full,
unconditionally, only for the **kube env channel**
(`KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS`) — no variable, no file, ever. It is
**superseded** for both ports *and* kube **references** (`path`/`context`/
`endpoint`, non-credential per U4) once a consumer binds `--output-var`: an
earlier revision claimed the kube framing was "untouched," which is only
true for the env channel — the moment a consumer reads
`nodes.<node>.kube.<name>.path` (or `context`/`endpoint`) out of
`var.tunstrap`, that is connection data travelling through a Terraform input
variable, exactly what the ticket wanted stopped, whether or not the field
is itself a credential. For ports specifically, no env-native path exists at
all — a generic TCP endpoint is not a Terraform-provider convention the way
`KUBE_CONFIG_PATH` is — so a `host:port` value has nowhere to go except into
HCL as a value, one way or another. Given that constraint,
materialization-primary + var-as-convenience + the explicit stability
contract (entry 14's corrected mechanism) is the closest available
approximation to the ticket's stricter framing that a live TCP endpoint can
actually achieve, and it is adopted as superseding the ticket's stricter
framing **for the unified structure's var form — ports and kube references
both — while the kube env channel's full compliance is untouched.**

**Consequences.** A new materialization writer is needed (plan). The recipe
gains a stability-contract section a consumer must read before choosing
which form to bind to a resource. This is real new surface area (a second
delivery path with its own failure mode) that a var-only or
materialization-only design would not have had — accepted as the cost of
satisfying both U2's explicit requirement and a genuine plan-safety need
neither alternative covers alone.

## 12. "через js" is read as JSON/jsondecode, not literal JavaScript — recorded assumption

**Context.** U5: the user's instruction for consumer-side transformation used
the phrase "через js." This stack (Terragrunt, OpenTofu, HCL) has no
JavaScript runtime anywhere in the consumer chain.

**Alternatives considered.** Silently interpreting the phrase as "JSON" and
moving on (no explicit record) was the default temptation; rejected per the
standing instruction to record interpretations rather than silently apply
them, and because a genuinely ambiguous phrase deserves a durable record of
which reading was taken, so a reviewer who meant something else (e.g. a
literal `local-exec` calling `node`) can catch the mismatch cheaply instead
of discovering it after implementation.

**Decision.** "через js" is read as **JSON**, consumed via HCL's `jsondecode`
function inside `locals`, exactly the mechanism the pre-pivot `--output-var`
recipe already used. No JavaScript runtime, no `local-exec` invoking `node`,
no new runtime dependency anywhere in the design.

**Consequences.** If this reading is wrong, it is wrong in a single,
clearly-labelled place (design doc "Consumer-side transformation", this
entry) rather than baked silently into fifteen sentences of recipe prose —
cheap to correct if a future reviewer disagrees.

## 13. `MultiNodeEnvUnsupported` and `inject_scalars` are removed, not narrowed further

**Context.** Entries 2 and 9 narrowed `MultiNodeEnvUnsupported` to "the
scalar channel only" and built a two-branch `_build_child_env` keyed on
`inject_scalars`. The pivot's own stated semantics (design doc, requirement
text: "the `inject_scalars` gate semantics change: unified output is emitted
regardless of node count") removes the scalar channel that both of these
existed to gate.

**Alternatives considered.** Keeping `MultiNodeEnvUnsupported` as an unused,
unraisable class "for future use" was considered and rejected: every
remaining channel (unified output, kube env) is either structurally
node-safe (nested keys) or already node-count-agnostic by design (the kube
channel), so there is no remaining scenario that class could ever describe.
An exception class with zero reachable raise sites is exactly the kind of
dead code `vulture`'s gate exists to catch, and per the org's no-backward-
compatibility rule there is no reason to keep it as a courtesy.

**Decision.**

- `MultiNodeEnvUnsupported` is deleted: the class, its `_EXIT_CODES` entry,
  and both raise sites (`render_env`'s internal guard — moot anyway since
  `render_env` itself is deleted; and `cli.py:640`'s pre-spawn
  multi-node-without-`--output-var` gate, which is removed because
  materialization now covers multi-node unconditionally, so the thing that
  gate used to force an opt-in for no longer needs one).
- `inject_scalars` (the boolean, its `len(schema.nodes) == 1` computation at
  `cli.py:648`, and its threading through `_run_child`/`_supervise_child`/
  `_build_child_env`) is deleted. Nothing left needs to know the node count
  to decide what to compute — `render_kube_env` and `render_unified_output`
  are both called unconditionally.
- `_build_child_env` collapses to: always call `render_kube_env(output)`,
  always build+inject the unified structure per `--output-var`'s presence,
  always materialize it for `run`. No node-count branch anywhere in this
  function.

**Consequences.** This is a bigger ripple than entry 9's retarget: every test
asserting `inject_scalars`'s value, mocking it, or asserting
`MultiNodeEnvUnsupported`'s exit code (1) needs deletion or a rewrite to the
new "multi-node succeeds unconditionally" behaviour — including entry 9's own
retargeted test, `test_multi_node_suppresses_scalars_but_exports_kube_channel`,
which is retargeted *again* (its "no `TUNSTRAP_*` scalars leak" assertion no
longer describes a real guard once there is no scalar-producing code path
left to leak from) — see the plan's **Task 5's grep-driven blast-radius
enumeration** for the concrete, exhaustive list (iteration 4: this ripple was
first accounted for case-by-case across three review rounds before being
fixed systemically — see "Iteration 4" note below).

**Not everything in this ripple is a deletion.** `test_predicted_env_keys_
matches_render_env` (the anti-drift guard between `predicted_env_keys` and
the actual injected-key computation) is **retargeted, not deleted** — the
two-independent-implementations problem it guards against does not go away
just because one of the two implementations (`render_env`) is replaced by
another (`_build_child_env`'s own hardcoded-plus-`render_kube_env` logic);
see the design doc's "Anti-drift guard extension" subsection, iteration-4
addendum, for why the guard survives in re-scoped form. This was itself a
drill-caught defect in an earlier revision of this plan, which had deleted
the guard on the (false) premise that only one implementation remained.
**[Further evolved by entry 16, iteration 6 — R11.]** The re-scoped,
single-equality form this entry describes is itself superseded once
`predicted_env_keys` becomes a conservative over-approximator rather than an
exact predictor (entry 16): exact equality can no longer hold in general, so
the guard splits into a formula test (exact equality against a
hand-computed expected set) plus a safety-envelope test (`actual ⊆
predicted`, driven by a cardinality-shrink case). See entry 16 for the full
reasoning — this entry's framing of "retargeted, not deleted" is the
conclusion that still holds; its specific single-equality mechanism does
not.

`cli.py:640`'s removal also means the exit-code table in the pre-#15 design
spec (`docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md`, "Error
handling") has one fewer row for multi-node input; that spec is left as-is
per its own "kept as-is, not superseded" status (this ADR's header) — the
row simply no longer reflects current behaviour, which is normal for a design
doc describing a point-in-time decision that a later ADR entry overrides,
and is recorded here rather than edited into that older, closed doc.

**Iteration 4 note — systemic fix, not a fourth case-by-case patch.** The
first three review rounds each caught this same defect class in a different
single spot (a test here, a stale reference there). Iteration 4's fix is a
full grep-driven enumeration of every symbol/shape the pivot removes, across
`tunstrap/`, `tests/unit`, `tests/integration`, `tests/e2e`, and `docs/`, with
an explicit disposition per hit, made the authoritative blast-radius record
inside the plan's Task 5 rather than trusted to be found again by inspection.
See the plan for the table; it is not duplicated here.

---

## Iteration 6: three-model red-team review corrections

Entries 14-18 record sarge's rulings on a consolidated 12-finding red-team
review (three independent models). Where a ruling is given rather than
derived, that is stated plainly, matching the discipline entries 10-13
already established for user-directed decisions.

## 14. The var-locator pattern is unsound and retracted; three independent delivery modes replace it

> **[R16, iteration 7 — correction annotation, this entry's decision is
> further superseded, not rewritten in place.]** Mode 2 below (the
> literal, caller-pinned `--session-dir` file) is itself retracted by
> iteration 7's user-confirmed direction: the session root stays ephemeral
> unconditionally, and `TUNSTRAP_OUTPUT_FILE` — an env-carried locator, not
> a pinned path — replaces it. Delivery collapses from three modes to two.
> This entry's "Decision"/"Consequences" below are kept verbatim as the
> historical record of what iteration 6 actually shipped (the same
> annotate-don't-rewrite discipline this entry itself applied to entry 11);
> see entry 19 for the current, superseding decision.

**Context.** Entry 11 shipped a hybrid delivery mechanism: inject
`var.tunstrap` and materialize a file, with the recommended plan-safe
pattern being "read `var.tunstrap` only to locate the file
(`jsondecode(var.tunstrap).session.session_dir`), then `file()` the located
path." A three-model red-team review (findings #1, #5) found this unsound.

**Alternatives considered.**

- **The locator pattern itself** (entry 11's original mechanism): rejected
  for three independent, compounding reasons: (1) finding #1 measured
  content-change tolerance at a **stable** path, but the locator pattern put
  a **changing** value (`var.tunstrap`'s full JSON, different every
  invocation) into a Terraform variable — OpenTofu's plan-variable
  consistency check (finding #6) compares the **whole bound value** of a
  root-module variable between `plan` and `apply`, not just the sub-fields an
  expression reads, so reading only `session.session_dir` from it does not
  narrow the exposure at all; (2) this is the same finding #6 firing,
  restated — the locator is exactly as exposed as binding the ports
  directly; (3) the **default** session directory (auto-minted via
  `tempfile.mkdtemp` when no `--session-dir` is given, `cli.py:427`) is
  deleted by teardown, so even ignoring (1)/(2), a locator pointing at it
  would frequently reference a directory that no longer exists by the time a
  later, separate `apply` tried to read it.
- **Materialization-only, drop the var entirely**: re-considered here (entry
  11 already rejected it, reasoning unchanged) — still rejected, since a
  plain shell env var pointing at a file path is invisible to HCL unless also
  `TF_VAR_*`-mapped, and dropping the var removes the only bootstrap
  mechanism HCL has for anything it does not already know a literal path to.
- **Keep the var, drop the locator recommendation, downgrade the var to
  one-shot-only, and make the file mode's plan-safety depend on an
  *operator-chosen* literal path rather than anything decoded from the
  var**: **adopted** — this is the only option that is honest about what
  finding #6 actually measures (the whole-variable comparison) rather than
  trying to work around it with an indirection that does not change what is
  compared.

**Decision.** Three genuinely independent delivery modes, none locating
another:

1. Kube env channel — unchanged, plan-safe unconditionally, no var, no file.
2. Unified file at a **literal, caller-pinned** path — plan-safe **only**
   when the caller supplies a stable `--session-dir` (verified against
   `session.py`: a caller-supplied root is `generated=False`; cleanup on
   that path removes only `tunnel-data/`, never the root) and the consumer's
   HCL hardcodes that same path as a literal, never derived from
   `var.tunstrap`.
3. `--output-var` (var form) — one-shot `plan && apply` in the same
   invocation only. No saved-plan-reuse exemption of any kind, including via
   a locator — corrected from entry 11's original recommendation.

**Consequences.** Every example in the design doc and the recipe using
`var.tunstrap_session_dir` (or any locator pattern) is deleted, not adapted —
there is no variant of the locator that survives this correction. The
recipe's Mode B (design doc, "Documentation") is rewritten to show the
literal-path pattern exclusively for the plan-safe case, with the var form
demoted to explicitly one-shot. See the design doc's "Delivery" and
"Stability contract" subsections (rewritten, iteration 6) for the shipped
contract, and entry 11 above (annotated, not rewritten) for the decision
this corrects.

## 15. Naming collision detection: validation-time check, not structural prevention

**Context.** Entry 4 claimed the `tunstrap-<node>-<target>` join was unique
by construction. False: `_FETCH_FILES_KEY_RE` (`schemas.py:11`) permits
internal hyphens in both `node` and `target`, and the join itself uses a
hyphen, so `(node="a-b", target="c")` and `(node="a", target="b-c")` both
render `tunstrap-a-b-c`. This is a different defect class from the mandatory
k3s-style collision test (entry 4 area / design doc "Testing contract"):
that test proves *upstream* kubeconfig names colliding is fixed by the
rename; this defect is tunstrap's *own* scheme colliding with itself,
independent of any upstream content.

**Alternatives considered.**

- **Change the join separator** (e.g. a character neither `node` nor
  `target` can contain) to prevent the collision structurally rather than
  detect it: rejected as a larger, unrequested change — it alters the
  user-visible naming scheme itself (`tunstrap-<node>-<target>`'s exact
  rendered form), which nothing in the ticket or the pivot asked to change,
  for a defect a validation check closes just as completely.
- **Tighten `_FETCH_FILES_KEY_RE` to forbid hyphens in `node`/`target`
  entirely**: rejected — this is a shared regex used for `fetch_files`,
  `kube_targets`, and node keys generally (`schemas.py:11`, `_validate_
  identifier_key`); narrowing it to solve a kube-identity-naming problem
  would remove a legitimate character from every other identifier in the
  schema for an unrelated reason, and does not fully solve the problem
  either (two *node* names differing only in an internal hyphen could still
  collide against two different *target* names symmetrically).
- **Validation-time collision check across every `(node, target)` pair**:
  **adopted** — computed once, at `InputSchema` validation, before any SSH
  connection is attempted; rejects the payload with an error naming the
  exact colliding pairs.

**Decision.** Add a collision check at schema-validation time: for every
`(node, target)` pair across all nodes' `kube_targets`, compute the rendered
`tunstrap-<node>-<target>` name; if any two pairs render the same string,
reject the whole payload with an error identifying both colliding pairs by
name. The hyphen join itself is kept unchanged.

**Consequences.** A new unit test drives exactly the `(a-b, c)` vs. `(a,
b-c)` pair (design doc, "Testing contract," R10/R14 section) — not covered
by the existing mandatory k3s-style test, which must not be assumed to also
exercise this defect class. See the plan for the concrete validator and its
test.

## 16. `predicted_env_keys` becomes a conservative superset predictor; the anti-drift guard becomes two-part

**Context.** Entry 3 had `predicted_env_keys` model the *exact* same
cardinality-conditional rule `render_kube_env`'s actual export uses. A
red-team finding, logic-verified, shows this under-reserves: `predicted_env_
keys` runs pre-spawn against the *input* schema's declared cardinality, but
the *actual* materialized cardinality can be **smaller** — an optional
(`required: false`) node or kube target can fail without failing the run
(`manager.py:99-107` already builds successful-only `connections`). Two
kube targets declared, one optional node fails → one file actually
materializes → the real export uses the `==1` branch (`KUBE_CONFIG_PATH`),
but the exact predictor would have predicted the `≥2` branch
(`KUBE_CONFIG_PATHS` only) and **not reserved `KUBE_CONFIG_PATH`** — a
`--output-var KUBE_CONFIG_PATH` would then pass the pre-spawn collision
check and be **silently overwritten** by the real export.

**Alternatives considered.**

- **Exact cardinality prediction** (entry 3's original design): rejected, per
  the failure mode above — it under-reserves whenever cardinality shrinks
  between input and output, which is a normal, expected outcome of the
  `required: false` feature this codebase already has, not an edge case.
- **Compute the predictor from a live probe of what will actually
  materialize** (e.g. attempt each kube target's fetch before validating):
  rejected — `predicted_env_keys` is explicitly a **pre-spawn**, no-SSH-yet
  check (its whole purpose is rejecting a bad `--output-var` NAME before a
  daemon exists); making it probe live connectivity would defeat that
  purpose and reintroduce the exact daemon-orphan risk window the pre-#15
  design's "Cleanup must own the whole post-spawn window" invariant was
  written to close.
- **Reserve conservatively: whenever any node declares `kube_targets` at
  all, reserve all three kube names, regardless of exact count**: **adopted**
  — deliberately over-reserves; the asymmetry is the point. Over-reserving
  can only reject *more* `--output-var` names than strictly necessary (a
  cheap, immediately visible usage error); under-reserving risks a silent
  post-spawn collision, which is the exact failure this check exists to
  prevent.

**Decision.** `predicted_env_keys` reserves `{KUBECONFIG, KUBE_CONFIG_PATH,
KUBE_CONFIG_PATHS}` whenever any node's input schema declares
`kube_targets`, unconditional on exact count. `render_kube_env`'s actual
export (entry 3) is unaffected and stays exact, cardinality-conditional —
only the predictor changes.

**The anti-drift guard becomes two independent tests, not one equality**
(superseding entry 13's single-equality retarget, see the note there):

1. A **formula test** (exact equality, unit-test style): `predicted_env_
   keys(schema)` equals a hand-computed expected set for a representative
   schema, proving the conservative *formula* is implemented correctly.
2. A **safety-envelope test** (subset, the actual anti-drift property):
   `set(actual injected keys) ⊆ predicted_env_keys(schema)`, driven by a
   **cardinality-shrink** scenario (two kube targets declared, one optional
   node fails, one file materializes) — the case that would falsify a
   predictor that got the direction of the conservatism backwards.

**Consequences.** `predicted_env_keys`' own unit tests gain a case proving
the conservative reservation fires on *any* `kube_targets` presence, not
just above some count threshold. The design doc's "Anti-drift guard
extension" and "Env-export contract" sections carry the same correction; the
plan's Task 3 (formula) and Task 5 (safety-envelope) guard literals are
rewritten accordingly.

## 17. Materialization writer: true atomic replace, not mode-fixed-at-creation alone

**Context.** Entry 11 (and the design doc, prior to iteration 6) described
the materialization write as reusing "the atomic-secure-write primitive,"
`SessionDir._write_file` (`session.py:132`, `os.open(path,
O_CREAT|O_WRONLY|O_TRUNC, 0o600)`). A red-team finding: `O_TRUNC` + write in
place is **not** atomic — a reader (a consumer's `file()` call racing a
`run` restart that rewrites the same pinned path, entry 14's mode 2) can
observe a truncated-but-not-yet-rewritten file mid-write. `_write_file`'s
real, load-bearing property is **mode-fixed-at-creation** (no separate
`chmod`, no window of broader-than-`0600` permissions) — a different
property from atomicity, conflated by the word "atomic" in earlier text.

**Alternatives considered.**

- **Keep `O_TRUNC` + write in place** (matching the existing kube-file
  primitive exactly): rejected — the kube-file primitive never needed true
  atomicity, because nothing reads a kube-target file mid-write the way a
  `file()` call racing a `run` restart against the *same pinned path*
  (entry 14's mode 2, which did not exist for kube files pre-pivot) could.
  The unified-output file's own delivery contract creates the race this
  primitive was never exposed to before.
- **Temp file + `os.replace()` (true atomic rename)**: **adopted** — create
  the temp file in the same directory with `os.open(tmp, O_CREAT|O_WRONLY|
  O_EXCL, 0o600)` (mode still fixed at creation, `O_EXCL` only guards a
  colliding temp name), write the content, then `os.replace(tmp, final)` — a
  single filesystem rename, atomic on the same filesystem, so a reader can
  never observe a partial write.

**Decision.** The writer combines both properties: mode-fixed-at-creation
(inherited from the existing primitive's approach) **and** true atomicity
(the `os.replace` step, which the existing primitive alone does not
provide). **Process constraint, stated explicitly:** this writer runs in the
CLI **parent** process (`run_command`), which holds no live `SessionDir`
instance (kube materialization happens daemon/worker-side, inside the
process that does own one) — the unified structure is a pure transformation
of the already-complete `OutputSchema` the parent already has, so no daemon
round-trip is needed to write it. `SessionDir._write_file` is reused
directly only if refactored to be callable without a live instance;
otherwise the primitive (not necessarily the same function object) is
replicated inline in `cli.py`. The design doc's "reusing" language is
corrected to reflect this either/or, not a flat claim of code reuse.

**Consequences.** New code (`os.replace`-based atomic write), not a pure
reuse of `SessionDir._write_file` as earlier text implied. The design doc's
"Materialization write mechanism" subsection (new, iteration 6) and the
plan's Task 5 carry the concrete implementation. `SessionDir._write_file`'s
own description, wherever it appears, is corrected to drop "atomic" and say
"mode-fixed-at-creation" instead — a real property, just not this one.

**Stdin-mode guard, also recorded here.** A stdin-supplied `InputSchema`'s
`daemon.materialize` is the caller's own explicit statement, and `start`
(unlike `run`) does not force it true (`cli.py:160-174`). Under the
now-unconditional `render_kube_env` call (entries 10/13), a declared but
unmaterialized kube target (`path is None`) makes `render_kube_env` raise
`ValueError` — existing, unchanged behaviour, but newly reachable from
`start --output env`'s stdin-payload path since that call is now
unconditional. The plan must guard this explicitly (force materialization
for that path, or map the `ValueError` to a typed, user-facing error) rather
than let it surface as a bare traceback.

## 18. Relationship to #14: re-adopting fixes 1 and 4 for non-kube data, deferring fix 3

> **[R16, iteration 7 — correction annotation.]** The "Decision" below
> re-adopts #14 fix 1 (pin the session/state root) as an opt-in
> precondition. **That re-adoption is retracted by iteration 7**: fix 1 is
> no longer re-adopted in any form — the user's own constraint (session dir
> stays mandatory *lifecycle* infrastructure, but is not a consumer-facing
> pinning mechanism) settles this the way the ticket itself originally
> argued. Fix 4 (materialized file + `file()`) is still re-adopted, but its
> shape changes: located via the env-carried `TUNSTRAP_OUTPUT_FILE`, not a
> pinned path. Fix 3's deferral is unaffected. Kept verbatim below per the
> annotate-don't-rewrite discipline; see entry 19 for the superseding
> decision.

**Context.** Ticket #15 explicitly supersedes most of #14. This pivot's
non-kube (port) delivery mechanism (entry 14, mode 2) is, in substance, two
of #14's own original fixes — worth recording as a deliberate re-adoption,
not silently reinventing a third mechanism that happens to look similar.

**Alternatives considered.**

- **Invent a new mechanism distinct from anything #14 proposed**: rejected —
  there is no reason to; #14's fix 1 (pin the session/state root) and fix 4
  (materialized file + `file()`) are exactly the plan-safe mechanism finding
  #1 measures, and #15's own rejection of them was scoped to kube
  specifically (an env-native alternative existed there), not to data in
  general.
- **Treat #14 fix 3 (warn on a saved-plan-capturing invocation, e.g.
  `-out=`, while non-plan-safe delivery is in use) as in scope for #15**:
  rejected — it is a genuinely separate feature (parsing the child's own
  command line for Terraform-specific flags inside generic `run`), which the
  pre-#15 design deliberately confined to `tunstrap_tofu` alone rather than
  adding to `run` itself; taking it on here would re-litigate that
  confinement as a side effect of an unrelated pivot.

**Decision.**

- **#14 fix 1** (pin the session/state root): re-adopted as an **opt-in
  precondition** — a caller-supplied, stable `--session-dir` — not a
  default. #15's rejection of pinning-by-default holds for kube (env-native
  alternative exists) and does not extend to ports (no env-native
  alternative exists at all).
- **#14 fix 4** (materialized file + `file()`): re-adopted for the same
  reason — the only plan-safe mechanism available once fix 1 provides a
  stable path.
- **#14 fix 3** (CLI warning on `-out=`-style saved-plan capture): **out of
  scope for #15, deferred to #14.** The stability contract and the recipe's
  explicit warnings cover the risk in documentation.

**Consequences.** The design doc's "Relationship to #14" subsection (new,
iteration 6) is the durable record a future #14 implementer reads before
picking fix 3 back up; it also appears in "Out of scope" so an implementer
of *this* ticket sees the deferral as deliberate, not as a gap.

## 19. Unified env-native materialization contract: content on disk, paths in env — supersedes entries 14/18's pinned-path delivery and the pre-#15 fetch_files var-carriage decision

**Context.** Iteration 7 is the user's own confirmed direction after the
red-team round, plus one added constraint. Two things about the shipped
iteration-6 design were unsatisfying on reflection: (1) entry 14's mode 2
re-purchased plan-safety for ports by re-adopting #14 fix 1 (a pinned
`--session-dir`) — exactly the mechanism ticket #15's own text explicitly
rejected ("session root can stay ephemeral; only the path to the kubeconfig
has to be stable, and that is supplied through the environment"), re-adopted
anyway on the grounds that ports have no env-native alternative; (2) the
pre-#15 design's decision to let `fetch_files[*].content_b64` ride
`--output-var` unprojected, carried forward unexamined through every
iteration of this design, put arbitrary remote file content into a
Terraform variable — and therefore into any saved plan file — by default,
for any consumer who bound `--output-var` at all, independent of whether
they read `content_b64` themselves.

**Alternatives considered.**

- **Keep entry 14's three-mode design (mode 2's pinned path) as-is**:
  rejected — the user's own instruction settles this directly, and on
  reflection the pinned-path re-adoption was solving ports' plan-safety
  problem by quietly reintroducing the exact mechanism the ticket rejected,
  just relabelled as "opt-in." Retaining it would leave the design
  permanently answering "does the ticket's rejection of fix 1 hold?" with
  "yes, unless you want ports to be plan-safe," which is not an honest
  reading of a rejection the ticket stated without that carve-out.
- **Keep `fetch_files[*].content_b64` riding the var form inline, documented
  with a warning** (the pre-#15 decision, carried through entries 1-18
  unexamined): rejected — a warning is weaker than removing the exposure
  entirely, and removing it is cheap: the daemon already owns the session
  dir and already materializes kubeconfigs the same way (entry 3), so
  extending the identical mechanism to fetched files is not new
  infrastructure, only a new call site of infrastructure that already
  exists and is already trusted for credential-bearing kube data.
- **[Adopted, with the user's added constraint folded in.]** Content lives
  on disk, under the (still-ephemeral) session dir, mode `0600`, via the
  atomic-replace primitive (entry 17): kubeconfigs (already so, entry 3),
  `output.json` (already so, entry 17), and now fetch_files. Env carries
  only paths/locators — `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` (unchanged),
  `TUNSTRAP_OUTPUT_FILE` generalized from a `start`-only bootstrap scalar
  (entry 13) into the primary locator for `run` too, plus the session
  scalars. `--output-var` survives, narrowed to its one genuinely remaining
  job: a bridge for bare `tofu`, which has no `get_env(...)`-equivalent and
  cannot read the process environment from HCL at all without a variable
  binding. **User's added constraint, folded in rather than treated as a
  separate decision**: the session dir itself does not become optional or
  disappear just because it stops being a consumer-facing plan-safety
  mechanism — it remains required *process lifecycle infrastructure*
  (`daemon.pid`, `session.lock`, `stop`/recovery), a different concern from
  whether a consumer's HCL may assume its path is stable across
  invocations.

**Decision.**

1. Delivery collapses from entry 14's three modes to two: Mode A (kube
   env-native, unchanged) and Mode B (`TUNSTRAP_OUTPUT_FILE` → the unified
   manifest file, `get_env(...)`/`file()`/`jsondecode`/`try()`), with
   `--output-var` as Mode B's narrow bare-`tofu` bridge, not an
   independently-documented third mode.
2. `fetch_files` entries in every consumer-facing channel become
   `{path, size, sha256}` (or `{error}`), never `content_b64` — the daemon
   materializes fetched bytes to `tunnel-data/<node>-<fetchname>` (mode
   `0600`, atomic replace, mirroring entry 3's kube materialization exactly).
   `FetchedFile.content_b64` itself is not removed from the model — it stays
   internal plumbing between the SSH fetch and the on-disk write, exactly as
   `KubeTargetOutput.content_b64` already does for kube (entry 3); `start`'s
   raw default JSON stdout is unaffected, per the existing scope carve-out.
3. Entry 14's mode 2 (pinned `--session-dir` + literal HCL path) is dropped;
   entry 18's re-adoption of #14 fix 1 is retracted (fix 4 survives,
   reshaped to the env-carried locator).
4. The session dir stays mandatory, ephemeral, lifecycle infrastructure —
   this does not change; only its role as a *consumer-facing* mechanism is
   retracted.

**Consequences.** Plan-safety across a tunstrap restart for ports and
`fetch_files` is gone — it existed only via the now-dropped pinned mode; a
consumer needing it has no supported mechanism beyond re-running `plan`
within the same tunstrap invocation. Kube's plan-safety is untouched (its
env-native channel never depended on any of this). The "never `--fetch`
secrets with `--output-var`" warning (entries carried since the pre-#15
design) is resolved **as a class**: content no longer rides any
consumer-facing channel, so the warning is retracted, not restated, in the
recipe. Every `docs/recipe_terragrunt.md` example using a literal pinned
path or `content_b64` breaks and is rewritten (design doc, "Documentation");
every test asserting `content_b64` presence in a consumer-facing envelope
(`--output-var`, materialized file) is retargeted to assert its absence and
a `path` field instead — the plan's Task 5 blast-radius table carries the
full enumeration. Entries 14 and 18 are annotated above, not rewritten, per
this document's own established discipline (entry 14's own annotation of
entry 11).
