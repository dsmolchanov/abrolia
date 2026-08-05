"""Provider-neutral runtime email contracts and persistence."""

from hermes_cloud.email.contracts import EmailBinding, EmailDeliveryReceipt
from hermes_cloud.email.receipts import EmailBindingStore, EmailSendStore

__all__ = [
    "EmailBinding",
    "EmailBindingStore",
    "EmailDeliveryReceipt",
    "EmailSendStore",
]
