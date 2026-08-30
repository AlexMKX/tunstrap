# CLI run modes: flag input, env output, and the `run` wrapper (issues #6, #5)

- Status: design, awaiting review
- Date: 2026-06-25
- Issues: [#6 add CLI mode](https://github.com/AlexMKX/tunstrap/issues/6), [#5 add subcommand invocation feature](https://github.com/AlexMKX/tunstrap/issues/5)
- Scope: Tasks B (#6) and C (#5) of the CLI roadmap. Builds on Task A (#7, session reuse) — already merged.
- Depends on naming reserved by Task A: exit code 3 = `SessionActive`; env prefix `TUNSTRAP_`.

## Problem

Today `tunstrap start` only accepts an `InputSchema` JSON on **stdin** and only
emits an `OutputSchema` JSON on stdout. Two gaps:

- **#6** — to connect a single server you must hand-build JSON. There should be
  a flag-driven single-node mode, and the result should be consumable as either
  JSON (today) or shell `env` exports.
- **#5** — there should be a one-shot wrapper that opens the tunnel, injects the
  connection info as environment variables, runs a child command against it, and
  tears the tunnel down when the child exits.

## Commands (to-be)

Three behaviours across two lifecycles. `start`/`stop`/`status` remain the
**background** lifecycle; `run` is the **foreground** wrapper.

```
tunstrap start [USER@HOST[:PORT]] [conn-flags] [--output json|env] [--session-dir P]
tunstrap run    USER@HOST[:PORT]  [conn-flags] [--session-dir P] -- CMD [ARGS...]
tunstrap stop   --session-dir P [--grace-seconds N]      # unchanged
tunstrap status --session-dir P                          # unchanged
```

- `start` with a connection arg → flag mode; without it → existing stdin-JSON
  mode (unchanged). Background: the daemon stays up; the caller stops it later.
- `run` always uses flag mode, always ends in `-- CMD`. Foreground: opens the
  tunnel, runs `CMD` with injected env, then stops + cleans up.

### Connection shorthand `USER@HOST[:PORT]`

A single SSH-style endpoint string parsed into the node's `host`, `user`, and
`port`:
- `USER@` is **required** (no implicit local user).
- `:PORT` is optional, default `22`.
- IPv6 hosts use brackets: `user@[2001:db8::1]:22`.

The internal `InputSchema` node key is the fixed string `"node"` (the node-key
grammar forbids leading digits/dots/colons, so an IPv4/IPv6 literal host cannot
be the key). The host is preserved in the node body; `OutputSchema.connections`
is keyed by `"node"` in single-node CLI mode.

### Connection flags (full single-node parity with `InputSchema`)

- Auth (exactly one): `--ssh-key PATH` (read file → `ssh_pkey`), or
  `--ssh-password-stdin` (read password from stdin). `--ssh-key-passphrase TEXT`
  optionally accompanies `--ssh-key`.
- `--target NAME=HOST:PORT` (repeatable) → `remote_targets`.
- `--kube NAME=/abs/remote/path` (repeatable) → `kube_targets`.
- `--fetch NAME=/abs/remote/path` (repeatable) → `fetch_files`.
- Daemon: `--auto-stop-idle-seconds N`, `--materialize`, `--log-file PATH`.

## Schema change: allow kube-only / fetch-only nodes

Today `NodeInput._validate_remote_targets` (schemas.py) requires **≥1**
`remote_targets`, which would force a dummy forward for the headline #5 case
`run user@host --kube k3s=... -- helm list`. But `--kube` is self-sufficient:
`run_kube_targets` derives the API host:port from the kubeconfig's `server:`
field (kube.py:295 `_split_host_port`) and opens its own OS-assigned local
forward (kube.py:301 `forward_local_port("127.0.0.1", 0, host, port)`) — no
`remote_target` needed. `remote_targets` is only for non-kube ports.

Relax the rule: `remote_targets` may be **empty/absent** as long as the node
does at least one thing (i.e. `remote_targets` OR `kube_targets` OR
`fetch_files` is non-empty). A node that requests nothing is still rejected.
This applies uniformly to JSON-on-stdin and CLI flag mode (the README's
`remote_targets: {}` kube example becomes valid).

## New module: `tunstrap/cli_input.py`

A reusable Click option group + a builder that both `start` and `run` use.

```python
def build_single_node_schema(
    *,
    connection: str,            # "user@host[:port]"
    ssh_key: str | None,
    ssh_key_passphrase: str | None,
    ssh_password: str | None,   # already read (from --ssh-password-stdin)
    targets: tuple[str, ...],   # ["api=127.0.0.1:6443", ...]
    kube: tuple[str, ...],
    fetch: tuple[str, ...],
    daemon_opts: DaemonOptions,
) -> InputSchema
```

- Parses `connection` into `host`/`user`/`port` (rejects missing user, bad port,
  malformed brackets with `SchemaValidationError`).
- Parses each `NAME=VALUE` (rejects empty name, missing `=`, duplicate names).
- Reads `--ssh-key` file content; mutually-exclusive auth enforced.
- Returns a one-node `InputSchema`; reuses existing Pydantic validators (so the
  same constraints as JSON input apply).

## New module: `tunstrap/envrender.py`

```python
def render_env(output: OutputSchema) -> dict[str, str]
def format_exports(env: dict[str, str]) -> str   # "export K='V'\n..." shell-safe
```

Single-node env contract (no `<NODE>` segment):

```
TUNSTRAP_SESSION_DIR = <session_dir>
TUNSTRAP_PID         = <pid>
# per remote target (key from kube/target name, sanitized: upper, non-alnum->_):
TUNSTRAP_<TARGET>_HOST     = 127.0.0.1
TUNSTRAP_<TARGET>_PORT     = <local_port>
TUNSTRAP_<TARGET>_ENDPOINT = 127.0.0.1:<local_port>
# per kube target:
TUNSTRAP_<KUBE>_KUBECONFIG = <materialized 0600 path>
TUNSTRAP_<KUBE>_ENDPOINT   = https://127.0.0.1:<local_port>
# aggregate, consumed by BOTH kubectl and helm:
KUBECONFIG = <colon-joined materialized kube paths>
```

- `render_env` operates on the single node in `output.connections` (asserts
  exactly one in single-node modes).
- Name sanitization: uppercase, every non-`[A-Z0-9]` → `_`. Duplicate sanitized
  keys are a build-time error (caught in tests).
- `format_exports` single-quotes values with POSIX-safe escaping so
  `eval "$(...)"` is safe.

### Why `KUBECONFIG` to a file

Verified locally: `helm env` lists `$KUBECONFIG` ("set an alternative Kubernetes
configuration file") and kubectl reads `KUBECONFIG` (colon-separated list of
file **paths**, merged; current-context taken from the first). There is no
inline-content env var. Therefore env/`run` modes **must materialize** the
patched kubeconfig(s) to disk and point `KUBECONFIG` at them. For one kube
target this is a single path; for several, colon-joined (first wins on context).

## Materialization in env / run modes

`--output env` and `run` force kube materialization regardless of
`--materialize`: each kube target's patched kubeconfig is written 0600 into
`tunnel-data/`, path exposed via `TUNSTRAP_<KUBE>_KUBECONFIG` + `KUBECONFIG`.

`start --output json` keeps today's behaviour: content returned as base64 in
the JSON, materialized only if `--materialize` was passed.

**Fetch files in env mode (deferred):** `FetchedFile` carries content as base64,
not a filesystem path, and is not materialized with a recorded path. So
`--output env`/`run` do **not** emit fetch-file paths. `--fetch` is still a valid
input flag; its contents are available via `--output json`. Exposing fetch paths
via env would require a `FetchedFile.path` field + manager materialization —
out of scope here (see Out of scope).

## `run` lifecycle (#5)

```
tunstrap run user@host --kube k3s=/etc/rancher/k3s/k3s.yaml -- helm list
```

1. Build the single-node `InputSchema` (via `cli_input`), forcing `materialize`.
   Default session dir = generated temp (auto-removed on cleanup); `--session-dir`
   optional.
2. `spawn_daemon(schema, session_dir)` → IPC message.
   - On `required_failure`/`daemon_error`/`session_active` → exit 2/4/3, no child.
3. `render_env(output)` → merge over `os.environ`.
4. Run the child: `subprocess.run(cmd, env=merged)`. The child owns
   stdout/stderr/stdin; `run` prints nothing to stdout (errors → stderr).
5. Signal forwarding: `SIGINT`/`SIGTERM` received by `run` are forwarded to the
   child; `run` then proceeds to teardown.
6. **`finally`: guaranteed teardown** — `stop` the daemon (SIGTERM + grace, then
   SIGKILL) and clean up, even on exception/signal.
7. `sys.exit(child.returncode)` — the child's code is the process result.

Exit-code rule: tunnel-setup failures before the child use tunstrap codes
(1 schema, 2 required, 3 session-active, 4 daemon). Once the child runs, its exit
code wins (may overlap with tunstrap codes — that is expected and documented).

## Conflict validation (usage errors, exit 64)

The CLI rejects contradictory invocations with a clear message:

- `start` does **not** accept a trailing command. `--`/extra args →
  usage error: "`--` invokes a child command; use `tunstrap run ... -- CMD`".
- `run` **requires** `-- CMD`; missing command → usage error.
- `run` does **not** accept `--output` (it injects env into the child).
- `start`: in flag mode (connection arg present) stdin is **not** read as JSON;
  passing both a connection arg and stdin-JSON is rejected →
  "cannot mix a CLI connection with stdin JSON". Exception: `--ssh-password-stdin`
  legitimately consumes stdin as the password (one line), which is the only
  stdin reader in flag mode. `--ssh-password-stdin` and stdin-JSON mode are
  therefore incompatible (no connection arg ⇒ no password flag).
- Connection flags (`--target`/`--kube`/`--fetch`/`--ssh-*`/`--ssh-password-stdin`)
  require a connection arg; present without it → usage error.
- `--output` is only valid for `start` (default `json`).

## Components touched

| File | Change |
|------|--------|
| `tunstrap/schemas.py` | Relax `NodeInput` validation: allow empty `remote_targets` when `kube_targets`/`fetch_files` present; reject a node that does nothing. |
| `tunstrap/cli_input.py` | New: connection/flag parsing → `InputSchema` (single node). |
| `tunstrap/envrender.py` | New: `render_env` + `format_exports`. |
| `tunstrap/cli.py` | `start` gains connection arg + conn-flags + `--output`; new `run` command; conflict validation. |
| `tunstrap/session.py` / `manager.py` | Ensure fetch-file materialization path exists for env/run (extend existing kube materialize hook to fetched files). |
| `README.md` | Document flag mode, `--output env` (`eval "$(...)"`), and `run`. |

No changes to the IPC protocol, the daemon, or the lock — those are stable from
Task A.

## Error handling

- Malformed `USER@HOST:PORT` / `NAME=VALUE` → `SchemaValidationError` (exit 1).
- Conflicting invocation → usage error (exit 64).
- `run` child not found / non-zero → child's exit code propagates; teardown still
  runs.
- `run` interrupted (Ctrl-C) → child gets the signal, teardown runs, exit code
  reflects the child's signal-termination.

## Testing (TDD)

Unit:
- `cli_input`: `user@host:port` parsing (incl. default port, IPv6 brackets,
  missing user, bad port); `NAME=VALUE` parsing (dup/empty/missing `=`);
  ssh-key file read; mutually-exclusive auth; resulting `InputSchema` shape.
- `envrender`: name sanitization, collision detection, `KUBECONFIG` colon-join,
  empty target/kube/fetch sections, `format_exports` quoting safety.
- `cli` conflict matrix: `start --` rejected; `run` without `--` rejected;
  `run --output` rejected; connection+stdin rejected; conn-flags without
  connection rejected.
- `run` (mocked daemon): env merged into child; child exit code propagated;
  teardown called in `finally` on success, on child failure, and on exception.

Integration (Docker):
- `start user@host --kube k3s=... --output env` → `eval` → `kubectl get nodes`
  and `helm list` succeed; daemon still up; `stop --session-dir` cleans up.
- `run user@host --kube k3s=... -- kubectl get nodes` → exits 0, tunnel torn
  down (session dir gone), no leaked daemon.
- `run ... -- sh -c 'exit 7'` → tunstrap exits 7, teardown ran.

## Out of scope

- Multi-node CLI input (single-node only; multi-node stays JSON-on-stdin).
- Changing the background `start` stdin contract or the daemon/lock internals.
- Fetch-file paths in env/run mode (no `FetchedFile.path`; use `--output json`).
