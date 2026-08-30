# Recipe: Terragrunt / OpenTofu through a tunstrap tunnel

This recipe shows how to drive Terragrunt and OpenTofu (`tofu`) through a
tunstrap tunnel using the **CLI-proxy model**: the shipped `tunstrap_tofu`
console entry point installed as Terragrunt's `terraform_binary`, which brings
the tunnel up and runs `tofu` with the connection details in the environment.

It is written for someone adopting this in their own repo. It carries the
measured facts a future reader would otherwise have to re-derive, the failure
modes you will hit, and an honest statement of what is and is not proven.

> **Companion design.** The full design — why this shape over the alternatives
> (`--owner` watchdog, `--output-file`, `--placeholder-host`) — lives in
> [`docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md`](specs/2026-07-31-run-env-io-and-tofu-proxy-design.md).
> The e2e tier that proves the central claim lives in `tests/e2e/`.

## The model in one paragraph

Terragrunt lets you point `terraform_binary` at any executable. `tunstrap_tofu`
is that executable. For commands that touch a live cluster (`plan`, `apply`, …)
the proxy opens the tunnel and **becomes `tofu`'s parent**: it injects the
connection details into `tofu`'s environment, waits for `tofu`, and tears the
tunnel down in a `finally` — so the tunnel's lifetime is exactly the child's.
For commands that do not need a tunnel (`init`, `-version`), and whenever no
tunnel is wanted, the proxy `execvp`s `tofu` directly. tunstrap is never a
daemon you normally start and stop by hand in this model; it owns the child and
tears it down automatically. Whenever that teardown ends without a confirmed
stop — it reports a failure, itself raises, or the recorded identity is
unreadable — tunstrap keeps the session data instead of deleting it and prints
the `tunstrap stop --session-dir …` command that finishes the job by hand. That
command applies the same rule, so it is safe to repeat: it clears the tunnel
data once the daemon is confirmed gone, and otherwise preserves it and reports
`"preserved": true`. When the preserved directory is one tunstrap minted under
`TMPDIR`, the diagnostic names it too — `stop` never removes its own
`--session-dir` argument, so that directory is yours to delete once the daemon
is dealt with. A caller-supplied session dir must be owned by the invoking user;
tunstrap clears its group/other write bits on use, because it stores 0600
credentials (`tunnel-data/`) there — no pre-`chmod` is required.

`stop` exits 0 only when it clears `tunnel-data`: stopped, forced, or `not
found`. It exits 1 for every outcome that preserves data: identity mismatch,
identity check unavailable, still alive, identity changed during grace, and the
three identity-read failures (missing, unreadable, or malformed `daemon.pid`).
Repeating an unresolved recovery command keeps returning 1 until the session is
resolved by hand; the loop's behaviour is unchanged, only its status, and a
preserved session was never recoverable through repetition alone.

## Prerequisites

- `tunstrap_tofu` on `PATH` (the installed proxy entry point — see Installation
  below for a one-line install). `tofu` on `PATH` too (the proxy `execvp`s it
  for the pass-through branches and as the tunnelled child).
- An OpenTofu/Terraform module that reads the connection details from a
  `TF_VAR_*` variable. The shape is given below and is load-bearing.

## Installation

One piece, installed once: the `tunstrap` package, which ships **two** console
entry points — `tunstrap` and `tunstrap_tofu` (the proxy).

tunstrap is **not on PyPI** — a direct-reference dependency (the asyncssh fork
the package is built on) blocks publishing — so install it from the git source
or a local checkout:

```sh install
uv tool install "git+https://github.com/AlexMKX/tunstrap.git"
# or from a local checkout:
uv tool install /path/to/tunstrap
```

Use `uv tool install`, not `uvx`. `uv tool install` yields **stable** entry
points at `~/.local/bin/tunstrap` and `~/.local/bin/tunstrap_tofu`, identical
across reinstalls; `uvx` runs from an ephemeral `~/.cache/uv/archive-v0/…` path
that changes per resolution. There is nothing to copy into your repo: point
`terraform_binary` at the installed `tunstrap_tofu` and you are done.

