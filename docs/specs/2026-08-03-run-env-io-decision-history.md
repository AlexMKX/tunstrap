# Decision history: `run` env I/O and the tofu proxy

- Date: 2026-08-03
- Supersedes: `docs/superpowers/plans/2026-07-31-run-env-io.md` and
  `docs/superpowers/plans/2026-08-01-e2e-tier.md` (removed). Those were
  per-task execution scaffolding for `feature/run-env-io` — step-by-step
  instructions, predicted command output, expected test counts — none of which
  has forward value once the branch landed, and some of which had already gone
  stale against the tree it shipped beside (see "Why this document exists"
  below). The full task-by-task record, if it is ever needed, is the branch's
  git history (86 commits ending at PR #13); this document is not a
  replacement for `git log`, it is the distillation a maintainer actually
  needs.
- Companion design doc (kept as-is, not superseded):
  `docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md`.
- Companion consumer-facing doc (kept as-is): `docs/recipe_terragrunt.md` — it
  already carries most of the measured Terragrunt facts and the shim/entry-point
  design trade in durable, tested prose. This document does not repeat that
  content; it points at it and adds only what is *not* documented anywhere
  else durable.

## Why this document exists

The two plan documents it replaces were committed intermediary planning
artifacts — the org rule is that these must never land in the tree. They had
also drifted: `2026-07-31-run-env-io.md` states as a hard global constraint
*"Do not touch the `ruff>=0.8` pin"*, while this same branch changed that pin
to `ruff>=0.16,<0.17` (`pyproject.toml:23`, commit `e57ebd1` and later). The
plan also recorded now-false baselines ("252 passed", "29 passed" — the
branch's own work moved both) and a local-machine fact ("local dev interpreter
is Python 3.14.4") that has no bearing on anyone else's checkout. None of that
is safe to leave as the record of what was decided and why.

## Provider-cache finding (not published elsewhere)

**Cold `tofu init`: 7.65 s. Warm plugin cache *without* a `.terraform.lock.hcl`:
8.17–8.27 s — slower than cold. Warm cache *with* a lock file: 0.226 s.**

Measured twice independently (e2e-tier tasks 3.3 and 6.3), same shape both
times. The dominant cost is **registry version resolution**
(`Finding hashicorp/helm versions matching "~> 2.17"`), which reruns on every
`init` when the module ships no lock file; the plugin cache only removes the
provider *download*, which was never the bottleneck here. `TF_PLUGIN_CACHE_DIR`
alone is not the win it looks like — it has to be paired with a committed
`.terraform.lock.hcl` to actually collapse init time.

This is enforced and re-stated at `tests/e2e/conftest.py`'s `tofu_plugin_cache`
fixture docstring, which is the one place code and doc were briefly out of
sync: an earlier version of that docstring claimed the cache alone multiplies
only a cheap "warm init (~1-2s)", which is false and was the exact claim a
reviewer cited to dispute this measurement before the docstring was corrected.
Trust the measured numbers above over any restated version of "the cache helps"
that does not name a lock file.

## Design decisions, and the ones that were reversed

Everything in this section has a durable home in code or in
`docs/recipe_terragrunt.md`; the entries below are short pointers plus the one
line of "why" a maintainer needs before opening the source, not a duplicate of
the full reasoning.

- **The tofu shim moved from a copied consumer-repo shell file to an in-package
  console entry point (`tunstrap_tofu`, `tunstrap/tofu_proxy.py`).** This
  consciously reverses the original design's "keep Terraform vocabulary out of
  tunstrap" principle. Full trade, including the timing measurements that
  forced the shell-shim's existence in the first place (shell fast path 2.1 ms;
  bare Python 17.3 ms; Python + `import tunstrap.cli` 225 ms, ~184 ms of which
  is the import; shipped entry point end-to-end 24.6 ms; `import tunstrap`
  67.3→17.5 ms after making `__version__` lazy via PEP 562, `dd62372`): see
  `tunstrap/tofu_proxy.py`'s module docstring and
  `docs/recipe_terragrunt.md`, "Why a console script (now)".
- **The bypass predicate is a deny-list (`init`, `version`, no-subcommand), not
  a cluster allow-list.** An earlier version (`2425fb6`) shipped an allow-list
  of `{plan,apply,destroy,refresh,import,console}` and was rejected on review
  as Critical: `TUNSTRAP_INPUT` only exists for commands the consumer
  deliberately listed in Terragrunt's own `commands`, so an allow-list is dead
  code except in the one case where someone opted a command in on purpose —
  exactly the case it silently broke. `b891d0d` restored the deny-list. See
  the `_BYPASS_COMMANDS` comment in `tunstrap/tofu_proxy.py`.
- **`--output-var` projects through an allow-list (`RunKubeTarget`), not a
  deny-list.** `extra="ignore"` means a field added to `KubeTargetOutput` later
  is dropped from this channel until someone adds it here on purpose; a
  deny-list leaks each new field by default. This is the fix for a CRITICAL
  security defect (`083b36b`, `23d81ad`): `--output-var` previously put the
  whole `OutputSchema` — including `client_key_data` and `content_b64` (a full
  kubeconfig) — into a Terraform variable, which OpenTofu persists into the
  plan file; `sensitive = true` would **not** have been enough, since it
  suppresses rendering but leaves the value in the plan file itself. See
  `RunKubeTarget`'s docstring in `tunstrap/schemas.py`.
- **`fetch_files[*].content_b64` is deliberately *not* projected the same
  way — it rides the `--output-var` channel unprojected.** The asymmetry is
  intentional, not an oversight: kube credentials were tunstrap's own material,
  injected unasked, with a lossless on-disk alternative (`path`) already
  present, so dropping them cost nothing; a fetched file is opt-in *twice*
  (`--fetch` and `--output-var`), is the operator's own content, and
  `FetchedFile` has no `path` field, so dropping `content_b64` would be a
  silent, unrecoverable breakage of any consumer reading it. Silently
  discarding requested data is worse than persisting data someone asked to
  export. See `docs/recipe_terragrunt.md`, "Fetched files are exported
  verbatim, not projected", and the design spec's "Out of scope" section for
  the follow-up debt this leaves (give `FetchedFile` a `path`, materialize it
  at `0600`, then drop `content_b64`).
- **The `--input-env` payload variable is popped from the child's environment
  before anything is injected**, because `tofu` hands its environment to every
  provider plugin, `external` data source and `local-exec` provisioner, and the
  payload's `ssh_pkey` is an SSH private key. See `_build_child_env`'s
  docstring in `tunstrap/cli.py`.

## Traps for anyone editing tunstrap's own shim/recipe assets

Not consumer-facing — these bit the branch itself and have no other durable
home:

- **Editing a shim file in place with an in-process edit tool relaxes its mode
  to `0775`.** Git tracks only the owner execute bit, so it reports **no diff**
  and `git checkout` will **not** restore it. Only an exact `st_mode & 0o777 ==
  0o755` test (`tests/e2e/test_shim.py`) catches this. Any future in-place shim
  edit needs an explicit `chmod 0755` afterwards, checked, not assumed.
- **A unit that forgets `include "root"` renders an empty `terraform_binary`
  and falls back silently to plain `tofu`** — no error, just the module's inert
  branch reached later. Documented for consumers in
  `docs/recipe_terragrunt.md`, "Failure modes you will hit"; repeated here
  because it is easy to lose sight of while editing the recipe itself, where
  the silent-fallback shape is not obvious from the diff.

## Scope note: the three pre-existing plan documents

`docs/superpowers/plans/2026-05-30-kube-targets.md`,
`2026-06-24-session-reuse-task-a.md` and `2026-06-25-cli-run-modes.md` are
untouched by this branch and were already on `main` before it started. This
compression pass deliberately does **not** touch them — see
`.superpowers/sdd/artifact-compression-report.md` for the reasoning.
