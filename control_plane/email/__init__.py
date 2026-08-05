"""Provider-neutral email identity state and policy.

Imports are intentionally lazy because the root control-plane contracts use the
domain policy while repository classes depend on those root contracts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from control_plane.email.models import EmailIdentityRecord, EmailIdentityStatus, EmailOption
    from control_plane.email.repository import EmailIdentityRepository

__all__ = [
    "EmailIdentityRecord",
    "EmailIdentityRepository",
    "EmailIdentityStatus",
    "EmailOption",
]


def __getattr__(name: str) -> Any:
    if name in {"EmailIdentityRecord", "EmailIdentityStatus", "EmailOption"}:
        from control_plane.email import models

        return getattr(models, name)
    if name == "EmailIdentityRepository":
        from control_plane.email.repository import EmailIdentityRepository

        return EmailIdentityRepository
    raise AttributeError(name)
