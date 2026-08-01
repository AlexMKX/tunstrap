"""Vulture whitelist: symbols flagged as unused but kept on purpose.

Pydantic ``@field_validator`` classmethods receive ``cls`` as their first
positional argument by API contract but do not reference it inside the
function body. Vulture cannot tell ``cls`` apart from a regular unused
local. List one reference per validator so vulture's 100%-confidence
"unused variable" finding is silenced without weakening ``min_confidence``.
"""

from __future__ import annotations

from tunstrap import schemas as _schemas

# Each @field_validator below has a ``cls`` parameter we cannot omit.
_ = _schemas.FileSpec._validate_absolute
_ = _schemas.NodeInput._validate_fetch_files
_ = _schemas.InputSchema._validate_auth
