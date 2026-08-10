# tunstrap

> Open N SSH local-forward tunnels, fetch small remote config files, and
> produce ready-to-use kubeconfigs — all in a single bootstrap. Built for
> disposable CI / operator environments that talk to k3s or similar internal
> services without public ingress.

**Audience:** infrastructure engineers running short-lived jobs (CI, local
containers, Terragrunt hooks) that need SSH-tunneled access plus a kubeconfig
(or similar config file) from one or more remote hosts. The tool is generic
— it does not depend on Kubernetes — but the motivating use case is k3s edge
nodes whose apiserver binds to `127.0.0.1` only.

## Why this exists

- Internal apiservers (k3s, gitea, registries) are not publicly exposed; SSH
  is the only audited path in.
- Tools like `helm` / `kubectl` need an endpoint **and** a kubeconfig at
  plan/apply time — pulling both in one bootstrap avoids a second
  authentication and a second sshd session.
- Ephemeral environments cannot rely on persistent SSH setup, agent
  forwarding, or pre-installed kubeconfigs.
- Raw kubeconfigs from k3s point `server:` at the apiserver's own address
  (often `127.0.0.1:6443`) and carry a TLS certificate whose SAN does not
  include `127.0.0.1` — consumers previously had to rewrite `server:` and
  also determine the correct `tls-server-name` themselves. `kube_targets`
  handles both.

## Install

`uvx` (recommended for one-shot / disposable use — no install needed):

```bash
uvx --from git+https://github.com/AlexMKX/tunstrap.git tunstrap --help
```

`pipx` (persistent install):

```bash
pipx install git+https://github.com/AlexMKX/tunstrap.git
```

For development:

```bash
git clone https://github.com/AlexMKX/tunstrap.git && cd tunstrap
pip install -e ".[dev]"
```

Requires Python >= 3.10. Linux and macOS supported; Windows works via WSL only.

## End-to-end example

### Using `kube_targets` (recommended for k3s / Kubernetes)

```bash
#!/usr/bin/env bash
set -euo pipefail

SESSION_DIR=$(mktemp -d)
PRIVATE_KEY=$(cat ~/.ssh/id_ed25519)

JSON=$(cat <<EOF
{
  "nodes": {
    "edge1": {
      "host": "198.51.100.10",
      "user": "root",
      "ssh_pkey": $(jq -Rs . <<<"$PRIVATE_KEY"),
      "remote_targets": {},
      "kube_targets": {
        "k3s": {"kubeconfig_path": "/etc/rancher/k3s/k3s.yaml"}
      },
      "required": true
    }
  },
  "daemon": {
    "auto_stop_idle_seconds": 600
  }
}
EOF
)

RESULT=$(echo "$JSON" | tunstrap start --session-dir "$SESSION_DIR")

PORT=$(jq -r '.connections.edge1.kube_targets.k3s.local_port' <<<"$RESULT")
TLS_NAME=$(jq -r '.connections.edge1.kube_targets.k3s.tls_server_name' <<<"$RESULT")
KUBECONFIG_B64=$(jq -r '.connections.edge1.kube_targets.k3s.content_b64' <<<"$RESULT")

KUBECONFIG_FILE=$(mktemp)
base64 -d <<<"$KUBECONFIG_B64" >"$KUBECONFIG_FILE"
# server: and tls-server-name are already patched — no sed step needed

kubectl --kubeconfig="$KUBECONFIG_FILE" get nodes

tunstrap stop --session-dir "$SESSION_DIR"
rm -f "$KUBECONFIG_FILE"
```

### `start` JSON output and credential-bearing entries

With the default `daemon.materialize: false`, `start --output json` carries
`content_b64` on stdout because it is the only delivery channel for the patched
kubeconfig or fetched file. These values can include operator-chosen secrets.
Treat this mode's stdout as secret material: do not send it to CI logs or durable
shell captures.

Set `daemon.materialize: true` (or pass `--materialize` in flag mode) when the
session directory is an acceptable credential location. Each patched kubeconfig
and fetched file is then written mode `0600` under `tunnel-data/`. Materialized
kube targets contain only `{path, context, endpoint}`; materialized fetched
files contain only `{path, size, sha256}`. Neither form carries inline content.

