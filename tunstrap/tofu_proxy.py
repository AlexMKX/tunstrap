"""``tunstrap_tofu`` console entry: the OpenTofu proxy, in-process.

This is the in-package successor to the consumer-side shell shim (now retired
from the recipe; see the "Alternative: a shell shim for the fast path" section
of ``docs/recipe_terragrunt.md``). Shipping it as a second
``[project.scripts]`` entry point of this package means ``uv tool install``
yields both ``tunstrap`` and ``tunstrap_tofu``, so Terragrunt's
``terraform_binary`` can point at a stable installed path with nothing copied
into the consumer's repo.

Cost discipline. The pass-through branches (``TUNSTRAP_INPUT`` unset, or a
no-cluster subcommand like ``init``/``version``) ``execvp`` straight into
``tofu`` **without importing ``tunstrap.cli`` or any heavy dependency**.
``tunstrap/__init__.py`` resolves ``__version__`` lazily (PEP 562), so the
package import itself loads no ``importlib.metadata`` on this path. Measured
end-to-end via the installed entry point the fast path is **~25 ms** (≈17 ms
interpreter + a now-cheap package import + the execvp handoff) — about **12×
the ~2 ms shell shim**, i.e. **~74 ms added per ``terragrunt plan``** at three
fast-path hits, still noise beside an 8 s ``tofu init``. For a consumer for
whom every millisecond of the fast path matters, a 3-line shell shim remains
the lower-overhead option (see ``docs/recipe_terragrunt.md``). The tunnelled
branch imports ``tunstrap.cli`` lazily — that path already costs seconds for the
SSH handshake and the child, so the import is noise there.

Terraform vocabulary lives here by deliberate owner decision; see the
"Shipping the shim" history in
``docs/specs/2026-07-31-run-env-io-and-tofu-proxy-design.md`` and the recipe's
"Why a console script (now)" section for the trade.
"""

from __future__ import annotations

import os
import sys

_INPUT_ENV = "TUNSTRAP_INPUT"
_OUTPUT_VAR = "TF_VAR_tunstrap"
_TOFU = "tofu"

# tofu subcommands that BYPASS the tunnel when TUNSTRAP_INPUT is set — the ones
# that provably do not contact the cluster. Behaviourally equivalent to the
# shell shim's ``case "$1" in init|-version)`` plus the no-cluster extras
# (``version`` subcommand, and ``-help``/no-subcommand, which ``_find_subcommand``
# returns ``None`` for, also bypassing). ``init`` is the load-bearing entry:
# Terragrunt's extra_arguments.env_vars scopes TUNSTRAP_INPUT to the listed
# commands AND their automatic ``init`` (measured fact 4 in the design spec), so
# a ``terragrunt plan`` sets it for the auto-init too — without this bypass the
# auto-init would build a needless second tunnel. ``validate`` and ``fmt`` are
# the same kind of provable no-cluster-contact command as ``init``: ``validate``
# checks the configuration against installed provider schemas only and never
# configures a provider (no cluster round-trip, unlike ``plan``/``apply``);
# ``fmt`` touches only local ``.tf`` files. Bypassing them avoids a pointless
# SSH tunnel plus kubeconfig materialization on every ``validate``/``fmt``.
#
# Tension: this bypasses TUNSTRAP_INPUT even when a consumer deliberately
# listed ``validate``/``fmt`` in Terragrunt's ``extra_arguments.commands`` —
# the opt-in the "everything else tunnels" rule below otherwise honours. An
# earlier allow-list version of this bypass set was rejected on exactly that
# opt-in-should-win reasoning; ``validate``/``fmt`` are added here as narrow,
# individually-justified exceptions (provably cluster-free, same as ``init``),
# not a reopening of that allow-list.
#
# Everything NOT in this set TUNNELS. TUNSTRAP_INPUT is set only for commands
# the consumer deliberately listed in Terragrunt's ``commands``, so the proxy
# must honour that opt-in (e.g. ``output`` — the e2e tier lists it in
# ``commands`` and asserts the tunnelled row) rather than second-guess it with a
# cluster-only allow-list of its own. An earlier allow-list version did exactly
# that and was a behaviour change, not the ``-chdir`` gap fix it posed as.
_BYPASS_COMMANDS = frozenset({"init", "version", "validate", "fmt"})

