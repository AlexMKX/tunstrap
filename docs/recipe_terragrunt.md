# Recipe: Terragrunt / OpenTofu through a tunstrap tunnel

This recipe shows how to drive Terragrunt and OpenTofu (`tofu`) through a
tunstrap tunnel using the **CLI-proxy model**: a small shell shim installed as
Terragrunt's `terraform_binary`, which brings the tunnel up and `exec`s `tofu`
with the connection details in the environment.

It is written for someone adopting this in their own repo. It carries the
measured facts a future reader would otherwise have to re-derive, the failure
modes you will hit, and an honest statement of what is and is not proven.

> **Companion design.** The full design — why this shape over the alternatives
> (`--owner` watchdog, `--output-file`, `--placeholder-host`) — lives in
> [`docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md`](specs/2026-07-31-run-env-io-and-tofu-proxy-design.md).
> The e2e tier that proves the central claim lives in `tests/e2e/`.

## The model in one paragraph

Terragrunt lets you point `terraform_binary` at any executable. The shim is that
executable. For commands that touch a live cluster (`plan`, `apply`, …) the
shim `exec`s into `tunstrap run`, which **becomes `tofu`'s parent**: it opens
the tunnel, injects the connection details into `tofu`'s environment, waits for
`tofu`, and tears the tunnel down in a `finally` — so the tunnel's lifetime is
exactly the child's. For commands that do not need a tunnel (`init`,
`-version`), and whenever no tunnel is wanted, the shim `exec`s `tofu` directly.
tunstrap is never a daemon you start and stop by hand in this model; it owns the
child, so orphans become impossible by construction.

## Prerequisites

- `tunstrap` on `PATH` (the shim `exec`s `tunstrap run` — see Installation below
  for a one-line install).
- `tofu` on `PATH` (the shim `exec`s it for the pass-through branches and as the
  tunnelled child).
- The shim itself, committed at `bin/tofu-tunstrap` relative to your repo root.
  Terragrunt's `terraform_binary` resolves a path and probes it every run, so it
  wants a stable, reviewable home (see Installation).
- An OpenTofu/Terraform module that reads the connection details from a
  `TF_VAR_*` variable. The shape is given below and is load-bearing.

## Installation

Two pieces, installed once: `tunstrap` on `PATH`, and the shim at a stable path.

### tunstrap

tunstrap is **not on PyPI** — a direct-reference dependency (the asyncssh fork
the package is built on) blocks publishing — so install it from the git source
or a local checkout:

```sh
uv tool install "git+https://github.com/AlexMKX/tunstrap.git"
# or from a local checkout:
uv tool install /path/to/tunstrap
```

Use `uv tool install`, not `uvx`. `uv tool install` yields a **stable** entry
point at `~/.local/bin/tunstrap`
(→ `~/.local/share/uv/tools/tunstrap/bin/tunstrap`), identical across reinstalls;
`uvx` runs from an ephemeral `~/.cache/uv/archive-v0/…` path that changes per
resolution. (This is the correction to reason 3 in "Why not a console script"
below: that stable-path caveat held for `uvx`, not for `uv tool install`.)

### The shim

Copy the snippet in the next section into `bin/tofu-tunstrap`, `chmod 0755` it,
and commit it. Committing it — rather than generating it at runtime — keeps it
reviewable and pinned alongside the config, with no runtime moving parts.

### Localizing install in `terragrunt.hcl` (`run_cmd`, optional)

If you would rather the bootstrap lived entirely in `terragrunt.hcl`, the
`terraform_binary` attribute also accepts `run_cmd(...)`. The command runs
**before** the `-version` probe, is cached once per `plan`, and runs once **per
unit** under `run --all`:

```hcl
# runs before the -version probe; its stdout becomes terraform_binary
terraform_binary = run_cmd("${get_repo_root()}/bin/materialize-shim.sh")
```

where `materialize-shim.sh` idempotently writes the shim and `echo`s its path.
The cost is one shell-script exec per unit per run (there is no cross-unit
cache), and the script is a runtime moving part that can drift from the reviewed
shim — which is why the committed file remains the default.

A `before_hook` **cannot** install the shim instead. Terragrunt probes
`<terraform_binary> -version` roughly 50 ms *before* any hook runs, and with the
binary missing it fails outright before the hook executes (measured; the hook's
marker file never appears). Hook-based install is a dead end.

