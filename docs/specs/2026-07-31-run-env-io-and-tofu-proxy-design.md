# `run` env I/O + the tofu proxy pattern

- Status: design, awaiting review
- Date: 2026-07-31
- Scope: three generic CLI additions (`--input-env`, `--output-var`, a
  single-variadic `run` argument surface), the stdout and cleanup invariants a
  foreground wrapper needs, a consumer-side `tofu` shim recipe, and a new `e2e`
  test tier proving real Kubernetes/Helm providers through a tunnel. Supersedes
  `docs/artifacts/superseded/2026-07-30-owner-tracking-and-consumer-ergonomics-design.md`.
- Measurement basis: Terragrunt **v1.1.1** + OpenTofu **v1.12.5**, and **Click
  8.4.2** (the version `click>=8.3,<9` in `pyproject.toml:12` resolves to today)
  for the argument-parsing findings, and **kind 0.30.0 / `kindest/node` v1.34.0 /
  Docker 28.1.1 / kubectl v1.28.1 / tunstrap 0.0.4** for the `e2e` tier. Linux,
  all measured 2026-07-31. Every fact labelled *[measured 2026-07-31]* was
  re-derived then; none is a repo invariant. Items the current code cannot yet
  execute are labelled *[designed, unverified]*.
- Code citations (`cli.py:NNN` etc.) are against **`ddde94d`**, the tip of `main`
  at the time of writing. An in-flight `ValidationError`-leak fix on
  `fix/validation-error-leak` shifts `cli.py` by +4 lines; re-resolve citations
  against the merge base before implementing.

## Problem

The Terragrunt consumer (`consumer-repo/garuda/`) starts tunstrap from inside
`inputs` via `run_cmd`, writes an `OutputSchema` to a marker file, reads it back
with `jsondecode(file(...))`, and tears the daemon down in an `after_hook` —
~150 lines of bash embedded in HCL (`terragrunt.hcl:11-14`, `:55-83`, `:221-237`,
`:250-306`) plus `locals.tf:72`. Two consequences:

1. **Secret exposure in a command line.** `run_cmd` offers argv only — no stdin.
   The full `InputSchema`, including three `ssh_pkey` PEMs, is an argv element
   (`terragrunt.hcl:257-305`). Terragrunt's `ProcessExecutionError` joins Command
   and all args, so any failure of that `bash -c` prints the keys.
2. **Lifetime is inferred, not owned.** Nothing is the daemon's parent, so the
   `after_hook` is the only teardown authority and it misses every command not in
   its list (`terragrunt.hcl:222`). The consumer compensates with
   `auto_stop_idle_seconds = 7200` (`terragrunt.hcl:299`).

The previous spec attacked (2) with a `--owner` watchdog. This spec removes the
problem class instead: make tunstrap the **parent** of `tofu`.

## Current state (as-is)

- `start` takes input from a `USER@HOST[:PORT]` argument plus flags, or JSON on
  stdin read whole at `cli.py:194` (`cli.py:172-211`). It emits the
  `OutputSchema` envelope as JSON on stdout (`cli.py:219`) or, with
  `--output env`, as `export K='V'` lines (`cli.py:215-217`).
- `run` declares a **required** CONNECTION positional (`cli.py:251`) *followed by*
  a variadic COMMAND (`cli.py:255`), plus the same flags; no stdin input path, no
  `--output` flag. It merges the single-node scalar env into the child
  environment (`cli.py:303`), launches the child with `Popen` (`cli.py:311-313`),
  forwards `SIGINT`/`SIGTERM` (`cli.py:315-322`), and tears the session down in a
  `finally` (`cli.py:327-330`, `_teardown_run` at `cli.py:334-342`).
- **`run`'s teardown writes to stdout.** `_teardown_run` → `_kill_with_identity`
  emits a `{"stopped": ...}` JSON line on stdout for *every* outcome
  (`cli.py:376-437`). `stop_command` depends on that behaviour as its documented
  contract; `run` inherits it as a defect. Unguarded: the `run` integration tests
  assert only return codes and cleanup
  (`tests/integration/test_cli_modes.py:127-197`).
- **`run` has an unprotected post-spawn window.** `OutputSchema.model_validate`
  and `render_env` run at `cli.py:302-304`, after `spawn_daemon` succeeded but
  before the `try` that owns teardown opens at `cli.py:308`. Anything raised
  there orphans the daemon.
- `run` forces `daemon.materialize=True` regardless of the `--materialize` flag
  (`force_materialize=True`, `cli.py:290`), because `render_env` requires
  materialized kube paths (`envrender.py:42-43`).
- The daemon flags `--auto-stop-idle-seconds`, `--materialize` and `--log-file`
  are attached by `_connection_options` (`cli.py:75-77`) but deliberately
  excluded from `_conn_flags_present` (`cli.py:84-93`).
- `run` **cannot** read its payload from stdin: `Popen` is called without a
  `stdin=` argument (`cli.py:311-313`), so the child inherits the parent's stdin
  and a `sys.stdin.read()` would hand it a drained pipe. The one existing stdin
  use in `run` is bounded to a single line for `--ssh-password-stdin`
  (`cli.py:111-112`).
- `render_env` requires exactly one node and raises a bare `ValueError`
  otherwise (`envrender.py:19-20`). That `ValueError` has no exit-code mapping;
  it would surface through `run`'s absence of a top-level guard as a traceback
  (`start` has one at `cli.py:234-247`; `run` has none).
- `OutputSchema` fields are `connections`, `pid`, `session_dir`, `started_at`,
  `warnings` (`schemas.py:353-362`). `token` was removed by the 2026-06-24
  session-reuse design.
- Exit codes are `1` schema, `2` required/kube, `3` session-active, `4` daemon
  (`exceptions.py:57-63`), plus `64` for usage via `_UsageExit64`
  (`cli.py:33-49`). **`5` is unallocated.**
- `--owner`, `--output-file` and `--placeholder-host` from the superseded spec
  were **never implemented** — `grep -rn "owner|output_file|placeholder"
  tunstrap/*.py` matches only unrelated identifiers in `kube.py:235,245`. They
  are cancelled designs, not removals.

## Design (to-be)

### The I/O matrix

`start` and `run` have an incomplete input/output matrix:

|         | input                    | output                          |
|---------|--------------------------|---------------------------------|
| `start` | stdin JSON · flags       | stdout JSON · shell exports     |
| `run`   | **flags only**           | env → child (scalars only)      |

Two cells are missing and both are needed. The input gap is **not** a Terraform
accommodation — it is forced by the shape of a foreground wrapper: `run` owns a
child that inherits stdin (`cli.py:311-313`), so stdin is unavailable as a
control channel, and the only remaining out-of-band input channel a parent has
is its own environment.

### Addition 1 — `--input-env VAR`

Read the `InputSchema` JSON from `os.environ[VAR]` instead of from stdin or
flags — illustrative surface only:

```
tunstrap run --input-env TUNSTRAP_INPUT -- CMD [ARGS...]
```

**Lands on `run` only.** `start` already has a working, uncontended input
channel; the missing matrix cell is `run`'s. `start_command` already carries a
three-way input conflict guard (`cli.py:159-179`) and an explicit
`too-many-branches,too-many-statements` suppression (`cli.py:142`); a third
input mode there would add branches for zero unmet need. The flag is additive
and can be extended to `start` later. Parsing and validation reuse `start`'s
stdin path verbatim (`cli.py:194-211`), with the same three
`SchemaValidationError` shapes and exit 1.

### Addition 2 — `--output-var NAME`

Inject the `OutputSchema`, JSON-encoded, into the child's environment
under `NAME`, alongside (not instead of) the existing scalar `TUNSTRAP_*` set.

```
tunstrap run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap -- CMD
```

Generic framing: *give the child the structure, not a flattened projection of
one node*. The scalar env is lossy by construction — single-node
(`envrender.py:19-20`), and it drops `warnings`, `started_at` and every
`kube_targets` field except `path`/`endpoint`. `--output-var` is the structured
channel; consumers wanting structure parse JSON, consumers wanting `KUBECONFIG`
keep the scalars. It is **not** byte-identical to `start`'s stdout: the kube
credentials are projected out before export (`render_output_var`, not
`OutputSchema.model_dump_json()`), and `fetch_files[*].content_b64` passes
through verbatim — see "Out of scope" and `docs/recipe_terragrunt.md`. `start`
stdout is unchanged and still writes the complete envelope.

`NAME` must match `[A-Za-z_][A-Za-z0-9_]*`, else usage error; `NAME` colliding
with a `render_env` key is a usage error too, mirroring the collision discipline
already inside `render_env` (`envrender.py:29-30`). Collision with an unrelated
inherited variable is a documented overwrite.

**Multi-node interaction.** `--input-env` makes multi-node input reachable from
`run` for the first time. Rule: one node → scalars + `KUBECONFIG` as today, plus
`--output-var` if given; >1 node with `--output-var` → `--output-var` only, no
scalars; >1 node without `--output-var` → typed error, exit 1.