The `daemon.auto_stop_idle_seconds: 600` setting makes the daemon shut
itself down after 10 minutes with no client connections. Useful for
ephemeral CI runs that may abort before reaching `tunstrap stop`.
Omit the field (or set to `null`) to keep the daemon alive until you call
`stop` explicitly.

### Using `fetch_files` (generic byte fetch)

```bash
RESULT=$(echo "$JSON" | tunstrap start --session-dir "$SESSION_DIR")
KUBECONFIG_B64=$(jq -r '.connections.edge1.fetch_files.kubeconfig.content_b64' <<<"$RESULT")
KUBECONFIG_FILE=$(mktemp)
base64 -d <<<"$KUBECONFIG_B64" >"$KUBECONFIG_FILE"
# you must patch server: and determine tls-server-name yourself
sed -i "s|server: https://127.0.0.1:6443|server: https://127.0.0.1:${PORT}|" \
    "$KUBECONFIG_FILE"
tunstrap stop --session-dir "$SESSION_DIR"
```

With `daemon.materialize: true` (or `--materialize` in flag mode), read the
fetched file from `fetch_files.kubeconfig.path` instead; see [`start` JSON
output and credential-bearing entries](#start-json-output-and-credential-bearing-entries).

## CLI run modes (flag input, `--output env`, `run`)

Besides the JSON-on-stdin interface above, a single remote host can be driven
entirely from command-line flags — no JSON required.

### Flag mode (`start USER@HOST[:PORT]`)

```bash
tunstrap start root@edge1.example.net \
  --ssh-key ~/.ssh/id_ed25519 \
  --target api=127.0.0.1:6443 \
  --kube k3s=/etc/rancher/k3s/k3s.yaml
```

- `USER@HOST[:PORT]` sets the SSH user, host, and port (default `22`). IPv6
  literals are bracketed: `root@[2001:db8::1]:6443`.
- Repeatable `--target NAME=HOST:PORT` opens a local forward; `--kube
  NAME=/abs/path` and `--fetch NAME=/abs/path` mirror `kube_targets` /
  `fetch_files`.
- Auth: `--ssh-key <file>` (optionally `--ssh-key-passphrase`) **or**
  `--ssh-password-stdin` (the password is read from the first stdin line).
  When neither flag is given, tunstrap uses keys from the running ssh-agent
  (via `$SSH_AUTH_SOCK`).
- Daemon knobs: `--auto-stop-idle-seconds`, `--materialize`, `--log-file`,
  `--session-dir`.

> The connection host becomes the schema node key, which must match
> `^[a-zA-Z_][a-zA-Z0-9_-]*$`. Use a hostname (e.g. `localhost`,
> `edge1.example.net`) rather than a bare IP literal in flag mode.

### `--output env` (consume via `eval`)

`start` defaults to `--output json`. With `--output env` it instead prints
POSIX `export` lines and force-materializes both patched kubeconfigs and
fetched files under `tunnel-data/` (mode 0600; see [On-disk
materialization](#on-disk-materialization)), ready for `eval`:

```bash
eval "$(tunstrap start root@edge1 --ssh-key ~/.ssh/id_ed25519 \
  --target api=127.0.0.1:6443 --kube k3s=/etc/rancher/k3s/k3s.yaml --output env)"

kubectl get nodes          # KUBECONFIG is exported automatically

tunstrap stop --session-dir "$TUNSTRAP_SESSION_DIR"
```

Variables emitted:

| Variable | Meaning |
|---|---|
| `TUNSTRAP_SESSION_DIR` | Session dir — pass to `stop --session-dir`. |
| `TUNSTRAP_PID` | Daemon PID. |
| `TUNSTRAP_OUTPUT_FILE` | Absolute path to the materialized unified output JSON. |
| `KUBECONFIG` | Colon-joined materialized paths of all kube targets; emitted when at least one kube file exists. |
| `KUBE_CONFIG_PATH` | Same value as `KUBECONFIG`; emitted with exactly one kube file. It takes precedence over `KUBE_CONFIG_PATHS` in the OpenTofu providers. |
| `KUBE_CONFIG_PATHS` | Same value as `KUBECONFIG`; emitted with two or more kube files. It is not emitted with `KUBE_CONFIG_PATH`. |

### `run` (foreground wrapper with guaranteed teardown)

`run` opens the tunnel and injects the same session scalars and kube channel
(`KUBECONFIG` / `KUBE_CONFIG_PATH` / `KUBE_CONFIG_PATHS`, as applicable) into a
child command. It always removes inherited `KUBECONFIG`,
`KUBE_CONFIG_PATH`, and `KUBE_CONFIG_PATHS` before starting the child, even
when the input has no kube targets. A direct `tunstrap run host -- kubectl ...`
therefore does not retain an unrelated operator kube configuration.

`run` waits for the child and then **always attempts teardown** (even if the
child crashes or fails to launch). Teardown normally
stops the daemon and removes the session data. When it cannot confirm the stop
— the stop reports a failure, raises, or the recorded identity is unreadable —
it **keeps** the session data instead and prints the `tunstrap stop
--session-dir …` command that finishes the job, rather than destroying the only
handle on a daemon that may still be running. Either way the child's exit code
is never changed by teardown:

`stop` follows the same rule, so that recovery command is safe to run and safe
to repeat: it removes `tunnel-data` only after a confirmed stop (or a `not
found`, which means no daemon is recorded), and otherwise leaves the session
data in place, adds `"preserved": true` to its JSON line and explains itself on
stderr. That includes the cases where it cannot read
`tunnel-data/daemon.pid` at all — missing, unreadable or malformed: `stop`
deletes nothing on any of them, so all three report `"preserved": true` too.
`stop` exits 0 only for the three outcomes that clean `tunnel-data`: stopped,
forced, and `not found`. It exits 1 for every preserved outcome: identity
mismatch, identity check unavailable, still alive, identity changed during
grace, and the three identity-read failures (missing, unreadable, or malformed
`daemon.pid`). Repeating an unresolved recovery command keeps returning 1 until
the session is resolved by hand; the loop's behaviour is unchanged, only its
status, and repetition could never resolve a preserved session on its own.
`"preserved"` is therefore present on exactly the outcomes that kept data, and
absent on exactly those that cleaned it; the three cleaning shapes
(`{"stopped": true}`, `{"stopped": true, "forced": true}` and
`{"stopped": false, "reason": "not found"}`) are unchanged to the byte, so a
strict-schema consumer of those is unaffected. Two outcomes it cannot resolve on its own are `identity mismatch` and
`identity check unavailable` — the recorded pid can no longer be verified as
ours, so `stop` refuses to signal it rather than risk killing an unrelated
process. Re-running will keep reporting the same thing; that is the case the
preserved `tunnel-data/daemon.pid` exists for, and it has to be resolved by
hand:

```bash
tunstrap run root@edge1 \
  --ssh-key ~/.ssh/id_ed25519 \
  --kube k3s=/etc/rancher/k3s/k3s.yaml \
  -- helm list
```

Everything after `--` is the child command and its arguments. `SIGINT` /
`SIGTERM` are forwarded to the child.

**`--` is mandatory** whenever the child command or any of its arguments
begins with `-`. Without it Click parses those tokens as tunstrap's own
options: `tunstrap run --input-env X tofu -version` fails with
`No such option: '-v'`. Everything after `--` reaches the child verbatim,
including flags spelled like tunstrap's own.

#### Env input: `--input-env VAR`

`run` can take the complete `InputSchema` as JSON from an environment
variable instead of from a connection argument and flags. This is the only
out-of-band input channel a foreground wrapper has: `run`'s child inherits
stdin, so stdin is not available to `run` as a control channel.

```bash
TUNSTRAP_INPUT="$(cat payload.json)" \
  tunstrap run --input-env TUNSTRAP_INPUT -- helm list
```

In this mode there is no `USER@HOST[:PORT]` argument — every token after `--`
is the child command. The following are usage errors (exit `64`), because the
payload's own `nodes` and `daemon` blocks are complete and authoritative and
there must be exactly one place to look:

- any connection flag: `--ssh-key`, `--ssh-key-passphrase`,
  `--ssh-password-stdin`, `--target`, `--kube`, `--fetch`;
- any daemon flag: `--auto-stop-idle-seconds` (use `daemon.auto_stop_idle_seconds`),
  `--grace-seconds` (use `daemon.shutdown_grace_seconds`),
  `--log-file` (use `daemon.log_file`), `--materialize` (redundant, see below).

Payload problems are exit `1` with a `SchemaValidationError` envelope on
**stderr**: the variable unset, empty or whitespace-only; its content not
JSON; or the JSON not satisfying `InputSchema`. All of these are decided
before any daemon is started.

**`run` always materializes.** It overrides `daemon.materialize` to `true`
even when the payload sets it to `false`. `KUBECONFIG` injection needs a real
file on disk, and an unmaterialized kube target would give `--output-var`
consumers `"path": null`. This is the one place `run` modifies the supplied
schema.

#### Structured output: `--output-var NAME`

`--output-var NAME` puts a JSON-encoded, node-keyed unified structure under
`NAME`, alongside the three session scalars and the kube channel when kube
files exist. Its top-level keys are `session` and `nodes`; each node contains
`ports`, `kube`, and `fetch_files`.

Each `nodes.<node>.ports.<target>` value is the string
`"127.0.0.1:<local_port>"`, not an integer. The Terragrunt/OpenTofu recipe
extracts the port with `split(":", ...)[1]`; see
[`docs/recipe_terragrunt.md`](docs/recipe_terragrunt.md).

`run` always materializes. The unified structure carries kube references as
`{path, context, endpoint}` and fetched-file references as `{path, size,
sha256}` (or `{error}` for an optional fetch failure). It does not carry file
content or kube credentials. The documented consumer binds `NAME` to a
Terraform variable, while `path` names the on-disk file a consumer can read.
`tunstrap start --output json` likewise projects materialized kube and
fetched-file entries; only an unmaterialized `start` envelope carries inline
content on stdout.

```bash
tunstrap run --input-env TUNSTRAP_INPUT --output-var TF_VAR_tunstrap \
  -- tofu plan
```

The scalar environment carries session bookkeeping and kube file locations; it
does not describe ports, warnings, or node-qualified metadata. `--output-var`
is the node-keyed structured channel for that metadata.

- The variable named by `--input-env` is **removed** from the child's
  environment. It holds the `InputSchema`, whose `ssh_pkey` is an SSH private
  key, and the child (`tofu`) would otherwise pass it to every provider
  plugin, `external` data source and `local-exec` provisioner.

- `NAME` must match `[A-Za-z_][A-Za-z0-9_]*`, else exit `64`.
- `NAME` may not collide with a variable `run` itself injects or scrubs, else
  exit `64`.
  Collision with an unrelated inherited variable is a documented overwrite.
- **Any node count:** the three session scalars are injected, as is the
  cardinality-appropriate kube channel when kube files exist; `NAME` is added
  if given. Ports remain available in the unified JSON or its materialized file,
  not as target-scoped environment variables.

> This is the flag the Terragrunt/OpenTofu recipe builds on — it puts the
> connection envelope (kube credentials projected out) into `TF_VAR_tunstrap`
> for a module to decode. See
> [`docs/recipe_terragrunt.md`](docs/recipe_terragrunt.md).

#### `run` never writes to stdout

After the child starts, `run` writes nothing to file descriptor 1 — stdout
belongs exclusively to the child. Every tunstrap diagnostic, including
teardown failures, goes to stderr, and a teardown failure never changes the
exit code. (`tunstrap stop` is unaffected: its JSON line on stdout is still
its documented contract.)

**Exit codes (`run`):** the child's exit code wins on success. Before the
child runs, `run` may exit with `64` (usage error, including every row of the
`--input-env` conflict matrix and an invalid or colliding `--output-var`),
`1` (bad `--input-env` payload),
`2` (required tunnel failure), `3` (a live session already holds the
requested `--session-dir`), or `4` (daemon error). `127` if the child binary
cannot be launched, and `4` for any unexpected failure after the tunnel came
up — in which case the daemon has already been torn down.

## Input reference (`InputSchema`)

**Top level**

| Field | Type | Default | Description |
|---|---|---|---|
| `nodes` | `dict[str, NodeInput]` | required | One entry per remote host |
| `daemon.log_file` | `str \| null` | `null` | If set, daemon's stdout/stderr go here. Never contains fetched content. |
| `daemon.shutdown_grace_seconds` | `int` | `10` | SIGTERM grace period before SIGKILL |
| `daemon.startup_timeout_seconds` | `int` | `300` | Bounds the parent's wait for the worker's startup IPC frame. On expiry the parent terminates the worker within `shutdown_grace_seconds`. Must exceed a node's worst-case startup: `fetch_files` and `kube_targets` are fetched serially, each bounded by `ssh_options.connect_timeout`. |
| `daemon.auto_stop_idle_seconds` | `int \| null` | `null` | Seconds of idle (no active forward connections) before the daemon SIGTERMs itself. `null` disables. |
| `daemon.materialize` | `bool` | `false` | Write patched kubeconfig files to `<session-dir>/tunnel-data/` (mode 0600). See [On-disk materialization](#on-disk-materialization). |

**`NodeInput`** (per entry in `nodes`)

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | `str` | required | Remote SSH hostname or IP |
| `port` | `int` | `22` | Remote SSH port |
| `user` | `str` | required | Remote SSH user |
| `ssh_pkey` | `str \| null` | `null` | PEM-encoded private key (in-memory, never written) |
| `ssh_password` | `str \| null` | `null` | Password fallback. If neither `ssh_pkey` nor `ssh_password` is set, keys from `$SSH_AUTH_SOCK` (ssh-agent) are used; if the agent is also unavailable, schema validation fails. |
| `ssh_pkey_passphrase` | `str \| null` | `null` | Optional passphrase for `ssh_pkey` |
| `remote_targets` | `dict[str, str] \| null` | `null` | Up to 16 entries; each value is `"host:port"`. Host is resolved on the SSH server side, enabling bastion-style cross-host forwards. |
| `ssh_options.compression` | `bool` | `false` | Enable SSH compression |
| `ssh_options.connect_timeout` | `int` | `60` | Seconds for connection establishment and each SFTP file fetch. |
| `required` | `bool` | `true` | If false, this node may fail without aborting `start` |
| `fetch_files` | `dict[str, FileSpec] \| null` | `null` | Files to read at start (max 16) |
| `kube_targets` | `dict[str, KubeTarget] \| null` | `null` | Kubernetes clusters to access via the SSH tunnel (max 16). See [Kube mode](#kube-mode-kube_targets). |

**`FileSpec`** (per entry in `fetch_files`)

| Field | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | Absolute remote path (no `~`, no `$VAR` expansion) |
| `required` | `bool` | `true` | If false, fetch failure does not fail the node |

Constraints:
- `fetch_files` / `kube_targets` logical name: `^[a-zA-Z_][a-zA-Z0-9_-]*$`, 1..64 chars
- `FileSpec.path` / `KubeTarget.kubeconfig_path`: starts with `/`, 1..4096 chars
- Per-file size cap: 1 MiB (exceeded → `EFBIG`)
- Host key verification: **not enforced** in this release. Use on trusted
  networks or with disposable hosts.

## Kube mode (`kube_targets`)

`kube_targets` is the high-level interface for k3s / Kubernetes access. For
each entry the tool:

1. Fetches the remote kubeconfig over SFTP (same 1 MiB cap as `fetch_files`).
2. Reads the `current-context` and extracts the associated cluster + user.
3. Resolves the `server:` host on the SSH-server side (split-horizon DNS
   correct).
4. Opens a local forward `127.0.0.1:<os-assigned>` → apiserver.
5. Probes the apiserver's TLS certificate SAN to choose a `tls-server-name`.
6. Rewrites `server:` to `https://127.0.0.1:<local_port>` and injects
   `tls-server-name`. Other clusters in the file are byte-stable.
7. Renames the `current-context`'s context, cluster, and user to the
   deterministic `tunstrap-<node>-<target>` -- the consumer-facing literal
   documented in `docs/recipe_terragrunt.md`. Non-selected contexts keep
   their own names; their `cluster`/`user` references are rewritten when they
   point at the renamed cluster/user. A fetched kubeconfig that already
   contains `tunstrap-<node>-<target>` in any `clusters`, `users`, or
   `contexts` entry is rejected: the target fails (subject to `required`)
   rather than the name being uniquified, because the deterministic name is a
   contract consumers rely on.
8. Returns the patched kubeconfig plus already-extracted fields
   (`endpoint`, `certificate_authority_data`, `client_certificate_data`,
   `client_key_data`, `tls_server_name`).

**One cluster per target.** Only the `current-context` triple (its context,
cluster, and user) is selected and renamed; other contexts keep their own
names, but their `cluster`/`user` references are rewritten when they point at
the renamed cluster/user. To access two clusters, use two
`kube_targets` entries. If the kubeconfig contains more than one context, a
`warnings[]` entry names the ignored contexts.

**`KubeTarget`** (per entry in `kube_targets`)

| Field | Type | Default | Description |
|---|---|---|---|
| `kubeconfig_path` | `str` | required | Absolute remote path to the kubeconfig file |
| `tls_server_name` | `str \| null` | `null` | Explicit TLS server name hint. If set, overrides the SAN probe entirely. |
| `insecure_fallback` | `bool` | `false` | See below. |
| `required` | `bool` | `true` | If false, this target's failure does not fail the node. |

**TLS server name selection.** When `tls_server_name` is not set, the tool
probes the apiserver certificate SAN and selects in order:

1. The original `server:` host, if it appears in the SAN.
2. The first DNS-type SAN.
3. The first IP-type SAN.

If the selected name is not an exact match of the original `server:` host (a
fallback fired), a `warnings[]` entry records the chosen SAN.

**`insecure_fallback`.** When the SAN probe yields no usable name and no
explicit `tls_server_name` is set:

- `false` (default): the target fails (subject to `required`) with a clear
  error. Fail-fast.
- `true`: the patched kubeconfig carries `insecure-skip-tls-verify: true`,
  `certificate-authority-data` is dropped, and a `warnings[]` entry records
  that TLS verification was disabled for this target. Use only on disposable
  hosts on trusted networks.

**Kube target output fields** (under `connections[node].kube_targets[name]`):

| Field | Description |
|---|---|
| `cluster_name` | Deterministic renamed cluster identity `tunstrap-<node>-<target>` (unmaterialized only) |
| `context_name` | Deterministic renamed context identity `tunstrap-<node>-<target>`, i.e. the patched file's `current-context` (unmaterialized only) |
| `local_port` | OS-assigned local forwarded port (unmaterialized only) |
| `endpoint` | `https://127.0.0.1:<local_port>` |
| `tls_server_name` | Chosen TLS server name, or `null` on insecure fallback (unmaterialized only) |
| `certificate_authority_data` | Base64 CA cert, or `""` on insecure fallback (unmaterialized only) |
| `client_certificate_data` | Base64 client cert (unmaterialized only) |
| `client_key_data` | Base64 client private key (unmaterialized only) |
| `content_b64` | Full patched kubeconfig (unmaterialized only) |
| `path` | Absolute path to the materialized file, or `null` if `daemon.materialize=false` |

Materialized `start --output json` targets use the projected form `{path,
context, endpoint}`. Only the key is renamed from the raw envelope's
`context_name` to `context`; its value is identical in both shapes and is
always `tunstrap-<node>-<target>`.

## Output reference

**Success (`OutputSchema`)**

```jsonc
{
  "connections": {
    "edge1": {
      "ports": {
        "kubeapi": 40123
      },
      "fetch_files": {
        "kubeconfig": {
          "content_b64": "YXBpVmVyc2lvbjogdjEK...",
          "size": 2918,
          "sha256": "d2a0bf3c..."
        }
      },
      "kube_targets": {
        "k3s": {
          "cluster_name": "tunstrap-edge1-k3s",
          "context_name": "tunstrap-edge1-k3s",
          "local_port": 40124,
          "endpoint": "https://127.0.0.1:40124",
          "tls_server_name": "edge1.example.net",
          "certificate_authority_data": "<b64>",
          "client_certificate_data": "<b64>",
          "client_key_data": "<b64>",
          "content_b64": "YXBpVmVyc2lvbjogdjEK...",
          "path": null
        }
      }
    }
  },
  "pid": 12345,
  "session_dir": "/tmp/tunstrap-session-abc123",
  "started_at": "2026-05-30T10:00:00Z",
  "warnings": []
}
```

`session_dir` is **always** present. Pass it to `stop --session-dir`.

With `daemon.materialize: true`, `start --output json` retains the
`OutputSchema` envelope but projects every materialized content-bearing entry
to a reference. For example:

```jsonc
{
  "connections": {
    "edge1": {
      "ports": {"kubeapi": 40123},
      "fetch_files": {
        "kubeconfig": {
          "path": "/tmp/tunstrap-session-abc123/tunnel-data/fetch-edge1-kubeconfig",
          "size": 2918,
          "sha256": "d2a0bf3c..."
        }
      },
      "kube_targets": {
        "k3s": {
          "path": "/tmp/tunstrap-session-abc123/tunnel-data/kube-edge1-k3s",
          "context": "tunstrap-edge1-k3s",
          "endpoint": "https://127.0.0.1:40124"
        }
      }
    }
  }
}
```

The materialized kube projection renames only raw `context_name`'s key to
`context`; its value is identical in both shapes and is always
`tunstrap-<node>-<target>`.
`--output-var` uses the same projected references under its node-keyed
`{session, nodes}` structure, rather than this `OutputSchema` envelope.

**Failure (`ErrorOutput`)**

```json
{
  "error": "RequiredTunnelFailure",
  "message": "required tunnel(s) failed to start",
  "details": {
    "failed": [
      {"node": "edge1", "error": "required fetch_files failed: ['kubeconfig']"}
    ]
  }
}
```

Always inspect the top-level `error` key first to distinguish success from
failure.

## Error reference (`fetch_files[name].error`)

| Value | Meaning | First remediation |
|---|---|---|
| `SSH_FX_NO_SUCH_FILE` | Path doesn't exist | `ssh user@host ls -la <path>` |
| `SSH_FX_PERMISSION_DENIED` | File ACL blocks the SSH user | Check ownership/mode |
| `SSH_FX_FAILURE` | Generic server-side SFTP failure | Inspect remote sshd logs |
| `SSH_FX_NO_CONNECTION` | SFTP subsystem rejected the channel | Verify `Subsystem sftp` in `sshd_config` |
| `SSH_FX_CONNECTION_LOST` | Channel died mid-read | Network instability; retry |
| `SSH_FX_OP_UNSUPPORTED` | Server doesn't implement the operation | Non-OpenSSH SFTP server; not supported |
| `EFBIG` | File exceeds the 1 MiB hard cap | This tool is for configs, not blobs |
| `ChannelOpenError` / `ConnectionResetError` / `TimeoutError` | Transport-level failure | Network or sshd config issue |
| `RuntimeError` | Internal state issue | Check stderr and `daemon.log_file` |

## Security notes

- `daemon.log_file` (if set) receives only asyncssh/asyncio debug noise. No
  `print`/`log` call path in this codebase carries decoded file bytes.
- `content_b64` is base64; callers must decode and protect it.
- Private keys (`ssh_pkey`) stay in process memory; they are never written
  to `~/.ssh` or to a tempfile. Parsing happens via
  `asyncssh.import_private_key`.
- A caller-supplied `--session-dir` must be owned by the invoking user; tunstrap
  clears its group/other write bits on use, because it stores 0600 credentials
  (`tunnel-data/`) there. No pre-`chmod` is required of the operator.

**On-disk materialization** (`daemon.materialize`)

By default (`materialize=false`) fetched content travels exactly once: from the
daemon to the parent process via an IPC pipe, then to the parent's stdout. The
tool itself never writes content to disk — the "content never to disk" guarantee
is preserved.

When `materialize=true`: the patched kubeconfig (including embedded private keys)
is written mode 0600 to `<session-dir>/tunnel-data/kube-<node>-<kube_target_name>`.
Fetched files materialize to `fetch-<node>-<fetch_name>`; these leaf names are an
implementation detail, and consumers must read `path` from the output envelope
rather than construct it.
The daemon removes these files on `stop` or `atexit` — except when `stop` cannot
confirm the daemon died, in which case it deliberately keeps them (see the `run`
teardown notes above) and says so with `"preserved": true`. The `path` field in the
kube target output becomes non-null. Callers opting in accept that decoded files
(including private keys) land on disk until `stop`/`atexit` runs. If the daemon
is killed with `kill -9`, `tunnel-data/` is orphaned and must be cleaned up
manually: `rm -rf <session-dir>/tunnel-data`.

**Host-key verification — threat model**

Remote host keys are **not** verified in this release. This is a deliberate
choice re-affirmed for kube mode: the tool targets disposable/CI hosts on
trusted networks where the SSH endpoint is established out-of-band by the
caller (e.g. infrastructure outputs). In kube mode the SSH transport carries
the kubeconfig (with private keys) and the SAN probe result; a MITM on an
unverified connection could tamper with both. Operators on untrusted networks
must not use kube mode until host-key pinning lands. Pinning is a tracked
future feature.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `error: SchemaValidationError`, `details` mentions `require` | Old top-level `require` field. Use per-node `required: bool` instead. |
| `error: SchemaValidationError`, `details` mentions `connections[...]` | Old output shape. `connections[node]` is now `{ports, fetch_files, kube_targets}`, not a list. |
| `error: SchemaValidationError`, `details` mentions `remote_ports` | The old `remote_ports: list[int]` field is gone. Use `remote_targets: {"handle": "host:port"}`. |
| `fetch_files[name].error == "EFBIG"` | File exceeds 1 MiB. Wrong file, or this tool isn't the right transport. |
| `fetch_files[name].error == "SSH_FX_PERMISSION_DENIED"` | The SSH user lacks read on the file. Check ACLs. |
| `kube_targets[name]` missing or has error | Check `warnings[]` for SAN-probe details; try setting explicit `tls_server_name`. |
| `start` with a supplied `--session-dir` fails with "tunnel-data already exists" | Orphaned `tunnel-data/` from a previous `kill -9`. Remove it: `rm -rf <session-dir>/tunnel-data`. |
| `start` hangs | Node firewalled / DNS-stuck. Increase `ssh_options.connect_timeout` or remove the node. |

## Migration from `v2026.10516.11702`

Two breaking changes from the original release:

**Output shape**

```diff
- jq '.connections.edge1[0].local_port'
+ jq '.connections.edge1.ports.kubeapi'
```

**Input require → per-node required**

```diff
  nodes:
    edge1: {host: ..., remote_targets: {kubeapi: "127.0.0.1:6443"}}
-   edge2: {host: ..., remote_targets: {kubeapi: "127.0.0.1:6443"}}
+   edge2: {host: ..., remote_targets: {kubeapi: "127.0.0.1:6443"}, required: false}
- require: ["edge1"]
```

Pydantic's `extra=forbid` on `InputSchema` rejects the old `require` field
with a clear error.

**Remote targets**

```diff
- remote_ports: [6443]
+ remote_targets: {kubeapi: "127.0.0.1:6443"}
```

```diff
- jq '.connections.edge1.ports[0].local_port'
+ jq '.connections.edge1.ports.kubeapi'
```

Previous `remote_ports: list[int]` implied `127.0.0.1` on the SSH server.
New `remote_targets` makes the target host explicit, enabling
bastion-style forwards to other hosts in the SSH server's network.
`local_ports` is removed — local listeners are always OS-assigned.

**Removed `ssh_options` fields:** `host_key_policy`, `known_hosts_path`, `threaded` (unused since the asyncssh migration; `extra=forbid` rejects them).

## Migration from `v2026.51916.0` (fetch-files release)

**`stop --pid --token` removed**

The legacy `stop --pid <pid> --token <token>` interface is gone. The only stop
interface is now `stop --session-dir <path>`.

```diff
- RESULT=$(echo "$JSON" | tunstrap start)
- PID=$(jq -r '.pid' <<<"$RESULT")
- TOKEN=$(jq -r '.token' <<<"$RESULT")
- tunstrap stop --pid "$PID" --token "$TOKEN"

+ SESSION_DIR=$(mktemp -d)
+ RESULT=$(echo "$JSON" | tunstrap start --session-dir "$SESSION_DIR")
+ tunstrap stop --session-dir "$SESSION_DIR"
```

`--session-dir` is optional on `start` (a temporary dir is generated if
omitted), but `session_dir` is **always** present in the output JSON. The
simplest migration is to capture and reuse it:

```bash
RESULT=$(echo "$JSON" | tunstrap start)
SESSION_DIR=$(jq -r '.session_dir' <<<"$RESULT")
# ... do work ...
tunstrap stop --session-dir "$SESSION_DIR"
```

## Running tests

Unit:

```bash
pip install -e ".[dev]"
pytest tests/unit
```

Integration (Linux + Docker Compose v2):

```bash
pytest tests/integration -m integration
```

## Project documents

- Terragrunt / OpenTofu recipe: [`docs/recipe_terragrunt.md`](docs/recipe_terragrunt.md)
- Kube-targets design: `docs/specs/2026-05-30-kube-targets-design.md`
- Fetch-files design: `docs/specs/2026-05-20-feature-fetch-files-design.md`
- Original design (historical): `docs/specs/2026-05-16-tunstrap-design.md`

## License

MIT.