# Global flags that take their value as a SEPARATE argv token (``-chdir DIR``).
# Their ``=`` form (``-chdir=DIR``) is one token and is handled by the bare
# ``tok.startswith("-")`` skip below. ``-chdir`` is the flag the shell shim's
# literal-``$1`` match could not see past, so ``tofu -chdir=DIR init`` missed
# the bypass and built a needless tunnel — the documented gap this parser fixes.
_GLOBAL_VALUE_FLAGS = frozenset({"-chdir", "--chdir"})


def main() -> int:
    """``tunstrap_tofu`` entry point.

    Never returns on the pass-through branches: it ``execvp``s into ``tofu``
    and this process image is replaced. The tunnelled branch delegates to
    ``run`` (in-process) and exits with the child's code, so it does not
    return either.
    """
    argv = sys.argv[1:]
    raw = os.environ.get(_INPUT_ENV, "")
    if not raw.strip():
        _exec_tofu(argv)
    if _should_bypass(argv):
        _exec_tofu(argv)
    _run_tunnelled(argv)
    return 0  # pragma: no cover — _run_tunnelled exits


def _should_bypass(argv: list[str]) -> bool:
    """True iff the subcommand needs no tunnel (``TUNSTRAP_INPUT`` assumed set).

    The pinned bypass set: ``init``, ``version``, ``validate`` and ``fmt``
    subcommands, plus anything ``_find_subcommand`` returns ``None`` for
    (``-version``/``-help`` global flags, or no subcommand at all). Everything
    else tunnels. Pinned exhaustively by the ``_should_bypass`` table test; do
    not broaden without updating it.
    """
    subcmd = _find_subcommand(argv)
    return subcmd is None or subcmd in _BYPASS_COMMANDS


def _exec_tofu(argv: list[str]) -> None:
    """Replace this process with ``tofu``, argv untouched."""
    try:
        os.execvp(_TOFU, [_TOFU, *argv])
    except OSError as exc:
        sys.stderr.write(f"tunstrap_tofu: cannot execute tofu: {exc}\n")
        sys.exit(127)


def _find_subcommand(argv: list[str]) -> str | None:
    """Return the tofu subcommand, parsed past leading global flags.

    Mirrors tofu's own grammar: ``-version``/``-help`` as a global flag and an
    empty command line short-circuit to "no subcommand" (no cluster contact);
    ``-chdir DIR`` and ``-chdir=DIR`` are consumed so the real subcommand is
    reached. The first token that is neither a consumed value-flag nor any
    other ``-``-prefixed global flag is the subcommand.

    This is structural parsing, not a substring match: ``tofu -chdir init plan``
    consumes ``init`` as the chdir value and reports ``plan`` as the
    subcommand, where a naive "``init`` anywhere in argv" predicate would
    wrongly bypass.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-version", "--version", "-help", "-h", "--help"):
            return None
        if tok in _GLOBAL_VALUE_FLAGS:
            i += 2  # consume the flag and its separate value
            continue
        if tok.startswith("-"):
            i += 1  # any other global flag (=form or bare); skip one token
            continue
        return tok
    return None


def _run_tunnelled(argv: list[str]) -> None:
    """Open the tunnel and run ``tofu`` as its child, in-process.

    Replaces ``exec tunstrap run --input-env … --output-var … -- env -u
    KUBECONFIG tofu …``. Going in-process drops one process level (``sh`` →
    ``tunstrap``) and lets ``run`` build the child environment directly, so
    ``env -u KUBECONFIG`` becomes ``suppress_kubeconfig=True``: same property
    (a broken ``config_path`` chain cannot fall back to an inherited or
    injected ``KUBECONFIG``), no child-side wrapper.

    ``tunstrap.cli`` is imported here, on the tunnelled path only, so the
    pass-through branches never pay for it.
    """
    # Lazy on purpose: importing tunstrap.cli on the pass-through paths would
    # cost ~180 ms (click/pydantic/asyncssh), defeating the entry point.
    from tunstrap.run_invocation import run_via_env_input  # pylint: disable=import-outside-toplevel

    run_via_env_input(
        _INPUT_ENV,
        _OUTPUT_VAR,
        [_TOFU, *argv],
        suppress_kubeconfig=True,
    )
    sys.exit(0)  # pragma: no cover — run_via_env_input exits
