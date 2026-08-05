"""Provider-neutral email identity state and policy."""

from control_plane.email.models import EmailIdentityRecord, EmailIdentityStatus, EmailOption
from control_plane.email.repository import EmailIdentityRepository

__all__ = [
    "EmailIdentityRecord",
    "EmailIdentityRepository",
    "EmailIdentityStatus",
    "EmailOption",
]