## The shim

This is the shim verbatim — copy it into your repo (e.g. `bin/tofu-tunstrap`),
`chmod 0755` it, and commit it. It is identical to the one the e2e tier drives
(`tests/e2e/shim/tofu-tunstrap`).

```sh tofu-shim
#!/bin/sh
# Consumer-facing OpenTofu shim. Terraform-specific decisions live here, not in
# tunstrap; see docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md,
# "The tofu shim - consumer-facing, Terraform-specific, outside tunstrap".
#
# Never write to stdout. Terragrunt captures and labels tofu's stdout by
# default, and `terragrunt output -json` consumers parse it. Diagnostics go to
# stderr or a file.
#
# `exec` is correct in both pass-through branches: there is nothing to clean up.
# `exec`ing *into* `tunstrap run` is also correct - run owns the child via Popen
# + signal forwarding + a finally teardown, so nothing execs past teardown, and
# Terragrunt's signals reach tunstrap directly.

# Pass through when the payload is absent: the consumer omits the env_vars key
# entirely when the infrastructure is not applied yet.
[ -n "$TUNSTRAP_INPUT" ] || exec tofu "$@"

# Skip the tunnel for init and -version. `tofu init` configures the backend and
# downloads providers; it contacts neither the Kubernetes API nor Helm, and the
# consumer's state backend is S3-compatible over the public internet, not
# tunnelled. Without this, env-var scoping yields two tunnels per
# `terragrunt plan` - one for the auto-init, one for the plan.
case "$1" in init|-version) exec tofu "$@" ;; esac

# `env -u KUBECONFIG` is not optional. `run` builds
# child_env = {**os.environ, **render_env(out)} and render_env sets KUBECONFIG
# last, so it wins over anything inherited - the clearing has to happen inside
# the child command. Left in place it is a silent fallback pointing at the same
# materialized file config_path would use, so a broken TF_VAR_tunstrap chain
# would still find a working cluster and every positive assertion in the e2e
# tier would prove nothing.
exec tunstrap run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap \
  -- env -u KUBECONFIG tofu "$@"
```

### What each branch does

| Condition | Branch | What happens |
|---|---|---|
| `TUNSTRAP_INPUT` unset | line 1 | `exec tofu "$@"` — no tunnel, no tunstrap. This is the "infra not applied yet" path: Terragrunt simply omits the `env_var`, so the shim is a transparent `tofu` wrapper. |
| `$1` is `init` or `-version` | line 2 | `exec tofu "$@"` — same. `init` only configures the backend and downloads providers; it never reaches the cluster API. Skipping it avoids a redundant tunnel. |
| otherwise | line 3 | `exec tunstrap run --input-env … --output-var … -- env -u KUBECONFIG tofu "$@"` — tunstrap opens the tunnel, injects `TF_VAR_tunstrap` (the full connection envelope) plus the scalar env, runs `tofu`, and tears down. |

`exec` is correct everywhere here: in the pass-through branches there is nothing
to clean up, and `exec`ing *into* `tunstrap run` is desirable — one fewer
process level, and Terragrunt's signals reach tunstrap directly. tunstrap owns
the child via `Popen` + signal forwarding + a `finally` teardown, so nothing
`exec`s *past* teardown.

### `env -u KUBECONFIG` is load-bearing — do not drop it

This is the single most important line in the shim, and the temptation to
"clean it up" must be resisted. Here is why, in full:

For a single-node payload, `tunstrap run` injects `KUBECONFIG` into the child
environment (pointing at the same materialized kubeconfig file that
`config_path` would use). It does this by building
`child_env = {**os.environ, **render_env(out)}`, and `render_env` places
`KUBECONFIG` **last** — so it overrides anything inherited. Clearing it has to
happen *inside the child command*, with `env -u KUBECONFIG`.

Left in place, `KUBECONFIG` is a **silent fallback**: if the
`TF_VAR_tunstrap` → `try(jsondecode(...))` → `config_path` wiring were broken or
removed, the `kubernetes` and `helm` providers would still find a working
cluster via `KUBECONFIG` and everything would *appear* fine.

This is not hypothetical. The e2e tier proved it behaviourally: with
`env -u KUBECONFIG` removed, an `apply` with a deliberately broken
`config_path` chain **succeeded** — reaching the cluster through the injected
`KUBECONFIG` instead of through the decoded path. With `env -u KUBECONFIG` in
place, the same broken chain fails. Dropping the flag turns a hard failure into
a silent wrong result that looks identical to success.