## How the proxy works

`tunstrap_tofu` is a thin dispatcher around `tofu` with three branches, decided
from `argv` and `TUNSTRAP_INPUT`:

| Condition | Branch | What happens |
|---|---|---|
| `TUNSTRAP_INPUT` unset | pass-through | `execvp tofu "$@"` — no tunnel, no `tunstrap`. This is the "infra not applied yet" path: Terragrunt omits the env_var, so the proxy is a transparent `tofu` wrapper. |
| subcommand is `init`/`version`/`validate`/`fmt`/`-version`/`-help`, or no subcommand | pass-through | `execvp tofu "$@"` — same. `init` only configures the backend and downloads providers; `validate` checks the configuration against installed provider schemas only; `fmt` touches only local `.tf` files — none of the three ever reach the cluster API. Skipping them avoids a redundant tunnel per `terragrunt plan`/`validate`/`fmt` (Terragrunt's `extra_arguments.env_vars` reaches the listed commands **and** their automatic `init`). The subcommand is parsed past global flags, so `tofu -chdir=DIR init` also bypasses (see "A fixed gap" below). **Note:** this bypasses `TUNSTRAP_INPUT` even if you deliberately list `validate`/`fmt` in `commands` below — both are provably cluster-free, so the proxy does not build a tunnel for them regardless of that opt-in. |
| otherwise | tunnelled | opens the tunnel, injects `TF_VAR_tunstrap` (the connection envelope) plus the scalar env, runs `tofu`, and tears the tunnel down in a `finally` — so the tunnel's lifetime is exactly the child's. Reuses `tunstrap run`'s hardened path in-process; no second process level. |

**Never write to stdout.** Terragrunt captures and labels `tofu`'s stdout by
default, and `terragrunt output -json` consumers parse it. Diagnostics go to
stderr or a file. `tunstrap run` is silent on stdout after the child starts.

**`KUBECONFIG` is suppressed in the child environment, not on the command line —
and only `KUBECONFIG`.** For a single-node payload `run` injects `KUBECONFIG`
(pointing at the same materialized file `config_path` would use); left in place
it is a **silent fallback for `var.tunstrap`-driven configs (Mode B, below)** —
if the `TF_VAR_tunstrap` → `config_path` wiring were broken, providers would
still find a working cluster via `KUBECONFIG` and everything would appear fine.
The proxy sets `suppress_kubeconfig`, so `run` builds the child environment with
the *injected* `KUBECONFIG` removed, making the decoded `config_path` the
**only** route to the cluster for a Mode B config. `KUBE_CONFIG_PATH`/
`KUBE_CONFIG_PATHS` — the provider-facing names Mode A (below) relies on — are
**not** touched by this guard: providers never read plain `KUBECONFIG` at all,
so suppressing it protects a different, real audience instead —
`kubectl`/`helm` CLI invocations inside `local-exec` provisioners, which do
honour it. **Mode A works through `tunstrap_tofu` as documented**: any
inherited `KUBECONFIG`/`KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS` from the operator's
own shell is also always dropped before `run` injects the real channel, on both
the plain and the proxied path, so a stray operator environment can never leak
into or override it either. The e2e tier proves the suppression by recording
`tofu`'s actual environment and asserting `KUBECONFIG` is absent.

### Wiring it into Terragrunt

`terraform_binary` takes a **path only** (see "Measured Terragrunt facts"
below). The **default** is a literal absolute path — nothing runs at config
parse time and there is one less moving part. Find the path once with
`command -v tunstrap_tofu` (`uv tool install` places it at
`~/.local/bin/tunstrap_tofu`) and paste it:

```hcl terragrunt-root
# root.hcl - shared across units; each unit inherits it with
#   include "root" { path = find_in_parent_folders("root.hcl") }
# (see the unit block below). terraform_binary is a TOP-LEVEL attribute, not a
# member of the terraform {} block: TG rejects it there with "An argument named
# terraform_binary is not expected here." See "Measured Terragrunt facts" below.
# For a single-unit repo you may instead put this line at the top of the unit's
# terragrunt.hcl and drop the include.
#
# Default: the absolute path of the installed tunstrap_tofu entry point.
terraform_binary = "$HOME/.local/bin/tunstrap_tofu"
```

#### Localizing the bootstrap with `run_cmd` (optional)

If you would rather the bootstrap lived entirely in `terragrunt.hcl` — no pasted
path to update after a reinstall — `terraform_binary` also accepts `run_cmd`,
which resolves the entry point at run time:

```hcl terragrunt-root-runcmd
# Optional: resolve the installed tunstrap_tofu at run time instead of pasting
# the path. run_cmd execs its first arg directly (no shell), so go through
# `sh -c` to use the POSIX `command -v` builtin.
#
# "--terragrunt-quiet" is LOAD-BEARING and it is the first argument to run_cmd
# itself, not a terragrunt CLI flag. run_cmd consumes it to suppress logging the
# command's output; without it, that output (the resolved path) is prepended to
# every `terragrunt output -json`, corrupting the JSON. Measured against
# Terragrunt v1.1.1:
#   run_cmd("--terragrunt-quiet", "sh", "-c", "command -v tunstrap_tofu")  -> clean
#   run_cmd("sh", "-c", "command -v tunstrap_tofu")                        -> path leaks
# This is the same shape as `env -u KUBECONFIG` in the old shell shim: drop the
# incantation and the failure surfaces somewhere that looks unrelated.
terraform_binary = run_cmd("--terragrunt-quiet", "sh", "-c", "command -v tunstrap_tofu")
```

`run_cmd`'s real costs: it runs **before** Terragrunt's `-version` probe, is
cached once per `plan`, and runs once **per unit** under `run --all` — and it
needs the marker. The literal-path default has none of those moving parts, which
is why it is the recommendation.

A `before_hook` **cannot** install the proxy instead. Terragrunt probes
`<terraform_binary> -version` roughly 50 ms *before* any hook runs, and with the
binary missing it fails outright before the hook executes (measured; the hook's
marker file never appears).

### Alternative: a shell shim for the fast path

For the unusual consumer for whom every millisecond of the fast path matters,
`tunstrap_tofu` costs ~25 ms per pass-through invocation (vs ~2 ms for a shell
`exec`) — about ~74 ms added per `terragrunt plan`, noise beside an 8 s
`tofu init`. A 3-line `/bin/sh` shim recovers the ~2 ms fast path at the cost of
copying, committing and keeping it in sync (it is not driven by the e2e tier;
`sh -n`-checked and smoke-tested as a labelled fence below —
`tofu-shim-alt`). For nearly everyone the entry point is the better trade.

```sh tofu-shim-alt
#!/bin/sh
# Lower-overhead alternative to tunstrap_tofu: a shell exec on the fast paths
# (~2 ms vs ~25 ms). Copy into bin/tofu-tunstrap, chmod 0755, commit. Behaves
# the same as the proxy EXCEPT it cannot parse past -chdir to a global flag, so
# `tofu -chdir=DIR init` builds a needless tunnel (the fixed gap, unfixed here).
[ -n "$TUNSTRAP_INPUT" ] || exec tofu "$@"
case "$1" in init|-version) exec tofu "$@" ;; esac
exec tunstrap run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap \
  -- env -u KUBECONFIG tofu "$@"
```

Then, in the unit that needs the tunnel, declare `TUNSTRAP_INPUT` as an
`extra_arguments` env var scoped to the commands that actually contact the
cluster. The unit **must** inherit the root for `terraform_binary` to take
effect — without `include "root"`, Terragrunt silently falls back to plain `tofu`
on `PATH` and the apply dies at the inert `https://127.0.0.1:0` endpoint (see
"Failure modes" — this is indistinguishable from a forgotten `commands` entry by
the exit code alone, and the recipe exists to keep them apart):

```hcl terragrunt-unit
# unit terragrunt.hcl
#
# `terraform_binary` lives in root.hcl; inherit it. find_in_parent_folders
# defaults to searching for "terragrunt.hcl", so name "root.hcl" explicitly -
# the bare find_in_parent_folders() errors with ParentFileNotFoundError when the
# parent is root.hcl (measured).
include "root" {
  path = find_in_parent_folders("root.hcl")
}

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
#   # PEM-format PRIVATE key (fed to asyncssh.import_private_key) - NOT the
#   # ssh-ed25519 AAAA... .pub line. Pull it from your secret store rather
#   # than committing it inline.
#   ssh_private_key = get_env("TUNSTRAP_SSH_PRIVATE_KEY", "")
# }
```

When `local.cluster_host == ""` (infra not applied, or a mock-state run), the
`env_vars` map is empty, `TUNSTRAP_INPUT` is unset, and the proxy takes its
pass-through branch — so the same unit plans cleanly with no tunnel and
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

`validate` and `fmt` are also in the proxy's own bypass set (see "How the proxy
works," above): even if you list either in `commands`, the proxy still
`execvp`s `tofu` directly for them rather than tunnelling, because both are
provably cluster-free. Consequence: `tofu validate` therefore runs *without*
`TF_VAR_tunstrap` set at all — keep a default on the variable, as the module
below does (`default = ""`), or `validate` hits an unset-variable error.

Everything not listed gets `TUNSTRAP_INPUT` unset and takes the proxy's
pass-through branch, so the failure mode of forgetting a command is a **provider
error against an inert loopback endpoint**, not a silent wrong result.

## The module side

The proxy hands the module the connection envelope as a JSON string in
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
  tunnel   = try(jsondecode(var.tunstrap), { nodes = {} })
  kubepath = try(local.tunnel.nodes.node.kube.k3s.path, "")

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

Four things to get right, each of which the tier proved can pass for the wrong
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
   `run` always forces `daemon.materialize = true`, so `nodes.*.kube.*.path`
   is a real on-disk kubeconfig (mode 0600), patched so `server:` and
   `tls-server-name` already point at the tunnelled port. The provider just reads it.
4. **`sensitive = true` on the variable.** Defence in depth, not the fix — it
   suppresses rendering in plan/apply output and diagnostics, but it does *not*
   keep the value out of the plan file. See the note below.

### What is, and is not, in `TF_VAR_tunstrap`

`run` **projects** the envelope before exporting it on this channel. Each
`kube` entry keeps:

`path`, `context`, `endpoint`

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
inline provider configuration rather than `config_path` reads
`certificate_authority_data` (a published trust anchor, not a credential),
`tls_server_name`, and its own client credentials from that file directly —
only `path`, `context` and `endpoint` travel through `TF_VAR_tunstrap` itself.

`tunstrap start --output json` projects every materialized kube target through
the same `path` / `context` / `endpoint` allow-list and every materialized
fetched file through `{path, size, sha256}`. Without `--materialize`, each
entry's `content_b64` is its only delivery channel, so that entry instead
retains the complete envelope on stdout. Treat this unmaterialized mode's stdout
as credential material; do not place it in CI logs or durable shell captures.

### Fetched files are materialized, not carried in the envelope

The projection above (kube) and this one (`fetch_files`) follow the same rule:
`run` and materialized `tunstrap start --output json` materialize content to
disk under the session dir's `tunnel-data/`, mode `0600`, and their
consumer-facing envelope carries only a reference to it. Each `fetch_files`
entry becomes `{path, size, sha256}` on success, `{error}` on failure — never
`content_b64`.

`FetchedFile` **has a `path`** (`schemas.py`, extended for this ticket), so
the lossless on-disk alternative exists, the same way it already existed for
kube.

**The plan-file-persistence risk is resolved as a class, not documented
around**: since fetched content never enters `TF_VAR_tunstrap` or the
materialized file at all, `--fetch`ing a secret cannot land it in a saved
Terraform plan file through this channel. Read the file directly at
`fetch_files.<name>.path` if you need its contents.

One free-form string rides this channel unprojected: `warnings[*].error`.
It is exception text from an optional-node or kube-target failure (`manager.py`,
`kube.py`) — connection, auth or TLS messages, not key material — so it is left
intact rather than truncated.

### The input variable is scrubbed

The variable named by `--input-env` — `TUNSTRAP_INPUT` in the proxy above —
holds the `InputSchema`, including `ssh_pkey`. `run` removes it from the child's
environment before exec'ing `tofu`, because `tofu` passes its environment to
every provider plugin, `external` data source and `local-exec` provisioner.
Nothing in the module needs it; if you need a value from the payload downstream,
export it explicitly rather than relying on inheritance.

If you have more than one node in the payload, the kube env channel —
`KUBECONFIG`/`KUBE_CONFIG_PATH(S)` — is still injected: it is unconditional
on node count and aggregates every kube target across every node into one
file list. Only the old `TUNSTRAP_<TARGET>_*` scalars — a concept that no
longer exists — were ever suppressed for multi-node. `TF_VAR_tunstrap` is set
unconditionally too, and the module picks the node out of `nodes[<node>]`.
See the `--output-var` rules in the README.

## Mode A: env-native kube (satisfies the ticket's strict "nothing live enters Terraform" contract)

The module above reads its kube identity from `var.tunstrap`, which is
connection data travelling through a Terraform input variable — exactly what
ticket #15 asked to stop. Mode A is the alternative that actually satisfies
that contract: no `var.`-bound value, no file read in HCL at all, for kube.

**Mode A works through `tunstrap_tofu`**, the proxy this recipe recommends as
`terraform_binary` — that is the point of it. The proxy's
`suppress_kubeconfig` guard only removes the injected `KUBECONFIG`; it never
touches `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS`, so item 1 below reaches a
provider block unfiltered whether you invoke `tofu` directly or through the
proxy (see "How the proxy works," above, for why).

1. `run` exports `KUBE_CONFIG_PATH` (one kube target) or `KUBE_CONFIG_PATHS`
   (two or more) from its own process environment, unconditionally — see "The
   input variable is scrubbed," above. A provider block that sets nothing but
   a literal `config_context = "tunstrap-<node>-<target>"` per alias resolves
   its `config_path`/`config_paths` from those env vars alone, via the
   provider's own `EnvDefaultFunc`. A two-alias worked example, one provider
   block per kube target, sharing the same `KUBE_CONFIG_PATHS` list:

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

2. **Warning, explicit:** never derive `config_context`'s value from
   `var.tunstrap` or any other live/decoded data. It must be a literal string
   in the config, matching the deterministic naming scheme exactly
   (`tunstrap-<node>-<target>`). Deriving it live would reintroduce a
   variable-bound value for data that has an env-native, fully static
   alternative, defeating the point of Mode A.

3. **Measured facts a consumer needs**, restated from the ticket's own six
   findings plus this design's provider findings, not re-derived:

   - **#1** — provider configuration **is** re-evaluated at apply.
   - **#2** — outputs **freeze silently**: `file()` read through an output
     returns the plan-time value at apply, with no error — the nastiest
     failure mode, name it as such.
   - **#3** — per-alias `config_context` works with an env-supplied
     kubeconfig path (Mode A's own basis, shown in item 1's example).
   - **#4** — plan-safe end to end, measured live in the unpublished #15 spike:
     plan with one set of
     ports, mutate only the kubeconfig, apply the *saved* plan → the alias
     uses the mutated value, zero "Mismatch between input and plan variable
     value" — the e2e-level confirmation that Mode A's env-native path
     really is plan-safe across a saved-plan reuse, not just a theoretical
     consequence of finding #1.
   - **#5** — `KUBE_CONFIG_PATHS` is colon-separated on Linux (comma
     silently falls back to `localhost:80`).
   - **#6** — a live value bound to a `var.` **does** trip "Mismatch between
     input and plan variable value" on a saved plan (Mode B's one-shot rule
     rests on this).
   - A live value bound to a **resource attribute** (not a provider config
     block) produces `Error: Provider produced inconsistent final plan` —
     confirmed for `hashicorp/kubernetes` v2.38.0
      (`docs/specs/2026-08-10-issue15-provider-env-precedence.md`, Q3).
     Provider-block placement, as shown in item 1's example, is the only
     supported shape in both Mode A and Mode B.