That last case is decided **before `spawn_daemon`**, from `len(schema.nodes)` on
the *input* schema — never after, where it would orphan a daemon (see "Cleanup
must own the whole post-spawn window"). The bare `ValueError` at
`envrender.py:20` still becomes a `TunstrapError` subclass with an exit code, as
defence in depth for any path that reaches `render_env` with the wrong shape.

### Addition 3 — `run`'s argument surface becomes a single variadic

**Making `connection` merely `required=False` does not work, and would have made
the documented shim invocation unreachable.** `run` declares CONNECTION as a
positional *before* the variadic COMMAND (`cli.py:251`, `cli.py:255`). In Click,
`--` terminates **option** parsing only; the tokens after it are still
distributed over the declared positionals in order. Measured on Click 8.4.2
[2026-07-31]:

```
run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap -- tofu plan
  →  connection='tofu'   command=('plan',)
```

So `tofu` binds to CONNECTION, `plan` becomes the whole child command, and the
spec's own "CONNECTION + `--input-env` → 64" rule then rejects the one
invocation the shim must use. The positional pair cannot express "no connection".

**Resolution: collapse the two positionals into one variadic.** CONNECTION stays
a positional (no `--connection` option, no new subcommand — both would break the
documented `run USER@HOST -- CMD` form in `README.md:175-195` and its integration
tests), but the split is decided *after* parsing, by whether `--input-env` is
present:

```python
@main.command("run")
@_connection_options
@click.option("--input-env", "input_env", default=None, metavar="VAR")
@click.option("--output-var", "output_var", default=None, metavar="NAME")
@click.option("--session-dir", "session_dir", default=None)
@click.option("--grace-seconds", "grace_seconds", type=int, default=10, show_default=True)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def run_command(..., input_env: str | None, output_var: str | None, args: tuple[str, ...]) -> None:
    ...
```

Split rule:

| `--input-env` | `args` | CONNECTION | child command |
|---|---|---|---|
| absent | `(conn, *cmd)` | `args[0]` | `args[1:]` |
| present | `(*cmd,)` | — (none exists) | `args` |

This is behaviour-preserving for flag mode. Measured on Click 8.4.2
[2026-07-31] with the single variadic:

```
run user@host --ssh-key /k --target web=a:80 -- helm list
  →  args=('user@host','helm','list')  ssh_key='/k'  targets=('web=a:80',)
run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap -- tofu plan
  →  args=('tofu','plan')
run --input-env X -- tofu plan -out=x -var a=b
  →  args=('tofu','plan','-out=x','-var','a=b')
run --input-env X -- env --ssh-key sneaky
  →  args=('env','--ssh-key','sneaky')   ssh_key=None
```

Two properties this buys, both measured: option-looking child arguments after
`--` are never absorbed by tunstrap (`-out=x`, `-var`, even `--ssh-key`), and the
`--` position is *not* needed to find the split — Click consumes the first `--`
and only the first (`run user@host -- -- helm` → `args=('user@host','--','helm')`,
which is exactly why the existing strip at `cli.py:272-274` exists; it is
retained, applied to the child-command slice).

**`--` is mandatory whenever the child command or any of its arguments begins
with `-`.** Without it Click parses those tokens as tunstrap options:
`run --input-env X tofu -version` → `NoSuchOption: No such option '-v'`
[measured 2026-07-31]. The shim always passes `--`; the docs must say so.

### Conflict matrix for `run`

Because env mode has no connection slot, the old "CONNECTION + `--input-env`"
conflict cannot be expressed and is therefore **structurally impossible** rather
than an error. The matrix is stated over `args` length instead:

| `--input-env` | `args` | conn flags | result |
|---|---|---|---|
| absent | `len ≥ 2` | any | flag mode, unchanged |
| absent | `len == 1` | any | usage **64** — "run requires a command: `tunstrap run USER@HOST ... -- CMD`" (today's message, `cli.py:276`) |
| absent | empty | any | usage **64** — "run requires USER@HOST[:PORT] or `--input-env`" (today Click emits "Missing argument 'CONNECTION'"; message becomes explicit, code stays 64) |
| present | `len ≥ 1` | none | env-input mode |
| present | empty | any | usage **64** — "run requires a command" |
| present | any | **any set** | usage **64** — "`--input-env` supplies the full InputSchema; connection flags are redundant" |

"conn flags" is `_conn_flags_present` (`cli.py:84-93`): `--ssh-key`,
`--ssh-key-passphrase`, `--ssh-password-stdin`, `--target`, `--kube`, `--fetch`.
`--ssh-password-stdin` is additionally the one stdin consumer in `run`
(`cli.py:111-112`), so its rejection under `--input-env` is doubly required.

**Daemon flags.** `--auto-stop-idle-seconds`, `--materialize` and `--log-file`
are attached by `_connection_options` (`cli.py:75-77`) but deliberately excluded
from `_conn_flags_present` (`cli.py:84-93`), so they need their own rule. Chosen
rule: **each is a usage error 64 under `--input-env`**, not an override and not
silently ignored.

| `--input-env` + | result |
|---|---|
| `--auto-stop-idle-seconds` | usage **64** — set `daemon.auto_stop_idle_seconds` in the payload |
| `--log-file` | usage **64** — set `daemon.log_file` in the payload |
| `--materialize` | usage **64** — redundant; `run` always forces it (below) |

Silent precedence between two authorities is the worst outcome for a caller
debugging a tunnel: the `InputSchema` already carries a complete `daemon` block
(`schemas.py:270`), so there must be exactly one place to look. Rejecting is
cheap and reversible; a precedence rule is neither.

**Invariant: `run` forces `daemon.materialize = True`, including on an
`--input-env` payload.** Today `run` passes `force_materialize=True`
(`cli.py:290`), so the flag mode cannot produce unmaterialized kube targets. An
env payload can say `materialize: false`, and then `render_env` raises "kube
target not materialized; cannot set KUBECONFIG" (`envrender.py:42-43`) — and,
worse for the shim, `--output-var` would hand the consumer `path: null` and the
`kubernetes`/`helm` providers would get an empty `config_path`. So `run`
overrides the payload's `materialize` to `True` unconditionally and documents it.
This is the one place `run` mutates the supplied schema; it is an invariant of
the verb, not a flag precedence rule.

`--output-var` is orthogonal and composes with every row. Payload-state failures
are separate and all exit 1 — see Error handling.

### `run` must not write to stdout

The shim's load-bearing rule is that **only the child's own output reaches
stdout**, because Terragrunt parses tofu's stdout and `terragrunt output -json`
consumers parse it downstream [measured 2026-07-31, fact 8]. `run` violates this
today, in its teardown:

`_teardown_run` (`cli.py:334-342`) calls `_kill_with_identity`, which writes a
`{"stopped": ...}` JSON line **to stdout on every outcome** — success
(`cli.py:399-402`, `:409-412`, `:430-433`, `:434-437`), `not found`
(`cli.py:381-384`), `identity mismatch` (`cli.py:385-389`), `unavailable`
(`cli.py:390-394`), and `identity changed during grace` (`cli.py:421-426`). Under
the shim this line lands in the middle of tofu's output stream. The existing
integration tests never catch it: `tests/integration/test_cli_modes.py:127-197`
assert only `returncode` and that `tunnel-data` is gone.

Fix: split the mechanism from its reporting.

```python
# identity/session-side primitive: performs the stop, writes nothing.
def stop_session(session_dir: str, pid: int, grace_seconds: int, *, force: bool) -> StopOutcome:
    ...  # returns e.g. StopOutcome(stopped=bool, reason=str|None, forced=bool)
```

- `stop_command` (`cli.py:345-358`) calls it and renders **exactly today's JSON
  on stdout**, key for key — `{"stopped": false, "reason": "not found"}`,
  `{"stopped": true, "forced": true}`, and so on. `stop`'s stdout is its
  documented contract and is unchanged.
- `_teardown_run` calls it and prints **nothing on success**. A failed teardown
  is a real diagnostic and goes to **stderr**, never stdout.
- **The failure diagnostic must be reachable.** `SessionDir.cleanup_path` is
  `shutil.rmtree(data, ignore_errors=True)` (`session.py:132-135`), which
  swallows every filesystem error — so a promise to report cleanup failures on
  stderr would be unsatisfiable as long as `run` calls it unchanged. Reconciled
  by making `cleanup_path` **report without raising**: it returns an outcome
  (e.g. the list of paths it could not remove) and still never propagates an
  exception, so `stop_command`'s behaviour is untouched while `run` has something
  to print. Concretely, the two teardown failure sources are: a non-`stopped`
  `StopOutcome` from `stop_session`, and a non-empty removal-failure list. Both
  go to stderr; neither changes the exit code.
- `read_identity` failure remains a silent branch (`cli.py:336-340`) — with (3)
  above, a missing identity file no longer means the path is unknown.

Combined with `run`'s existing stderr-only error paths (`cli.py:294`, `:299`,
`:325`), this makes the invariant total: **after the child starts, tunstrap
writes nothing to fd 1, ever.**

### Cleanup must own the whole post-spawn window

Today `run` does real work between a successful `spawn_daemon` and the `try`
whose `finally` tears down: `OutputSchema.model_validate` and `render_env`
(`cli.py:302-304`), with the `try` opening only at `cli.py:308`. Anything raised
in that window orphans the daemon, and `run` has no top-level guard (`start` has
one at `cli.py:234-247`). `--output-var` adds JSON encoding to the same window.

Requirements:

1. **All usage validation happens before `spawn_daemon`** — the whole conflict
   matrix, `--output-var` NAME validity, and reading/parsing/validating the
   `--input-env` payload. Nothing that can exit 64 or 1 may run after a daemon
   exists.
2. **The multi-node/`--output-var` check is pre-spawn too.** Node count is a
   property of the *input* schema (`len(schema.nodes)`), not the output, so
   `len(schema.nodes) != 1 and output_var is None` → exit 1 **before** spawning.
   This removes the largest new orphan risk this design would otherwise add.
3. **`run` mints the session path *before* spawning, and never learns it from
   the payload.** Today `run` passes `session_dir` straight through to
   `spawn_daemon`, and when it is `None` the **worker** generates it
   (`session.py:create`, `tempfile.mkdtemp`), so the parent can only recover the
   path by parsing the success envelope. That is the root cause of the orphan
   window: cleanup depends on the very object whose validation can fail. Instead,
   when the caller supplies no `--session-dir`, `run` creates one itself
   (`tempfile.mkdtemp`) and passes it explicitly. `SessionDir.create` already
   accepts a supplied absolute path and does `mkdir(parents=True,
   exist_ok=True)`, so an empty pre-created directory is valid input. The session
   path is then a **precondition of spawning**, known before the daemon exists
   and independent of the payload.

   Consequence to honour: a supplied path sets `generated=False`, so the worker
   will not remove the directory root. `run` therefore removes its **own** minted
   temp root after teardown, and never removes a caller-supplied `--session-dir`
   (matching today's `_teardown_run`, which only clears `tunnel-data`).
4. **One `try/finally` opens the instant `spawn_daemon` returns success**, with
   *nothing whatsoever* between the two. It encloses `model_validate`,
   `render_env`, `--output-var` encoding, `Popen`, signal handling and `wait`.
   Because the session path came from (3), the `finally` can always locate and
   stop the daemon — including when `model_validate` raises on a malformed
   success payload, which is exactly the case an earlier draft of this spec left
   unguarded.
5. **Teardown must not be skippable by a failure in the `finally` itself.**
   Signal-handler restoration (`cli.py:328-329`) precedes `_teardown_run`
   (`cli.py:330`) in the same `finally`; if restoration raised, teardown would
   never run. Restoration therefore goes in its own nested `try/finally` whose
   `finally` performs the teardown, so the daemon is stopped regardless. The stop
   primitive itself must not raise: unexpected exceptions inside it are caught,
   reported on stderr, and **must not override the already-determined child exit
   code** — a child that ran and returned 7 still exits 7 even if teardown
   misbehaves.
6. **A top-level guard on `run`** mirroring `start`'s (`cli.py:234-247`) but
   writing the `DaemonError` envelope to **stderr** (never stdout, see above) and
   exiting **4**.

### Explicitly NOT done: generalizing `render_env` to multi-node

`render_env` requires exactly one node (`envrender.py:19-20`) because
`TUNSTRAP_<TARGET>_*` has no node dimension: two nodes with a target named `k3s`
collide irreducibly, and `2026-06-25-cli-run-modes-design.md:245` already places
multi-node CLI input in Out of scope. `--output-var` serves the case with a
channel that *has* a node dimension (`connections` is keyed by node), so the
single-node contract stays exactly as documented — untouched and unbroken — and
the codebase gains one fewer branch. The only `envrender.py` change is turning
the bare `ValueError` into a typed error.

### The tofu shim — consumer-facing, Terraform-specific, outside tunstrap

```sh
#!/bin/sh
[ -n "$TUNSTRAP_INPUT" ] || exec tofu "$@"
case "$1" in init|-version) exec tofu "$@" ;; esac
exec tunstrap run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap \
  -- env -u KUBECONFIG tofu "$@"
```

**Why `env -u KUBECONFIG`.** For a single-node payload `run` injects
`KUBECONFIG` into the child (`envrender.py:48-49`), pointing at the same
materialized file `config_path` would use. Left in place it is a silent
fallback: if the `TF_VAR_tunstrap` → `config_path` wiring were broken or
removed, the `kubernetes` and `helm` providers would still find a working
cluster via `KUBECONFIG` and everything would appear fine. Clearing it makes the
decoded `config_path` the **only** route to the cluster, so a broken chain fails
instead of silently working. This restores a protection the consumer already has
and this spec had dropped: the script being replaced does exactly this at
`terragrunt.hcl:61-63` — *"Clobber $KUBECONFIG so neither tunstrap itself nor
any child process probe can fall back to the operator's personal kubeconfig."*
The same reasoning makes the e2e tier's central assertion meaningful rather than
decorative (assertion 4 below).

Both Terraform-specific decisions live here, not in tunstrap:

- **Pass-through when `TUNSTRAP_INPUT` is unset.** Replaces the whole
  `--placeholder-host` design: `env_vars` is an ordinary HCL map, so the
  consumer omits the key entirely when infra is not applied.
- **Skip the tunnel for `init` and `-version`.** `tofu init` configures the
  backend and downloads providers; it contacts neither the k8s API nor Helm, and
  the consumer's state backend is S3-compatible over the public internet
  (`root.hcl:24-41`), not tunneled. Without the skip, [measured 2026-07-31]
  env-var scoping (below) yields **two** tunnels per `terragrunt plan` — one for
  the auto-`init`, one for `plan`.

`exec` is correct in both pass-through branches (nothing to clean up). Nothing
must `exec` *past* teardown, and nothing does: `tunstrap run` already owns the
child via `Popen` + signal forwarding + `finally` teardown (`cli.py:306-331`).
`exec`ing *into* `tunstrap run` is fine and desirable — one less process level,
and Terragrunt's signals reach tunstrap directly.

**The shim must never write to stdout.** Terragrunt captures and labels tofu
stdout by default (`--tf-forward-stdout` changes this) and `terragrunt output
-json` consumers parse it. Diagnostics go to stderr or a file.

### Shipping the shim: consumer file *and* a console script (revised)

> **Revised after this design landed.** The original recommendation was
> "consumer keeps the shim in its own repo; the package ships none". The owner
> has since reversed that: the proxy **also** ships in-package as a second
> `[project.scripts]` entry, `tunstrap_tofu` (`tunstrap/tofu_proxy.py`), so
> `uv tool install` yields both `tunstrap` and `tunstrap_tofu` and
> `terraform_binary` points at a stable installed path with nothing copied into
> the consumer's repo. The consumer-file shim remains available and is still
> what the e2e tier drives; the two coexist.

The original three reasons for keeping the proxy out of the package, and where
each stands after the reversal:

(a) **Interpreter startup on the fast paths** — real, and it kills the naive
approach. Measured (see `docs/recipe_terragrunt.md` "Why a console script
(now)"): `sh` shim ~2 ms; bare Python ~17 ms; Python plus `import
tunstrap.cli` ~225 ms (the import alone ~184 ms). The shipped `tunstrap_tofu`
does **not** import `cli` on the pass-through paths — it `execvp`s `tofu`
first — so its measured fast path is **~59 ms** end-to-end (~17 ms interpreter
+ ~41 ms `importlib.metadata` in the package `__init__`, structural, + ~1 ms
execvp; the `tofu_proxy` module itself adds nothing observable). At three
fast-path invocations per `terragrunt plan` that is ~180 ms/plan, judged noise
beside an 8 s `tofu init`. The discipline (no `cli`/`click`/`pydantic`/etc. on
the pass-through paths) is guarded by a unit test; the consumer-file shim
remains the ~2 ms option for cost-sensitive consumers.

(b) **Terraform vocabulary inside the package** — this is the trade being
**deliberately taken**. `init`, `-version`, and `TF_VAR_` now live in
`tunstrap/tofu_proxy.py`. The Terraform-free principle this design is structured
around (decision-log items 7 and 20) is therefore no longer absolute; the
reversal is recorded there, not silently applied. `cli.py` itself stays generic
— the only vocabulary added there is a `suppress_kubeconfig` parameter, not
any Terraform name.

(c) **No stable distribution path** — answered. `uv tool install` yields a
stable `tunstrap_tofu` entry point identical across reinstalls; the ephemeral
`~/.cache/uv/…` path was always a `uvx` artefact, not a `uv tool install` one.

The shipped entry point also closes the consumer shim's documented
`tofu -chdir=DIR init` gap (see the recipe): it parses argv past global flags
rather than matching a literal first token.

### Measured Terragrunt facts

All *[measured 2026-07-31, Terragrunt v1.1.1 / OpenTofu v1.12.5]*. These are
observations about that pair, not repo invariants.

1. `terraform_command_line` is **not** an attribute. The hook is
   `terraform_binary` / `--tf-path` / `TG_TF_PATH`, and it is a **path only**:
   `--tf-path "/tmp/.../wrapper.sh --dummy"` fails on the `-version` probe with
   `fork/exec /tmp/.../wrapper.sh --dummy: no such file or directory`.
2. `inputs` reach tofu as **`TF_VAR_<name>` env vars with JSON-encoded values** —
   not `.tfvars.json`, not `-var-file`. Observed:
   `TF_VAR_secret_thing={"key":"-----BEGIN OPENSSH PRIVATE KEY-----\n..."}`. The
   consumer's `ssh_private_key` is already in the child environment today.
3. `terraform { extra_arguments "n" { commands env_vars } }` sets env vars for
   the tofu child **and the shim itself receives them** — the crux.
4. **Env-var scoping**, per-unit. `terragrunt plan` with
   `commands = ["plan","apply"]`: `-version` unset, auto-`init` **set**, `plan`
   set. `terragrunt output` (unlisted): all three unset.
5. **Invocation counts.** `plan`/`apply`/`output`/`validate` → 3 tofu
   invocations (`-version`, `init`, the command); `init` → 2; a `dependency`
   adds 2 in the *dependency's own* unit (`init`, `output -json`), which never
   see the dependent unit's `env_vars`. `-version` fires once per terragrunt
   run, not once per unit.
6. **`dependency.*` resolves inside `terraform { extra_arguments { env_vars } }`,
   but not inside `locals`** (there: `"dependency" is not defined`). The
   consumer's comment at `terragrunt.hcl:50-51` — "Terragrunt 1.0.x resolves
   `dependency.*` only inside `inputs`" — is **too narrow** on 1.1.1. This is the
   fact the whole design rests on.
7. **Payload fidelity.** A 10,065-byte JSON value arrived at both auto-`init` and
   `plan` with identical length and SHA-256 — no truncation, no `E2BIG`.
   Multi-line content with PEM delimiters, `"` and `$` arrived byte-identical.
8. Terragrunt labels and processes tofu stdout by default; anything the shim
   writes there can corrupt `terragrunt output -json`.

## Components touched

| File | Change |
|------|--------|
| `cli.py` | `run`: replace the CONNECTION + COMMAND positional pair (`:251`, `:255`) with one `args` variadic and the post-parse split; add `--input-env VAR` and `--output-var NAME`; implement the full conflict matrix **pre-spawn**; build the schema from the env payload reusing `start`'s parse/validate path (`:194-211`); force `daemon.materialize=True` on that payload; **mint the session path before `spawn_daemon` so cleanup never depends on the payload**; inject `--output-var` into `child_env` beside `render_env(out)` (`:303`); move `model_validate`/`render_env` inside the teardown `try` (`:302-308`) with signal restoration in a nested `try/finally`; make `_teardown_run` silent on success and stderr-only on failure; add a stderr-only top-level guard (exit 4). |
| `cli.py` (`stop`/`status`) | `_kill_with_identity` (`:376-437`) loses its `sys.stdout.write` calls; `stop_command` (`:345-358`) renders the identical JSON from the returned outcome, so `stop`'s stdout contract is byte-for-byte unchanged. |
| `identity.py` or `session.py` | New silent `stop_session(session_dir, pid, grace_seconds, *, force) -> StopOutcome` primitive carrying the mechanism with no I/O. |
| `session.py` (`cleanup_path`) | Return an outcome (paths it could not remove) instead of discarding every error via `ignore_errors=True` (`:132-135`); still never raises, so `stop` is unaffected, but `run`'s stderr diagnostic becomes reachable. |
| `cli_input.py` | New `build_schema_from_env(var_name)` — read, JSON-decode, `InputSchema.model_validate`, all failures as `SchemaValidationError`. Mirrors the existing `build_single_node_schema` error discipline (`:113-123`). |
| `envrender.py` | Replace the bare `ValueError` for multi-node (`:19-20`) with the new typed error. No multi-node rendering. |
| `exceptions.py` | Add `MultiNodeEnvUnsupported(TunstrapError)` mapped to exit **1** in `_EXIT_CODES` (`:57-63`). No new exit code is needed; `5` stays unallocated. |
| `README.md` | Document both flags and the mandatory `--` on `run`; link the new recipe; fix the three stale `token` references. |
| `docs/recipe_terragrunt.md` | New. |
| `pyproject.toml` (test config + `tunstrap_tofu` entry) | Add the `e2e` marker; `addopts` → `-m 'not integration and not e2e'`. **Revised (see "Shipping the shim"):** add the second console script `tunstrap_tofu = "tunstrap.tofu_proxy:main"`. No dependency changes. |
| `tunstrap/tofu_proxy.py` (revised) | New module: the in-package `tunstrap_tofu` entry point. Pass-through branches `execvp` `tofu` without importing `cli`; the tunnelled branch delegates to `run` in-process via `run_via_env_input` with `suppress_kubeconfig=True`. Parses argv past global flags so `-chdir=DIR init` bypasses correctly. See "Shipping the shim (revised)". |
| `tests/e2e/` | New, self-contained tier: `conftest.py` (own keypair + kind + compose lifecycle), `docker-compose.yml` (one `sshd-kube` on the external `kind` network), committed `_sshd_conf/allow_tcpfwd.conf` and `shim/tofu-tunstrap`, `module/` (providers + local chart), `test_tofu_providers.py`, `test_shim.py`. Borrows nothing from `tests/integration/`. |
| `.gitignore` | Add `tests/e2e/_keys/` and `tests/e2e/_kube/` (both fixture-generated). |
| `.github/workflows/test.yml` | New `e2e` job installing kind + tofu; not added to the coverage combine. |

`pyproject.toml` now adds the `tunstrap_tofu` console script (revised — see
"Shipping the shim"). `cli.py` gains only a generic `suppress_kubeconfig`
parameter on `_build_child_env`/`_run_child`/`_supervise_child`/`run_command`
and a generic `run_via_env_input` programmatic entry; **no Terraform vocabulary
is added to `cli.py`** — the proxy's `init`/`TF_VAR_tunstrap`/`tofu` names live
entirely in `tunstrap/tofu_proxy.py`.
`daemon.py`, `_worker.py`, `schemas.py`, `manager.py`, `kube.py`, `fetcher.py`
are untouched, and `OutputSchema` gains no fields — no `owner`, no
`placeholder`.

## Error handling

Every row above the `spawn_daemon` line is evaluated **pre-spawn**, so none of
them can orphan a daemon.

| Condition | Phase | Channel | Exit |
|---|---|---|---|
| any row of the conflict matrix (incl. conn flags and daemon flags under `--input-env`) | pre-spawn | Click usage message, stderr | 64 |
| `--output-var` NAME invalid, or colliding with a `render_env` key | pre-spawn | Click usage message | 64 |
| named variable unset / empty / whitespace | pre-spawn | `SchemaValidationError` JSON on stderr | 1 |
| named variable not JSON | pre-spawn | `SchemaValidationError` + `{"position": n}` | 1 |
| JSON fails `InputSchema` | pre-spawn | `SchemaValidationError` + pydantic errors | 1 |
| `len(schema.nodes) != 1` and no `--output-var` | pre-spawn | `MultiNodeEnvUnsupported` JSON on stderr | 1 |
| required tunnel failed / session active / daemon error | spawn | existing paths (`cli.py:297-300`) | 2 / 3 / 4 |
| anything raised post-spawn (`model_validate`, `render_env`, `--output-var` encode) | post-spawn, inside the `try` | `DaemonError` JSON on stderr, teardown runs | 4 |
| child cannot be launched | post-spawn, inside the `try` | `cli.py:324-326` | 127 |
| teardown itself fails (non-`stopped` outcome, or unremovable paths) | `finally` | diagnostic on **stderr** only | **never** changes the exit code |
| child ran | — | child's code (`cli.py:331`) | child |

**Every one of these channels is stderr.** `run`'s existing error paths already
are (`cli.py:294`, `:299`, `:325`); this design closes the last stdout leak in
the teardown (see "`run` must not write to stdout"). Under the shim, fd 1 belongs
to tofu and to nothing else.

## Testing (TDD)

**Parser tests come first.** The argument-surface change (Addition 3) is the one
place where a plausible-looking implementation silently mis-binds, so these are
written before anything else and use the **exact documented shim invocation
verbatim**, not a paraphrase:

```python
# must yield: connection=None, command=("tofu", "plan")
["run", "--input-env", "TUNSTRAP_INPUT", "--output-var", "TF_VAR_tunstrap",
 "--", "tofu", "plan"]
```

Plus, at parser level: `run user@host --ssh-key K --target web=a:80 -- helm list`
→ connection `user@host`, command `("helm","list")`, flags bound (flag-mode
regression); `run --input-env X -- tofu plan -out=x -var a=b` → command keeps all
five tokens; `run --input-env X -- env --ssh-key sneaky` → command keeps
`--ssh-key sneaky` **and** tunstrap's own `ssh_key` stays `None`;
`run user@host -- -- helm` → command `("helm",)` after the existing strip
(`cli.py:272-274`); `run --input-env X tofu -version` (no `--`) → usage error 64,
documenting that `--` is mandatory for `-`-prefixed child arguments.

Unit (`tests/unit/`):

- `--input-env`: valid single-node JSON → schema equals the stdin-parsed
  equivalent; valid multi-node JSON → N nodes; variable absent, empty or
  whitespace → exit 1; malformed JSON → exit 1 with `details.position`;
  schema-invalid JSON → exit 1 with `details.errors`.
- Conflict matrix: every row → exit 64 **and** `spawn_daemon` asserted not
  called — including each daemon flag (`--auto-stop-idle-seconds`, `--materialize`,
  `--log-file`) under `--input-env`. No usage error may ever leak a daemon.
- Forced materialize: an `--input-env` payload with `daemon.materialize=false`
  reaches `spawn_daemon` with `materialize=True`.
- `--output-var`: child env has NAME whose value round-trips through
  `OutputSchema.model_validate`, scalars still present for one node; invalid
  NAME and `render_env`-key collision → 64.
- Multi-node: with `--output-var` → NAME present, **no** `TUNSTRAP_*` scalars,
  `connections` carries every node; without it → exit 1 **pre-spawn**
  (`spawn_daemon` asserted not called), typed error, not a traceback.
- Post-spawn safety: with `spawn_daemon` mocked to succeed and
  `OutputSchema.model_validate` / `render_env` / the `--output-var` encode each
  patched to raise in turn, teardown is still invoked exactly once and the exit
  code is 4. Explicitly including a **malformed success payload** (no
  `session_dir` key, or a non-string one): teardown must still stop the daemon,
  which is only possible because the path was minted pre-spawn.
- Teardown is not skippable: with signal restoration patched to raise, teardown
  still runs. With the stop primitive patched to raise, the child's exit code
  (7) is still what `run` exits with, and the diagnostic lands on stderr.
- Silent teardown: `_teardown_run` writes nothing to stdout on success, and its
  failure diagnostic goes to stderr.
- `stop` regression: `stop_command` stdout is byte-identical to today for each
  outcome — `{"stopped": true}`, `{"stopped": true, "forced": true}`,
  `{"stopped": false, "reason": "not found"}`, `"identity mismatch"`,
  `"identity check unavailable"`, `"identity changed during grace"`.
- Regression: `run USER@HOST -- CMD` with no new flags produces a
  byte-identical child env.

Integration (`tests/integration/`, whose compose already provides `sshd-a/b/c`,
`sshd-bastion`, `http-target-1/2` and `fake-apiserver`):

- `run --input-env X --output-var Y -- <child printing $Y>` against one sshd
  node: child sees a valid `OutputSchema` and its endpoints are live. Same with
  two sshd nodes: `connections` has both keys, no `TUNSTRAP_*` scalars, both
  endpoints reachable.
- Teardown: after exit, `session_dir` is gone and no daemon remains (mirrors
  `2026-06-25-cli-run-modes-design.md:238-240`); `-- sh -c 'exit 7'` → exit 7.
- **Stdout purity — `run`'s stdout is byte-for-byte the child's stdout**, with
  the child emitting a known sentinel, asserted across all five outcomes:
  (a) success; (b) child exit 7; (c) graceful stop (daemon exits within
  `--grace-seconds`); (d) forced stop (daemon ignores SIGTERM → SIGKILL path,
  `cli.py:427-437`); (e) teardown identity error (`tunnel-data` identity
  tampered so `verify_session` returns `mismatch`). Today's tests
  (`tests/integration/test_cli_modes.py:127-197`) assert only return codes and
  cleanup, which is exactly why this defect was unguarded.
- Shim behaviour without tofu: the shim + a fake `tofu`, asserting pass-through
  when the variable is unset, pass-through for `init`/`-version`, tunnel +
  `TF_VAR_*` injection otherwise, `KUBECONFIG` absent from the child env, and
  clean stdout in every branch.

### The `e2e` tier — real Kubernetes through the tunnel

**Why it must exist.** The current rig's `fake-apiserver` /
`fake-apiserver-nosan` are `openssl req -x509` TLS listeners that accept a
handshake and immediately close (`tests/integration/docker-compose.yml`). They
prove SAN probing and kubeconfig patching and nothing more. **No test anywhere
proves that a real Kubernetes client — let alone OpenTofu's `kubernetes` and
`helm` providers — works through a tunstrap tunnel**, which is this design's
central value claim. This tier would have caught the iteration-2
`materialize: false` → `path: null` → empty `config_path` defect by execution
rather than by inspection.

Everything in this subsection labelled *[measured 2026-07-31]* was executed
end-to-end on this workstation (Docker 28.1.1, kind 0.30.0, `kindest/node`
v1.34.0, OpenTofu v1.12.5, kubectl v1.28.1, tunstrap 0.0.4). Items labelled
*[designed, unverified]* could not be executed and say why.

#### Topology

kind creates its own Docker bridge network named `kind`; the control-plane
container joins it and publishes `6443` on a **random** host port
(`127.0.0.1:45491->6443/tcp` in the probe run), so the host-side kubeconfig is
useless to a container. Measured reachability:

| From | To | Result |
|---|---|---|
| container on network `kind` | `https://<cluster>-control-plane:6443/version` | HTTP 200 |
| container on network `kind` | `https://172.18.0.2:6443/version` | HTTP 200 |
| container on the default bridge | `https://172.18.0.2:6443/version` | connect failure |

So the SSH node container **must** join the `kind` network. This mirrors
production faithfully: tunstrap SSHes to a host that can reach the API server,
and forwards to it.

Compose changes — a new `tests/e2e/docker-compose.yml` (not an edit to the
integration one, see Constraints below) with a single `sshd-kube` service that
declares the kind network as **external**:

```yaml
services:
  sshd-kube:
    image: lscr.io/linuxserver/openssh-server:latest
    environment: [PUID=1000, PGID=1000, USER_NAME=tester,
                  PUBLIC_KEY_FILE=/keys/id_test.pub,
                  SUDO_ACCESS=false, PASSWORD_ACCESS=false]
    volumes:
      - ./_keys:/keys:ro              # generated by the e2e fixture, NOT shared
      - ./_sshd_conf:/config/sshd/sshd_config.d:ro   # REQUIRED, see below
      - ./_kube:/etc/kube:ro          # the node's kubeconfig, fixture-populated
    ports: ["127.0.0.1::2222"]
    networks: [kind]
networks:
  kind:
    external: true            # created by `kind create cluster`
```

**The `sshd_config.d` mount is mandatory, not cosmetic.** In
`lscr.io/linuxserver/openssh-server:latest` as pulled 2026-07-31, the shipped
`/config/sshd/sshd_config` sets `AllowTcpForwarding no` at line 92, and the image
emits an `Include /config/sshd/sshd_config.d/*.conf` line **only when that
directory exists**. Because OpenSSH takes the first obtained value for a keyword,
the mounted `allow_tcpfwd.conf` (`AllowTcpForwarding yes`) wins over line 92 —
but only if the directory is mounted. Without it the server refuses every
forwarded connection with `administratively prohibited`, while the tunnel still
appears to start cleanly [measured 2026-07-31 — see "Why the `sshd_config.d`
mount matters" below].

#### The remote kubeconfig

The fixture copies the control-plane's **in-node** kubeconfig
(`/etc/kubernetes/admin.conf`, obtained with `docker cp` / `docker exec cat`)
into `tests/e2e/_kube/admin.conf`, which the compose file mounts read-only at
`/etc/kube/admin.conf` inside `sshd-kube`. That path is what `kube_targets`
reads over SSH, exactly as the consumer reads `/etc/rancher/k3s/k3s.yaml`.

Measured properties of that file, all of which the flow depends on:

- `server: https://<cluster>-control-plane:6443` — a DNS name, resolvable on the
  `kind` network. `kube.py:295` parses this to decide the forward target, so the
  forward lands on the right host without any extra configuration.
- It carries embedded `certificate-authority-data`, `client-certificate-data`
  and `client-key-data` — the shape `parse_kubeconfig` expects.
- The apiserver certificate SANs are
  `DNS:kubernetes, kubernetes.default, kubernetes.default.svc,
  kubernetes.default.svc.cluster.local, localhost, <cluster>-control-plane` and
  `IP:10.96.0.1, 172.18.0.2, 127.0.0.1`.

Because the original host (`<cluster>-control-plane`) **is** in the DNS SAN list,
`choose_tls_server_name` (`kube.py:177-196`) returns it as an exact match with
`fellback=False`, so the tier exercises the **clean, warning-free** path rather
than a fallback. Measured result of `tunstrap start` against this fixture:

```
endpoint        : https://127.0.0.1:37141
tls_server_name : <cluster>-control-plane
materialized    : <session>/tunnel-data/node-k3s
warnings        : []
```

and `kubectl --kubeconfig <materialized> get nodes` returned the control-plane
node `Ready` [measured 2026-07-31]. Cluster naming must be deterministic
(`kind create cluster --name tunstrap-e2e` → container
`tunstrap-e2e-control-plane`) because that name is both the forward target and
the expected `tls_server_name`.

#### Fixture layout

```
tests/e2e/
  __init__.py
  conftest.py                  # kind + compose lifecycle, session-scoped
  docker-compose.yml
  _keys/                       # GENERATED, gitignored: id_test, id_test.pub
  _sshd_conf/                  # TRACKED: allow_tcpfwd.conf ("AllowTcpForwarding yes")
  _kube/                       # GENERATED, gitignored: admin.conf
  shim/tofu-tunstrap           # TRACKED: the shim under test
  module/
    versions.tf                # kubernetes + helm provider constraints
    main.tf                    # var.tunstrap -> try(jsondecode) -> config_path
    charts/probe/
      Chart.yaml               # apiVersion: v2, name: probe, version: 0.1.0
      templates/configmap.yaml # one ConfigMap: data.proof = "through-the-tunnel"
  test_tofu_providers.py
  test_shim.py
```

**The tier must be self-contained; it cannot borrow the integration rig's
fixtures.** `tests/integration/_keys/` is gitignored (`.gitignore:42`) and
untracked (`git ls-files tests/integration/_keys/` → empty); the keypair exists
only as a side effect of the `ssh_keypair` fixture in
`tests/integration/conftest.py:24-57`, which pytest loads **only** for tests
under `tests/integration/`. A `pytest tests/e2e` run in a clean checkout would
therefore mount a non-existent directory. Hence: the e2e fixture generates its
**own** Ed25519 keypair into `tests/e2e/_keys/` (same `cryptography`-based
approach as the integration fixture, which avoids a paramiko dependency), and
`_sshd_conf/allow_tcpfwd.conf` is **committed** rather than referenced across
suites. No cross-suite coupling in either direction — which also keeps the
"do not break the existing rig" constraint literally true.

`.gitignore` gains `tests/e2e/_keys/` and `tests/e2e/_kube/`.

#### Session fixture lifecycle

One session-scoped fixture, yielding a dict of connection facts. Sequence:

1. **Preflight.** Skip the whole tier with a clear reason if `kind`, `tofu` or
   `docker` is absent — a missing tool must not look like a product failure.
2. **Keys.** Generate `_keys/id_test` (0600) + `id_test.pub` if absent.
3. **Cluster.** `kind create cluster --name tunstrap-e2e --wait 90s`. The name is
   fixed because the control-plane container name (`tunstrap-e2e-control-plane`)
   is both the SSH forward target and the expected `tls_server_name`. Delete any
   pre-existing cluster of that name first, so a crashed prior run cannot leave a
   half-configured cluster that silently changes results.
4. **Kubeconfig.** `docker exec tunstrap-e2e-control-plane cat
   /etc/kubernetes/admin.conf` → `_kube/admin.conf` (0644 — it is read over SSH
   by the `tester` user).
5. **Compose.** `docker compose up -d --wait`, which joins the external `kind`
   network. Then **SSH readiness**: poll an actual authenticated SSH command
   (not a TCP connect — the listener accepts before the key is installed) until
   it succeeds or a timeout expires. Then discover the **dynamic** host port with
   `docker compose port sshd-kube 2222`, exactly as the integration conftest does
   (`conftest.py:91-99`); the published port is random by design.
6. **Yield** `{host, port, private_pem, cluster_name, kubeconfig_in_node_path,
   module_dir, shim_path}`.
7. **Teardown**, unconditional and in reverse: `docker compose down -v`, then
   `kind delete cluster --name tunstrap-e2e`, then remove `_kube/`. Cluster
   deletion must run even if compose teardown fails.

**Per-test isolation.** Each test gets its own copy of `module/` in a `tmp_path`,
with `TF_DATA_DIR` and the state file inside it, so tests cannot share
`.terraform/` or `terraform.tfstate` and cannot pass because of a neighbour's
leftovers. Provider *downloads* are shared via a session-scoped
`TF_PLUGIN_CACHE_DIR`, so isolation costs one `init` per test but not one
download per test.

`module/main.tf` is the **exact chain this spec designs**, not a hand-written
kubeconfig path:

```hcl
variable "tunstrap" {
  type      = string
  default   = ""
  sensitive = true
}

locals {
  tunnel   = try(jsondecode(var.tunstrap), { connections = {} })
  kubepath = try(local.tunnel.connections.node.kube_targets.k3s.path, "")
}

provider "kubernetes" {
  config_path            = local.kubepath != "" ? local.kubepath : null
  host                   = local.kubepath == "" ? "https://127.0.0.1:0" : null
  cluster_ca_certificate = local.kubepath == "" ? "" : null
  client_certificate     = local.kubepath == "" ? "" : null
  client_key             = local.kubepath == "" ? "" : null
}

provider "helm" {
  kubernetes {
    # same five lines
  }
}

resource "kubernetes_namespace" "probe" {
  metadata { name = "tunstrap-e2e" }
}

resource "helm_release" "probe" {
  name      = "probe"
  chart     = "${path.module}/charts/probe"
  namespace = kubernetes_namespace.probe.metadata[0].name
}
```

The inert branch is deliberately identical in shape to the consumer's
(`terragrunt.hcl:35-47`), so this module is also a regression test for the
provider configuration the recipe tells consumers to write.

**Pin the providers.** `>= 2.30.0` resolved to `hashicorp/kubernetes` **v3.2.1**,
which emits `Deprecated; use kubernetes_namespace_v1` for
`kubernetes_namespace` [measured 2026-07-31]. Pin `~> 2.30` (matching the
consumer's `versions.tf:9-33`) or use the `_v1` resource names, so a provider
major bump cannot turn this tier red for a reason unrelated to tunnelling.

#### Assertions

Every assertion below states **how it fails when its target is broken**. An
assertion with no such mechanism is decorative and does not belong here.

| # | Assertion | Fails when broken because… | Status |
|---|---|---|---|
| 1 | `tofu apply` creates Namespace `tunstrap-e2e`, read back through the API | no tunnel ⇒ provider cannot reach any apiserver ⇒ apply errors | *[measured — "Apply complete! Resources: 2 added"]* |
| 2 | `helm_release` creates ConfigMap `probe-cm` with `data.proof=through-the-tunnel` | value is compared, not just existence; a stale/foreign object fails the compare | *[measured]* |
| 3 | Helm release recorded in-cluster as Secret `sh.helm.release.v1.probe.v1` | absent if the provider only rendered locally without reaching the cluster | *[measured]* |
| 4 | **Chain integrity** (see below) | KUBECONFIG cleared + negative control + `kubepath` output compare | *[measured in part — see "On assertion 4"]* |
| 5 | `tofu destroy` removes both; Namespace lookup returns `NotFound` | a no-op destroy leaves the Namespace present and the lookup succeeds | *[measured — "Destroy complete! Resources: 2 destroyed"]* |
| 6 | Inert branch: `TF_VAR_tunstrap` unset ⇒ `tofu plan` succeeds | if `try()` were dropped to bare `jsondecode`, `jsondecode("")` errors and plan fails | *[measured]* |
| 7 | **`init` pass-through** (see below) | poisoned `TUNSTRAP_INPUT` makes any accidental tunnel abort before tofu starts | *[measured in part]* |
| 8 | **Child exit-code propagation** (see below) | sentinel proves the child ran; exit code 42 is outside tunstrap's reserved set | *[designed, unverified]* |
| 9 | **stdout purity** (see below) | byte-equality against the same child run without the shim | *[designed, unverified — the leak it guards is reproduced live below]* |
| 10 | Real provider failure still surfaces: apply against a stopped cluster exits non-zero | distinct from 8; covers the error path 8 deliberately excludes | *[designed, unverified]* |

**On assertion 4 — chain integrity.** The tier's whole purpose is to prove
providers are configured by `--output-var` → `TF_VAR_tunstrap` →
`try(jsondecode(...))` → `config_path`. Three mutually reinforcing checks,
because the naive version can pass for the wrong reason:

- **KUBECONFIG is cleared** by the shim (`env -u KUBECONFIG`, above), and the
  test asserts it: a `tofu` wrapper on `PATH` dumps its environment, and the test
  requires `KUBECONFIG` to be **absent** and `TF_VAR_tunstrap` **present**.
  Without this, a broken chain would silently succeed via the injected
  `KUBECONFIG`, which points at the very same materialized file.
- **Negative control.** The identical apply with `TF_VAR_tunstrap` **unset** but
  everything else unchanged (tunnel up, `run` still injecting its env) must
  **fail**. If it succeeds, some other path is reaching the cluster and the
  positive result proves nothing. This is the single most valuable assertion in
  the tier.
- **Value compare.** `module/` exposes `output "kubepath_used" { value =
  local.kubepath }`; the test asserts it equals
  `connections.node.kube_targets.k3s.path` from the envelope. A hard-coded or
  fallback path fails the compare.

**On assertion 7 — `init` pass-through.** Checking that no session directory
exists *after* `init` returns cannot distinguish "never tunnelled" from
"tunnelled and torn down correctly". Make the wrong path fail loudly instead:
run `init` with `TUNSTRAP_INPUT` set to a **deliberately invalid, non-empty**
value (e.g. `{invalid`). Pass-through never reads it, so `init` succeeds and the
`tofu` wrapper records that it ran. Any accidental `tunstrap run` exits **1**
(`SchemaValidationError`, pre-spawn) and tofu never launches. Assert exit 0 **and**
the wrapper's execution sentinel — the two together are unambiguous.

**On assertion 8 — child exit code.** Asserting merely "non-zero" proves nothing:
tunnel failure (2/3/4), usage error (64) and provider failure are all non-zero
and most do not involve the child running at all. So: a fake `tofu` on `PATH`
prints a fixed sentinel to stdout and exits **42** — outside tunstrap's reserved
set (1, 2, 3, 4, 64) and distinct from the launch-failure code 127. Assert the
exact code 42 **and** the sentinel. Real provider failures are covered separately
by assertion 10, so this assertion is not weakened to accommodate them.

**On assertion 9 — stdout purity.** Asserting merely that the known
`{"stopped": …}` line is absent would pass while some *other* tunstrap message
contaminated the stream. The oracle is byte equality: the same deterministic
fake `tofu` (fixed sentinel bytes, no timestamps, no random ordering) is run
twice — once directly, once through the shim — and the two captured stdouts must
be **byte-for-byte identical**. Any injected byte, from any source, fails.

**What "measured in part" means for assertions 7-9.** The designed shim calls
`tunstrap run --input-env ... --output-var ...`, and those flags do not exist in
0.0.4, so the real shim could not be executed. What was executed is a
**branch-for-branch simulation** using today's `start`/`stop`, with identical
dispatch:

```
$ shim init    → PASSTHRU(init/version) init …          (no session created)
$ shim apply   → TUNNEL apply …  session_created=yes
                 Apply complete! Resources: 2 added
```

So the *dispatch logic* and the *init-skip* are measured; the exact flag surface
is not, and becomes verifiable the moment Additions 1–2 land.

The stdout defect assertion 9 guards is not hypothetical. Under today's code:

```
$ tunstrap run … -- sh -c 'echo CHILD_STDOUT_SENTINEL'
1  CHILD_STDOUT_SENTINEL
2  {"stopped": false, "reason": "identity changed during grace"}
```

[measured 2026-07-31] — line 2 is `_kill_with_identity` writing to stdout, and
under the shim it would land inside tofu's output stream. This tier is where
that assertion runs against real tofu output, complementing the unit-level
checks.

#### Marker, CI job, and blast radius

**Own marker `e2e`, own CI job.** `pyproject.toml` gets
`markers = [..., "e2e: requires kind + tofu (real cluster)"]` and `addopts`
becomes `-m 'not integration and not e2e'`. Rationale: the existing
`integration` marker's contract is "docker compose alone", and this tier needs
kind, `tofu`, a 1.45 GB node image and an external Docker network. Folding it
into `integration` would (a) break that contract, (b) make every integration run
pay ~90 s of cluster setup, and (c) couple a fast, always-run suite to a slow
one. A separate job also lets it be `continue-on-error` or nightly if it proves
flaky, without weakening the required checks.

- **`addopts` effect:** today `-m 'not integration'` already excludes unmarked-as-
  integration tests; adding `and not e2e` keeps `pytest` (bare) and the unit job
  unchanged. **Unit tests still run on macOS untouched** — nothing in this tier
  is imported by `tests/unit/`.
- **Coverage combine:** the `coverage` job currently downloads exactly two
  artifacts and runs `coverage combine` + `--fail-under=80`. Simplest correct
  choice: **the `e2e` job does not produce coverage data** and is not added to
  the combine step. It exercises tofu and a cluster, not new tunstrap lines
  beyond what the integration suite already covers, and adding a third artifact
  would make the gate depend on the slowest, most environment-sensitive job.
- **Existing rig untouched:** new directory `tests/e2e/`, new compose file. The
  `integration` marker stays runnable with docker compose alone.

#### Cost

Measured on this workstation (10 cores, 31 GB, Docker 28.1.1):

| Step | Time |
|---|---|
| `kindest/node` image pull (1.45 GB, cold) | ~28 s |
| `kind create cluster --wait` (image cached) | 36 s |
| `kind create cluster` including the pull | 64 s |
| `sshd-kube` container ready | ~14 s |
| `tunstrap start` (SSH + SAN probe + materialize) | ~2–3 s |
| `tofu init`, first time (downloads `kubernetes` + `helm`) | 8.8 s |
| `tofu init`, per test with `TF_PLUGIN_CACHE_DIR` warm | ~1–2 s |
| `tofu apply` (2 resources) | 1.3 s |
| `tofu destroy` | 7.6 s |
| `kind delete cluster` | ~5 s |

Per-test module isolation (see "Session fixture lifecycle") multiplies only the
warm `init`, not the download, so a handful of tests adds seconds rather than
minutes.

**Realistic total: ~2–2.5 minutes of work**, plus GitHub Actions job overhead
(checkout, Python, `pip install -e ".[dev]"`, installing kind and tofu) — call it
**4–5 minutes wall clock** for the job. That is cheap enough to run per-PR and is
why this is worth automating.

**`tofu init` needs network** to fetch providers from
`registry.opentofu.org`; it cannot be avoided. Options: accept the 8.8 s
(recommended — it is small next to cluster setup), or cache
`~/.terraform.d/plugin-cache` keyed on `versions.tf` via `actions/cache` and set
`TF_PLUGIN_CACHE_DIR`. Note this is the *runner's* network, unrelated to the
tunnel, and unrelated to the shim's `init`-skip decision.

#### Why the `sshd_config.d` mount matters — and why the existing rig is fine

**The existing integration suite is green.** Measured 2026-07-31:
`pytest tests/integration -m integration -q` → **`29 passed in 78.53s`**. Nothing
in this subsection reports a defect in the current rig; it explains a fixture
constraint the new `e2e` tier must respect.

Two independent facts about the rig, both measured, which are easy to conflate:

1. **Forwarding is administratively disabled on `sshd-a/b/c`.** They have no
   `sshd_config.d` mount, and in `lscr.io/linuxserver/openssh-server:latest` as
   pulled 2026-07-31 the shipped config sets `AllowTcpForwarding no` (line 92).
   The image emits `Include /config/sshd/sshd_config.d/*.conf` **only when that
   directory exists**, so only `sshd-bastion` gets the override. Effective
   configuration, read from the running containers with
   `sshd.pam -T -f /config/sshd/sshd_config -h …`:

   ```
   sshd-a        allowtcpforwarding no
   sshd-bastion  allowtcpforwarding yes
   ```

   and a forward through `sshd-a` to `127.0.0.1:2222` — a target inside the
   container itself, needing no cross-network path — is refused by the server:
   `channel 2: open failed: administratively prohibited: open failed`.

2. **`sshd-a/b/c` are not on the `internal` network.** They declare no
   `networks:` block (`docker-compose.yml:2-16`), so they sit on
   `integration_default` only, while `target-1`/`target-2` are aliased solely on
   `internal`. `sshd-bastion` is the only service on both. So `sshd-a` has no
   route to `target-1` **independently** of fact 1 — being the only cross-network
   host is precisely the bastion's purpose, and what the cross-host forwarding
   tests exercise.

An earlier draft of this spec attributed a single probe (`sshd-a` → `target-1`
returning empty) wholly to fact 1 and extrapolated that the suite must be red.
That extrapolation was wrong: **both** facts applied to that probe, and the suite
is green because **no test moves data through an `sshd-a` forward**. The `sshd-a`
tests are SFTP-only (`test_fetch_files.py`, `test_fetch_security.py`) or assert
port *allocation* without traffic (`test_multiport.py:20-47` checks only that two
forwards get distinct local ports; `test_start.py:30-54` checks `connections`
keys and `pid`). Every test that actually moves bytes through a forward uses the
bastion — including `test_kube_targets.py:70` and `:121`, which pass
`ssh_test_cluster["bastion_port"]`.

**Consequence for the `e2e` tier — this is the part that matters.** The new
`sshd-kube` service *does* move data through a forward, so it **must** mount
`_bastion_sshd_config` at `/config/sshd/sshd_config.d`, exactly as the compose
snippet above shows. Omitting it produces a tunnel that opens cleanly and then
refuses every connection.

Two notes, neither a defect and both **out of scope**:

- The image tag is unpinned (`:latest`). That is genuine supply-chain fragility
  — an upstream change to the shipped `sshd_config` would alter fixture
  behaviour with no repo change — and pinning a digest would be cheap insurance.
- `tunstrap start` returns success when a forward target is later unroutable or
  administratively refused. This is **not a bug**: SSH `direct-tcpip` failures
  surface per *connection*, not at listener-setup time, so a local listener can
  only ever be optimistic. A startup reachability probe (open and immediately
  close one channel per target, downgrading a failure to a `TunnelWarning`) is a
  plausible **enhancement** — it would have turned the fixture hazard above into
  an immediate diagnostic — but it changes `start`'s contract and cost, and
  belongs in its own proposal rather than here.

### Verification gates

From `.github/workflows/test.yml`: `black --check .`, `ruff format --check .`,
`ruff check .`, `pylint tunstrap/` (`fail-under = 9.0`, `pyproject.toml`),
`vulture tunstrap/ vulture_whitelist.py`, `mypy --strict tunstrap`,
`pytest tests/unit` on {ubuntu, macos} × {3.10–3.13}, `pytest tests/integration
-m integration`, and combined `coverage report --fail-under=80`.

Added by this spec: a **separate `e2e` job** (ubuntu only) running
`pytest tests/e2e -m e2e`, which installs kind and `tofu` and creates a real
cluster. It is **not** part of the coverage combine (see "Marker, CI job, and
blast radius"), and it does not alter the unit or integration jobs.

**Known pre-existing breakage — record, do not fix here.** `pyproject.toml:23`
pins only `ruff>=0.8`, so a fresh `pip install -e ".[dev]"` resolves ruff
**0.16.1**, against which [measured 2026-07-31] `ruff check .` reports **59
errors** in `.py` sources and tests (19 `I001`, 8 `PLW1510`, 7 `UP037`, 6
`RUF100`, 4 `UP035`, …) and `ruff format --check .` wants **6 files** — *all six
Markdown* (`docs/specs/2026-05-16|20|21`, `docs/superpowers/plans/2026-05-30`,
`2026-06-24`, `2026-06-25`), because ruff 0.16 formats Python code fences inside
`.md`. `black --check .` is clean (66 files); ruff **0.15.18** is clean on both
gates. So black and `ruff format` have **not** diverged on Python — zero `.py`
files differ; the gate breaks on new 0.16 lint rules plus Markdown formatting.
Fix = a version pin plus an exclude or a docs reformat; a separate change.

## HCL consumer impact

**Before** (`consumer-repo/garuda/`): `terragrunt.hcl:11-14` (`mktemp -u` marker),
`:55-83` (28-line `tunnel_up_script` with `jq`, `umask 077`, `unset KUBECONFIG`,
mock short-circuit, `uvx ... tunstrap start`), `:221-237` (`after_hook` with a
second inline bash + `jq` + `tunstrap stop`), `:250-306` (`run_cmd` passing the
whole `InputSchema` as argv), `locals.tf:72`
(`jsondecode(var.tunnel_path == "" ? ... : file(var.tunnel_path))`), and
`variables.tf:273` (`variable "tunnel_path"`).

**After.** `root.hcl:5` already reads `terraform_binary = "tofu"`, so this is a
one-line edit, not a new attribute: `terraform_binary =
"${get_repo_root()}/bin/tofu-tunstrap"`. Unit `terragrunt.hcl`:

```hcl
terraform {
  source = "."

  extra_arguments "tunstrap" {
    commands  = ["plan", "apply", "destroy", "refresh", "import"]
    arguments = []

    # dependency.* resolves here [measured 2026-07-31]; it does NOT
    # resolve in `locals`, so the conditional must be inline.
    env_vars = dependency.infra.outputs.connection_data_hub.host != "0.0.0.0" ? {
      TUNSTRAP_INPUT = jsonencode({
        nodes = merge(
          {
            hub = {
              host           = dependency.infra.outputs.connection_data_hub.host
              port           = 22
              user           = dependency.infra.outputs.connection_data_hub.user
              ssh_pkey       = dependency.infra.outputs.connection_data_hub.ssh_private_key
              remote_targets = { k3s = "127.0.0.1:6443" }
              kube_targets   = { k3s = { kubeconfig_path = "/etc/rancher/k3s/k3s.yaml" } }
              required       = true
            }
          },
          {
            for k, cd in dependency.infra.outputs.connection_data_edges :
            k => {
              host           = cd.host
              port           = 22
              user           = cd.user
              ssh_pkey       = cd.ssh_private_key
              remote_targets = { k3s = "127.0.0.1:6443" }
              kube_targets   = { k3s = { kubeconfig_path = "/etc/rancher/k3s/k3s.yaml" } }
              required       = true
            }
          },
        )
        daemon = {
          shutdown_grace_seconds = 10
          materialize            = true
          # auto_stop_idle_seconds is intentionally absent: the daemon's
          # lifetime is now exactly the tofu child's.
        }
      })
    } : {}
  }
}
```

That `nodes`/`daemon` body is the existing payload from
`terragrunt.hcl:257-304`, moved verbatim except for the dropped
`auto_stop_idle_seconds`.

`locals.tf:72` becomes:

```hcl
tunnel = try(jsondecode(var.tunstrap), { connections = {} })
```

with `variable "tunstrap" { type = string, default = "" }` replacing
`tunnel_path` (`variables.tf:273`). **The `try()` is required, not stylistic**:
the default is `""`, and bare `jsondecode("")` fails, so plain
`jsondecode(var.tunstrap)` would break every command that gets no tunnel — the
exact case the next paragraph requires the module to tolerate. The two read sites
are unchanged in shape: `locals.tf:76` and `locals.tf:79` still take
`connections[<node>].kube_targets.k3s.path` — the comment at `locals.tf:65`
("Consumer reads only the path string") stays true.

### Which tofu commands need a tunnel

The `commands` list is a deliberate enumeration, not a copy of the old
`after_hook` list (`terragrunt.hcl:222`). The old list existed because the tunnel
was started during `inputs` evaluation and therefore had to be torn down for
*any* command that evaluated `inputs`; the new list answers a different question
— which commands actually make provider API calls.

| Command | Tunnel | Why |
|---|---|---|
| `plan`, `apply`, `destroy`, `refresh` | **yes** | `kubernetes`/`helm` providers read and write live cluster state |
| `import` | **yes** | reads the live resource to populate state; omitting it is a silent trap |
| `console` | **yes**, if used | can evaluate provider data sources; add it if the consumer uses it interactively |
| `init` | no | backend config + provider downloads only; the state backend is a public S3-compatible endpoint (`root.hcl:19-44`) reached directly rather than through the tunnel, and providers come from the public registry (`garuda/versions.tf:9-33`) |
| `validate` | no | schema/expression checks only; no provider calls. It *was* in the old `after_hook` list solely because it evaluated `inputs` |
| `output`, `show`, `state *`, `taint`, `untaint`, `fmt`, `providers` | no | read or rewrite state and files; no cluster contact |

Everything not listed in `commands` gets `TUNSTRAP_INPUT` unset and takes the
shim's pass-through branch, so the failure mode of forgetting a command is a
**provider error against the inert loopback endpoint**, not a silent wrong
result. `import` is called out explicitly because it is the one state-mutating
command that is easy to leave off the list.

**Deleted from the consumer:** the `run_cmd` block (`:250-306`), the `mktemp`
marker local (`:11-14`), the `tunnel_up_script` heredoc (`:55-83`), the
`after_hook` (`:221-237`), both `jq` invocations, the `umask 077`, and the
`InputSchema` in argv. The `auto_stop_idle_seconds = 7200` workaround (`:299`)
can shrink or go away — the daemon's lifetime is now the child's.
**`session_dir` has no Terraform consumer**: it appears in `.tf` only inside a
comment (`locals.tf:64`), and its sole functional use is the down-hook shell at
`terragrunt.hcl:229`, which this design deletes.

**`terragrunt output` gets no tunnel — deliberately.** `output` is not in
`commands`, so `TUNSTRAP_INPUT` is unset for it and its auto-`init` [measured
2026-07-31, fact 4]; it does not need one, since `tofu output` reads state.
**Consequence: the module must tolerate an unset `tunstrap` variable** — and it
already does. `locals.tf:69-72` has the empty-string branch, and
`terragrunt.hcl:35-47` defines the inert provider body substituted into the
generated `providers.tf` at `:129`, `:137`, `:144`, `:151`, pinning
`host = "https://127.0.0.1:0"` with empty cert material so the providers cannot
fall back to `$KUBECONFIG`. That branch now serves three cases instead of two:
`tofu test`, mock state, and non-tunneled commands.

**Security, stated honestly.** Private keys still travel in the child's
**environment** — but they already do today, as `TF_VAR_connection_data_hub`
[measured 2026-07-31, fact 2]. What this design removes is the *command line*:
`ProcessExecutionError` can no longer print a PEM. The stronger follow-on is the
already-shipped ssh-agent fallback (`schemas.py:272-283`,
`docs/specs/2026-06-25-ssh-agent-fallback-design.md`): if the consumer exports
`SSH_AUTH_SOCK` and drops `ssh_pkey` from the payload, key material leaves the
payload entirely. Recommend it in the recipe; it is not required by this design.

## Documentation deliverables

- **`docs/recipe_terragrunt.md`** (new) — the canonical recipe: the shim
  verbatim, the before/after HCL, and the ssh-agent recommendation. It must carry
  the measured facts a future agent would otherwise re-derive — path-only
  `terraform_binary`; env-var scoping including the auto-`init` behaviour; the
  invocation counts; `dependency.*` in `extra_arguments.env_vars` but not in
  `locals`; `TF_VAR_*` JSON inputs; payload fidelity at ~10 KB; and the hard rule
  that **the shim must never write to stdout**.
- **`README.md`** — document both flags in the `run` section (`:175-195`),
  extend its exit-code paragraph (`:191-195`), link the recipe from "Project
  documents" (`:518-522`), and fix three stale `token` references: `:340` (a
  `"token": "<opaque>"` line in the Output reference example), `:385` (a
  Security-notes bullet calling `token` the authorization handle), `:429` (a
  Troubleshooting row on "token mismatch"). `token` was removed from
  `OutputSchema` by the 2026-06-24 design and is absent from `schemas.py:353-362`.
  Lines `:474-485` mention `token` legitimately in a migration note — leave those.

## Decision log

Carried forward from the superseded spec (still binding):

1. **Deterministic `--session-dir`** — rejected; `session_dir` has no Terraform
   consumer (reconfirmed: `locals.tf:64` is a comment).
2. **Hash-keyed session reuse** — rejected; latency-only.
3. **Idempotent `up` verb / replace-active semantics** — rejected; breaks the
   race-free `SessionActive` invariant from issue #7.
4. **"Wait for zero active connections before replacing"** — rejected; idle ≠
   unused (the consumer regressed on this at `auto_stop_idle_seconds = 300`).
5. **Named sessions per terragrunt command** — rejected; multiplies daemons.
6. **`--owner-ancestor N` / negative pid as ancestor depth** — rejected; fragile
   to launcher depth, collides with `kill(2)` semantics.
 7. **A `--config` file with Terragrunt-output adapters inside tunstrap** —
    rejected; layering violation. **Reaffirmed and strengthened here**: it is the
    root principle behind putting the shim outside the package.
    **Revised (post-land):** the *direction* of this principle is reversed for
    the proxy specifically — see "Shipping the shim (revised)" and new item 31.
    The `--config` adapters themselves remain rejected; what changed is that the
    *consumer shim's logic* (pass-through, `init`/`-version` bypass, the
    `TF_VAR_tunstrap` + `tofu` invocation) now also ships in-package as
    `tunstrap/tofu_proxy.py`, not that tunstrap grew generic Terragrunt
    adapters. `cli.py` stays Terraform-vocabulary-free; only `tofu_proxy.py`
    carries it.
8. **Matching the joined `/proc/<pid>/cmdline`** — rejected; the owner pattern
   appears in its own cmdline [measured 2026-07-30]. Moot under (15).
9. **`re.fullmatch` / anchoring over `argv[0]`** — rejected. Moot under (15).
10. **Owner as a synthetic `TunnelWarning`** — rejected; `TunnelWarning` means
    "non-fatal failure on an optional node". Moot under (15) — `OutputSchema`
    gains no fields at all here.
11. **`--owner` on `run`** — rejected; `run` already guarantees teardown in its
    `finally` (`cli.py:327-330`). **This is the observation the new architecture
    generalizes.**
12. **Split exit codes for owner failures (64 vs a new 5)** — moot under (15);
    `5` returns to the unallocated pool (`exceptions.py:57-63`).
13. **`SecretStr` on the three secret fields** — deferred; `spawn_daemon`
    serializes with `model_dump_json()`, which `SecretStr` would break.
14. **Terragrunt-based integration tests in this repo** — out of scope; needs
    tofu + terragrunt in the test image and a state backend.

New:

15. **The entire `--owner` process-ownership feature** (watchdog, `owner_gone`
    IPC kind, exit 5, pid-reuse start-time guard) — **obsolete, cancelled before
    implementation.** Under the proxy model tunstrap *is* `tofu`'s parent and its
    lifetime is exactly the child's: orphans become impossible by construction
    rather than detected after the fact. Nothing is removed — none of it was ever
    built (`grep -rn "owner" tunstrap/*.py` → only `kube.py:235,245`).
16. **`--output-file`** — obsolete. The result goes into the child's environment
    via `--output-var`; under the shim, stdout belongs to tofu and is never a
    result channel [measured 2026-07-31, fact 8]. The 0600-file design solved a
    problem that no longer exists.
17. **`--placeholder-host`** — obsolete. `env_vars` is an ordinary HCL map, so
    the consumer omits `TUNSTRAP_INPUT` when infra is not applied; the shim's
    first line passes through and the module's inert branch (`locals.tf:69-72`)
    fires. Proven end-to-end both ways [measured 2026-07-31]. Zero tunstrap code,
    versus a new flag plus an `OutputSchema.placeholder` field.
18. **`terraform_command_line`** — does not exist. The attribute is
    `terraform_binary` / `--tf-path` / `TG_TF_PATH`, and it is path-only
    [measured 2026-07-31, fact 1].
19. **Generalizing `render_env` to multi-node** — rejected. `TUNSTRAP_<TARGET>_*`
    has no node dimension and same-named targets across nodes collide
    irreducibly; `2026-06-25-cli-run-modes-design.md:245` already ruled
    multi-node CLI input out of scope. `--output-var` serves the case with a
    node-keyed structure, so the documented single-node contract stays intact
    and the code gains one fewer branch.
20. **Putting the `init` skip or the pass-through inside tunstrap** — rejected.
    Both are Terraform knowledge (`init` is a tofu subcommand; "no input means
    no tunnel" is a Terragrunt mock-state convention). They belong in a
    small consumer shim. Same principle as (7).
    **Revised (post-land):** reversed by item 31. The `init`/`-version` bypass
    and the `TF_VAR_tunstrap` pass-through now also live in
    `tunstrap/tofu_proxy.py` (the shipped `tunstrap_tofu` entry point). The
    consumer-file shim remains a supported option; the principle is no longer
    absolute, by deliberate owner decision.
21. **Reading connection data from Terragrunt-generated tfvars files** —
    rejected. Such files do not exist: inputs travel as `TF_VAR_*` JSON env vars
    [measured 2026-07-31, fact 2]. Even if they did, parsing them would couple
    tunstrap to Terragrunt internals.

Added in review (iteration 2):

22. **Making `run`'s CONNECTION merely `required=False`** — rejected; it does not
    work. Click distributes post-`--` tokens over declared positionals in order,
    so `run --input-env X -- tofu plan` binds `connection='tofu'`
    [measured 2026-07-31, Click 8.4.2]. Alternatives weighed: a
    `--connection` **option** (rejected — breaks the documented positional form
    in `README.md:175-195` and its integration tests) and a **separate
    subcommand** (rejected — duplicates the entire `run` surface to express one
    input-source difference). Chosen: **one `args` variadic**, split after
    parsing on the presence of `--input-env`. Flag mode is untouched, and the
    old "CONNECTION + `--input-env`" conflict becomes structurally impossible
    instead of an error to enforce.
23. **Leaving `_kill_with_identity`'s stdout writes in `run`'s teardown** —
    rejected; it is a correctness bug under the shim, not a cosmetic one.
    `run` teardown emitted `{"stopped": ...}` on **every** outcome
    (`cli.py:376-437`) into the stream Terragrunt parses as tofu's stdout.
    Rejected fixes: redirecting fd 1 during teardown (fragile, hides genuine
    child output ordering) and making `stop` silent too (breaks `stop`'s
    documented JSON contract). Chosen: a silent `stop_session` primitive, with
    `stop_command` rendering today's JSON byte-for-byte and `run` printing
    nothing on success and stderr on failure.
24. **Daemon flags (`--auto-stop-idle-seconds`, `--materialize`, `--log-file`)
    under `--input-env`** — rejected as overrides and as silent no-ops; they are
    **usage errors (64)**. The payload's `daemon` block is complete and
    authoritative, so there must be exactly one place to look when a tunnel
    misbehaves. These flags are excluded from `_conn_flags_present`
    (`cli.py:84-93`), so without an explicit rule an implementer would have
    invented a precedence order silently.
25. **`run` forces `daemon.materialize = True` on an `--input-env` payload** —
    accepted as an invariant of the verb, matching today's unconditional
    `force_materialize=True` (`cli.py:290`). A payload saying
    `materialize: false` would make `render_env` raise (`envrender.py:42-43`)
    and would hand `--output-var` consumers `path: null`, giving the
    `kubernetes`/`helm` providers an empty `config_path`. This is the only place
    `run` mutates the supplied schema, and it is documented as such.
26. **Copying the old `after_hook` command list into `extra_arguments.commands`**
    — rejected. That list (`terragrunt.hcl:222`) answered "which commands
    evaluate `inputs` and therefore need teardown", which is not the new question
    ("which commands make provider API calls"). `init` and `validate` drop off;
    **`import` is added**, since it reads live resources and is the easiest
    state-mutating command to forget. Unlisted commands fail loudly against the
    inert loopback endpoint rather than silently returning wrong results.

Added for the `e2e` tier:

27. **kind, over k3d / a raw k3s container / a real remote cluster** — chosen for
    three concrete reasons, not familiarity. (a) It reproduces the *production
    shape* of the thing under test: its in-node kubeconfig lives at a real path
    (`/etc/kubernetes/admin.conf`) with an embedded CA and a `server:` naming a
    host reachable only from inside the cluster network — structurally identical
    to the consumer's `/etc/rancher/k3s/k3s.yaml`. (b) Its apiserver cert carries
    the control-plane hostname as a DNS SAN, so `choose_tls_server_name`
    (`kube.py:177-196`) takes the exact-match branch and the tier tests the
    warning-free path [measured 2026-07-31]. (c) It is a single static binary
    with no daemon. k3d was the closest alternative and would very likely work
    identically, but it adds a k3s-vs-kubeadm difference for no gain; a raw k3s
    container needs privileged mode and hand-rolled readiness; a real remote
    cluster is not hermetic. **This choice is cheap to revisit** — the tier
    depends on kind only through cluster creation and one `docker exec cat`.
28. **A local chart directory, not a remote Helm repository** — chosen. A
    `helm_release` needs a chart, and a two-file local chart (`Chart.yaml` +
    one ConfigMap template) removes a network dependency, a version-drift source
    and an availability risk from a test whose subject is the *tunnel*, not Helm.
    Measured: it produced a real in-cluster release Secret
    (`sh.helm.release.v1.probe.v1`), so nothing about the Helm path is stubbed.
29. **Own `e2e` marker and CI job, rather than extending `integration`** —
    chosen. `integration`'s contract is "docker compose alone"; this tier needs
    kind, tofu, a 1.45 GB image and an external Docker network. Extending
    `integration` would break that contract and add ~90 s of cluster setup to
    every integration run. Rejected alternative: one marker with a skip-if-no-kind
    guard — it silently degrades to a no-op in exactly the environment where the
    tier matters. `addopts` becomes `-m 'not integration and not e2e'`; the unit
    job and macOS are unaffected.
30. **Keeping the `e2e` job out of the coverage combine** — chosen. The combine
    step gates at `--fail-under=80` on merged data from exactly two artifacts;
    adding a third from the slowest and most environment-sensitive job would make
    the coverage gate hostage to cluster flakiness, while adding almost no
    tunstrap lines that the integration suite does not already cover. The tier's
    value is behavioural proof, not line coverage.

Added post-land (the `tunstrap_tofu` entry point):

31. **Shipping the proxy in-package as `tunstrap_tofu`, alongside the consumer
    shim** — chosen, reversing items (7) and (20) for the proxy specifically.
    `uv tool install` yields a stable `tunstrap_tofu` entry point (item (c) of
    "Shipping the shim" answered), so `terraform_binary` points at a stable
    installed path with nothing copied into the consumer's repo. The fast-path
    cost (item (a)) is managed by `execvp`-ing `tofu` before any `cli` import —
    measured ~59 ms end-to-end vs ~225 ms naive (see the recipe). The
    Terraform-vocabulary objection (item (b)) is the trade being deliberately
    taken: `init`/`-version`/`TF_VAR_tunstrap`/`tofu` live in
    `tunstrap/tofu_proxy.py`, while `cli.py` stays generic (it gains only a
    `suppress_kubeconfig` parameter and a `run_via_env_input` entry, no
    Terraform names). The consumer-file shim remains supported and is still
    what the e2e tier drives. The `env -u KUBECONFIG` incantation becomes
    `suppress_kubeconfig=True` inside the built child env — same property (a
    broken `config_path` chain cannot fall back to KUBECONFIG), no child-side
    wrapper; the property is pinned by a unit test proven red against the wrong
    implementation. The `-chdir=DIR init` gap closes as a side-effect of
    structural argv parsing.

## Out of scope

- **The pydantic `ValidationError` secret leak.** `cli.py:205-211` and
  `cli_input.py:113-123` both put `json.loads(exc.json())` into `details`, and
  pydantic v2 error entries include the offending `input` — so a malformed node
  echoes `ssh_pkey`. `TunstrapError._scrub` (`exceptions.py:7-12`) drops only
  **top-level** keys and never reaches nested `errors[].input`. This design adds
  a **third** such site (`--input-env`), raising the priority but not changing
  the fix, which needs its own plan.
- **Packaging / PyPI publication**, blocked by the direct-reference `asyncssh`
  fork (`pyproject.toml:10`). The shim decision depends on the outcome and should
  be revisited when the OCI release contract lands.
- **Multi-node scalar env rendering** — see decision (19).
- **Terragrunt-level integration tests in this repo** — decision (14).
  The new `e2e` tier is **tofu-level, not Terragrunt-level**, and the two must
  not be confused. The `e2e` tier drives `tofu` directly with `TF_VAR_tunstrap`
  set by a shim, proving the genuinely novel path: *providers reach a real
  cluster through a tunstrap tunnel*. Terragrunt-level testing would additionally
  need terragrunt in the image, a state backend, `dependency` units and mock
  outputs, in order to re-prove only Terragrunt's own behaviour — which is
  already captured as measured facts 1–8 and belongs in
  `docs/recipe_terragrunt.md`, not in a test. **Still out of scope:** the
  `extra_arguments.env_vars` wiring, the `dependency.*` resolution order, and the
  `terraform_binary` hook itself.
- **The ruff 0.16 gate breakage** — recorded under "Verification gates";
  pre-existing, needs a version pin and a docs decision, not this spec.
- **Migrating the consumer repo.** This spec specifies the tunstrap side and the
  recipe; the `consumer-repo/garuda` edit is a consumer-repo change.
- **Symmetric projection for fetched files.** `--output-var` projects
  `kube_targets` (drops the kube credentials) but passes
  `fetch_files[*].content_b64` through verbatim. The asymmetry is documented
  and deliberate for now (see `docs/recipe_terragrunt.md`): fetched files are
  operator-requested and `FetchedFile` has no on-disk `path`, so dropping
  `content_b64` would lose data. The end-state is symmetry with kube targets —
  materialize fetched files under the session dir at mode `0o600`, add a `path`
  field to `FetchedFile`, force that materialization in `run` (as is already
  done for kubeconfigs), and *then* drop `content_b64` from the projection.
  That is a schema addition plus a new forced-materialization rule,
  deliberately not done in a security fix at the tail of a 64-commit branch.