## Wiring it into Terragrunt

`terraform_binary` takes a **path only** (see "Measured Terragrunt facts" below).
Set it once in your root `terragrunt.hcl`:

```hcl terragrunt-root
# root.hcl (or the top of each unit's terragrunt.hcl)
# terraform_binary is a TOP-LEVEL attribute, not a member of the terraform {}
# block: TG rejects it there with "An argument named terraform_binary is not
# expected here." See "Measured Terragrunt facts" below.
terraform_binary = "${get_repo_root()}/bin/tofu-tunstrap"
```

Then, in the unit that needs the tunnel, declare `TUNSTRAP_INPUT` as an
`extra_arguments` env var scoped to the commands that actually contact the
cluster:

```hcl terragrunt-unit
# unit terragrunt.hcl
terraform {
  source = "."

  extra_arguments "tunstrap" {
    # Commands that make provider API calls. init/validate/output are
    # intentionally absent: they read state/files or the registry, not the
    # cluster. import IS included - it reads a live resource and is the easiest
    # state-mutating command to forget.
    commands  = ["plan", "apply", "destroy", "refresh", "import"]
    arguments = []

    # dependency.* resolves here [measured]; it does NOT resolve in `locals`,
    # so the conditional must be inline in env_vars, not factored out.
    env_vars = local.cluster_host != "" ? {
      TUNSTRAP_INPUT = jsonencode({
        nodes = {
          node = {
            host           = local.cluster_host
            port           = 22
            user           = "root"
            ssh_pkey       = local.ssh_private_key
            remote_targets = { k3s = "127.0.0.1:6443" }
            kube_targets   = { k3s = { kubeconfig_path = "/etc/rancher/k3s/k3s.yaml" } }
            required       = true
          }
        }
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

The unit references `local.cluster_host` / `local.ssh_private_key`, which it does
not define, so a copy-paster gets `"local.cluster_host is not defined"` until they
add a `locals` block. `dependency.*` does NOT resolve in `locals` (measured
below), so build these from your source of truth and keep any `dependency.*`
reference inline in `env_vars` above. Expected shape (uncomment and fill):

```hcl terragrunt-locals
# locals {
#   cluster_host    = "k3s.example.internal"
#   ssh_private_key = "ssh-ed25519 AAAA…"
# }
```

When `local.cluster_host == ""` (infra not applied, or a mock-state run), the
`env_vars` map is empty, `TUNSTRAP_INPUT` is unset, and the shim takes its
first pass-through branch — so the same unit plans cleanly with no tunnel and
no mock-state workaround. This replaces the entire `--placeholder-host` idea:
`env_vars` is an ordinary HCL map, so "no tunnel" is just "key omitted".

### The `commands` list is an enumeration, not a copy of an old hook list

If you are migrating from a `run_cmd`/`after_hook` design, do **not** copy the
old hook's command list. That list answered "which commands evaluated `inputs`
and therefore needed teardown". This list answers a different question: "which
commands make provider API calls". Concretely:

| Command | Tunnel | Why |
|---|---|---|
| `plan`, `apply`, `destroy`, `refresh` | yes | providers read and write live cluster state |
| `import` | **yes** | reads the live resource to populate state; omitting it is a silent trap |
| `console` | yes, if you use it | can evaluate provider data sources; add it if interactive |
| `init`, `validate` | no | backend config / schema checks only; no cluster contact |
| `output`, `show`, `state *`, `taint`, `untaint`, `fmt`, `providers` | no | read/rewrite state and files |

Everything not listed gets `TUNSTRAP_INPUT` unset and takes the shim's
pass-through branch, so the failure mode of forgetting a command is a **provider
error against an inert loopback endpoint**, not a silent wrong result.

## The module side

The shim hands the module the connection envelope as a JSON string in
`TF_VAR_tunstrap`. The module decodes it and derives its provider `config_path`
from it. This is the exact chain the e2e tier exists to prove:

```hcl tf-module
variable "tunstrap" {
  type      = string
  default   = ""
  sensitive = true
}

