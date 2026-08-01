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

- `tunstrap` on `PATH` (it `exec`s `tunstrap run`).
- `tofu` on `PATH` (the shim `exec`s it for the pass-through branches and as the
  tunnelled child).
- The shim itself at a **stable absolute path** (Terragrunt's `terraform_binary`
  needs a path; see "Why not a console script" below). `bin/tofu-tunstrap`
  relative to your repo root is conventional.
- An OpenTofu/Terraform module that reads the connection details from a
  `TF_VAR_*` variable. The shape is given below and is load-bearing.

## The shim

This is the shim verbatim — copy it into your repo (e.g. `bin/tofu-tunstrap`),
`chmod 0755` it, and commit it. It is identical to the one the e2e tier drives
(`tests/e2e/shim/tofu-tunstrap`).

```sh
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

```hcl
# root.hcl (or the top of each unit's terragrunt.hcl)
terraform {
  terraform_binary = "${get_repo_root()}/bin/tofu-tunstrap"
}
```

Then, in the unit that needs the tunnel, declare `TUNSTRAP_INPUT` as an
`extra_arguments` env var scoped to the commands that actually contact the
cluster:

```hcl
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

# Build `local.cluster_host` / `local.ssh_private_key` from whatever your
# source of truth is. dependency.* works inside extra_arguments.env_vars but
# NOT in locals, so keep the dependency reference inside the env_vars block
# above if you read connection data from another unit's outputs.
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

```hcl
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

This is **untested and unfixed**. It is not dangerous — the tunnel is harmless,
it just costs time — but it is wasteful, and the failure is silent (you will
not see an error, only a slower `init`).

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
- **No test exercises a real Terragrunt or OpenTofu consumer parsing the
  labelled stream.** Every stdout-purity assertion in the tier uses a **fake
  `tofu`**, not real `tofu`, and none of them runs under Terragrunt at all. The
  claim that "Terragrunt parses this stream correctly" rests on the design and
  the shim's `# Never write to stdout` comment, **not on a test**. Treat it as a
  well-reasoned invariant, and verify it in your own environment if your
  downstream depends on `terragrunt output -json`.

## Why not a console script

The shim lives in **your** repo, not in tunstrap. There are three reasons, and
they are worth knowing before you are tempted to package it differently:

1. **A Python console script pays interpreter startup on the fast paths.** The
   `-version` and `init` branches run three times per `terragrunt plan`; a
   shell `exec` is effectively free, a `python -m` entry point is not.
2. **It keeps Terraform vocabulary out of tunstrap.** `init`, `-version`, and
   `TF_VAR_` are Terraform concepts. tunstrap is deliberately Terraform-free;
   the proxy knowledge belongs in a consumer shim, not in the package.
3. **`terraform_binary` needs a stable path.** tunstrap is installed via `uvx`
   (PyPI is blocked by a direct-reference dependency), whose scripts land in an
   ephemeral `~/.cache/uv/archive-v0/…` path. You have to materialize a stable
   path for the shim regardless, so it may as well be a committed file.

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