4. The `config_context` values above follow tunstrap's deterministic naming
   scheme (`tunstrap-<node>-<target>`) exactly — the same names
   `rename_identities` writes into the materialized kubeconfig. That matters
   beyond providers: anyone who pipes the materialized file straight into
   `kubectl --context` instead of through a provider block uses the same
   literal context names.

## Mode B: unified-file convenience (ports + kube references; does NOT satisfy the ticket's strict contract)

A real consumer may use Mode A for kube and Mode B for ports in the same
module. Nothing here satisfies the ticket's strict "nothing live enters
Terraform" guarantee — state that plainly to a reader, not glossed over.
There is no literal, operator-pinned path and no `var.`-derived locator
anywhere in this section: the session dir stays ephemeral unconditionally.

5. **The shape**, a worked example reading the env-carried
   `TUNSTRAP_OUTPUT_FILE` locator via Terragrunt's `get_env(...)` — no
   `--session-dir` precondition and no operator-agreed path:

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

   Read directly inside the `locals` block that feeds the provider config —
   never through an `output`, per finding #2.

6. **Ports lose their integer form** (`"host:port"`, not a bare port
   number) — the extraction idiom:

   ```hcl
   locals {
     service1_port = split(":", local.tunnel.nodes.node1.ports.service1)[1]
   }
   ```