locals {
  # try() is load-bearing: jsondecode("") is an error, so a bare jsondecode
  # would make `tofu plan` fail whenever the infrastructure is not applied yet
  # (the empty-string default).
  tunnel   = try(jsondecode(var.tunstrap), { connections = {} })
  kubepath = try(local.tunnel.connections.node.kube_targets.k3s.path, "")

  inert               = local.kubepath == ""
  kube_config_path    = local.inert ? null : local.kubepath
  kube_host           = local.inert ? "https://127.0.0.1:0" : null
  kube_ca_certificate = local.inert ? "" : null
  kube_client_cert    = local.inert ? "" : null
  kube_client_key     = local.inert ? "" : null
}

provider "kubernetes" {
  config_path            = local.kube_config_path
  host                   = local.kube_host
  cluster_ca_certificate = local.kube_ca_certificate
  client_certificate     = local.kube_client_cert
  client_key             = local.kube_client_key
}

provider "helm" {
  kubernetes {
    config_path            = local.kube_config_path
    host                   = local.kube_host
    cluster_ca_certificate = local.kube_ca_certificate
    client_certificate     = local.kube_client_cert
    client_key             = local.kube_client_key
  }
}
```

Three things to get right, each of which the tier proved can pass for the wrong
reason if dropped:

1. **`try()` around `jsondecode`.** `jsondecode("")` errors. Without `try`,
   every non-tunnelled command fails. The e2e tier has a dedicated inert-path
   test for this.
2. **The inert branch pins an unreachable host and empty cert material.** When
   `kubepath == ""`, the providers get `host = "https://127.0.0.1:0"` with empty
   cert/key fields so they cannot fall back to `$KUBECONFIG` or `~/.kube/config`.
   This is what makes a forgotten `commands` entry fail loudly instead of
   silently reaching a cluster some other way.
3. **`path` comes from the materialized file, not from a hand-written path.**
   `run` always forces `daemon.materialize = true`, so `connections.*.kube_targets.*.path`
   is a real on-disk kubeconfig (mode 0600), patched so `server:` and
   `tls-server-name` already point at the tunnelled port. The provider just reads it.
4. **`sensitive = true` on the variable.** Defence in depth, not the fix — it
   suppresses rendering in plan/apply output and diagnostics, but it does *not*
   keep the value out of the plan file. See the note below.

### What is, and is not, in `TF_VAR_tunstrap`

`run` **projects** the envelope before exporting it on this channel. Each
`kube_targets` entry keeps:

`cluster_name`, `context_name`, `local_port`, `endpoint`, `tls_server_name`,
`certificate_authority_data`, `path`

and **drops** `client_key_data` (a private key), `content_b64` (the whole
patched kubeconfig, which embeds that key) and `client_certificate_data` (not a
key, but it discloses the Kubernetes RBAC identity — CN is the username, O the
groups).

The reason is the consumer, not the transport: OpenTofu persists root-module
variable values in the **plan file**, which pipelines routinely archive, and
renders unmarked variables in diagnostics. `sensitive = true` fixes the
rendering half only — the plan file still contains the value — so the material
has to not be there in the first place.

Nothing is lost. `run` always materializes, so `path` points at an on-disk
kubeconfig (mode 0600) that contains every dropped field. A module that wants
inline provider configuration rather than `config_path` still has
`certificate_authority_data` (a published trust anchor, not a credential) plus
`endpoint` and `tls_server_name`, and supplies its own client credentials.

`tunstrap start` is **not** affected: it writes the complete envelope to
stdout, a pipe consumed directly by the caller who already supplied the input,
and without `--materialize` its `content_b64` is the only way to obtain the
kubeconfig at all.

### Fetched files are exported verbatim, not projected

The projection above touches only `kube_targets`. Every `fetch_files` entry
keeps its `content_b64` whole — the bytes the operator asked `--fetch` to
pull, unchanged. That asymmetry is deliberate, not an oversight:

- The kube credentials were **tunstrap's own** material, injected without the
  operator asking, with a lossless on-disk alternative (`path`) already in the
  envelope — dropping them cost nothing.
- A fetched file is **opt-in twice**: the operator names it with `--fetch`
  *and* elects `--output-var`, and it is the operator's own content under their
  own classification. `FetchedFile` has no `path` (`schemas.py:292`), so
  dropping `content_b64` would be a silent, unrecoverable breakage of any
  consumer that reads it.

Silently discarding data the operator explicitly requested is a worse failure
mode than persisting data they asked to be exported. The same plan-file
persistence applies, so **do not `--fetch` a secret while using `--output-var`**
— tunstrap fetches into the envelope (`content_b64`), not onto disk, so the
bytes would be persisted. Deliver a secret to the module by a path it reads
directly, not through this channel. The intended end-state (materialize fetched
files under the session dir at `0o600`, give `FetchedFile` a `path`, then drop
`content_b64`) is recorded in the spec's "Out of scope".

One other free-form string rides this channel unprojected: `warnings[*].error`.
It is exception text from an optional-node or kube-target failure (`manager.py`,
`kube.py`) — connection, auth or TLS messages, not key material — so it is left
intact rather than truncated.

### The input variable is scrubbed

The variable named by `--input-env` — `TUNSTRAP_INPUT` in the shim above —
holds the `InputSchema`, including `ssh_pkey`. `run` removes it from the child's
environment before exec'ing `tofu`, because `tofu` passes its environment to
every provider plugin, `external` data source and `local-exec` provisioner.
Nothing in the module needs it; if you need a value from the payload downstream,
export it explicitly rather than relying on inheritance.

If you have more than one node in the payload, the scalar `TUNSTRAP_*` env and
`KUBECONFIG` are not injected (they have no node dimension and would collide);
only `TF_VAR_tunstrap` is set, and the module picks the node out of
`connections[<node>]`. See the `--output-var` rules in the README.

## Measured Terragrunt facts

All measured 2026-07-31 against **Terragrunt v1.1.1** and **OpenTofu v1.12.5**.
These are observations about that pair, not tunstrap invariants — but the whole
recipe rests on them.

1. **`terraform_command_line` does not exist.** The hook is `terraform_binary`
   / `--tf-path` / `TG_TF_PATH`, and it accepts a **path only**:
   `--tf-path "/tmp/wrapper.sh --flag"` fails on the `-version` probe with
   `fork/exec /tmp/wrapper.sh --flag: no such file or directory`. This is why
   the shim is a file, not a command-line template.
2. **`inputs` are delivered to the child as `TF_VAR_<name>` environment
   variables** (JSON-encoded values), not `.tfvars.json`, not `-var-file`. This
   is why the envelope travels as `TF_VAR_tunstrap`.
3. **`extra_arguments.env_vars` reach the listed command *and* its automatic
   `init`, but not `-version`.** So a `terragrunt plan` with
   `commands = ["plan","apply"]` sets your env var for the auto-`init` and for
   `plan`, but leaves it unset for the `-version` probe — which is exactly why
   the shim's `-version` pass-through works without a tunnel.
4. **`dependency.*` resolves inside `extra_arguments.env_vars`, but not inside
   `locals`.** In `locals` you get `"dependency" is not defined`. This is why
   the recipe keeps any `dependency.*` reference inline in the `env_vars` block.
5. **Payloads survive byte-for-byte.** A ~10 KB JSON value arrived at both the
   auto-`init` and `plan` with identical length and SHA-256 — no truncation, no
   `E2BIG`. Multi-line content with PEM delimiters, `"` and `$` arrived
   byte-identical. So embedding SSH private keys in the payload is safe at the
   transport level (see "Security" for the stronger alternative).

