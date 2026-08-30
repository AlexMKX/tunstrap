"""Public package entry point. Only ``__version__`` is exposed.

``__version__`` is resolved lazily via PEP 562 ``__getattr__``: the
``importlib.metadata`` import (and its ``version("tunstrap")`` call) is deferred
until ``__version__`` is first read. Importing this package therefore does not
pay the ~41 ms ``importlib.metadata`` startup cost, which matters for the
``tunstrap_tofu`` pass-through branches — they ``execvp`` ``tofu`` without ever
reading ``__version__``, so they get the package import essentially free.

The only consumer is the ``--version`` flag (``cli.py``), which resolves
``__version__`` on demand through a lazy callback.
"""

from __future__ import annotations

# ``__version__`` is provided dynamically by ``__getattr__`` below (PEP 562),
# so ``from tunstrap import *`` resolves it correctly at runtime; pylint's
# static check cannot see that, hence the targeted disable.
__all__ = ["__version__"]  # pylint: disable=undefined-all-variable


def __getattr__(name: str) -> str:
    """Resolve ``__version__`` lazily; reject any other attribute access."""
    if name == "__version__":
        # Imported inside the getter so a plain ``import tunstrap`` never loads
        # importlib.metadata. pylint: disable=import-outside-toplevel.
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("tunstrap")
        except PackageNotFoundError:  # source checkout without install
            return "0.0.0+unknown"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
