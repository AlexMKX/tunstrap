# Functional liveness over a session control channel (issue #41)

- Status: **design, awaiting review.** This document presents options and marks
  a recommendation for each; every decision remains the repo owner's. No
  production code and no tests are written for it.
- Date: 2026-08-31
- Issue: [#41 design: SessionActive is PID-only; functional liveness needs a
  cross-process query](https://github.com/AlexMKX/tunstrap/issues/41), split
  out of #33.
- Scope: the *protocol addition* — a cross-process query channel to a running
  daemon, and what `status` and `start` do with the answer. **Not** the worker
  self-health loop, which is #33 and shipped as PR #42.
- Code basis: every current-behaviour claim below was read at `5c8cf1f` (the
  merge of PR #42) and is cited in the repo's path-and-symbol form. No claim
  in this document is inherited from an issue body; the issue's own premise
  about directory permissions turned out to be wrong (see question 2).
- Library basis: Python stdlib behaviour was measured on CPython 3.14.4 in this
  worktree's `.venv` and cross-checked against the CPython `3.10` and `3.12`
  branch sources for the two version-sensitive facts (marked inline). asyncssh
  claims are against **2.23.0**, the version pinned in this `.venv`. Where a
  fact could not be established without an experiment this document says so
  rather than asserting it — see "What this design cannot settle".

## Problem

`SessionDir.create` (`tunstrap/session.py::create`) decides that a session is
active from exactly one signal: `acquire_session_lock`
(`tunstrap/identity.py::acquire_session_lock`) raised `BlockingIOError`, which
it translates into `SessionActive` (`tunstrap/exceptions.py::SessionActive`,
exit 3 via `tunstrap/exceptions.py::_EXIT_CODES`). `BlockingIOError` from
`fcntl.flock(LOCK_EX | LOCK_NB)` proves one thing and one thing only: *some
process is alive and holds an exclusive flock on that inode.* It says nothing
about whether that process is forwarding anything.

`status` inherits the same limitation from the other side.
`tunstrap/cli_stop.py::status_command` computes `alive` as
`verify_session(session_dir, pid) == IdentityCheckResult.match`, and
`tunstrap/identity.py::verify_session` is `_process_exists(pid)` — an
`os.kill(pid, 0)` probe in `tunstrap/identity.py::_process_exists` — plus the
flock/recorded-pid comparison in `tunstrap/identity.py::_check_lock`. Signal 0
and flock are both properties of *a process existing*, not of *a tunnel
working*. The two are conflated, and the conflation is structural: no caller
has any way to address the daemon at all.

## What #33 already settled, and must not be re-opened here

PR #42 gave the worker **self**-health. `tunstrap/_worker.py::_supervise`
replaced a bare `await stop_event.wait()` with a supervised wait in which one
`tunstrap/_worker.py::_tunnel_loss_watchdog` per started node awaits
`conn.wait_closed()`; on loss of a `required` node the watchdog sets the same
`stop_event` the signal handlers and `tunstrap/_worker.py::_idle_watchdog` set,
so the worker exits. `tunstrap/_worker.py::_dispose_session` then takes the
preserving branch — `tunstrap/session.py::release_lock_preserving_data`, which
drops the flock and touches nothing else — instead of
`tunstrap/session.py::cleanup`'s `rmtree`. The cause is recorded through
`tunstrap/session.py::write_tunnel_loss` into `tunnel-data/tunnel-loss.json`
and surfaced by `status_command` as an additive `tunnel_loss` key read via
`tunstrap/session.py::read_tunnel_loss`. The cleanup ordering in `_supervise`
is load-bearing: watchdogs are cancelled *before*
`tunstrap/manager.py::stop_all`, because `stop_all` would otherwise complete
every `wait_closed()` and a clean shutdown would record itself as a loss and
leak every session dir.

Two consequences matter for this design and are treated as fixed:

1. The **connection-closed** class of failure is already handled, end to end.
   Nothing proposed here should re-detect it.
2. `status` already reports *degraded but running*: `status_command` reads the
   loss record unconditionally, so a non-required node loss (which does not set
   `stop_event`) renders as `alive: true` **plus** `tunnel_loss`. That is the
   existing precedent for an additive diagnostic key, and this design follows
   it rather than inventing a second convention.

## The gap, stated precisely

The residue #33 does not cover is a daemon that is **alive, holds the lock, and
has connections that asyncssh has not declared closed, yet is not serving**.
Concretely:

- the asyncio event loop is not scheduling (a synchronous call wedged inside a
  coroutine, a blocked syscall on the loop thread, a `SIGSTOP`ped process);
- the SSH transport is a black hole — TCP is up, the peer is gone, and no
  keepalive has yet expired, so `conn.wait_closed()` has not returned;
- a local listener was closed or its accept path is broken without the
  connection closing.

For all three, `verify_session` returns `match` and `status` prints
`{"alive": true}`.

### What a control channel actually buys — and what it does not

This is worth stating bluntly, because it changes the design.

The *payload* a control channel can carry is largely information the daemon
already has and could, in principle, have written to a file. `live_nodes`
(`tunstrap/manager.py::live_nodes`) yields `(name, conn, required)` per started
node, and asyncssh 2.23.0 exposes `SSHConnection.is_closed()` — verified in the
pinned source at `asyncssh/connection.py`, where it returns
`self._close_event.is_set()`, the *same* event `wait_closed()` awaits and the
same one set in `SSHConnection._cleanup`. So a per-node `closed` flag in a
control response is, by construction, the identical signal `#33`'s watchdog
already acts on. It adds diagnostic detail, not new detection.

The genuinely new capability is that **answering at all proves the event loop
is scheduling and the process is not wedged.** A response is a liveness proof
that no file and no signal can supply: a `SIGSTOP`ped or loop-blocked daemon
still answers `os.kill(pid, 0)` and still holds its flock, but cannot accept a
connection and write a reply.

The design should therefore be judged primarily on *how reliably the
round-trip itself is a proof*, and only secondarily on payload richness. A rich
payload delivered by a mechanism that can hang is worse than a thin payload
delivered by one that cannot.

**A caveat that must not be glossed.** The kernel completes a TCP/UNIX
`connect(2)` to a listening socket via the accept backlog *without the
listening process running*. So a successful `connect()` alone proves nothing.
The proof is the **reply**, which only a scheduling event loop can produce.
Any implementation must therefore treat "connected but no bytes before the
deadline" as a *failure* to answer, not a success.

## Current state (as-is), by mechanism

| Mechanism | Where | What it proves | What it misses |
|---|---|---|---|
| `flock` on `<session_dir>/session.lock` | `tunstrap/identity.py::acquire_session_lock`, probed by `tunstrap/identity.py::_check_lock` | a process holds the lock | anything about forwarding |
| `os.kill(pid, 0)` | `tunstrap/identity.py::_process_exists` | a pid exists (and, for a child, may be a zombie — see `tunstrap/session.py::_has_exited`) | same |
| `tunnel-data/daemon.pid` | written by `tunstrap/session.py::write_identity`, read by `tunstrap/session.py::read_identity` | what pid to address | nothing live |
| `tunnel-data/tunnel-loss.json` | `tunstrap/session.py::write_tunnel_loss` / `tunstrap/session.py::read_tunnel_loss` | why a daemon exited or degraded | only what the daemon itself noticed |
| startup IPC pipe | `tunstrap/daemon.py::spawn_daemon` → `tunstrap/daemon.py::_read_ipc_response` | the startup outcome | one-shot; see below |

### The startup pipe is one-shot by construction, not by convention

`tunstrap/daemon.py::spawn_daemon` creates `os.pipe()`, passes the write end to the
worker through `pass_fds` and `--ipc-fd`, and closes its own copy of the write
end in the `finally` immediately after `Popen`. The worker writes exactly one
JSON frame with `tunstrap/_worker.py::_write_message` and then calls
`os.close(args.ipc_fd)` on **every** outcome path in
`tunstrap/_worker.py::_run` and `tunstrap/_worker.py::main`. On the parent
side, `tunstrap/daemon.py::_read_ipc_response` loops `select` + `os.read` **until
`os.read` returns empty** — that is, until EOF — and only then parses. It then
closes the read fd in its `finally`.

Two properties follow, and both are load-bearing for question 1:

- **EOF *is* the frame delimiter.** There is no length prefix and no newline
  framing on the read side (`_write_message` appends `\n`, but nothing consumes
  it as a delimiter). Keeping the pipe open for a second message would make
  `_read_ipc_response` block until the startup deadline on every successful
  start.
- **The pipe is unidirectional and the parent exits.** `start` returns; the
  read end is closed. The pipe has no name in the filesystem, so a *later*,
  unrelated process — which is exactly what `status` is — has no way to obtain
  a handle to it at all.

## Question 1 — Channel shape

### Option 1a: extend the existing startup pipe

Keep the pipe open past the handshake and add a request/response protocol.

- **Cost:** replace EOF framing in `_read_ipc_response` with explicit framing
  (length prefix or newline-terminated), keep the read fd alive past
  `spawn_daemon`, and give the fd an owner across the `start` process exit.
- **How it fails:** it cannot work for the actual use case. `status` and `stop`
  run in a *different process* that never spawned the daemon (that is the
  premise `tunstrap/session.py::_has_exited` is written around). An anonymous pipe fd
  cannot be reached from an unrelated process without either passing it over
  another IPC channel — which begs the question — or `pidfd_getfd`/`/proc/<pid>/fd`
  tricks, which are Linux-only and require the same uid plus ptrace-scope
  permissions.
- **Verdict:** disqualified on the primary requirement. It also spends a change
  to the one code path in the repo whose EOF-framing contract is currently
  simple and correct.

### Option 1b: a named UNIX stream socket in the session dir

`<session_dir>/tunnel-data/control.sock`, served by
`asyncio.start_unix_server` inside the worker's event loop; clients connect
with `asyncio.open_unix_connection` or a plain blocking `socket` with a
`settimeout`.

- **Cost:** a new server task in the worker; a new client helper; a request
  grammar; roughly one new module.
- **Why the session dir is the right namespace:** the session dir is *already*
  the session's identity and capability. That was settled in
  `docs/specs/2026-06-24-session-reuse-design.md` ("with a path-keyed lock, the
  session path *is* the identity and the capability") when `token` was removed.
  A socket beside `session.lock`'s directory inherits that property for free:
  reaching the socket requires reaching the directory, and only one daemon can
  hold the directory's lock.
- **Measured behaviours that make this workable (Linux, CPython 3.14.4, this
  worktree):**
  - connecting to a **stale socket file** with no listener raises
    `ConnectionRefusedError` (ECONNREFUSED, 111); connecting to an **absent
    path** raises `FileNotFoundError` (ENOENT, 2). The two are cleanly
    distinguishable, which is what makes questions 3 and 5 answerable.
  - `asyncio.start_unix_server` unlinks a pre-existing *socket* file before
    binding. Verified in the CPython `3.10` and `3.12` branch sources as well as
    3.14 — the `stat.S_ISSOCK(...) → os.remove(path)` guard in
    `create_unix_server` is present in all three. A stale socket left by a
    preserved session therefore never blocks a rebind.
- **Known sharp edges, all measured or sourced:**
  - **`sun_path` length.** `/usr/include/x86_64-linux-gnu/sys/un.h` declares
    `char sun_path[108]`. Measured on this host: bind succeeds at a 107-character
    path and fails at 108 with `OSError: AF_UNIX path too long`. macOS declares
    104. `--session-dir` is caller-supplied and unbounded, so
    `<session_dir>/tunnel-data/control.sock` **will** exceed the limit for deep
    paths. This is a real, reachable failure that must be designed for, not
    discovered. See "Recommendation" below.
  - **Socket file mode is umask-derived and there is no `mode` parameter.**
    `loop.create_unix_server` has no mode argument (checked against the 3.14
    signature and the published docs). Measured under this shell's umask (002),
    the bound socket file came out `0o775`. On Linux, `connect(2)` to a
    pathname socket requires write permission on the socket inode, so the
    socket's own mode is *not* a boundary you can rely on. Question 2 covers
    why that turns out not to be fatal, and what it forces.
  - **Removal on close is 3.13+.** The `cleanup_socket` parameter of
    `loop.create_unix_server` is documented "Changed in version 3.13: Added the
    *cleanup_socket* parameter", and it is absent from the 3.10 and 3.12 branch
    sources. The supported floor is 3.10 (see
    `tunstrap/session.py::_rmtree_reporting`'s docstring) and CI runs 3.10–3.13, so on
    three of four supported versions the socket file **survives** server close.
    Combined with `release_lock_preserving_data` — which deliberately preserves
    everything — a self-terminated daemon leaves a stale `control.sock` behind
    on 3.10–3.12. That is not a leak to fix but a state to *interpret*: it
    yields ECONNREFUSED, which is the correct diagnostic ("this session had a
    channel; nothing is serving it now").

### Option 1c: abstract-namespace UNIX socket (`\0`-prefixed)

- **Cost:** none; sidesteps `sun_path` path length pressure and leaves no file.
- **How it fails:** abstract sockets have **no filesystem permissions at all**.
  Any process in the same network namespace can connect, regardless of uid. That
  contradicts the entire posture of `tunstrap/session.py::_secure_supplied_root` and
  `tunstrap/identity.py::acquire_session_lock`, which spend considerable effort proving
  ownership. It is also Linux-only, and CI runs `macos-latest`.
- **Verdict:** reject.

### Option 1d: a filesystem heartbeat instead of a channel

The daemon periodically rewrites `tunnel-data/health.json` (via
`tunstrap/session.py::atomic_write`, already atomic); `status` reads it and treats a
stale mtime as unhealthy.

- **Cost:** lowest of all options. No new transport, no framing, no timeout
  negotiation. Reuses `atomic_write` and `read_tunnel_loss`'s
  never-raises-on-garbage discipline verbatim.
- **What it buys:** it *does* detect a wedged event loop — a stopped loop stops
  writing, and staleness is observable. This is not a weak substitute; it
  catches the primary novel case.
- **How it fails:** (i) resolution is bounded by the write interval, so
  "unhealthy" lags by up to one period; (ii) it writes to the session dir
  forever, on every session, whether or not anyone ever asks; (iii) it cannot
  carry a *request* — no `stop`, no on-demand probe, no future verb; (iv) mtime
  granularity and clock changes make staleness slightly fuzzy (`time.monotonic`
  is not persistable, so the record must carry a wall-clock timestamp and
  inherits its problems).
- **Verdict:** genuinely competitive, and cheaper. Its disqualifier is (iii):
  #41 asks for a *query channel*, and every follow-on use (race-free shutdown,
  active probe, future verbs) needs a request path a heartbeat structurally
  cannot grow into.

### Recommendation — Q1

> **RECOMMENDATION: Option 1b — a named UNIX stream socket at
> `<session_dir>/tunnel-data/control.sock`**, with a newline-delimited JSON
> request/response grammar, one request per connection, server closing the
> connection after the reply.
>
> Rationale: 1a cannot reach an unrelated process at all; 1c gives up the
> ownership boundary the codebase is built on; 1d cannot grow a request path.
>
> Three constraints I would treat as part of the recommendation, not as
> implementation detail:
>
> 1. **One request per connection, reply, close.** No session state, no
>    multiplexing, no keep-alive. The connection lifetime *is* the frame, which
>    mirrors the startup pipe's EOF framing rather than inventing a second
>    convention.
> 2. **Bind before the success frame.** `_run` writes the `success` frame and
>    only then calls `_supervise`. The control server must be listening
>    *before* `_write_message` sends `success`, or a caller that runs `status`
>    the instant `start` returns races the bind and sees ENOENT — which under
>    question 5 is indistinguishable from "old daemon". If the bind fails
>    (`sun_path` too long, exotic filesystem), the daemon should still start and
>    report the absence in its success payload rather than fail the start: the
>    tunnels are the product, the channel is a diagnostic.
> 3. **`sun_path` overflow needs an explicit answer, and I do not think it
>    should be a silent skip.** The two candidates are (a) fall back to a
>    short path under `$XDG_RUNTIME_DIR`/`tempfile.gettempdir()` keyed by a hash
>    of the session dir, which reintroduces a second namespace and a second
>    cleanup owner; or (b) bind by `chdir`-relative path — open the
>    `tunnel-data` directory, bind to the bare leaf name from a process whose
>    cwd is that directory — which keeps `sun_path` to `len("control.sock")`
>    regardless of session-dir depth. (b) is much more attractive but requires
>    changing the worker's cwd or a `fork`-free equivalent, and I have **not**
>    verified that a bind to a relative `sun_path` behaves identically on macOS.
>    See "What this design cannot settle", experiment E1.

## Question 2 — Permissions

### The issue's premise is false for a caller-supplied `--session-dir`

The issue says "the session dir is 0700; a control socket inherits that, but
confirm it holds for a caller-supplied `--session-dir`". Reading
`tunstrap/session.py::_secure_supplied_root`, **it does not hold.** That
function does not force 0700 on a supplied root. It:

1. opens the root with `O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`;
2. refuses it outright if `st_uid != os.getuid()`;
3. if `S_IWGRP | S_IWOTH` are set, `fchmod`s **only those two bits off**, then
   re-`fstat`s through the same fd and refuses the root if they survived.

Read and execute bits are deliberately preserved. Its own docstring is explicit:
"clearing preserves read/exec, so a legitimate 0755 root is left at 0755 rather
than force-chmodded to 0700." That was a deliberate correction — refusing
group-writable roots broke `mkdir d && tunstrap run --session-dir d` on a stock
umask-0002 account.

So the accurate statement of current behaviour is:

| Directory | Mode | Set by |
|---|---|---|
| generated session root | 0700 | `tempfile.mkdtemp` in `tunstrap/session.py::create` |
| caller-supplied session root | **whatever the caller had, minus g+w/o+w** — commonly 0755 | `tunstrap/session.py::_secure_supplied_root` |
| `tunnel-data/` | **0700** (subject to umask) | `data.mkdir(mode=0o700)` in `tunstrap/session.py::create` |

`tunnel-data/` is the only component with a guaranteed 0700, and it gets it on
both paths. Every credential the tool materializes already lives there for
exactly this reason (`tunstrap/session.py::_validated_path`, `tunstrap/session.py::atomic_write`).

### Why that settles the placement question

A pathname UNIX socket is reachable only if the client can traverse **every**
path component (`x` on each directory) *and* — on Linux — has write permission
on the socket inode. Placing `control.sock` inside `tunnel-data/` means another
uid is stopped at the directory: 0700 grants no `x` to group or other, so the
socket is unreachable no matter what mode `bind` happened to give it. That is
the boundary to rely on, because as measured above the socket's own mode is
`0777 & ~umask` (observed `0o775`) and `loop.create_unix_server` offers no way
to set it.

Placing it at the session **root** would be wrong: under a caller-supplied
0755 root, other uids get `x` on the root and the socket's own umask-derived
mode becomes the only barrier — and under umask 002 that mode is group-writable,
i.e. no barrier at all for a shared group.

Two residual notes, stated honestly:

- `mkdir(mode=0o700)` is masked by the process umask. A umask that clears owner
  bits (e.g. `0o077` is fine; `0o700` is not) would leave `tunnel-data`
  unusable — but that would already break every materialized file today, so it
  is a pre-existing condition, not something this design introduces.
- Defence in depth is cheap here and I would take it: `os.fchmod` the socket to
  0600 immediately after bind, or bind under a temporarily tightened umask.
  It does not replace the directory boundary; it removes the reliance on a
  single check.

### Recommendation — Q2

> **RECOMMENDATION: put the socket at
> `<session_dir>/tunnel-data/control.sock`, never at the session root**, and
> rely on `tunnel-data`'s 0700 (set by `tunstrap/session.py::create`) as the access
> boundary — because a caller-supplied root is **not** guaranteed 0700, only
> guaranteed owned-by-us and not group/other-writable
> (`tunstrap/session.py::_secure_supplied_root`). Additionally tighten the socket file
> itself to 0600 after bind, as defence in depth and because the umask-derived
> default was measured at `0o775`.
>
> I would **not** add `SO_PEERCRED`/`getsockopt(SO_PEERCRED)` uid checking in a
> first version. It is Linux-specific in that spelling (macOS needs
> `LOCAL_PEERCRED` or `getpeereid`), and any peer that reached the socket has
> already traversed a 0700 directory it must own. It is worth revisiting only
> if the socket is ever moved out of `tunnel-data`.

## Question 3 — Timeout semantics

The issue is exactly right that "alive but not answering" is itself diagnostic
and must not collapse into `alive: false`. Making it `false` would be an
outright regression: it would report a daemon that still holds the lock — and
will therefore still make the next `start` fail with `SessionActive` — as if
nothing were there. The operator would be told to do the one thing that cannot
work.

### The states that must be distinguishable

| # | Observation | Meaning |
|---|---|---|
| S1 | pid gone / lock free | dead. `alive: false` today, correct. |
| S2 | pid + lock held, socket absent (ENOENT) | alive; **no channel** — either a pre-#41 daemon or a daemon whose bind failed. Not a health verdict. |
| S3 | pid + lock held, socket present, ECONNREFUSED | alive under this pid, but nothing is serving that socket. Anomalous: on 3.13+ the socket should have been removed at close; on 3.10–3.12 this is also the expected shape of a *preserved* dead session's leftovers. |
| S4 | pid + lock held, connect ok, reply within deadline | answered. The event loop is scheduling. Payload carries per-node detail. |
| S5 | pid + lock held, connect ok, **no reply** within deadline | **the case #41 exists for.** Alive, holds the lock, cannot answer. |
| S6 | pid + lock held, reply arrives but reports a closed/broken node | degraded; overlaps with the existing `tunnel_loss` reporting. |

`alive` must keep meaning S1-vs-rest, unchanged. Everything else belongs in a
separate key.

### Options for expressing S2–S5

- **3a: a single `health` string** — `"ok" | "unresponsive" | "degraded"`,
  key omitted when not probed (S2). Minimal, but conflates S3 with S5 unless a
  fourth value is added, and gives no room for the payload.
- **3b: a `health` object** — `{"probe": "<one of answered|timeout|refused>",
  "timeout_seconds": N, "nodes": {...}}`, key omitted entirely when no probe was
  possible (S2). Verbose, extensible, and each of S3/S4/S5 gets its own value.
- **3c: exit codes** — make `status` exit non-zero on S5. Rejected: `status`
  today exits 0 for every observation including `alive: false`
  (`tunstrap/cli_stop.py::status_command` has no `sys.exit`), and scripts rely on that
  to distinguish "status failed to run" from "the thing is dead". Changing it
  is a breaking change to a contract nobody asked to change.

### The deadline itself

A fixed constant is the wrong shape for a tool whose every other deadline is
schema-configurable: `tunstrap/schemas.py::DaemonOptions` already carries
`startup_timeout_seconds` and `shutdown_grace_seconds`. But `status` takes only
`--session-dir` (`tunstrap/cli_stop.py::status_command`) and never sees the schema — the
schema lives in the daemon, not on disk in a form `status` reads. So a
`DaemonOptions` field cannot reach `status` without a new mechanism.

That leaves: a constant with a `--probe-timeout` CLI override on `status`. A
short default is right, because the probe is on the interactive path and the
answer to "no reply in N seconds" does not get materially better with a larger
N. Something in the 1–3 s range; I have no measurement that distinguishes
within that band, and I would rather say so than invent a number with false
precision.

### Recommendation — Q3

> **RECOMMENDATION: option 3b.** Keep `alive` meaning exactly what it means
> today (pid alive **and** holds the lock, per
> `tunstrap/identity.py::verify_session`). Add **one** additive key — I would call it
> `health` — emitted **only when a probe was actually attempted and produced a
> result**, i.e. never in S1 and never in S2. Distinct values for `answered`,
> `timeout` and `refused` so S3, S4 and S5 never collapse into each other.
> `status` keeps exiting 0 in every case.
>
> Deadline: a small constant (1–3 s) overridable by a new `--probe-timeout`
> option on `status`. Not a `DaemonOptions` field, because `status` never
> reads the schema.
>
> The operator-facing sentence I would want the README to be able to say:
> *`alive: true` with `health.probe == "timeout"` means the daemon is wedged —
> `stop --grace-seconds 0` it and start again; it will not release the lock on
> its own.*

## Question 4 — Auto-recovery on `start`

### The race, concretely

`start` does not hold the lock — that is the whole point; it is trying to
acquire it. Any auto-recovery is therefore a three-step
observe-then-act on state it does not own:

1. `start` observes: lock held by pid P, probe times out → "P is wedged".
2. Between the observation and the action, P can exit on its own (idle
   watchdog, `_tunnel_loss_watchdog`, an operator's `stop`, a crash). The lock
   is released and a **different** `start` acquires it, writing a new pid.
3. `start` acts on the stale conclusion.

The existing machinery blunts step 3 but does not eliminate it.
`tunstrap/session.py::stop_session` calls `tunstrap/identity.py::verify_session` *before*
`os.kill(SIGTERM)` and returns `identity mismatch` if the recorded pid changed;
after the grace period it re-verifies before escalating to `SIGKILL` and returns
`identity changed during grace` rather than killing. So the window narrows to
the gap between the final `verify_session` and the `os.kill` — the classic
check-then-signal race, in which P could exit and its pid be recycled by an
unrelated process. Closing that fully needs a pid handle
(`os.pidfd_open`, Python 3.9+ / Linux 5.3+), which does not exist on macOS.
That residual is *already present* in today's `stop` and is not made worse by
auto-recovery; but auto-recovery would make the tool take it **without an
operator asking**, which is a different proposition.

There is a second, worse hazard specific to *automatic* action: the probe can
be wrong. A daemon under heavy load, on a paused VM, or momentarily blocked by
a slow synchronous `tunstrap/session.py::write_tunnel_loss` on the loop thread (which is
deliberately synchronous — see the `_record_losses` docstring) can miss a 2 s
deadline while being perfectly healthy. Auto-recovery would kill a working
tunnel and take down whatever was using it. A false positive on the *report*
path costs a confusing line of JSON; on the *act* path it costs a live service.

### Options

- **4a: report only.** `start` continues to fail with `SessionActive` (exit 3),
  but the error `details` gain the probe result, so the operator is told *why*
  the incumbent is not usable and what to run.
- **4b: opt-in flag.** `start --recover-unresponsive`, which on a timed-out
  probe performs the existing `stop_session(..., force=True)` and retries once.
  The operator has asked, so the residual pid race is a risk they accepted, and
  it composes from parts that already exist and are already tested.
- **4c: automatic by default.** Rejected. It converts a probe false-positive
  into an unrequested kill of a working daemon, and it does so on the code path
  operators run in CI without watching.

### The race-free alternative worth flagging

Once a control socket exists, a `shutdown` **request** over it is race-free in
a way signalling can never be: the socket lives *inside the session directory*,
so a connection that reaches it can only reach the daemon that owns that
directory. No pid is involved, so pid reuse is structurally impossible. This is
a genuinely better `stop` primitive than `os.kill`.

It does **not** rescue auto-recovery, and I want to be plain about why: the
daemon that most needs recovering is precisely the one that did not answer the
health probe, and it will not answer a shutdown request either. Signals remain
the only tool for a wedged process. The socket-shutdown path is worth building
for *ordinary* `stop`, not for this question.

### Recommendation — Q4

> **RECOMMENDATION: 4a now, 4b as a follow-up, never 4c.**
>
> `start` should **report, not recover.** Keep the `SessionActive` / exit 3
> contract exactly as it is; enrich the error `details` (which
> `TunstrapError.to_error_output` already carries through
> `tunstrap/cli.py::start_command`'s handler) with the probe outcome and the exact
> recovery command. An operator who reads "the incumbent daemon is not
> answering; run `tunstrap stop --session-dir X --grace-seconds 0`" is one
> command away from recovery and has *chosen* it.
>
> If the owner wants recovery automated, 4b behind an explicit flag is
> defensible because it reuses `tunstrap/session.py::stop_session`'s existing
> `verify_session`-before-signal guards on both the SIGTERM and SIGKILL steps.
> I would not ship it in the same change as the channel itself.

## Question 5 — Backwards compatibility

A daemon started by a pre-#41 build has no socket. The requirement is that
`status` degrade rather than error.

The measurement above makes this straightforward: connecting to an absent path
raises `FileNotFoundError` (ENOENT), which is distinct from the
`ConnectionRefusedError` of a stale socket. So:

- **ENOENT → no channel.** Not "unhealthy". `status` omits the `health` key
  entirely and emits exactly the pre-#41 shape. This is state S2, and it is the
  *only* correct degradation: an old daemon may be perfectly healthy, and
  reporting it as unresponsive would be a false alarm on every session that
  survives an upgrade.
- **ECONNREFUSED → S3, a real observation.** The socket was created by a
  daemon that is no longer serving it. Reportable, and distinct.

Three further compatibility points, each grounded:

1. **A stale socket cannot poison a reused session dir.**
   `tunstrap/session.py::_reclaim_data_slot` `rmtree`s any pre-existing `tunnel-data`
   before `tunstrap/session.py::create` recreates it, and it does so while holding the
   exclusive lock. Independently, `create_unix_server` unlinks a pre-existing
   socket file before binding (verified in the 3.10, 3.12 and 3.14 sources). So
   there are two independent guarantees, not one.
2. **New client / old daemon and old client / new daemon are both fine.** The
   first is ENOENT-degradation above. The second is trivially fine: an old
   `status` never looks at the socket.
3. **Protocol evolution needs a version field from day one.** A daemon and its
   socket are the same process, so the channel never changes version *within*
   one session's life. But a *newer client* talking to an *older-but-post-#41
   daemon* is a real scenario, and a routine one: daemons are long-lived and
   detached (`start_new_session=True` in `tunstrap/daemon.py::spawn_daemon`), so
   one readily outlives a `pip install -U`. A `version` integer in the response,
   plus a client that treats an unknown version as "answered but not
   interpretable" rather than erroring, costs nothing now and cannot be
   retrofitted later.

### Recommendation — Q5

> **RECOMMENDATION: ENOENT on connect is "no channel", not "unhealthy".** In
> that state `status` emits byte-identical pre-#41 output, with no `health`
> key at all. ECONNREFUSED is a *different*, reportable state and must not be
> folded into it. Carry a `version` integer in every response and have the
> client degrade — not error — on an unknown one.
>
> There is nothing to migrate and no flag day: the degradation is derived from
> a syscall errno, not from a recorded version number.

## The `status` envelope: exactly what stays byte-identical

The envelope is a pinned contract and this section states the boundary
precisely, as required.

**Byte-identical, unconditionally:**

- `{"alive": false}\n` for a session with no live daemon and no records. Pinned
  by `tests/unit/test_status_tunnel_loss.py::test_status_without_a_record_is_byte_identical_to_before`,
  which compares `result.stdout` byte for byte.
- `{"alive": true}\n` for a live daemon with no records. Pinned by
  `tests/unit/test_cli_runner.py::test_status_alive_by_session_dir`, which
  asserts whole-dict equality against `{"alive": True}`.
- The `tunnel_loss` shape and its position immediately after `alive`, pinned by
  `tests/unit/test_status_tunnel_loss.py` including the
  `startswith('{"alive": false, "tunnel_loss": ')` prefix assertion.
- The value and semantics of `alive` itself. It stays exactly
  `verify_session(session_dir, pid) == IdentityCheckResult.match`.
- `status`'s exit code: 0 in every case.

**Additive, and only under a stated condition:**

- one new key — `health` — emitted **only** when a probe was attempted *and*
  produced a result (S3/S4/S5). Absent in S1 and S2, which is what preserves
  both byte-pinned shapes above: neither test's fixture has a socket, so
  neither would see the key.

**`stop`'s envelope is not touched at all.** It is byte-pinned across all seven
outcomes by `tests/unit/test_cli_stop_output.py::test_stop_stdout_is_byte_identical`
and `tests/unit/test_cli_stop_output.py::test_stop_identity_failure_envelope_is_byte_exact`,
and `tunstrap/cli_stop.py::_stop_resolved`'s rule is unchanged. If the owner later adopts
the socket-based `shutdown` primitive noted under Q4, that is a change of
*mechanism* behind `tunstrap/session.py::stop_session`, whose `StopOutcome` shape — and
therefore `stop`'s bytes — stays as it is.

**Ordering, if `health` is adopted:** `alive`, then `tunnel_loss` when present,
then `health` when present. Appending keeps every existing prefix assertion
valid.

## Components a first implementation would touch

Sketch, for scoping only; nothing here is a commitment.

| File | Change |
|---|---|
| new module | control-channel server coroutine and client probe helper; request/response grammar; the connect-errno → state mapping |
| `_worker.py` | bind the server *before* `_write_message` sends `success` in `_run`; add the server to the tasks `_supervise` cancels, keeping the existing cancel-before-`stop_all` ordering intact |
| `manager.py` | a read-only accessor for per-node health built on `live_nodes` and asyncssh's `SSHConnection.is_closed()` |
| `cli_stop.py` | `status_command` gains the probe and the conditional `health` key; a `--probe-timeout` option |
| `cli.py` | `start_command`'s `SessionActive` path gains probe detail in `details` (Q4 option 4a) |
| `README.md` | the new key, the new state table, the wedged-daemon recovery sentence |

Note for whoever implements: `tunstrap/_worker.py::main` ends in `os._exit(rc)`, which
bypasses interpreter shutdown. Any socket cleanup must therefore happen in
`_supervise`'s `finally`, not in an `atexit` hook or a `__del__`.

## What this design cannot settle without an experiment

Stated as experiments, per the brief, rather than guessed at.

- **E1 — relative-`sun_path` bind portability.** Q1's preferred answer to the
  108/104-byte `sun_path` limit is to bind to the bare leaf `control.sock` from
  a process whose cwd is `tunnel-data`. I verified the *limit* on Linux
  (107 chars OK, 108 fails) but **not** that a relative bind resolves against
  cwd identically on macOS, nor that `asyncio.start_unix_server`'s
  stale-unlink and `cleanup_socket` logic behave correctly with a relative
  path. *Experiment:* on both `ubuntu-latest` and `macos-latest`, bind
  `start_unix_server` to `"control.sock"` with cwd set to a deep directory
  (>200 chars), connect from another process by absolute path, assert the
  round-trip, then close the server and assert the file's fate on 3.12 and 3.13.
- **E2 — the actual timeout distribution.** I asserted 1–3 s is the right band
  on reasoning, not measurement. *Experiment:* instrument a healthy daemon under
  load (concurrent fetch/kube materialization, which does synchronous file I/O
  on the loop thread via `tunstrap/session.py::atomic_write`) and record the p99
  request→reply latency. The default should sit an order of magnitude above it.
- **E3 — whether the wedge case is real in this codebase.** I have argued the
  novel capability is detecting a non-scheduling event loop, but I have **not**
  demonstrated a tunstrap daemon reaching that state by any route other than an
  external `SIGSTOP`. *Experiment:* audit every `await`-free synchronous call
  on the loop thread post-startup (`_record_losses` is the known one) and try to
  produce a multi-second stall from a plausible cause — a slow or hung NFS
  `tunnel-data`, for instance. If nothing plausible surfaces, option 1d (the
  heartbeat) becomes materially more attractive than I have rated it, and the
  owner should know that before paying for a socket.
- **E4 — the black-hole transport case.** I claimed a dead peer with no
  keepalive expiry leaves `wait_closed()` unfired. That follows from
  asyncssh 2.23.0 setting `_close_event` in `SSHConnection._cleanup`, which a
  silent network partition does not reach — but I did **not** verify what the
  effective keepalive configuration is for connections opened by
  `tunstrap/ssh.py`, so I cannot say how long that window is. *Experiment:*
  open a tunnel, `DROP` the peer's traffic with a firewall rule, and measure
  the time until `status`'s `alive` changes. If that is short, the black-hole
  case is largely covered by #33 already and only the wedge case remains.

## Bug found while reading, reported and not fixed

`tunstrap/daemon.py::spawn_daemon`'s `except BaseException` handler — the
pre-detach path where `Popen` itself raises — removes the minted session root
and re-raises, and the `finally` closes `ipc_write_fd`, but **`ipc_read_fd` is
never closed on that path**. This is issue #40, already open and being fixed
concurrently. Recorded here only because this design reads that function
closely; **not touched by this change.**

I found nothing else in `session.py`, `identity.py`, `_worker.py`, `daemon.py`,
`manager.py` or `cli_stop.py` that I would report as a defect at `5c8cf1f`.

## Out of scope

- The worker self-health loop. That is #33, shipped as PR #42, and this
  document treats `_supervise`'s watchdog set and its cancel-before-`stop_all`
  ordering as fixed.
- The `ipc_read_fd` leak (#40).
- Any change to `stop`'s stdout envelope or to `StopOutcome`.
- Replacing `os.kill`-based stop with a socket `shutdown` request. Noted under
  Q4 as the strictly better primitive it is, and deliberately left as a separate
  decision, because it changes `stop`'s failure modes and deserves its own pass.
- Active probing of the forwards (opening a channel to a real target to prove
  end-to-end reachability). It needs probe-target configuration
  `tunstrap/schemas.py::DaemonOptions` does not have, and a probe that can
  itself hang undermines the one property that makes the channel worth having.

## Relationship to prior specs

- `docs/specs/2026-05-16-tunstrap-design.md` records the original decision:
  "Status command minimal (PID liveness only) — Tunnel-internal health
  introspection from outside the daemon needs IPC. YAGNI for pilot; logs cover
  diagnostics." This document is the deferred pass, not a reversal: the pilot
  is over and the specific gap is now demonstrated rather than hypothetical.
- `docs/specs/2026-06-24-session-reuse-design.md` established that the session
  path *is* the identity and the capability, which removed `token`. Q1's choice
  to namespace the control channel by session directory follows directly from
  that decision rather than introducing a new capability model.