7. **The stability contract**, restated plainly: **both** Mode B forms —
   item 5's `TUNSTRAP_OUTPUT_FILE` form and the `--output-var`
   (`var.tunstrap`) form — are **one-shot `plan && apply` only**, with no
   saved-plan reuse across a tunstrap restart for either and no locator
   exemption of any kind (the check compares the variable's whole value; the
   file itself is deleted at teardown alongside the rest of `tunnel-data/`).
   Findings #1, #2 and #6 back this. Stated as plainly as the design doc
   states it: *"Neither Mode B form survives a tunstrap restart. If you need
   a saved plan to apply cleanly against fresh ports or fetched-file
   content, re-run plan in the same tunstrap invocation."*

8. **`jsondecode`, not JavaScript.** Consumption is via HCL's `jsondecode`;
   there is no JS runtime anywhere in this stack (ADR entry 12).

9. **Fetched files:** *"Fetched file content never enters a Terraform
   variable or plan file — only its path, size, and checksum do. Read the
   file itself at `fetch_files.<name>.path` if you need its contents."*

## Measured Terragrunt facts

All measured 2026-07-31 against **Terragrunt v1.1.1** and **OpenTofu v1.12.5**.
These are observations about that pair, not tunstrap invariants — but the whole
recipe rests on them.

