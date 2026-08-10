"""SFTP file fetch over a live asyncssh SSHClientConnection.

The fetcher rides the same SSH session as the local port forwards (no
second TCP connection, no second authentication). The SFTP channel is
multiplexed over the existing transport.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Final

import asyncssh

from tunstrap.schemas import FetchedFile, FileSpec

# Exceptions we expect from a live SSH/SFTP session. Anything outside this
# tuple is a programmer error and must bubble up — never silently turned
# into a FetchedFile.error.
_SFTP_TRANSPORT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    asyncssh.Error,
    OSError,
    asyncio.TimeoutError,
)

_MAX_FETCH_BYTES: Final[int] = 1 << 20  # 1 MiB hard cap

_SFTP_ERRNO_NAMES: Final[dict[int, str]] = {
    1: "SSH_FX_EOF",
    2: "SSH_FX_NO_SUCH_FILE",
    3: "SSH_FX_PERMISSION_DENIED",
    4: "SSH_FX_FAILURE",
    5: "SSH_FX_BAD_MESSAGE",
    6: "SSH_FX_NO_CONNECTION",
    7: "SSH_FX_CONNECTION_LOST",
    8: "SSH_FX_OP_UNSUPPORTED",
}


class _CapExceeded(Exception):
    """Sentinel for the 1 MiB safety cap."""


def _classify_error(exc: BaseException) -> str:
    """Map an exception to the canonical FetchedFile.error string."""
    if isinstance(exc, asyncssh.SFTPError):
        return _SFTP_ERRNO_NAMES.get(int(exc.code), "SSH_FX_UNKNOWN")
    return type(exc).__name__


async def _fetch_one(sftp: asyncssh.SFTPClient, spec: FileSpec, timeout: float) -> FetchedFile:
    """Fetch one file, mapping every *expected* failure to a ``FetchedFile.error``.

    Owns the whole per-file decision tree — the 1 MiB cap checked twice (from
    ``stat`` before opening, and again on the bytes actually read, because the
    file may grow in between) and the transport-error classification. Anything
    outside ``_SFTP_TRANSPORT_ERRORS`` is a programmer error and propagates, so
    the caller's own handler decides what a dead channel means for the rest of
    the batch.
    """
    try:

        async def _read_remote_file() -> bytes | str:
            """Keep stat and read in one timeout so metadata cannot hang startup."""
            stat = await sftp.stat(spec.path)
            if stat.size is not None and stat.size > _MAX_FETCH_BYTES:
                raise _CapExceeded
            async with sftp.open(spec.path, "rb") as fh:
                return await fh.read(_MAX_FETCH_BYTES + 1)

        data = await asyncio.wait_for(_read_remote_file(), timeout=timeout)
        raw: bytes = data if isinstance(data, bytes) else data.encode()
        if len(raw) > _MAX_FETCH_BYTES:
            raise _CapExceeded
        return FetchedFile(
            content_b64=base64.b64encode(raw).decode("ascii"),
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    except _CapExceeded:
        return FetchedFile(error="EFBIG")
    except _SFTP_TRANSPORT_ERRORS as exc:
        return FetchedFile(error=_classify_error(exc))


def _record_channel_failure(
    specs: dict[str, FileSpec],
    results: dict[str, FetchedFile],
    required_failures: list[str],
    code: str,
) -> None:
    """Attribute a whole-channel failure to every spec not already resolved.

    Files fetched before the channel died keep their own result; the ones that
    never got a turn inherit the channel's error code. When the channel fails
    before the first file, that is simply every spec.
    """
    for name, spec in specs.items():
        if name in results:
            continue
        results[name] = FetchedFile(error=code)
        if spec.required:
            required_failures.append(name)


async def fetch_files(
    conn: asyncssh.SSHClientConnection,
    specs: dict[str, FileSpec],
    *,
    timeout: float,
) -> tuple[dict[str, FetchedFile], list[str]]:
    """Fetch all files for a node over one SFTP channel within each-file timeout."""
    if not specs:
        return {}, []

    results: dict[str, FetchedFile] = {}
    required_failures: list[str] = []

    try:
        sftp_cm = conn.start_sftp_client()
    except _SFTP_TRANSPORT_ERRORS as exc:
        _record_channel_failure(specs, results, required_failures, _classify_error(exc))
        return results, required_failures

    try:
        async with sftp_cm as sftp:
            for name, spec in specs.items():
                fetched = await _fetch_one(sftp, spec, timeout)
                results[name] = fetched
                if fetched.error is not None and spec.required:
                    required_failures.append(name)
    except _SFTP_TRANSPORT_ERRORS as exc:
        _record_channel_failure(specs, results, required_failures, _classify_error(exc))

    return results, required_failures
