# Kubeconfig-as-identity delivery: deterministic contexts + multi-node kube channel

> **Redaction/repoint note (2026-08-10):** Repointed provider evidence to its
> committed spec, replaced a local worktree path with a placeholder, and marked
> finding #4 as an unpublished-spike measurement because no automated test
> covers its saved-plan mutation scenario.

- Status: design, awaiting review
- Date: 2026-08-07 (revised same day, iteration 3: the unified-output-contract
  pivot, marked **[PIVOT]** at each affected section below)
- Scope: **[kube part, unchanged by the pivot]** rename the materialized
  kubeconfig's cluster/user/context identities deterministically per
  `(node, target)`; export the OpenTofu-provider-facing kube env vars
  (`KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS`) conditionally on how many
  kubeconfig files were materialized. **[PIVOT, iteration 3]** Replace
  tunstrap's *entire* consumer-facing output — ports, kube references,
  session metadata — with one unified, node-qualified structure, delivered
  both as an `--output-var` value and as a materialized JSON file, entirely
  superseding the flat `TUNSTRAP_<TARGET>_*` scalar channel (which is
  removed, not extended with a node dimension). Document the one recipe both
  parts enable. Ticket: [AlexMKX/tunstrap#15](https://github.com/AlexMKX/tunstrap/issues/15)
  (handoff, supersedes most of #14). Target branch: `feature/run-env-io`
  (PR #13).
- Measurement basis: the ticket's own six OpenTofu v1.12.5 findings (2026-08-06/
  07, **not re-derived here**); the provider-behaviour verification in
  `docs/specs/2026-08-10-issue15-provider-env-precedence.md` (OpenTofu v1.12.5,
  `hashicorp/kubernetes` v2.38.0, `hashicorp/helm` v2.17.0, live-probed against
  a real `kind` cluster, 2026-08-07); and untracked implementation-spike notes
  (six prototype variants, each run against the full `tests/unit` suite,
  2026-08-07). The provider result is repeated in the committed spec.
- Code citations (`kube.py:NNN`, `envrender.py:NNN`, `cli.py:NNN`) are against
  `e5ed15d`, the tip of `feature/run-env-io` at the time of writing (== the
  spike's `spike/issue15-variants` base commit).
- Reference implementation: `variant/combined` in the scratch worktree
   `<spike-worktree>` — reviewed,
  475/475 pre-existing unit tests pass plus one new regression test (476/476).
  **Cherry-pick precisely, not wholesale — corrected iteration 6.** The spike
  is safe to cherry-pick for exactly two things: `rename_identities` (V1c)
  and the `render_kube_env` split (Axis 2). **Its Axis 3 (the unconditional
  superset env export) is *rejected*, not adopted** — an earlier revision of
  this note credited the spike with "the conditional cardinality contract,"
  which is false: the spike never prototyped the conditional contract this
  design actually ships (that is a post-spike, iteration-1 design decision,
  built *in reaction to* the spike's Axis 3 and the provider findings, not
  cherry-picked from it). Do not copy the spike's `render_kube_env` body
  verbatim for its env-export tail; only the node-count-agnostic path
  collection is reusable, the export-key selection is not. **`cli.py`
  wiring was never in the spike at all** — the spike's own findings document
  raised it only as an open question (open question 1: whether wiring
  `render_kube_env` into `run` was in scope), never as a prototyped variant;
  every line of `_build_child_env`'s wiring in this design and the plan is
  new work, not a cherry-pick. **Scope note, otherwise unchanged: the spike
  predates the iteration-3 pivot and covers the kube part only** — nothing
  in it prototypes the unified output contract below, and the spike's
  `render_env`-delegation mechanism for wiring `render_kube_env` into `run`
  (iteration 2's two-branch `_build_child_env`, itself new work built after
  the spike, not from it) is superseded by a simpler unconditional call once
  the scalar channel it branched around is removed — see "The unified
  output contract" and the plan's Task 5.

## Problem

Connection data currently travels through two channels that were never
designed for what the ticket calls the actual shape of the domain:

1. **Terraform input variables**, which OpenTofu persists into the plan file —
   any live value bound there (a private key, a materialized kubeconfig)
   becomes durable, pipeline-archived state (design spec
   `2026-07-31-run-env-io-and-tofu-proxy-design.md`, `RunKubeTarget`'s
   allow-list rationale).
2. **The `TUNSTRAP_*` scalar channel**, which has no node dimension:
   `render_env` raises `MultiNodeEnvUnsupported` outright for `len(nodes) != 1`
   (`envrender.py:26-30`), because `TUNSTRAP_<TARGET>_*` has no way to
   disambiguate two nodes sharing a target name. **[PIVOT, iteration 3]** The
   fix adopted below is not "add a node dimension to the scalars" — a flat
   `KEY=VALUE` shape has no natural place to put one without inventing a
   second encoding scheme inside the key name. The scalar channel is instead
   **removed outright** and replaced by a structure whose node dimension is
   just a normal nested key, because that is what a node dimension actually
   is. See "The unified output contract" below.

Two framing corrections drive the fix. The bolded lead phrase in each bullet
is close to the ticket's own words; **the explanatory sentence after it is
this design's own re-reading, not a ticket quotation** — an earlier revision
of this section claimed both bullets were "from the ticket verbatim" in
full, which overstated how much of the surrounding prose is actually the
ticket's own, corrected here:

- **Multi-node is the base path, not an edge case.** [Design's own
  elaboration, not the ticket's words:] the single-node assumption in
  `render_env` was never a deliberate design choice — it is an artefact of
  the scalar channel's own limitation, wrongly generalized to the whole kube
  delivery mechanism.
- **Kubeconfig contexts are the natural addressing mechanism, and tunstrap was
  not using them.** [Design's own elaboration:] a kubeconfig set with one
  context per target addresses any number of nodes/targets and has no
  node-dimension problem — *if* the context
  names are actually distinct. Today they are not: `kube.py:99` sets
  `context_name=current` from the source document's own `current-context`
  verbatim, and `dump_kubeconfig` serializes that same document unchanged
  (see "Correction to the ticket text" below for the precise division of
  labour). Two k3s targets — which both ship `current-context: default` — thus
  collide irreducibly on merge; this is the expected case, not an edge case
  (see "The collision trap", below).

The same defect exists beyond OpenTofu: `kubectl --context`, helmfile, ArgoCD
and anything else consuming a materialized kubeconfig sees whatever name the
upstream cluster happened to pick. This is a general contract fix to the
materialized kubeconfig's identity, not a Terraform patch — the env-export
contract (below) is the one part that is Terraform/OpenTofu-shaped, because it
exists to feed a provider.

## The deterministic-naming contract

Naming scheme for **context, cluster and user alike**:

```
tunstrap-<node>-<target>
```

- **NOT unique by construction — a real collision surface, closed by an
  explicit check [R10, corrected iteration 6].** An earlier revision of this
  design claimed the join was unique because `node`/`target` are validated
  identifiers. That claim is false: `_FETCH_FILES_KEY_RE`
  (`schemas.py:11`, `^[a-zA-Z_][a-zA-Z0-9_-]*$`) permits internal hyphens, and
  the render itself joins with a hyphen (`tunstrap-<node>-<target>`), so two
  **different** `(node, target)` pairs can render the **identical** string:
  `(node="a-b", target="c")` and `(node="a", target="b-c")` both produce
  `tunstrap-a-b-c`. This is a real, not theoretical, collision, and the
  mandatory k3s-style regression test (below) does **not** cover it — that
  test proves *upstream*-name collisions are fixed by the rename; this is a
  different defect class entirely (tunstrap's *own* naming scheme colliding
  with itself, upstream names never entering the picture). **Fix: a
  validation-time collision check** across every `(node, target)` pair in the
  whole payload (all nodes × each node's `kube_targets`), computed at schema
  validation — before any SSH connection is attempted — rejecting the
  payload with an error naming the exact colliding pairs if any two joined
  names coincide. The hyphen join itself is kept (changing the separator is
  a larger, unrequested change); the fix detects and rejects the collision
  rather than structurally preventing it.
- **Node-qualified**, so it stays unique across multiple nodes *for a given
  join*, closing the exact gap `render_env`'s node-blindness left open — this
  property is real and unaffected by the join-collision defect above, which
  is about two *different* joins coinciding, not about the node dimension
  itself being absent.
- **Fixed `tunstrap-` prefix**, giving tunstrap's own contexts **conventional,
  probabilistic namespacing** against whatever else a consumer's kubeconfig
  set already carries — not a guarantee. **Residual risk, documented:** a
  consumer-owned context already literally named `tunstrap-<node>-<target>`
  for the same `(node, target)` pair collides on merge (accepted — an
  operator choosing that exact name is choosing to alias tunstrap's own
  scheme); two **independent** tunstrap runs whose node/target names happen
  to coincide also collide (accepted per the no-configurable-prefix decision
  below — the node name is assumed unique *within* one run's payload, not
  across unrelated runs an operator chooses to merge).
- **No configurable prefix.** Org rule: avoid excessive configurability. The
  node name already solves the "merge two separate tunstrap runs" scenario a
  configurable prefix would otherwise be reached for, for the common case
  where the operator controls both runs' node names; it does not solve two
  runs an operator merges without also controlling their node-naming
  overlap, which is the residual risk stated above.

All three identity strings (cluster name, user name, context name) get the
**same** rendered value — there is no reason for them to diverge, and a single
shared name is what a `kubectl config get-contexts` or `--context` invocation
actually needs to be unambiguous.

### Rename scope: the active triple only

Only the entries the current-context actually resolves to are renamed —
**but every reference to them must be updated, including references that
live inside otherwise-ignored entries [R14, corrected iteration 6]:**

- the `contexts[]` entry named `doc["current-context"]`, plus its
  `context.cluster` / `context.user` references;
- the `clusters[]` entry that reference resolves to;
- the `users[]` entry that reference resolves to;
- `doc["current-context"]` itself;
- **any *other* `contexts[]` entry (one already reported via
  `ignored_contexts`, since it is not the current context) whose own
  `context.cluster` or `context.user` happens to reference the *same*
  cluster/user name being renamed** — a kubeconfig can legitimately have two
  contexts sharing one cluster or user entry (e.g. two contexts against the
  same cluster with different users). Leaving such a reference unrenamed
  while the entry it points at *is* renamed produces a dangling reference:
  the ignored context would name a cluster/user that no longer exists under
  that name anywhere in the document, which is strictly worse than the
  pre-rename state (a mis-typed context that fails immediately if selected,
  rather than an odd-but-working one).

**Every entry that is neither part of nor referencing the active triple
remains byte-stable, unrenamed** — a narrower, correct claim than an earlier
revision's "every other context/cluster/user... is left byte-stable," which
did not distinguish "genuinely unrelated to the active triple" from
"unrelated as far as being the *current* context, but still pointing at the
same cluster/user by name." The warning for skipped contexts is emitted in
`run_kube_targets` (`kube.py:330-337`, not `parse_kubeconfig`'s
`_ignored_contexts` collection helper at `kube.py:179-183`, which only
*computes* the list — the warning itself is logged where that list is
consumed). This matches the module's pre-existing, documented contract:
*"One kube_target maps to exactly one cluster: the kubeconfig's
current-context. Other contexts/clusters are ignored and left byte-stable in
the patched output"* (`kube.py:1-8`) — read now as "left byte-stable" meaning
"not independently re-targeted," not "guaranteed to still reference their
original names once a shared entry is renamed." The rename does not change
that contract's scope, it only fixes the one triple tunstrap already claims
ownership of, correctly this time.

**Accepted residual risk, documented, not solved here:** if an upstream
kubeconfig carries *other* (non-current) contexts that also collide across two
materialized files, that merge exposure is not addressed by this change. This
is accepted under the ticket's own "one cluster per kube_target" framing, and
is the normal case in practice — k3s and kind both ship single-context
kubeconfigs. If it becomes a real problem, the fix is pruning ignored entries
entirely at materialization time, which is a larger, separate change (see
"Open questions" in the spike findings).

### Where the rename happens

`rename_identities(doc, node, target) -> str` — a standalone **deterministic
in-place transformation** in `tunstrap/kube.py` (not a pure function in the
strict sense: it mutates `doc`, the same ruamel document `parse_kubeconfig`
returned, rather than returning a new one — "standalone" and "deterministic"
are the properties that actually matter here, not side-effect-freedom). It
operates on the raw parsed document alone: it resolves
`doc["current-context"]` itself, so it needs no `KubeconfigView` and can be
unit-tested with a bare dict, independent of `parse_kubeconfig`,
`run_kube_targets`, or any SSH-driven orchestration. It returns the new name
(shared by cluster, user and context), and the caller — `run_kube_targets`,
between `patch_view` and `dump_kubeconfig` — updates `KubeTargetOutput` from
that return value.

This was one of three placements prototyped and measured (spike findings,
"Part 2 — variant comparison"; ADR entry "Rename placement"); the alternatives
(rename inline inside `run_kube_targets` mutating `KubeconfigView` in place; or
rename as a side effect of `dump_kubeconfig`) both work with zero test
breakage too, but neither is independently unit-testable without going
through the fuller orchestration, and the `dump_kubeconfig` placement couples
"serialize to bytes" with "mutate identity" — see the correction note below
for why that coupling is specifically worth avoiding here.

### Correction to the ticket text

The ticket states: *"`dump_kubeconfig` serialises that same document with only
the server address patched."* This describes the **combined effect** of
`patch_view` followed by `dump_kubeconfig` in sequence, not a responsibility of
`dump_kubeconfig` itself. Precisely:

- `patch_view` (`kube.py:243-270`) is what rewrites `server:`, sets
  `tls-server-name` or the insecure pair — on the current-context cluster
  only.
- `dump_kubeconfig` (`kube.py:273-277`) is a **pure serializer**: it does not
  patch anything, it YAML-dumps `view.doc` exactly as it finds it.

This spec's rename call site sits between the two (`patch_view` → *rename* →
`dump_kubeconfig`), and `dump_kubeconfig` gains no new responsibility — it
stays a pure serializer, which is also why the rename is a standalone function
rather than a `dump_kubeconfig` parameter (the placement the spike prototyped
and rejected for exactly this reason).

## The unified output contract [PIVOT, iteration 3]

**This section is the overarching decision this design was revised around; it
supersedes the "scalar channel stays single-node" framing everywhere else in
this document.** User decision (encoded, not re-litigated here; ADR entries
10-13 carry the alternatives-considered detail).

### Shape

The entire consumer-facing output — ports, kube references, session metadata
— is one JSON structure, node-qualified by construction (the node dimension
is a nested key, not an encoding problem):

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
      "ports": {
        "service1": "127.0.0.1:5432"
      },
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

- **Ports**: a plain `"host:port"` string per target — the minimal,
  consumer-friendly shape the user's own sketch names (`node1 { service1:
  hostport }`).
- **Kube**: a small object per kube target — `{path, context, endpoint}` —
  never credentials, never file content (U4; the existing
  `RunKubeTarget`/`KubeTargetOutput` credential-scrubbing already established
  in the pre-#15 design is preserved, just reshaped). `context` is the
  post-rename `tunstrap-<node>-<target>` name from the kube part above, so a
  consumer that wants to address the cluster by context rather than by
  `config_path` can (`kubectl --context "$(jq -r ...)"`, or a provider's
  `config_context` field with `config_path` also set).
- **`fetch_files`** — **[R16, supersedes this bullet's pre-iteration-7 text,
  which is retracted, not extended]** no longer rides through unprojected.
  The pre-#15 design's own choice to let `content_b64` ride the var form
  verbatim (`docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md`,
  decision history's `fetch_files[*].content_b64` entry) is superseded: R16's
  core principle — *content on disk, paths in env* — extends to fetched files
  the same way it already applied to kubeconfigs. The daemon (which already
  owns the session dir and already materializes kube files there) writes
  each fetched file's bytes to `tunnel-data/<node>-<fetchname>` (mode `0600`,
  the same atomic-replace primitive as "Materialization write mechanism"
  below) and the consumer-facing entry is exactly `{path, size, sha256}` — no
  `content_b64` anywhere in it, mirroring `UnifiedKubeRef`'s own
  `{path, context, endpoint}` narrowing (U4, above) rather than being a new
  pattern. A failed fetch still projects `{"error": "..."}`, unchanged. See
  "Fetched-file materialization [R16, new]" below for the mechanism and
  "Compatibility" for why this is a breaking change stated plainly, not a
  silent narrowing.

**Judgment call, not literally specified by the user's sketch:** the root
object has exactly two reserved top-level keys, `session` and `nodes`, rather
than putting node names directly at the document root. A flat root (node
names as literal top-level keys, matching the sketch most literally) was
considered and rejected: `node` names are operator-controlled identifiers up
to 64 characters matching `^[a-zA-Z_][a-zA-Z0-9_-]*$` (`schemas.py:14-22`) —
an operator is free to name a node `session` or `nodes`, which would collide
with the reserved top-level keys themselves in a flat root, with no
validation catching it (a node literally named `warnings` would only collide
if `warnings` were also hoisted to the document root — it is not, in the
adopted shape, since `warnings` lives nested under `session`; the flat-root
collision example is `session`/`nodes` colliding with themselves, not
`warnings`). Two reserved keys eliminate that collision by construction, at
the cost of one extra nesting level from the literal sketch. `session_dir`
and `pid` are kept inside `session`, alongside `warnings`, purely for
grouping and symmetry with the rest of the document (every piece of
non-node-scoped metadata lives in one place) — not for a second collision
reason, since neither `session_dir` nor `pid` would themselves collide with
anything at a bare top level.

### Delivery: two independent modes [R16, iteration 7 — supersedes R9's three-mode design]

**[R16] Iteration 7's user-confirmed direction retracts R9's mode 2 (the
literal-pinned-`--session-dir` file), collapsing delivery from three modes to
two.** R9's own reasoning for modes 1 and 3 below is unchanged and restated
here, not re-litigated; only mode 2 is gone, and `TUNSTRAP_OUTPUT_FILE`
(previously a `start`-only bootstrapping scalar, see "The scalar channel is
removed" below) is generalized into mode 2's replacement — the **primary**,
env-carried locator for the unified manifest, for `run` as well as `start
--output env`. This is the direction the user confirmed after the red-team
round: **content lives on disk under the (ephemeral) session dir, only paths
travel through the environment** — never a pinned, operator-chosen root the
consumer's HCL has to independently know in advance.

R9's original three-mode framing (superseded by the two-mode list below —
not reproduced verbatim here; ADR entry 14 carries the original text so the
retraction reads as a decision, not a silent deletion) is retracted for the
reason the user's own instruction states directly: ticket #15 explicitly
rejected #14 fix 1 ("session root can stay
ephemeral; only the path to the kubeconfig has to be stable, and that is
supplied through the environment") — R9's mode 2 **was** fix 1, re-adopted
for ports against the ticket's own explicit rejection, on the grounds that
ports have no provider-native env path the way kube does. Iteration 7
retracts that re-adoption instead of continuing to defend it: root stays
ephemeral, unconditionally, and ports' plan-safety story is a genuine loss
(see "Stability contract," "what is lost," below) rather than being
purchased via a pinned path. See "Relationship to #14 [R15]" below for the
corrected, no-longer-re-adopting-fix-1 text, and ADR entries 14/18/19 in the
decision history.

**Iteration 4's design is retracted below, not extended.** It treated the
unified structure as delivered by one hybrid mechanism — inject `var.tunstrap`
and *also* materialize a file, with the recommended plan-safe pattern being
"read `var.tunstrap` only to locate the file, then `file()` the located
path." A three-model red-team review found this unsound for three
independent, compounding reasons (findings #1/#5):

1. **Finding #1 measured content-change tolerance at a *stable* path.** The
   locator pattern instead put a *changing* value — `var.tunstrap`, whose
   full JSON differs on every invocation (fresh `pid`, `started_at`, ephemeral
   local ports, a fresh kube `path`) — into a Terraform variable and called
   that "safe" because only one field of it (`session.session_dir`) happened
   to be read. That does not help: OpenTofu's plan-variable consistency check
   (finding #6) compares the **whole bound value** of a root-module variable
   between `plan` time and `apply` time, not just the sub-fields an
   expression happens to reference. A locator built from `var.tunstrap` trips
   finding #6 on any saved-plan reuse exactly as readily as binding the ports
   directly would — the indirection buys nothing.
2. **Finding #6 fires on the *whole* variable, confirmed by (1).**
3. **The default session directory does not survive to be located.** Without
   a caller-supplied `--session-dir`, `run` auto-mints an ephemeral root via
   `tempfile.mkdtemp` (`cli.py:427`, `_mint_session_dir`) and teardown deletes
   it. Even setting aside (1)/(2) entirely, a locator pointing at that default
   root would usually be pointing at a directory that no longer exists by the
   time a later, separate `apply` invocation tried to read it.

**[R16] Revised contract: two genuinely independent delivery modes.** Mode
1 is unchanged from R9. Mode 2 replaces R9's modes 2 and 3 combined: the
env-carried `TUNSTRAP_OUTPUT_FILE` locator (a real env var, not a Terraform
variable) is now the *only* way a consumer reaches the unified manifest, for
both a plain-shell reader and an HCL one — `--output-var` survives only as a
narrower fallback for a caller that cannot read the process environment at
all (bare `tofu`, see below), not as a second, independently-named mode:

1. **Kube env channel** (`KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS`) — unchanged,
   primary for kube, plan-safe per findings #1/#3 (see "Env-export contract"
   below), no variable and no file read anywhere in the path at all.
2. **`TUNSTRAP_OUTPUT_FILE` → the unified manifest file** — the session dir
   stays ephemeral, unconditionally (no `--session-dir` precondition, no
   caller-pinned path); `run` (and `start --output env`) exports
   `TUNSTRAP_OUTPUT_FILE=<session_dir>/tunnel-data/output.json` as a plain
   process env var and the consumer reads it via `get_env(...)` (Terragrunt)
   or `os.environ[...]` (a plain shell/Python child) — never a literal path
   baked into the consumer's own config, because the path is fresh, ephemeral,
   and different on every invocation by design. This is **one-shot,
   `plan && apply` within the same tunstrap invocation only** — the same
   restriction R9's mode 3 stated for the var form, now stated for the file
   form too, because the file itself is deleted at `stop`/teardown alongside
   the rest of `tunnel-data/` (see "session dir as lifecycle infrastructure,"
   below): there is nothing left to `file()` on a later, separate `apply`
   against a saved plan from a prior `run`. Ports and `fetch_files` have no
   plan-safe-across-restart story left at all — the honest loss R9's mode 2
   used to paper over (see "Stability contract," "what is lost").
   ```hcl
   locals {
     tunnel = try(
       jsondecode(file(get_env("TUNSTRAP_OUTPUT_FILE"))),
       { nodes = {} },
     )
   }
   ```
3. **`--output-var NAME` (var form) — bridge for bare `tofu` only**, which
   cannot read `get_env(...)` (a Terragrunt function) or, for that matter,
   any process env var directly inside HCL at all without a variable
   binding. Same one-shot restriction as mode 2, for the same reason
   (finding #6: OpenTofu's plan-variable consistency check compares the
   variable's whole bound value between `plan` and `apply`, and there is no
   locator exemption — reading only a sub-field does not narrow the
   exposure). Carries the lightweight manifest described in "The scalar
   channel is removed," below — ports as `host:port` strings, kube as
   `{path, context, endpoint}`, fetch_files as `{path, size, sha256}`, no
   `content_b64` anywhere (R16).

**No variable, anywhere, locates the materialized file.** `TUNSTRAP_OUTPUT_FILE`
is a plain env var, read by `get_env(...)`/`os.environ`, never by decoding
`var.tunstrap`/`TF_VAR_tunstrap` to extract a path — that indirection is
exactly what R9's finding #1 analysis (above) showed does not help, and
nothing about R16 changes that specific finding.

### Materialization write mechanism [R13, corrected iteration 6]

The mode-2 file (`<session_dir>/tunnel-data/output.json`, mode `0600`,
cleaned up on `stop`/atexit alongside the kube materialized files — same
directory, same lifecycle, since `fetch_files` content can carry arbitrary
remote file content and deserves the same handling) must be written as a
**true atomic replace**, not merely mode-fixed-at-creation:

- Create a temp file in the same directory with
  `os.open(tmp_path, O_CREAT | O_WRONLY | O_EXCL, 0o600)` (the `O_EXCL`
  guards against a colliding temp name, not a security property — the mode
  is already fixed at creation, as with the existing kube-file primitive).
- Write the full JSON content to it.
- `os.replace(tmp_path, final_path)` — a single filesystem rename, atomic on
  the same filesystem, so a reader can never observe a partially-written
  `output.json`. **`O_TRUNC` + write in place, used by the existing kube-file
  primitive, is *not* atomic** — a reader can observe a
  truncated-but-not-yet-rewritten file mid-write. **[R16, iteration 8 —
  rationale re-grounded]** An earlier revision justified this by a consumer's
  `file()` call racing a `run` restart that rewrites the *same pinned path* —
  that race no longer exists under R16 (there is no pinned path; mode 2's
  `TUNSTRAP_OUTPUT_FILE` names a fresh, per-invocation ephemeral path, and the
  write completes before the child is even spawned, so nothing can be reading
  the file concurrently with this process writing it under the current
  design). The requirement **stays** — it is strictly safer than `O_TRUNC`
  and costs nothing — but on grounds that do not depend on a race the design
  no longer has: (1) **torn-read prevention on crash mid-write** — if the
  writing process is killed between opening and finishing the write, `O_TRUNC`
  leaves a truncated file at the final path with no signal anything is wrong,
  where `os.replace` leaves either the old complete file or the new complete
  one, never a partial one; (2) **defense-in-depth** against any future
  change that reintroduces a stable/reusable path (this design already
  retracted one such mechanism once, R16's own retraction of R9's mode 2 —
  the atomic-replace property should not need re-deriving if that ever
  happens again); (3) **consistency between writers** — every `run` mints a
  fresh, ephemeral session dir (`tempfile.mkdtemp`), so in the current design
  no two invocations' `tunnel-data/` paths ever collide and the fetched-file
  writer (below) has exactly the same no-current-race property `output.json`
  does, for the same reason. Sharing one atomic-replace primitive between both
  writers is a maintainability argument, not a second race-prevention
  argument dressed up as one: one primitive to reason about instead of two,
  and reason (1) above (crash mid-write) applies identically to both. This is
  new work, not present in the primitive this design otherwise reuses.
- **Process constraint, stated explicitly:** this writer runs in the CLI
  **parent** process (`run_command`, `cli.py`), which holds **no
  `SessionDir` instance** — materialization of the kube files happens
  worker/daemon-side, inside the process that already owns a `SessionDir`,
  but the unified structure is a pure transformation of the already-complete
  `OutputSchema` the parent already has, so writing it parent-side needs no
  daemon round-trip. Reusing `SessionDir._write_file` (`session.py:132`)
  directly is only possible if it is refactored into something callable
  without a live `SessionDir` instance (or the parent is given one purely for
  this write, which the design does not otherwise need); if that refactor is
  not straightforward, the primitive above is replicated inline in `cli.py`
  instead — the plan permits either, this spec's "reusing" language should be
  read as "reusing the *primitive* (atomic, mode-fixed-at-creation write)," not
  necessarily reusing the *same function object* — an earlier revision's flat
  claim of reuse did not make this distinction.
- **`SessionDir._write_file`'s own real property, restated precisely:** it is
  **mode-fixed-at-creation** (`os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)`
  — no separate `chmod`, no window of broader permissions), **not
  "atomic"** in the sense that matters here (`O_TRUNC` overwrites in place,
  visible mid-write to a concurrent reader) — an earlier revision described
  it as "the atomic-secure-write primitive," which conflated the two
  properties. This design's materialization write needs *both*
  mode-fixed-at-creation (still true of the temp file above) *and* true
  atomicity (the `os.replace` step, which `_write_file` alone does not
  provide). **[R16, iteration 8 — rationale re-grounded]** The kube-file
  primitive's own gap was originally justified by a `file()` call racing a
  `run` restart against the *same pinned path* — that specific race is
  retired under R16 (no pinned path survives; see the note above this list).
  The gap is real for a different, still-live reason: torn-read prevention if
  the writing process is killed mid-write (a truncated-but-not-rewritten file
  at the final path is indistinguishable from a valid empty/short one to a
  naive reader, where `os.replace` guarantees the reader only ever sees a
  complete old or complete new file), plus defense-in-depth against any
  future change that reintroduces a stable/reusable path. `output.json`'s
  writer needs both properties for the same reasons stated there, not because
  of the retired race.
- **Stdin-mode guard, flagged not silently assumed:** a stdin-supplied
  `InputSchema` payload's `daemon.materialize` is the caller's own explicit
  statement and `start` (unlike `run`) leaves it alone rather than forcing it
  true (`cli.py:160-174`, `"a stdin payload's daemon.materialize is the
  caller's own statement and is left alone"`). Under the now-unconditional
  `render_kube_env` call, a kube target that was declared but never
  materialized (`materialize: false` in the stdin payload) has `path is
  None`, and `render_kube_env` raises `ValueError` for exactly that case
  (`envrender.py`, existing behaviour, unchanged by this design). This is
  fine for `run` (which always forces `materialize = True`, so the case
  cannot occur), but `start`'s `--output env` path can reach it with an
  operator-supplied stdin payload that explicitly disables materialization
  while still declaring `kube_targets` — the plan must guard this
  (materialization forced or the `ValueError` mapped to a typed,
  user-facing error) rather than let an unconditional call surface a bare
  `ValueError` traceback.

### Fetched-file materialization [R16, new]

Mirrors the kube-file precedent exactly, not a new pattern: `FetchedFile`
(`schemas.py:292-313`) gains a `path: str | None = None` field, the same
shape `KubeTargetOutput.path` already has alongside its own `content_b64`
(`schemas.py:317-336`). The daemon-side step that already materializes kube
files (worker/daemon process, holds a live `SessionDir`) gains a parallel
step: for each successful `FetchedFile` a node's `fetch_files` produced,
base64-decode `content_b64` and write the raw bytes to
`tunnel-data/<node>-<fetchname>` (mode `0600`, the same atomic-replace
primitive as `output.json`'s writer — temp file + `O_EXCL` + `os.replace`,
not `_write_file`'s mode-fixed-but-not-atomic `O_TRUNC`, for the same reason:
a consumer's `file()` call inside a provider/data block could race a `run`
restart rewriting the same node-qualified filename), then set `.path`
accordingly. A failed fetch (`FetchedFile.error` set) materializes nothing
and projects `{"error": ...}` unchanged. `content_b64` itself is **not**
removed from the `FetchedFile` model — it stays as internal daemon-side
plumbing between the SSH fetch and the on-disk write, exactly as
`KubeTargetOutput.content_b64` already does for kube; only the
**consumer-facing projection** (`render_unified_output`'s `fetch_files` entry,
and by extension the materialized file's and `--output-var`'s content) drops
it, per U4's already-established narrowing pattern for kube. `start`'s raw
default JSON stdout (the "complete envelope," unchanged scope per
"Compatibility," below) continues to show both `content_b64` and `path` on
`FetchedFile`, exactly as it already shows both on `KubeTargetOutput` today —
no new carve-out, the existing one already covers this symmetrically.

### Session dir: ephemeral, but not optional — lifecycle infrastructure [R16, user constraint]

**Stated explicitly per the user's own constraint, not left implicit.**
Retracting R9's mode 2 (above) removes the session dir's role as a
*consumer-facing* plan-safety mechanism, but the session dir itself does not
disappear and does not become any less mandatory. It remains required
**process lifecycle infrastructure**, unrelated to Terraform/consumer
concerns: `daemon.pid`, `session.lock`, and everything `tunstrap stop
--session-dir <dir>` and crash-recovery depend on to find and signal a
running daemon. Nothing about R16 changes any of that — `TUNSTRAP_SESSION_DIR`
and `TUNSTRAP_PID` stay exported exactly as before (they are session
*lifecycle* metadata, not a consumer-facing locator repurposed by R16), and
the existing `stop`/recovery guidance is unaffected. The only thing that
changed is what a *consumer's HCL* is allowed to assume about the directory's
path being stable across invocations — nothing changed about tunstrap's own
internal need for the directory to exist and be addressable while a session
is live.

### Stability contract (explicit) [R16, iteration 7 — supersedes R9's version]

- **Kube env channel: always plan-safe, unconditionally.** No caveat — no
  variable, no file, findings #1/#3. Unchanged by R16.
- **`TUNSTRAP_OUTPUT_FILE` (mode 2) and `--output-var` (mode 3): both
  one-shot `plan && apply` only, unconditionally.** No saved-plan reuse
  across a tunstrap restart for either — the file is deleted at
  teardown/`stop` alongside the rest of `tunnel-data/` (the session dir stays
  ephemeral, per the subsection above), and the var form was already
  one-shot-only under R9 (finding #6, no locator exemption). **This is a real
  narrowing from R9**: R9's mode 2 (the literal-pinned-`--session-dir` file)
  was plan-safe across restarts, unconditionally, given its precondition;
  that precondition — and the plan-safety it bought — is retracted along with
  it.
- **What is lost, stated plainly, not glossed over [R16.7]:** plan-safety
  across a tunstrap restart for **ports and `fetch_files`** is gone entirely
  — it existed only via R9's now-retracted pinned mode. A consumer needing a
  saved plan to `apply` cleanly against fresh ports or fetched-file content
  after a `run` restart has no supported mechanism under this design; the
  only remaining recourse is re-running `plan` in the same tunstrap
  invocation that produced the current `output.json`. **Kube stays plan-safe**
  via the env-native channel (mode 1), which R16 does not touch — this loss
  is specific to the data that has no provider-native env equivalent.
- Finding #2 still applies unchanged to mode 2: **outputs freeze silently** —
  `file()` read *through an output* (or through any value only computed once
  at plan time and never re-touched) returns the plan-time content at apply,
  with **no error**. Read `get_env("TUNSTRAP_OUTPUT_FILE")`/`file()` directly
  inside the provider/resource config block that consumes it, never through
  an intermediate `output` block.
- **Q3's resource-attribute warning still applies unchanged**: binding
  live data to a *resource* attribute (not a provider config block) produces
  `Error: Provider produced inconsistent final plan`, confirmed for
  `hashicorp/kubernetes` v2.38.0. The provider-config-block placement is the
  only supported shape for the kube path and for any unified-structure value
  read at apply time, in both modes 2 and 3.

### Reconciliation with the ticket's "nothing live enters Terraform" framing [U6, restated iteration 6 — R12; ports bullet corrected iteration 7 — R16]

**Stated honestly, not glossed over, and corrected from an earlier revision
that scoped this reconciliation too narrowly.** Ticket #15's own framing says
"connection data should stop travelling through Terraform input variables,"
and the pre-pivot recipe's three delivery conditions included "no connection
data enters an input variable." The reconciliation below is scoped by *kind
of channel*, not just *kind of data* as an earlier revision put it — because
kube itself now has **two** channels, and they do not have the same
relationship to the ticket's framing:

- **Kube env channel** (mode 1 above): the ticket's "nothing live enters
  Terraform" holds in full, unconditionally — no variable, no file, ever.
- **Kube references carried inside the unified structure** (mode 2's file or
  mode 3's var — `path`/`context`/`endpoint` per kube target, non-credential
  per U4, but still *connection data* in the literal sense): **the ticket's
  framing is superseded here too, not just for ports.** When a consumer binds
  `--output-var` and reads `nodes.<node>.kube.<name>.path` (or `context` or
  `endpoint`) from it, that is connection data travelling through a Terraform
  input variable — exactly what the ticket wanted to stop — even though none
  of those three fields is a credential. A consumer who needs the ticket's
  strict guarantee for kube must use the env channel exclusively (Mode A in
  "Documentation" below) and never bind any of `--output-var`'s `kube.*`
  fields to a resource; choosing to use `--output-var` for kube at all is
  choosing to accept the superseded framing, the same choice a consumer
  reading ports from it already makes.
- **Ports**: no env-native path exists for a generic TCP endpoint the way
  `KUBE_CONFIG_PATH` exists for the Kubernetes provider convention — nothing
  about `host:port` is any provider's own configuration vocabulary.
  **[R16, corrected iteration 7]** An earlier revision of this bullet
  claimed a "genuine third option" — a literal, operator-pinned file path
  (R9's mode 2) — as ports' plan-safety story, re-adopting #14 fix 1. That
  re-adoption is **retracted**: the user's own instruction, and the ticket's
  own explicit rejection of fix 1 ("session root can stay ephemeral; only the
  path to the kubeconfig has to be stable"), settle this the other way. Ports
  read the same env-carried `TUNSTRAP_OUTPUT_FILE` locator kube's
  non-env-native connection data would use (mode 2, "Delivery" above) — but
  **one-shot only**, since the session root stays ephemeral and the file it
  names does not survive a tunstrap restart. There is no remaining mechanism
  that buys ports plan-safety *across a restart* the way kube's env-native
  channel does — see "Stability contract," "what is lost," above, and
  "Relationship to #14" immediately below for the corrected fix-1
  disposition.

This explicitly **supersedes the ticket's stricter framing for the unified
structure's var form — both ports and kube references carried in it — while
leaving the kube env channel's full compliance untouched.** See ADR entry 11
for this reasoning recorded as a decision with its own alternatives
considered.

### Relationship to #14 [R15, new; fix-1 disposition corrected iteration 7 — R16]

Ticket #15 explicitly supersedes most of #14. An earlier revision of this
section (R15) re-adopted two of #14's original fixes for non-kube delivery;
**iteration 7 (R16) corrects that: fix 1 is no longer re-adopted, only fix
4 is, and fix 4's own shape changes** (an env-carried locator, not a pinned
path) — stated here so the correction reads as a decision, not scope creep
from a superseded ticket, and cross-referenced from ADR entries 14 and 18 so
the three documents cannot silently re-diverge on this point:

- **#14 fix 1 (pin the session/state root) — [R16] NO LONGER re-adopted, in
  either form.** An earlier revision (R15) re-adopted it as an opt-in
  precondition (a caller-supplied, stable `--session-dir`) specifically to
  give ports a plan-safe-across-restart story. The user's confirmed direction
  after the red-team round retracts that: the session root stays ephemeral
  unconditionally, matching the ticket's own explicit rejection of fix 1
  ("session root can stay ephemeral; only the path to the kubeconfig has to
  be stable, and that is supplied through the environment") — which R15's
  re-adoption had, on reflection, only honored for kube while quietly
  reintroducing the exact thing the ticket rejected for everything else. This
  is not cost-free: see "Stability contract," "what is lost," above.
- **#14 fix 4 (materialized file + `file()`) — re-adopted, reshaped.** The
  mechanism (finding #1 measured it as plan-safe) survives, but **not** via a
  pinned path anymore: the file is located by the env-carried
  `TUNSTRAP_OUTPUT_FILE` (mode 2, "Delivery" above), one-shot within a single
  tunstrap invocation, never a stable path the consumer's HCL independently
  hardcodes. This is a narrower re-adoption than R15's — it buys plan-safety
  within one invocation, not across a restart — but it is real, and it is
  what "content on disk, paths in env" (R16's core principle) means
  concretely for ports and `fetch_files`, which have no env-native provider
  path the way kube does.
- **#14 fix 3 (warn when the child's invocation captures a saved plan, e.g.
  a `-out=` flag, while non-plan-safe delivery is in use)** — **explicitly
  out of scope for #15, deferred to #14** (also listed in "Out of scope"
  below so an implementer sees it as a deliberate deferral, not a gap). This
  design's stability contract and the recipe's explicit warnings (below)
  cover the risk in documentation; a runtime CLI warning would require
  tunstrap to parse its own child command line for Terraform-specific flags
  like `-out=`, which is exactly the kind of Terraform-vocabulary-inside-
  generic-`run` trade the pre-#15 design deliberately confined to
  `tunstrap_tofu` alone (`docs/specs/2026-07-31-run-env-io-and-tofu-proxy-
  design.md`, "Shipping the shim") rather than adding to `run` itself. That
  confinement is orthogonal to this pivot and not re-litigated here; fix 3
  stays #14's remaining scope.

### Consumer-side transformation [U5]

The consumer parses the unified JSON with `jsondecode` and reshapes it in
HCL `locals` into whatever their own tooling needs (a Terragrunt `inputs`
map, a set of `provider` blocks, etc.) — the same pattern the pre-pivot
recipe already used for the (smaller) `--output-var` payload.

**Assumption recorded, not silently interpreted:** the user's instruction
used the phrase "через js" ("via js"). This stack has no JavaScript runtime
anywhere in its consumer chain (Terragrunt/OpenTofu, both Go binaries, HCL
configuration language) — there is no `js`/`node` step between tunstrap's
output and the consumer's config. This is read as **JSON** delivery consumed
via HCL's `jsondecode` function, not literal JavaScript execution, and that
interpretation is recorded here explicitly per the instruction to record
assumptions rather than guess silently.

### The scalar channel is removed, not extended

Every remaining reference to `render_env`, `TUNSTRAP_<TARGET>_*`,
`inject_scalars`, or `MultiNodeEnvUnsupported` below describes what is
**removed**, not a surviving single-node-only contract. **Three** scalars
survive, deliberately, because they are session metadata rather than
`<TARGET>`-scoped connection data and because they solve a real bootstrapping
need (locating the var/materialized-file payload from a plain shell context
that hasn't parsed anything yet): `TUNSTRAP_SESSION_DIR`, `TUNSTRAP_PID`, and
**`TUNSTRAP_OUTPUT_FILE`** (new — judgment call, not literally named by any
of U1-U6, found while tracing the consequence of removing `render_env` from
`start --output env`'s call site; see below). Every other
`TUNSTRAP_<TARGET>_*` key (`_HOST`, `_PORT`, `_ENDPOINT`, and the
per-kube-target `_KUBECONFIG`/`_ENDPOINT` pair) is deleted outright, along
with the `render_env` function that produced them, `predicted_env_keys`'
per-target enumeration, and every raise site of `MultiNodeEnvUnsupported`
(the class itself is removed — see below).

**Why a third survivor.** `start --output env`'s exported lines shrink
correspondingly to the same three-survivors-plus-kube-channel shape (it calls
`render_env` too, at a second call site — `cli.py:206` — deleting the
function without touching this call site breaks `start` outright, not just a
test; see the plan). Doing that naively (dropping to only
`TUNSTRAP_SESSION_DIR`/`TUNSTRAP_PID`) leaves `start --output env` with **no
way at all** to tell a plain shell consumer (not an HCL consumer — `start`
has no Terraform-shaped output channel, unlike `run`) where a plain
`remote_targets` port landed, which is a real functional regression, not a
cosmetic one: `TUNSTRAP_WEB_PORT` used to be the only thing `--output env`
existed to provide for that case. `TUNSTRAP_OUTPUT_FILE` — the absolute path
to the materialized `<session_dir>/tunnel-data/output.json` (see "Delivery,"
above; **[R13, corrected iteration 6]** only `start --output env` gains
materialization, mirroring `run`'s new unconditional write — `start`'s other
modes are untouched, see "Compatibility" below) — restores that: a shell
consumer of `--output env` (or of `run`'s child
environment, for a non-Terraform child) does
`jq .nodes.web.ports.service1 "$TUNSTRAP_OUTPUT_FILE"` instead of reading a
now-nonexistent scalar. This is safe to expose as a plain env var (unlike
`session_dir` alone driving an HCL `file()` call) precisely *because* the
shell/non-Terraform consumer this scalar serves can read arbitrary env vars —
the "only `TF_VAR_*`-mapped names are visible to HCL" constraint that shaped
the var-vs-materialization design above applies to Terraform config, not to
this scalar's actual audience.

`MultiNodeEnvUnsupported`'s disposition: **removed entirely, class and all**.
Its only purpose was guarding the scalar channel's node-count collision; with
the scalar channel gone, no code path can raise it. The pre-spawn gate at
`cli.py:640` (`len(schema.nodes) != 1 and output_var is None` → exit 1) is
removed with it: multi-node input no longer needs an explicit
`--output-var` opt-in, because the unified structure is materialized
unconditionally regardless of node count or of whether `--output-var` was
passed — "unified output is emitted regardless of node count" is the pivot's
own stated semantics for the `inject_scalars` gate, and once that gate no
longer decides *whether* an alternate channel exists (materialization always
does), the gate that used to force choosing one has nothing left to protect
against.

## Multi-node kube channel [kube part, unchanged by the pivot — U4]

`render_env` currently has one node-count guard covering three unrelated
things at once: the `TUNSTRAP_<TARGET>_*` scalars, the per-kube-target
`TUNSTRAP_<KUBE>_{KUBECONFIG,ENDPOINT}` scalars, and the plain `KUBECONFIG`
colon-joined list (`envrender.py:24-60`). Only the first two actually have a
node-dimension problem — a target named `k3s` on two different nodes
genuinely collides in `TUNSTRAP_K3S_PORT`. The `KUBECONFIG` line does not:
it is *already* a path list, and `render_env` *already* colon-joins it
(`envrender.py:56-59`) — the only thing stopping it from working across nodes
is the early-return that raises before it is ever built. **This subsection
describes the kube channel's own contract, which the pivot does not change;
only its wiring into `_build_child_env` simplifies, per the rewritten "`cli.py`
wiring" subsection below, once the scalar channel it used to branch around no
longer exists.**

**Split**, per the spike's Axis 2:

- `render_kube_env(output: OutputSchema) -> dict[str, str]` — new. No
  node-count guard. Iterates every node's `kube_targets`, in order, collecting
  one materialized `path` per kube target across the **whole** envelope (not
  one node), and builds the conditional env-export contract below from that
  flat path list. Callable for any node count, including zero (returns `{}`)
  and multi-node.
- `render_env(output: OutputSchema) -> dict[str, str]` — unchanged contract
  **at the point this split lands** (Task 3 in the plan). Still requires
  `len(output.connections) == 1` and still raises `MultiNodeEnvUnsupported`
  otherwise (`MultiNodeEnvUnsupported`'s own docstring, `exceptions.py:80-87`,
  already states the reason precisely: *"has no node dimension"* — a claim
this change does not touch). For the single-node case, `render_env` now
delegates its kube-path-list line(s) to `render_kube_env`. **[Editorial fix,
iteration 6]** This is **not** "reproducing the previous combined behaviour
exactly," as an earlier revision put it — precisely: `KUBECONFIG`'s *value*
(the colon-joined path list) is unchanged, but the *key set* grows, by
design, per the conditional cardinality contract this same split introduces
(e.g. a single materialized kube target now also exports `KUBE_CONFIG_PATH`
alongside `KUBECONFIG`, which the pre-split `render_env` never did). The ADR
carries the same correction (decision 2). **[PIVOT correction]** `render_env`
  itself is **not** part of "kube part, unchanged by the pivot" — only
  `render_kube_env`, the function this bullet's sibling describes, survives.
  `render_env` and `MultiNodeEnvUnsupported` are both deleted once the
  unified output contract lands (see "The scalar channel is removed" above);
  this bullet describes their contract as it stands in the plan's Task 3,
  before Task 5 removes them, not the shipped end state.

`predicted_env_keys` (`envrender.py:83-112`) — the pre-spawn predictor used to
reject a colliding `--output-var` NAME before a daemon exists — gains the same
conditional logic in lockstep (see env-export contract below); the anti-drift
guard test (`test_predicted_env_keys_matches_render_env`,
`test_envrender.py:96-126`) is **extended, never weakened**, to assert the
predictor and `render_env` agree exactly for both the one-file and
two-or-more-file cases. **[PIVOT time-scope note, matching the correction
above]** This paragraph describes the pairing as it stands at the plan's
Task 3 (`predicted_env_keys` vs. `render_env`) — accurate at that point, not
the shipped end state. Once Task 5 deletes `render_env`, the guard's *other
half* changes, not its existence: the anti-drift property (two independent
"what will `run` inject" implementations must agree) still matters and the
guard is **re-scoped, not deleted** — the pair it compares becomes
`predicted_env_keys(schema)` vs. the actual key set `_build_child_env`
injects for a corresponding `OutputSchema`, since that is the pair capable of
silently diverging once `render_env` is gone. See "The scalar channel is
removed" above and the plan's Task 5 for the concrete rewritten test.

### `cli.py` wiring is in scope [simplified under the pivot]

**Iteration 2 shipped a two-branch `_build_child_env`** (`inject_scalars=True`
→ `render_env`, which delegated to `render_kube_env`; `inject_scalars=False`
→ `render_kube_env` directly), built to satisfy the ruling that "the kube
channel's trigger condition is `kube_targets` presence, not node count and
not `inject_scalars`'s value" while `render_env`'s scalar half still existed
as something to branch around.

**Under the pivot that branch collapses.** `render_env` (the scalar-emitting
function) is deleted outright (see "The scalar channel is removed" above), so
there is nothing left to delegate from and nothing left to branch on.
`_build_child_env` calls `render_kube_env(output)` **unconditionally**, every
time, regardless of node count and regardless of whether the unified output
is also being materialized/injected in the same call — the iteration-2
ruling's requirement ("kube channel fires on `kube_targets` presence, not
node count") is now satisfied trivially, by construction, because there is no
other function it could have been routed through instead. This is a genuine
simplification the pivot buys, not a new requirement: one function, one call
site, no condition on it beyond `render_kube_env`'s own internal
"`kube_targets` empty → return `{}`" check.

**[Editorial fix, iteration 6 — the wiring description below was imprecise;
corrected to match what the plan actually implements.]** Two more pieces of
wiring exist alongside the unconditional `render_kube_env` call above, and
they are **not** the same call site, nor both unconditional:

- `render_output_var(output) -> str` — the function's name and signature are
  unchanged from the pre-pivot design (still `OutputSchema -> str`, still
  the value injected under `--output-var`); only its *body* changes, to build
  the unified structure via a new function, `render_unified_output(output)
  -> dict[str, Any]`, and serialize that instead of the old
  `RunKubeTarget`-based projection. `_build_child_env` calls
  `render_output_var` **only when `output_var is not None`** — this is
  unchanged from the pre-pivot contract (no `--output-var` flag, no var
  injected) and is **conditional**, not unconditional.
- **Materialization is a separate, unconditional call, in a different
  function.** It does not live inside `_build_child_env` at all: `run_command`
  (`cli.py`) calls `render_output_var(output)` a **second** time (or a shared
  helper that also calls `render_unified_output`), unconditionally, in its
  success path, and writes the result to
  `<session_dir>/tunnel-data/output.json` — independent of whether
  `--output-var` was passed and independent of node count. See "The unified
  output contract," "Delivery," mode 2 above for the full write-mechanism
  description (R13), and the plan's Task 5 for the concrete call sites.

Without this wiring the multi-node kube channel and the unified output are
dead code reachable only by unit tests calling the render functions
directly.

## Env-export contract (superset rejected; conditional adopted) [kube part, unchanged by the pivot — U4]

`docs/specs/2026-08-10-issue15-provider-env-precedence.md` (live-probed,
source-cited against `hashicorp/kubernetes` v2.38.0 and `hashicorp/helm`
v2.17.0) settles the question the ticket's work item 3 left open:

- **Plain `KUBECONFIG` is not read by either provider — evidence differs in
  strength per provider, split here rather than conflated [editorial fix,
  iteration 6]:**
  - `hashicorp/kubernetes`: source evidence (`kubernetes/provider.go`'s
    `initializeConfiguration()`, no `KUBECONFIG` read) **and** the stronger
    live negative control — setting `KUBECONFIG` to a **valid** kubeconfig
    file still fails (`dial tcp 127.0.0.1:80: connect: connection refused`,
    the provider's zero-value default), proving the provider never reads it
    regardless of the value's validity.
  - `hashicorp/helm`: source evidence (`helm/structure_kubeconfig.go`'s
    `newKubeConfig()`, same absence of a `KUBECONFIG` read) **plus** a live
    negative control that used a deliberately **wrong** path
    (`KUBECONFIG=/definitely/wrong`, failing with "no configuration has been
    provided") — weaker than kubernetes' valid-file negative control on its
    own (an invalid path failing does not by itself rule out a partial read),
    so the "not read at all" conclusion for helm rests more heavily on the
    source reading than on this transcript alone.
- **Provider resolution order**: configured `config_path` (whose own default
  reads env `KUBE_CONFIG_PATH`) → configured `config_paths` → env
  `KUBE_CONFIG_PATHS` (colon-split via `filepath.SplitList`) — confirmed by
  source reading for both providers.
- **`KUBE_CONFIG_PATH` wins over `KUBE_CONFIG_PATHS` when both are set** —
  confirmed live for **both** providers, each with its own valid-file
  transcript (`kubernetes` data source read; `helm_release` apply): with both
  set, only the file named by `KUBE_CONFIG_PATH` is reachable; a cluster
  reachable only through the `KUBE_CONFIG_PATHS` list is invisible.

That last fact is why the spike's Axis 3 prototype — "export the superset,
`KUBECONFIG` + `KUBE_CONFIG_PATH` + `KUBE_CONFIG_PATHS`, always" — is
**rejected**, not adopted. Exporting `KUBE_CONFIG_PATH` unconditionally
alongside `KUBE_CONFIG_PATHS` would silently shadow every cluster but the
first the instant a second kube target is materialized — exactly the failure
mode this whole design exists to prevent, just moved one layer down.

**Adopted contract, conditional on how many files were materialized** (one
materialized file per kube target, so this is a cardinality condition on
`render_kube_env`'s collected path list):

| Materialized kube files | `KUBECONFIG` | `KUBE_CONFIG_PATH` | `KUBE_CONFIG_PATHS` |
|---|---|---|---|
| 0 | not exported | not exported | not exported |
| exactly 1 | `<file>` | `<file>` | **not exported** |
| ≥ 2 | `<f1>:<f2>:...` (colon-joined) | **not exported** | `<f1>:<f2>:...` (colon-joined) |

- `KUBECONFIG` is exported whenever any files exist, always as the full
  colon-joined list — it is the kubectl/Helm-CLI convention, unaffected by the
  provider precedence problem (no provider here reads it), and a human running
  `kubectl` by hand still benefits from the complete list.
- `KUBE_CONFIG_PATH` is exported **only** for the single-file case, where its
  precedence-winning behaviour is exactly the desired outcome (there is only
  one file to reach, so "wins over `KUBE_CONFIG_PATHS`" is moot).
- `KUBE_CONFIG_PATHS` is exported **only** for the two-or-more case, and
  `KUBE_CONFIG_PATH` **must not** be exported alongside it — the whole point
  of the condition.

**`predicted_env_keys` must NOT model the exact cardinality condition — it
must over-approximate it, conservatively [R11, corrected iteration 6].** An
earlier revision of this design had `predicted_env_keys` compute the *input*
schema's exact `kube_targets` count and apply the same one-vs-two-or-more
conditional `_kube_channel_keys` logic the actual export uses. That is
wrong: `predicted_env_keys` runs **pre-spawn**, against the *input* schema,
before any node has connected — but the *actual* materialized count can be
**smaller** than the input count, because an optional (`required: false`)
node or kube target can fail without failing the run (`manager.py:99-107`
already builds `connections` from successful nodes only). Concretely: two
kube targets declared in the input (→ predicted the `≥2` branch,
`KUBE_CONFIG_PATHS` only) but one optional node fails at connect time (→ only
one file actually materializes, and the real export uses the `==1` branch,
`KUBE_CONFIG_PATH`). If `predicted_env_keys` had predicted the `≥2` branch's
key set, it would **not** have reserved `KUBE_CONFIG_PATH` — a
`--output-var KUBE_CONFIG_PATH` would then pass the pre-spawn collision check
and get **silently overwritten** by the real, one-file export at
`_build_child_env` time. This is exactly the collision the pre-spawn check
exists to prevent, defeated by predicting from the wrong (optimistic) side of
a value that can only shrink, never grow, between input and output.

**Fix: reserve conservatively.** Whenever **any** node in the input schema
declares `kube_targets` (regardless of exact count, regardless of how many
of those targets are `required`), `predicted_env_keys` reserves **all three**
kube env names — `KUBECONFIG`, `KUBE_CONFIG_PATH`, `KUBE_CONFIG_PATHS` — not
the exact cardinality-conditional subset. This is deliberately a superset of
what will usually actually be injected; that asymmetry is the whole point —
over-reserving can only reject *more* `--output-var` names than strictly
necessary (a false-positive usage error, cheap and immediately visible),
while under-reserving risks a silent post-spawn collision (the failure mode
this check exists to prevent). Under the scalar-channel removal above,
`predicted_env_keys` also loses its entire per-target/per-node scalar
enumeration and its `len(schema.nodes) == 1` branch — it collapses to:

```
{"TUNSTRAP_SESSION_DIR", "TUNSTRAP_PID", "TUNSTRAP_OUTPUT_FILE"}
  | ({"KUBECONFIG", "KUBE_CONFIG_PATH", "KUBE_CONFIG_PATHS"}
     if any(node.kube_targets for node in schema.nodes.values())
     else set())
```

unconditionally on node count, matching `_build_child_env`'s own
unconditional `render_kube_env` call above (which itself stays exact,
cardinality-conditional, computed from the *actual* materialized output —
only the *predictor* becomes conservative, not the actual export). Its
remaining job — reject an `--output-var` NAME that collides with an injected
key — is otherwise unchanged, and the guard verifying the relationship
between the two is **preserved, not deleted, and now two-part** (an earlier
revision's "extended, never weakened"/"re-scoped" framing evolves into this,
below) — see "Anti-drift guard extension."

### Interaction with the tofu proxy's `suppress_kubeconfig`

**Not covered by any ruling; identified while writing this contract, flagged
here rather than silently folded in.** `tunstrap_tofu` sets
`suppress_kubeconfig=True` (`tofu_proxy.py:155`) specifically so that a broken
`TF_VAR_tunstrap` → `config_path` wiring fails loudly instead of silently
still reaching the cluster through an inherited/injected `KUBECONFIG`
(`cli.py:388-392`). **[Editorial fix, iteration 6 — narrowed]** The provider
findings above show plain `KUBECONFIG` was never read by either provider's
**own Go configuration chain** — so the guard was inert **for that specific
purpose** (stopping `KUBECONFIG` from silently reconfiguring the
`kubernetes`/`helm` providers themselves), not "inert all along" in general,
as an earlier revision overstated. The same suppression is, and always was,
load-bearing for a different, real audience: `KUBECONFIG` is the
kubectl/Helm-**CLI** convention, and `tofu`'s children include `local-exec`
provisioners and `external` data sources, which can shell out to `kubectl`
or the `helm` CLI directly — both of which *do* honour plain `KUBECONFIG`.
Suppressing it was never protecting a nonexistent fallback in general; it was
(and is) protecting exactly those two provider-native config chains, while
already correctly protecting kubectl/Helm-CLI-invoking children the whole
time.

**[Issue #14 fix, iteration 9 — the paragraph below is falsified; kept for
the historical record of what iteration 6 actually shipped, not restated as
current.]** Iteration 6 went on to conclude: "Once this design ships
`KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` — the vars the providers' own Go
chains actually do read — the guard becomes load-bearing for that
provider-native audience too, for the first time, and `_build_child_env`'s
`suppress_kubeconfig` handling must drop **all three** exported names, not
just `KUBECONFIG`." That is backwards: this same subsection already
established that providers never read plain `KUBECONFIG` — they read
`KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` directly, which is exactly Mode A's
delivery channel through the proxy, not a fallback for it. Dropping those
two through `suppress_kubeconfig` does not close a silent-fallback gap; it
deletes Mode A's only channel through `tunstrap_tofu`, the documented
`terraform_binary` entry point — measured against a real tunnel by the
issue #14 report: through `tunstrap_tofu` all three names came back unset,
and a provider block following Mode A's own item 1 (only `config_context`
set) failed against the inert `localhost:80` loopback.

**Corrected contract.** `suppress_kubeconfig` drops only the *injected*
`KUBECONFIG` — never `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS`, so Mode A keeps
working through `tunstrap_tofu` exactly as documented. Two further
guarantees hold unconditionally, on both the plain and the proxied path,
independent of `suppress_kubeconfig`: an *inherited*
`KUBECONFIG`/`KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` from the parent
environment is always dropped before `render_kube_env` injects, so a stray
operator environment can never contribute to the child's kube channel
either. See `docs/specs/2026-08-07-issue15-kube-identity-decisions.md`
entry 20 for the full record, including the alternative considered and
rejected (pop-before-inject ordering) and why.

## Compatibility

Breaking, deliberately — org rule, no backward compatibility unless
instructed:

- Upstream context/cluster/user names in the materialized kubeconfig change
  from whatever the source cluster used to `tunstrap-<node>-<target>`.
- `KubeTargetOutput.context_name` and `.cluster_name` report the **new**
  names, not the upstream ones — both fields are plain `str` with no
  validation tying them to the source document (`schemas.py:317-336`), so
  this requires no schema change, only a different value at construction
  time.
- **[Editorial fix, iteration 6]** `RunKubeTarget` (`schemas.py:339-372`)
  does **not** "carry the same fields through unchanged" into the unified
  structure, as an earlier revision claimed — that class is **deleted**
  under the pivot (its allow-list job is now done by explicit-keyword
  construction inside `render_unified_output`, see the plan). Of its seven
  fields, only `context_name` survives into the consumer channel, renamed to
  `context`; `cluster_name`, `local_port`, `tls_server_name`, and
  `certificate_authority_data` are **dropped** — a real, intentional breaking
  narrowing beyond the pre-#15 credential fix, not an oversight (design
  rationale: U4 scopes the unified kube entry to `{path, context, endpoint}`
  references only). A consumer reading any of the four dropped fields out of
  the old `--output-var` payload breaks.
- **[Editorial fix, iteration 6]** The superset env export the ticket's work
  item 3 asked to prototype as a placeholder — final choice deferred to the
  parallel provider-behaviour verification (paraphrased, not a ticket
  quotation; an earlier revision rendered this in quotation marks as if it
  were verbatim) — is explicitly **not** the shipped contract; see
  "Env-export contract" above.
- **[PIVOT, iteration 3]** The entire `TUNSTRAP_<TARGET>_*` scalar channel is
  removed, not extended — every `run`/`start --output env` consumer reading
  `TUNSTRAP_<NAME>_PORT` or similar breaks outright, with no compatibility
  shim. **Three** survivors, not two (corrected, iteration 4):
  `TUNSTRAP_SESSION_DIR`, `TUNSTRAP_PID`, `TUNSTRAP_OUTPUT_FILE` (session
  metadata, not target-scoped).
- **[R11, iteration 6]** `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` export
  behaviour changes for multi-node: pre-pivot, the kube env channel was
  gated by the same single-node guard as the scalars and so was never
  injected for multi-node input at all; post-pivot it is injected
  unconditionally on node count (`render_kube_env` has no node-count guard —
  see "Multi-node kube channel" above), so a multi-node `run` now exports
  `KUBECONFIG`/`KUBE_CONFIG_PATH(S)` where it previously exported nothing
  kube-related. `predicted_env_keys` reserves all three names conservatively
  whenever *any* node declares `kube_targets`, regardless of how many
  actually materialize — see "Env-export contract," `predicted_env_keys`
  paragraph.
- **[PIVOT, iteration 3]** `--output-var NAME`'s injected JSON shape changes
  from the raw `OutputSchema`-minus-kube-credentials projection to the
  unified structure above — any consumer parsing the old flat
  `connections.<node>.ports.<target>` shape breaks; the new shape is
  `nodes.<node>.ports.<target>` (a string, not an int) plus the reorganized
  `kube`/`fetch_files`/`session` keys.
- **[PIVOT, iteration 3]** `MultiNodeEnvUnsupported` is removed entirely
  (class, `_EXIT_CODES` entry, and both raise sites — `render_env`'s internal
  guard and `cli.py:640`'s pre-spawn multi-node-without-`--output-var` gate).
  A multi-node `run` without `--output-var` is no longer a usage error: the
  unified structure is materialized unconditionally, so multi-node input
  always has a channel.
- **[PIVOT, iteration 3]** `run` now unconditionally writes
  `<session_dir>/tunnel-data/output.json` — new on-disk artefact, new content
  in a directory whose kube-only contents were previously the sole thing
  present. **[R13, corrected iteration 6]** `start`'s **default** JSON stdout
  gains no such artefact and is otherwise unaffected by this design — only
  `start --output env` materializes, matching `run`, and only because it
  shares `run`'s new `--output env` export shape (see "The scalar channel is
  removed" above).
- **[Editorial fix, iteration 6]** Scope carve-out, stated explicitly: the
  unified contract covers `run`'s `--output-var`, `run`'s materialization,
  and `start --output env`'s export lines. **`start`'s default/raw JSON
  stdout envelope (no `--output` flag, or `--output json`) is unchanged by
  this design** — a deliberate scope judgment call (plan, Self-Review), not
  an oversight: it remains the pre-#15 "complete envelope" contract for
  session-management tooling, a different audience than the consumer-facing
  channels this pivot reshapes.
- **[R16, iteration 7 — supersedes the iteration-6 bullet above, retracted,
  not extended]** `fetch_files` content is **no longer** plan-file-durable
  via the var form at all — the pre-#15 design's own choice to let
  `content_b64` ride `--output-var` unprojected (decision history's
  `fetch_files[*].content_b64` entry, cited by the retracted bullet this
  replaces) is superseded. Fetched bytes are now materialized to
  `tunnel-data/<node>-<fetchname>` (mode `0600`) exactly like kubeconfigs
  already were, and the consumer-facing projection — both the
  `TUNSTRAP_OUTPUT_FILE` manifest and `--output-var` — carries only
  `{path, size, sha256}` (or `{error}`), never `content_b64`. **This resolves
  the "never fetch secrets with `--output-var`" warning as a class, not case
  by case**: since content never rides the var (or the manifest) at all, a
  consumer using `--fetch` to retrieve a secret can no longer leak it into a
  saved Terraform plan file via that channel, regardless of whether
  `--output-var` is bound. This is breaking versus the pre-#15 fetch-files
  design, stated plainly: any consumer decoding `fetch_files.<name>.content_b64`
  out of the old envelope breaks and must instead read the file at
  `fetch_files.<name>.path`. `start`'s raw default JSON stdout keeps showing
  `content_b64` unchanged (see "Fetched-file materialization" above,
  mirroring the existing kube carve-out) — this narrowing is specific to the
  consumer-facing channels R16 reshapes, not to `FetchedFile` itself.

## Testing contract

### The collision trap — mandatory, unit-level

k3s ships `current-context: default`, `cluster: default`, `user: default` —
**two k3s targets collide on the exact upstream names verbatim**; this is the
expected case the rename exists to fix, not an edge case. **A kind-based test
proves nothing here**: kind's context is `kind-<cluster-name>`, already
unique, so a kind-only regression test would pass unchanged even with the
rename entirely absent. The mandatory regression test therefore uses two fake
upstream kubeconfigs whose context/cluster/user names are **identical**
(k3s-style), not kind-style — see the untracked spike prototype and
`variant/combined`'s `tests/unit/test_issue15_context_collision.py`. It drives
`run_kube_targets` twice (two different `node_name`s, same k3s-style fixture
content) and asserts:

- the two `KubeTargetOutput.context_name`/`.cluster_name` values differ, and
  match `tunstrap-<node>-kube` exactly;
- the rename reaches the **serialized** document (`content_b64`), not just the
  extracted fields — a consumer parsing the materialized file, not
  `KubeTargetOutput`, is what actually merges kubeconfigs.

Confirmed RED against the unmodified `feature/run-env-io` tip (both outputs
report `context_name == "default"`) and GREEN under `variant/combined`
(spike findings, "Part 3").

### Two more mandatory unit tests, distinct defect classes [R10, R14, new]

**Neither is covered by the collision trap above** — restated because both
are easy to mistake for "the same test, differently framed" and neither is:

- **R10 — tunstrap's own naming scheme colliding with itself**, independent
  of any upstream kubeconfig content: two different `(node, target)` pairs,
  e.g. `(node="a-b", target="c")` and `(node="a", target="b-c")`, both
  render `tunstrap-a-b-c`. Driven at schema-validation time (no SSH, no
  kubeconfig fixture needed at all — this is a pure `InputSchema`
  validation test), asserting the payload is rejected with an error naming
  both colliding pairs.
- **R14 — a dangling reference inside an *ignored* context after rename.**
  A fixture with two contexts sharing one cluster entry: the current context
  (renamed) and a non-current, ignored context whose own `context.cluster`
  reference names the *same* cluster. Assert that after `rename_identities`
  runs, the ignored context's `cluster`/`user` references have been updated
  to the new name too — not left pointing at a cluster/user entry that no
  longer exists under its old name anywhere in the document.

### `e2e` coverage — optional, with rationale

Kind-based `e2e` coverage of this feature is **not required** to land the
fix, for the reason above: kind's own context naming already makes the
collision unreachable, so an `e2e` test added naively would be decorative.
**If** `e2e` coverage is added, it must first rewrite the two kind clusters'
materialized kubeconfig identities to a **shared** name (e.g. force both to
`current-context: default` / `cluster: default` / `user: default`, matching
the k3s shape) before feeding them to `tunstrap start`/`run` — otherwise the
test exercises kind's own uniqueness, not tunstrap's rename. This is real
extra fixture work (a kubeconfig-identity rewrite step ahead of the existing
`kube_rig`/`node_kubeconfig` fixtures in `tests/e2e/conftest.py`), which is
why it is marked optional rather than mandatory in the plan — the unit-level
regression test above already exercises the real defect precisely and does
not need a live cluster to do so.

**[Editorial fix, iteration 6 — disambiguated from a different e2e change
this design also requires]** This optionality is about **collision-specific**
e2e coverage only. It does not extend to the e2e tier's existing
`nodes.<node>.kube.<name>.path` shape migration (`tests/e2e/module/main.tf`
and its dependent test files), which **is** mandatory — the shape change
ships with this design regardless, and a `try()` swallowing the shape
mismatch into an empty `config_path` would fail silently rather than loudly
if that migration were skipped, which is exactly the risk profile that makes
it non-optional. See the plan's Task 6 for the mandatory migration and Task 7
for where the two are kept distinct in the gate pass.

### Anti-drift guard extension

`test_predicted_env_keys_matches_render_env` (`test_envrender.py:96-126`)
must be extended with cases for both the exactly-one-file and
two-or-more-file conditions of the env-export contract above — the guard is
**extended, never weakened**. **[Editorial fix, iteration 6]** The concern
this guard addresses (two independent implementations of "what keys get
injected" silently diverging) is this codebase's own anti-drift discipline,
established by the spike/this design's own review process — not, as an
earlier revision misattributed it, "the ticket's own stated concern" (the
ticket never mentions this guard or this failure mode at all). Confirmed in
the spike: reverting only the `predicted_env_keys` half of the Axis-3
superset change (not the conditional version specified here, but the same
class of change) produced exactly one failure, with a clear diff (`Extra
items in the right set: 'KUBE_CONFIG_PATH', 'KUBE_CONFIG_PATHS'`) —
confirming the guard fires correctly and is not a false pin.

**[R11, corrected iteration 6 — the guard is two-part, not a single equality]**
Two prior corrections to this guard are both superseded by R11's conservative
predictor (above), which changes what "agree" even means between the two
implementations:

- **Iteration 4** deleted the guard outright on the false premise that only
  one implementation of the injected-key set remained after `render_env`'s
  removal — wrong; `_build_child_env` and `predicted_env_keys` are still two
  independent implementations.
- **Iteration 5** retargeted it to a single full-set-equality assertion,
  `predicted_env_keys(schema) == set(actual injected keys)` — this was
  correct *only* as long as `predicted_env_keys` computed the exact
  cardinality-conditional key set. R11 makes `predicted_env_keys`
  deliberately **conservative** (reserves all three kube names whenever any
  `kube_targets` exist, regardless of exact count), so exact equality can no
  longer hold in the general case — a schema with exactly one kube target
  that materializes cleanly now predicts `{KUBECONFIG, KUBE_CONFIG_PATH,
  KUBE_CONFIG_PATHS}` (conservative) while the actual export is
  `{KUBECONFIG, KUBE_CONFIG_PATH}` (exact, per the `==1` branch) — genuinely
  unequal, correctly so.

**The guard is now two independent tests, not one:**

1. **Formula test (exact equality, unchanged in spirit from iteration 5):**
   for a fixed, representative schema, `predicted_env_keys(schema)` equals a
   hand-computed expected set reflecting the conservative rule exactly (e.g.
   any `kube_targets` present → all three kube names, plus the three
   survivors) — this proves the *formula* is implemented correctly, and is a
   normal unit test, not a drift guard between two independent
   implementations.
2. **Safety-envelope test (subset, new, the actual anti-drift guard):**
   `set(actual injected keys from _build_child_env(out)) ⊆
   predicted_env_keys(schema)`, driven by a **cardinality-shrink** case — an
   input schema declaring two kube targets (one on an optional node that
   fails), producing an `OutputSchema` with only one kube target
   materialized. This is the property that actually matters for the
   pre-spawn collision check: predicted must always cover whatever actually
   gets injected, in every direction cardinality can move between input and
   output, and only a shrink case can falsify a formula that got the
   direction of the conservatism backwards.

Both tests live together in `tests/unit/test_envrender.py`; see the plan's
Task 3 (formula) and Task 5 (safety-envelope, since it needs
`_build_child_env` from Task 5) for the concrete literals. This remains a
**standing ruling (R1: the guard is extended, never weakened)** — R11
changes *what* the guard asserts, not whether one exists.

### Unified output contract tests [PIVOT, new territory]

Nothing in the spike prototypes the unified structure, its materialization,
or the removal of `render_env`/`predicted_env_keys`' scalar half — these are
new tests, not spike cherry-picks. At minimum: `render_unified_output`
produces the shape above for a multi-node, multi-kube-target `OutputSchema`
(including **[R16]** the `fetch_files` **projection** — `{path, size,
sha256}`, no `content_b64`, not a passthrough — and the
two-reserved-top-level-keys namespacing); materialization writes
`<session_dir>/tunnel-data/output.json`
at mode `0600` and it is valid JSON matching the injected `--output-var`
payload byte-for-byte; a multi-node `run` **without** `--output-var` now
succeeds (was: exit 1 `MultiNodeEnvUnsupported`) and the materialized file
still exists; `predicted_env_keys` no longer enumerates per-target scalar
keys and the anti-drift guard is re-scoped to the surviving
`{TUNSTRAP_SESSION_DIR, TUNSTRAP_PID, TUNSTRAP_OUTPUT_FILE} ∪ kube-channel`
set, compared against `_build_child_env`'s actual output rather than against
`render_env` (which no longer exists) — see "Anti-drift guard extension"
above. **A full grep-driven enumeration of every pre-existing test, fixture,
and shipped artifact this removal breaks — across unit, integration, e2e and
the recipe doc — is the authoritative blast-radius table in the plan's
Task 5**, not repeated here; this spec states the contract, the plan states
every concrete consequence of shipping it.

## Documentation (work item 4) [R12, rewritten as two explicit consumer modes; Mode B rewritten iteration 7 — R16]

`docs/recipe_terragrunt.md` gains **two explicit, named consumer modes**, not
a single blended recipe — an earlier revision's "kube-only" +
"unified-output" split by *feature area* is replaced by a split by
*compliance level*, because that is the axis a reader actually has to choose
on (per U6's restated reconciliation, above): does this consumer need the
ticket's strict "nothing live enters Terraform" guarantee, or is
materialization-primary/var-convenience acceptable? Write both modes in one
coherent document (a real consumer may use Mode A for kube and Mode B for
ports in the same module), but never present Mode B as satisfying Mode A's
guarantee.

**Mode A — env-native kube (satisfies the ticket's strict contract):**

1. `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` from tunstrap's own process
   environment (never a `var.`-bound value, never a file read in HCL at all)
   **plus a literal `config_context = "tunstrap-<node>-<target>"` per
   provider alias** — the ticket's own central pattern (finding #3), shown
   with a **two-alias worked example** (two `kubernetes`/`helm` provider
   blocks, one per target, each with its own literal `config_context`,
   sharing the same `KUBE_CONFIG_PATHS` list) citing findings #3 and #5 by
   number. `config_path`/`config_paths` need not be set explicitly at all in
   this mode — the env vars alone resolve them per the provider's own
   `EnvDefaultFunc`.
   ```hcl
   provider "kubernetes" {
     alias           = "node1_k3s"
     config_context  = "tunstrap-node1-k3s"  # literal -- see warning below
   }
   provider "kubernetes" {
     alias           = "node2_k3s"
     config_context  = "tunstrap-node2-k3s"
   }
   ```
2. **Warning, explicit:** never derive `config_context`'s value from
   `var.tunstrap` or any other live/decoded data — it must be a literal
   string in the config, matching the deterministic naming scheme exactly
   (`tunstrap-<node>-<target>`, "The deterministic-naming contract," above).
   Deriving it live would reintroduce a variable-bound value for data that
   has an env-native, fully static alternative, defeating the point of Mode
   A.

**Mode B — unified-file convenience (ports + kube references; does *not*
satisfy the ticket's strict contract, stated plainly, not glossed over):**

3. **[R16, iteration 7 — this item's HCL is rewritten, not just re-worded]**
   **The shape**, with a worked HCL example using the env-carried
   `TUNSTRAP_OUTPUT_FILE` locator — **never** a literal, operator-pinned path
   (R9's mode 2, now retracted) and never `var.tunstrap_session_dir` or any
   variable-derived locator; an earlier revision of this recipe used the
   unsound variable-derived form, a later one used the now-retracted pinned
   form, both corrected here:
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
   `get_env(...)` is Terragrunt's own function for reading the parent
   process's environment into HCL — this is exactly the bridge tunstrap's
   `run` (or `start --output env`) sets up by exporting
   `TUNSTRAP_OUTPUT_FILE` before spawning the child. No caller-supplied
   `--session-dir`, no operator-agreed literal path: `run` mints an ephemeral
   session dir the same way it always has, and every invocation's own child
   sees that invocation's own fresh path via the env var, never a stale one.
   Read directly inside the `locals` block that feeds the provider config —
   never through an `output`, per the stability contract's finding-#2
   warning.
4. **Ports lose their integer form in this shape** (a `"host:port"` string,
   not a bare port number) — show the HCL extraction idiom explicitly, one
   canonical form, not left to the reader to invent:
   ```hcl
   locals {
     service1_port = split(":", local.tunnel.nodes.node1.ports.service1)[1]
   }
   ```
5. **[R16, iteration 7 — corrected: item 3 is no longer plan-safe across a
   restart]** **The stability contract**, restated plainly and matching
   "Stability contract" above word-for-word on the load-bearing claims: Mode
   B via `TUNSTRAP_OUTPUT_FILE` (item 3) **and** Mode B via `--output-var`
   (`var.tunstrap`/`TF_VAR_tunstrap`) are **both one-shot `plan && apply`
   only** — no saved-plan reuse across a tunstrap restart, for either, since
   the session dir stays ephemeral (an earlier revision of this item claimed
   item 3 was unconditionally plan-safe given a `--session-dir` precondition
   — that precondition is retracted along with R9's mode 2, see "Relationship
   to #14," above). State this as plainly as: *"Neither Mode B form survives
   a tunstrap restart. If you need a saved plan to `apply` cleanly against
   fresh ports or fetched-file content, re-run `plan` in the same tunstrap
   invocation instead — there is no supported way to pin either form's
   locator to a stable path across invocations."*
6. **The `jsondecode`-not-JavaScript note** (U5): the recipe consumes JSON
   via HCL's `jsondecode`, there is no JS runtime in this stack, and the
   recipe should say so in one sentence to preempt the same question this
   design had to resolve as an explicit assumption.
7. **[R16, iteration 7 — this warning is retracted, not carried forward]**
   The "never `--fetch` secrets with `--output-var`" warning two earlier
   revisions of this recipe stated (one dropped it, the next restored it) is
   **resolved as a class**, not restated: `fetch_files` content no longer
   rides any consumer-facing channel at all — `--output-var` carries only
   `{path, size, sha256}` (see "Compatibility," above). The recipe should say
   this plainly instead of warning against something that can no longer
   happen: *"Fetched file content never enters a Terraform variable or plan
   file — only its path, size, and checksum do. Read the file itself at
   `fetch_files.<name>.path` if you need its contents."*

**Measured-facts list, corrected to cite all six of the ticket's own
findings [editorial fix, iteration 6 — an earlier revision's list, despite
its own header claiming "the ticket's own six findings," actually cited
only four]:**

- **#1** — provider configuration **is** re-evaluated at apply: a `file()`
  read inside a provider block picks up a post-plan change (Mode B's basis).
- **#2** — outputs **freeze silently**: `file()` read through an output
  returns the plan-time value at apply, with no error — the nastiest failure
  mode, name it as such.
- **#3** — **[was missing]** per-alias `config_context` works with an
  env-supplied kubeconfig path: two aliases, literal `config_context` each,
  sharing one `KUBECONFIG`/`KUBE_CONFIG_PATHS` list, each reach their own
  cluster — Mode A's basis, cited with the two-alias worked example above.
- **#4** — **[was missing]** plan-safe end to end, measured live in the
  unpublished #15 spike (no automated test covers this saved-plan mutation
  scenario): plan with
  one set of ports, mutate only the kubeconfig, apply the *saved* plan → the
  alias uses the mutated value, zero "Mismatch between input and plan
  variable value" — this is the e2e-level confirmation that Mode A's
  env-native path really is plan-safe across a saved-plan reuse, not just a
  theoretical consequence of finding #1.
- **#5** — `KUBE_CONFIG_PATHS` is colon-separated on Linux (comma silently
  falls back to `localhost:80`).
- **#6** — a live value bound to a `var.` **does** trip "Mismatch between
  input and plan variable value" on a saved plan — the negative control
  behind the one-shot-only rule for Mode B's variable form (R9's original
  finding, restated for the file form too under R16 since the file itself no
  longer survives a restart either).
- A live value bound to a **resource attribute** (not a provider config
  block) produces `Error: Provider produced inconsistent final plan` —
  confirmed for `hashicorp/kubernetes` v2.38.0 in this design's own
  provider-findings probe (Q3), reproducing the ticket's #14 claim on
  current provider versions. The recipe must show the provider-block
  placement as the only supported shape in both modes and name this failure
  mode explicitly as what happens if a reader tries the resource-attribute
  shape instead.

## Out of scope

- Pruning ignored (non-current) contexts/clusters/users from the materialized
  document — accepted residual risk, see "Rename scope" above.
- A configurable identity-naming prefix — explicitly rejected by the ticket's
  own org-rule citation.
- Any change to `KubeParseError`, `parse_kubeconfig`'s section-parser split,
  or `patch_view`'s server/TLS rewriting — all untouched by this design and
  still pinned by `tests/unit/test_kube_parse.py`,
  `tests/unit/test_kube_parse_invariants.py`, and
  `tests/unit/test_kube_patch.py` respectively.
- **[PIVOT]** Literal JavaScript execution or a JS runtime in the consumer
  chain — "через js" is interpreted as JSON delivery consumed via HCL's
  `jsondecode`, per the recorded assumption above; introducing an actual JS
  step (e.g. a `local-exec` calling `node`) is not part of this design and
  was never asked for beyond that phrase.
- **[PIVOT]** A configurable materialized-output filename/location — fixed at
  `<session_dir>/tunnel-data/output.json`, matching the fixed-naming
  philosophy the kube identity contract already established (decision 4);
  not separately requested, so not built.
- **[R15, new]** **#14 fix 3 — a CLI-level warning when the child's tofu
  invocation captures a saved plan (e.g. `-out=`) while non-plan-safe
  delivery is in use.** Deliberately deferred to #14, not forgotten: this
  design's stability contract and the recipe's explicit warnings cover the
  risk in documentation; a runtime warning would require `run` to parse its
  own child's command line for Terraform-specific flags, which the pre-#15
  design deliberately confined to `tunstrap_tofu` alone, not generic `run` —
  see "Relationship to #14," above, for the full reasoning.
- **[R10, new]** A different join separator (replacing the hyphen in
  `tunstrap-<node>-<target>`) to eliminate the naming-collision surface
  structurally instead of detecting it — a larger, unrequested change; the
  validation-time collision check (above) is the shipped fix.

## Pointers

- `docs/specs/2026-08-10-issue15-provider-env-precedence.md` — committed
  provider-precedence evidence behind the env-export contract above.
- The untracked issue #15 spike notes contained the six-variant comparison and
  regression-test prototype; they are not public reference material.
- `docs/specs/2026-08-07-issue15-kube-identity-decisions.md` — the
  decision-history companion to this design, one entry per decision with
  alternatives considered and consequences.
- `docs/superpowers/plans/2026-08-07-issue15-kube-identity.md` — the
  implementation plan, cherry-picking from `variant/combined`.
