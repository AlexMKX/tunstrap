"""Small stdlib-only primitives for writing complete byte sequences to file descriptors."""

from __future__ import annotations

import os


class ShortWriteError(OSError):
    """An ``os.write`` no-progress failure with the bytes still unwritten."""

    def __init__(self, remaining: int) -> None:
        super().__init__("os.write made no progress; cannot complete write")
        self.remaining = remaining


def write_all(fd: int, content: bytes) -> None:
    """Write all of ``content`` to ``fd``, looping past short writes.

    ``os.write`` may return fewer bytes than requested. A zero or negative
    result cannot advance the loop, so it is reported rather than spinning.
    """
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ShortWriteError(len(view))
        view = view[written:]