1. **`terraform_command_line` does not exist.** The hook is `terraform_binary`
   / `--tf-path` / `TG_TF_PATH`, and it accepts a **path only**:
   `--tf-path "/tmp/wrapper.sh --flag"` fails on the `-version` probe with
   `fork/exec /tmp/wrapper.sh --flag: no such file or directory`. This is why
   `terraform_binary` resolves a path (whether the `tunstrap_tofu` entry point
   or a shell-shim alternative), not a command-line template.
2. **`inputs` are delivered to the child as `TF_VAR_<name>` environment
   variables** (JSON-encoded values), not `.tfvars.json`, not `-var-file`. This
   is why the envelope travels as `TF_VAR_tunstrap`.
3. **`extra_arguments.env_vars` reach the listed command *and* its automatic
   `init`, but not `-version`.** So a `terragrunt plan` with
   `commands = ["plan","apply"]` sets your env var for the auto-`init` and for
   `plan`, but leaves it unset for the `-version` probe — which is exactly why
   the proxy's `-version` pass-through works without a tunnel.
 4. **`dependency.*` resolves inside `extra_arguments.env_vars`, but not inside
    `locals`.** In `locals` you get `"dependency" is not defined`. This is why
    the recipe keeps any `dependency.*` reference inline in the `env_vars` block.
 5. **Payloads survive byte-for-byte.** A ~10 KB JSON value arrived at both the
    auto-`init` and `plan` with identical length and SHA-256 — no truncation, no
    `E2BIG`. Multi-line content with PEM delimiters, `"` and `$` arrived
    byte-identical. So embedding SSH private keys in the payload is safe at the
    transport level (see "Security" for the stronger alternative).