## Failure modes you will hit

- **A `commands` entry you forgot.** The command runs without `TUNSTRAP_INPUT`,
  the module takes its inert branch, and the provider errors against
  `https://127.0.0.1:0`. That is the *designed* loud failure — add the command
  to the list. `import` is the classic omission.
- **A `--` you forgot inside `tunstrap run`.** This is a `run`-level rule, but
  surfaces through the shim: `--` is mandatory whenever the child command or any
  of its arguments begins with `-`. The shim always passes `--`, so this only
  bites if you hand-edit it.
- **A broken `config_path` chain that *succeeds*.** This is the silent one, and
  it is what `env -u KUBECONFIG` exists to prevent. If you ever see an apply
  succeed after a wiring change you expected to break it, the first thing to
  check is whether `KUBECONFIG` is still being cleared.
- **An `init` that *builds* a tunnel.** If the shim's `init` skip stops
  matching, you get two tunnels per `plan` (one for auto-`init`, one for the
  plan itself). See the gap below for one known way this happens.

## A real gap: `tofu -chdir=DIR init` builds a needless tunnel

The shim's skip branch is:

```sh
case "$1" in init|-version) exec tofu "$@" ;; esac
```

This matches only a **literal first token**. `tofu init` matches; `tofu
-version` matches. But `tofu -chdir=somewhere init` does **not**, because the
first token is `-chdir=…`, not `init`. So a `-chdir` invocation misses the
bypass and builds a tunnel it does not need.

