"""The session-dir primitives issue #33 added: record, read, release-without-delete.

Validates: ``release_lock_preserving_data`` drops only the flock; the loss
record round-trips through a real file; and reading it never raises, whatever
is (or is not) on disk.
Code: tunstrap/session.py::release_lock_preserving_data,
      tunstrap/session.py::write_tunnel_loss,
      tunstrap/session.py::read_tunnel_loss
Assertion: real filesystem state and a real ``fcntl.flock`` re-acquisition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tunstrap.exceptions import SessionActive
from tunstrap.session import SessionDir

pytestmark = pytest.mark.unit


def test_release_preserving_data_frees_the_lock_for_a_new_session(tmp_path: Path) -> None:
    """The half of decision 1 that unblocks ``start``, proved by taking the lock.

    ``SessionActive`` is raised from ``acquire_session_lock``'s
    ``BlockingIOError``, so the only honest proof that the lock is really gone
    is a second ``SessionDir.create`` on the same root succeeding. Asserting the
    absence of ``session.lock`` alone would pass even if the fd were still held
    open on an unlinked inode.
    """
    root = tmp_path / "s"
    first = SessionDir.create(supplied=str(root))
    with pytest.raises(SessionActive):
        SessionDir.create(supplied=str(root))

    first.release_lock_preserving_data()

    second = SessionDir.create(supplied=str(root))
    second.cleanup()


def test_release_preserving_data_deletes_nothing(tmp_path: Path) -> None:
    """The other half: every file the daemon materialized is still there.

    A generated root is used because that is the case ``cleanup`` would
    ``rmtree`` *entirely* -- the widest possible deletion, and so the strongest
    statement that this path performs none of it.
    """
    session = SessionDir.create(supplied=None, base=tmp_path)
    root = Path(session.session_dir)
    (root / "tunnel-data" / "materialized.kubeconfig").write_text("credential-bearing")
    session.write_identity(pid=4242)

    session.release_lock_preserving_data()

    assert root.is_dir(), "a generated root was removed on the preserve path"
    assert (root / "tunnel-data" / "materialized.kubeconfig").read_text() == "credential-bearing"
    assert (root / "tunnel-data" / "daemon.pid").read_text() == "4242\n"


def test_loss_record_round_trips(tmp_path: Path) -> None:
    """What the daemon writes is what ``status`` and ``run``'s teardown read back."""
    session = SessionDir.create(supplied=str(tmp_path / "s"))
    payload: dict[str, object] = {"self_terminated": True, "losses": [{"node": "edge"}]}

    session.write_tunnel_loss(payload)

    assert SessionDir.read_tunnel_loss(session.session_dir) == payload
    session.cleanup()


@pytest.mark.parametrize(
    "shape",
    ["absent", "unparsable", "not-an-object", "a-directory"],
)
def test_read_tunnel_loss_never_raises(tmp_path: Path, shape: str) -> None:
    """Both readers decorate a command that has already decided something else.

    ``status`` still has to answer ``alive`` and ``run``'s teardown still has to
    stop the daemon, so an unreadable record must degrade to "nothing to add"
    rather than fail the verb it annotates. A truncated file is the realistic
    shape here: the writer is a daemon that can be SIGKILLed mid-write.
    """
    data = tmp_path / "tunnel-data"
    data.mkdir()
    record = data / "tunnel-loss.json"
    if shape == "unparsable":
        record.write_text('{"self_terminated": tr')
    elif shape == "not-an-object":
        record.write_text("[1, 2, 3]")
    elif shape == "a-directory":
        record.mkdir()

    assert SessionDir.read_tunnel_loss(str(tmp_path)) is None