## Failure modes you will hit

Both of the first two land at the same symptom — a provider error against
`https://127.0.0.1:0`, the module's inert branch — so they are easy to confuse.
They have different causes, and the recipe's job is to keep them straight:

- **A unit that forgot `include "root"`.** The root's `terraform_binary` is not
  inherited, so Terragrunt silently falls back to plain `tofu` on `PATH`. The
  `extra_arguments.env_vars` still delivers `TUNSTRAP_INPUT` to that `tofu`, but
  nothing sets `TF_VAR_tunstrap` (the proxy never runs), the module takes its
  inert branch, and the provider dials `127.0.0.1:0`. Tell-tale: a recording
  `tofu` shows `TUNSTRAP_INPUT` **present** but `TF_VAR_tunstrap` **absent**.
  Fix: add the `include "root" { path = find_in_parent_folders("root.hcl") }`
  block to the unit.
- **A `commands` entry you forgot.** The command runs without `TUNSTRAP_INPUT`
  (the list controls delivery), so `TF_VAR_tunstrap` is absent for the same
  reason, the module takes its inert branch, and the provider errors against
  `https://127.0.0.1:0`. Tell-tale: a recording `tofu` shows `TUNSTRAP_INPUT`
  **absent** for that command. That is the *designed* loud failure — add the
  command to the list. `import` is the classic omission.

  The two are distinguished by whether `TUNSTRAP_INPUT` reached `tofu`: present
  ⇒ missing include; absent ⇒ missing `commands` entry. Both are loud (non-zero
  exit, a named endpoint); neither is a silent wrong result.