This is **untested and unfixed for the shell shim**. It is not dangerous — the
tunnel is harmless, it just costs time — but it is wasteful, and the failure is
silent (you will not see an error, only a slower `init`).

> **Fixed in the `tunstrap_tofu` entry point.** The in-package proxy parses
> argv structurally past global flags (`-chdir DIR` and `-chdir=DIR`), so
> `tunstrap_tofu -chdir=DIR init` correctly bypasses the tunnel. If you use the
> shipped entry point rather than the consumer shim, this gap does not apply.

Guidance, in order of preference:

1. **Let Terragrunt handle `chdir`.** Terragrunt already `cd`s into the unit
   directory before invoking `terraform_binary`, so `tofu` itself does not need
   `-chdir`. This is the normal path and avoids the issue entirely.
2. **If you must call the shim with `-chdir` directly**, extend the `case` to
   scan past leading global flags, e.g. match `init`/`-version` anywhere in
   `"$@"` rather than only in `"$1"`. Keep the `exec tofu "$@"` (the flags
   still belong to `tofu`). Be aware that broadening the match is itself a
   place to introduce a bug, so test it.

## What is proven — and what is not

The e2e tier (`tests/e2e/`) is real evidence for this design, but its scope is
exact. Cite it for what it proves; do not let prose drift past it.

**Proven by the e2e tier:**

- Real `kubernetes` and `helm` providers reach a real cluster (a local `kind`
  node) through a real tunstrap tunnel, via the
  `--output-var` → `TF_VAR_tunstrap` → `try(jsondecode(...))` → `config_path`
  chain.
- The decoded `config_path` is the **only** route from the module to the
  cluster. This was proven three ways: a valid path mutates real objects and the
  used path is read back from state; an absent path fails naming the inert
  `127.0.0.1:0` endpoint; a present-but-dead path fails naming the dead port.
  Clearing `KUBECONFIG` is what makes these distinctions meaningful.
- A dead endpoint surfaces as a non-zero exit.
- `tofu`'s exit code propagates verbatim through `tunstrap run` (the tier proves
  an exact code outside tunstrap's reserved set, not merely non-zero).
- `tunstrap run` adds no bytes to `tofu`'s stdout (asserted byte-for-byte
  against a direct-run oracle).

**Not proven — do not imply:**

- **Nothing about a remote cluster over a real network.** The entire tier runs
  against a local `kind` node on one workstation. Latency, packet loss, and
  real-network SSH behaviour are unexercised.
- **Nothing about TLS, auth, timeout or 5xx failures.** The only failure mode
  exercised is *connection-refused against a dead endpoint*. Other provider
  failures may not render a port at all, and the "`config_path` is the route"
  proof does not generalise to them.
