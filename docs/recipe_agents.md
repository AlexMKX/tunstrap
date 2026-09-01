# Agent quickstart: tunstrap + OpenTofu/Terragrunt

For a coding agent that needs to drive `tofu`/`terragrunt` through a tunstrap
tunnel and get it right on the first try. It is deliberately short — read it
in full before touching state. For everything this omits, see
[`README.md`](../README.md) (full CLI/schema reference) and
[`docs/recipe_terragrunt.md`](recipe_terragrunt.md) (the full Terragrunt
recipe, with the measured facts and failure modes behind every claim here).

## The one paragraph you need

tunstrap opens SSH tunnels to hosts whose apiserver/services are not publicly
reachable and hands the result to a child process. Two verbs matter for
Terraform work: `tunstrap run HOST -- CMD` (or `--input-env VAR -- CMD`) opens
the tunnel, runs `CMD` with the connection injected into its environment, and
**always tears the tunnel down when `CMD` exits** — this is the verb you want,
not `start`/`stop`, unless you specifically need a long-lived daemon. The
proxy `tunstrap_tofu` (installed as Terragrunt's `terraform_binary`) is `run`
wrapped around `tofu`: cluster-touching subcommands (`plan`, `apply`, …)
tunnel; `init`, `-version`, `validate`, `fmt` bypass the tunnel entirely
(`tunstrap/tofu_proxy.py::_should_bypass`, `tunstrap/tofu_proxy.py::_BYPASS_COMMANDS`).
Verified live: `TUNSTRAP_INPUT=garbage tunstrap_tofu init` succeeds (bypasses
before JSON is even parsed); `TUNSTRAP_INPUT=garbage tunstrap_tofu plan` fails
with a `SchemaValidationError` (it genuinely tries to tunnel).

## Mode A vs Mode B — pick before you write HCL

There are two ways cluster/connection data reaches a provider block. **Default
to Mode A for anything kube-shaped; reach for Mode B only for what Mode A
cannot express (ports, fetched-file paths).**

| | Mode A (env-native kube) | Mode B (unified file / `--output-var`) |
|---|---|---|
| Channel | `KUBE_CONFIG_PATH`/`KUBE_CONFIG_PATHS`, set by the OS env, read by the provider's own `EnvDefaultFunc` | `TUNSTRAP_OUTPUT_FILE` (JSON on disk) or `TF_VAR_tunstrap` (`--output-var NAME`) |
| Provider config | `config_context = "tunstrap-<node>-<target>"` — a **literal string**, never `var.`-derived | `config_path = local.tunnel.nodes.<node>.kube.<target>.path` — a `var.`/`local.`-derived value |
| Saved-plan safety | **Yes.** Provider config is re-evaluated at apply; a value that only ever entered through the environment survives a mutated env across a saved-plan `apply` (measured live in the design spike behind `docs/recipe_terragrunt.md`'s "Mode A" section — not independently re-verified in this pass) | **No.** `plan && apply` in the **same tunstrap invocation only**. See the trap below. |
| Covers | kube clusters only | ports, kube (as a reference), fetched-file paths |

If you are only forwarding a Kubernetes apiserver: use Mode A, skip
`--output-var` entirely, and never touch a `var.` for kube data. If you also
need a forwarded port's `host:port` inside HCL, you need Mode B for that piece
— Mode A has no port channel.

**Mode A, minimal:**

```hcl
provider "kubernetes" {
  config_context = "tunstrap-edge1-k3s"  # literal; tunstrap's naming: tunstrap-<node>-<target>
}
```

**Mode B, minimal** (reads the locator `run` exports, no `--output-var` needed):

```hcl
locals {
  tunnel = try(jsondecode(file(get_env("TUNSTRAP_OUTPUT_FILE"))), { nodes = {} })
}
provider "kubernetes" {
  config_path = local.tunnel.nodes.edge1.kube.k3s.path
}
```

Real captured shape of that JSON (`tunstrap run --input-env ... --output-var
TF_VAR_tunstrap -- ...`, live-run against a test fixture) — note `kube` carries
only `{path, context, endpoint}`, never key material:

```json
{
  "session": {"session_dir": "...", "pid": 123, "started_at": "...", "warnings": []},
  "nodes": {
    "edge1": {
      "ports": {},
      "kube": {"k3s": {"path": "/tmp/.../tunnel-data/kube-edge1-k3s",
                        "context": "tunstrap-edge1-k3s",
                        "endpoint": "https://127.0.0.1:38667"}},
      "fetch_files": {}
    }
  }
}
```

## Three traps that will burn a first attempt

1. **The saved-plan hazard.** A `-out=plan.tfplan` file freezes whatever it
   read at plan time. Mode B's whole contract is **one-shot `plan && apply`,
   same tunstrap invocation, no exceptions** — a saved plan reused after
   tunstrap restarts (fresh ports, fresh session dir) will not apply cleanly
   against the new state, and the locator file itself is deleted at teardown
   anyway. Mode A does not have this problem — see the table above. If you
   must save a plan, either apply it inside the same `run` invocation that
   produced it, or use Mode A for anything that must survive across a saved
   plan.

2. **`--output-var` / `--input-env` name collisions are usage errors (exit
   64), checked before any daemon spawns** — verified live:

   ```console
   $ tunstrap run --input-env TUNSTRAP_INPUT --output-var TUNSTRAP_INPUT -- tofu plan
   Usage: tunstrap run [OPTIONS] [ARGS]...
   Try 'tunstrap run --help' for help.

   Error: --output-var TUNSTRAP_INPUT collides with --input-env TUNSTRAP_INPUT
   $ echo $?
   64
   ```

   The same guard also rejects a `NAME` that collides with a key `run` itself
   injects/scrubs (`TUNSTRAP_SESSION_DIR`, `TUNSTRAP_PID`,
   `TUNSTRAP_OUTPUT_FILE`, `KUBECONFIG`, `KUBE_CONFIG_PATH`,
   `KUBE_CONFIG_PATHS`) and any `NAME` that fails
   `[A-Za-z_][A-Za-z0-9_]*` — both confirmed live, same exit code. No daemon is
   ever started on any of these three rejections
   (`tunstrap/cli.py::_validate_output_var`).

3. **`content_b64` is not a mistake — it is the only channel for
   unmaterialized secrets.** With `daemon.materialize: false` (the default for
   `start`; `run` always forces materialize), stdout is the *only* delivery
   path for a patched kubeconfig or fetched file, so that entry keeps its full
   `content_b64` inline. Once materialized, the same entry drops to
   `{path, context, endpoint}` (kube) or `{path, size, sha256}` (fetch) with no
   inline content at all (`tunstrap/envrender.py::render_start_json`).
   Consequence for an agent: **never assume `path` is non-null** unless you
   passed `--materialize` (or are inside `run`, which always materializes) —
   check for `content_b64` and treat it as secret material if present (do not
   echo it into a log). Regression coverage:
   `tests/unit/test_cli_runner.py::test_start_json_materialized_kubeconfig_never_prints_credential_content`
   and `tests/unit/test_cli_runner.py::test_start_json_unmaterialized_kubeconfig_keeps_stdout_delivery`.

## Exit codes worth checking in a script

| Code | Meaning | Source |
|---|---|---|
| `64` | Usage error (bad flags, `--input-env` conflicts, `--output-var` collision) | `_UsageExit64`, sysexits `EX_USAGE` |
| `1` | Schema/payload validation failed before any daemon started | `tunstrap/exceptions.py::SchemaValidationError` |
| `2` | A required tunnel failed to start | `tunstrap/exceptions.py::RequiredTunnelFailure` — reproduced live against an unreachable port |
| `3` | `SessionActive` — a live daemon already holds this `--session-dir` | `tunstrap/exceptions.py::_EXIT_CODES` |
| `4` | Daemon-side error (includes any unexpected post-spawn failure) | `tunstrap/exceptions.py::DaemonError` |
| `127` | `run`'s child command could not be launched (not tunstrap's own failure) | `tunstrap/cli.py::_run_child` |
| *child's own code* | `run` always propagates the child's exit code on a normal exit | `tunstrap/cli.py::_run_command` |

`run` never writes to stdout past the point the child starts — stdout belongs
to the child exclusively, so a script piping `run`'s stdout is safe to treat
as the child's output alone. All tunstrap diagnostics (including teardown
warnings) go to stderr and never change the exit code.

## Copy-pasteable fast checks (all run live during this doc's verification)

```bash
tunstrap --help
tunstrap run --help

# Fast paths never touch the tunnel or parse TUNSTRAP_INPUT:
TUNSTRAP_INPUT=garbage tunstrap_tofu init      # succeeds, no tunnel
TUNSTRAP_INPUT=garbage tunstrap_tofu -version  # succeeds, no tunnel

# Everything else tunnels and therefore requires valid JSON:
TUNSTRAP_INPUT=garbage tunstrap_tofu plan      # exit 1, SchemaValidationError
```

## Wiring it up

The Terragrunt `terraform_binary` plumbing (root.hcl, `extra_arguments`, the
`commands` allow-list, and why `dependency.*` must stay in `env_vars` not
`locals`) is unchanged from and fully covered by
[`docs/recipe_terragrunt.md`](recipe_terragrunt.md). Do not re-derive it here
— follow that recipe's "Wiring it into Terragrunt" section, then come back to
this page for the Mode A/B decision and the traps above.