- **A `--` you forgot inside `tunstrap run`.** This is a `run`-level rule. The
  proxy always passes `--` for the user, so a consumer driving `tunstrap_tofu`
  never hits it; it only surfaces if you hand-edit the `run` invocation (e.g. in
  the shell-shim alternative). `--` is mandatory whenever the child command or
  any of its arguments begins with `-`.
- **A broken `config_path` chain that *succeeds*.** This is the silent one, and
  it is what `suppress_kubeconfig` exists to prevent. If you ever see an apply
  succeed after a wiring change you expected to break it, the first thing to
  check is whether `KUBECONFIG` is still being cleared from the child env.
- **An `init` that *builds* a tunnel.** If the proxy's `init` bypass stops
  matching, you get two tunnels per `plan` (one for auto-`init`, one for the
  plan itself). See "A fixed gap" below for one known way this used to happen.

## A fixed gap: `tofu -chdir=DIR init` bypasses correctly

The original consumer shell shim matched the bypass with a literal first token:

```sh
case "$1" in init|-version) exec tofu "$@" ;; esac
```

This matched `tofu init` and `tofu -version`, but **not** `tofu -chdir=somewhere
init`, because the first token is `-chdir=…`, not `init`. So a `-chdir`
invocation missed the bypass and built a tunnel it did not need — wasteful, not
dangerous, and silent (a slower `init`, no error).

