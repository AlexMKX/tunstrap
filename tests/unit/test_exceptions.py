"""Exception hierarchy + exit-code mapping.

Validates: every TunstrapError subclass has the expected exit code
and to_error_output redacts secret keys.
Code: tunstrap/exceptions.py
"""

from __future__ import annotations

import pytest

from tunstrap.exceptions import (
    DaemonError,
    RequiredTunnelFailure,
    SchemaValidationError,
    SessionActive,
    TunnelStartupError,
    TunstrapError,
    exit_code_for,
)

pytestmark = pytest.mark.unit


def test_all_errors_inherit_base() -> None:
    """All public error classes inherit from TunstrapError."""
    for cls in [
        SchemaValidationError,
        TunnelStartupError,
        RequiredTunnelFailure,
        DaemonError,
    ]:
        assert issubclass(cls, TunstrapError)


@pytest.mark.parametrize(
    "exc, expected_code",
    [
        (SchemaValidationError("bad", {"field": "host"}), 1),
        (RequiredTunnelFailure("nope", {"failed": ["a"]}), 2),
        (DaemonError("fork failed", {"errno": 12}), 4),
    ],
)
def test_exit_code_for_known_errors(exc: TunstrapError, expected_code: int) -> None:
    """exit_code_for maps each known error to its documented exit code."""
    assert exit_code_for(exc) == expected_code


def test_to_error_output_does_not_leak_secrets() -> None:
    """to_error_output strips ssh_pkey/ssh_password from the details payload."""
    err = SchemaValidationError("bad", {"ssh_pkey": "-----BEGIN PRIVATE KEY-----..."})
    out = err.to_error_output()
    assert out["error"] == "SchemaValidationError"
    assert out["message"] == "bad"
    assert "ssh_pkey" not in out["details"]


def test_to_error_output_recursively_scrubs_secrets() -> None:
    """Nested validation details cannot retain SSH credentials."""
    err = SchemaValidationError(
        "bad",
        {
            "nested": {"ssh_password": "nested-password", "safe": "value"},
            "errors": [
                {"input": {"ssh_pkey": "nested-key", "safe": "still-here"}},
                {"ssh_pkey_passphrase": "nested-passphrase"},
            ],
            "nested_lists": [[{"ssh_password": "list-password", "safe": "list-safe"}]],
        },
    )

    assert err.to_error_output()["details"] == {
        "nested": {"safe": "value"},
        "errors": [{"input": {"safe": "still-here"}}, {}],
        "nested_lists": [[{"safe": "list-safe"}]],
    }


def test_session_active_exit_code_is_3() -> None:
    """SessionActive maps to exit code 3 and reports the correct error name."""
    exc = SessionActive("daemon already running")
    assert exit_code_for(exc) == 3
    assert exc.to_error_output()["error"] == "SessionActive"


def test_multi_node_env_unsupported_is_tunstrap_error() -> None:
    """MultiNodeEnvUnsupported is a TunstrapError so run's typed handler catches it."""
    from tunstrap.exceptions import MultiNodeEnvUnsupported

    assert issubclass(MultiNodeEnvUnsupported, TunstrapError)


def test_multi_node_env_unsupported_exit_code_is_1() -> None:
    """MultiNodeEnvUnsupported maps to exit 1 and names itself in the error envelope."""
    from tunstrap.exceptions import MultiNodeEnvUnsupported

    exc = MultiNodeEnvUnsupported("needs --output-var", {"nodes": ["a", "b"]})
    assert exit_code_for(exc) == 1
    assert exc.to_error_output()["error"] == "MultiNodeEnvUnsupported"
    assert exc.to_error_output()["details"] == {"nodes": ["a", "b"]}


def test_multi_node_env_unsupported_is_registered_not_defaulted() -> None:
    """The registry entry is asserted directly, because 1 is also the default.

    ``exit_code_for`` returns 1 for any unregistered type, so
    ``exit_code_for(MultiNodeEnvUnsupported(...)) == 1`` holds whether or not
    the mapping exists -- the assertion above cannot see the entry being
    deleted. Reading ``_EXIT_CODES`` directly can: removing the line raises
    KeyError here.
    """
    from tunstrap.exceptions import _EXIT_CODES, MultiNodeEnvUnsupported

    assert _EXIT_CODES[MultiNodeEnvUnsupported] == 1