- **`terragrunt output -json` parsing IS now tested end-to-end** (in
  `tests/e2e/test_terragrunt_apply.py`), in two configurations: with `output`
  absent from `commands` (the pass-through shim → `tofu`) and with `output`
  added (the worst case: output routed through `tunstrap run`, proving tunstrap
  run's own stdout survives a real Terragrunt consumer's parse — the property
  the shim's `# Never write to stdout` comment guards). **Still not tested:**
  stdout purity for `plan`/`apply` under real Terragrunt (their stdout is the
  plan/apply diff, consumed differently) and any consumer other than
  `terragrunt output -json`. The worst-case test proves the purity property; it
  does **not** recommend tunnelling `output` — the recipe keeps `output` out of
  `commands` (it reads state, not the cluster).

## Why a console script (now) — and what the consumer-file shim was protecting

> **Decision reversed.** The proxy now also ships **in-package** as a second
> console script, `tunstrap_tofu` (`tunstrap/tofu_proxy.py`), so
> `uv tool install` produces both `tunstrap` and `tunstrap_tofu` and
> `terraform_binary` can point at a stable installed path with nothing copied
> into the consumer's repo. The consumer-file shim above remains available and
> is still what the e2e tier drives; the two are behaviourally equivalent
> except where this section says otherwise. The original three objections are
> kept below, each with its resolution, because the trade is real and worth
> knowing before you choose between them.

The three reasons the design originally argued for a consumer file, and where
each stands now:

1. **A Python console script pays interpreter startup on the fast paths.**
   Real, and it kills the naive approach. Measured: the `sh` shim's fast path
   costs ~2 ms; bare Python startup ~17 ms; Python plus `import tunstrap.cli`
   ~225 ms (the import alone is ~184 ms by `-X importtime`). At 225 ms and
   three fast-path invocations per `terragrunt plan`, a naive entry point that
   imported `cli` on every call would add ~0.7 s per plan.
   **The `tunstrap_tofu` entry point does not import `cli` (or anything heavy)
   on the pass-through paths** — it `execvp`s `tofu` before any tunstrap import
   beyond the package `__init__`. Measured fast path: **~59 ms end-to-end**
   (≈17 ms interpreter + ≈41 ms `importlib.metadata` in `tunstrap/__init__`,
   which is structural and unavoidable without changing that file, + ~1 ms
   execvp handoff; the `tofu_proxy` module itself adds nothing observable).
   That is ~28 ms over the bare-Python floor and ~166 ms under the naive 225
   ms — roughly **~180 ms added per `terragrunt plan`**, noise beside an 8 s
   `tofu init`. The cost discipline is guarded by a unit test that imports only
   `tunstrap.tofu_proxy` in a fresh interpreter and asserts none of
   `tunstrap.cli`/`click`/`pydantic`/`asyncssh`/`cryptography`/`ruamel` loaded.
   The shell shim remains cheaper (≈2 ms); if a consumer's plan runs dozens of
   fast-path invocations and every millisecond matters, the consumer file is
   still the lower-overhead choice.
2. **A committed file is the stable path `terraform_binary` wants.** Answered.
   `uv tool install` yields a stable `tunstrap_tofu` entry point at
   `~/.local/bin/tunstrap_tofu` (mirroring `tunstrap`), identical across
   reinstalls; the ephemeral `~/.cache/uv/…` path is a `uvx` artefact only. So
   the package entry point *is* a stable path, and `terraform_binary` can point
   at it with no consumer-side file to copy, `chmod`, commit or drift.
3. **It keeps Terraform vocabulary out of tunstrap.** This one is **being
   consciously reversed by the owner.** `init`, `-version`, and `TF_VAR_` are
   Terraform concepts, and the original design was structured to keep them in a
   consumer shim, not in the package (see the spec's decision log, items 7 and
   20). That principle is now deliberately traded for the ergonomics of a
   shipped entry point: `tunstrap/tofu_proxy.py` owns exactly that vocabulary.
   The trade is recorded in the spec where the Terraform-free principle is
   stated, not silently abandoned. The consumer-file shim is the escape hatch
   for anyone who prefers the package to stay Terraform-free.

**Which to use.** Prefer `tunstrap_tofu` (nothing to copy, stable path, the
`-chdir` gap below is fixed). Keep the consumer shim if you want the ~2 ms fast
path or want no Terraform vocabulary in the package you depend on.

## Security

Private keys still travel in the child's **environment** — but in this model
they travel in `TUNSTRAP_INPUT` (an env var), never on the command line. That
matters: Terragrunt's `ProcessExecutionError` joins the command and all
arguments, so the old `run_cmd` design could print a PEM on any failure. The
proxy model removes that exposure surface entirely.

**The stronger option, recommended: use ssh-agent.** If you export
`SSH_AUTH_SOCK` and drop `ssh_pkey` from the payload, key material leaves the
payload altogether — tunstrap will use the agent. See
[`docs/specs/2026-06-25-ssh-agent-fallback-design.md`](specs/2026-06-25-ssh-agent-fallback-design.md).
It is not required by this recipe, but it is the right end state for any
non-disposable environment.

Two standing caveats, unchanged by this recipe:

- **Host-key verification is not enforced** in this release. The tool targets
  disposable/CI hosts on trusted networks. Do not use it over untrusted
  networks until host-key pinning lands.
- **Materialized kubeconfigs land on disk** (mode 0600, under
  `<session-dir>/tunnel-data/`) for the lifetime of the child. `run` removes
  them in its teardown; a `kill -9` of the daemon orphans them and you must
  clean up manually (`rm -rf <session-dir>/tunnel-data`). This is inherent to
  giving the provider a real `config_path`.