**`tunstrap_tofu` closes the gap.** The proxy parses argv structurally past
global flags (`-chdir DIR` and `-chdir=DIR`, both space and `=` forms), so
`tunstrap_tofu -chdir=DIR init` correctly identifies `init` as the subcommand
and bypasses. The bypass set is pinned exhaustively by a unit test
(`test_should_bypass_*` in `tests/unit/test_tofu_proxy.py`), so a future edit
that re-broadens or re-narrows it cannot pass silently.

If you use the shell-shim alternative instead of the entry point, the gap
returns — the shell `case` cannot parse past flags without becoming a substring
match (which would wrongly bypass `tofu -chdir init plan`). The entry point is
the recommended path precisely because it can make this distinction.

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
  absent from `commands` (the pass-through proxy → `tofu`) and with `output`
  added (the worst case: output routed through `tunstrap run`, proving tunstrap
  run's own stdout survives a real Terragrunt consumer's parse — the property
  the proxy's "never write to stdout" rule guards). **Still not tested:**
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
> into the consumer's repo. The consumer-file shell shim is retired from this
> recipe (the e2e tier now drives `tunstrap_tofu`); it survives only as the
> lower-overhead alternative the section after next mentions. The two agree on
> every command the consumer deliberately opted into Terragrunt's `commands`
> (the proxy must not veto that with a cluster-only allow-list of its own), and
> both bypass `init`. The two deliberate differences: the proxy also bypasses
> `version`/`-version`/`-help`/no-subcommand (harmless no-cluster cases the shell
> pointlessly tunnelled), and it parses argv past `-chdir` so `tofu -chdir=DIR
> init` bypasses correctly — closing the shell shim's documented gap. The
> original three objections are kept below, each with its resolution, because the
> trade is real and worth knowing before you choose between them.

The three reasons the design originally argued for a consumer file, and where
each stands now:

1. **A Python console script pays interpreter startup on the fast paths.**
   Real, and it kills the naive approach. Measured: the `sh` shim's fast path
   costs ~2 ms; bare Python startup ~17 ms; Python plus `import tunstrap.cli`
   ~225 ms (the import alone is ~184 ms by `-X importtime`). At 225 ms and
   three fast-path invocations per `terragrunt plan`, a naive entry point that
   imported `cli` on every call would add ~0.7 s per plan.
   **The `tunstrap_tofu` entry point does not import `cli` (or anything heavy)
   on the pass-through paths** — it `execvp`s `tofu` first — and
   `tunstrap/__init__.py` resolves `__version__` lazily (PEP 562), so the
   package import loads no `importlib.metadata` either. Measured fast path,
   end-to-end via the installed entry: **~25 ms** (≈17 ms interpreter + a
   now-cheap package import + the execvp handoff). That is about **12× the
   ~2 ms shell shim**, i.e. **~74 ms added per `terragrunt plan`**, noise beside
   an 8 s `tofu init`. (Before the lazy `__init__`, the same path was ~59 ms —
   `importlib.metadata` contributed ~41 ms; making `__version__` lazy dropped
   `import tunstrap` 67.3→17.5 ms and the pass-through 58.8→24.6 ms.) The cost
   discipline is guarded by a unit test that imports only `tunstrap.tofu_proxy`
   in a fresh interpreter and asserts none of `tunstrap.cli`/`click`/`pydantic`/
   `asyncssh`/`cryptography`/`ruamel`/`importlib.metadata` loaded. The shell
   shim remains cheaper (≈2 ms); a consumer for whom every millisecond of the
   fast path matters can still write the 3-line shim — but for everyone else
   ~25 ms is noise, and the entry point removes the copy/commit/drift entirely.
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
 `-chdir` gap above is fixed). Keep the consumer shim if you want the ~2 ms fast
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
