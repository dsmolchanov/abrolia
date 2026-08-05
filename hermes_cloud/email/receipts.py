"""Persistence for public email bindings and idempotent delivery receipts."""

from __future__ import annotations

import json
import time

from hermes_cloud.core.db import Database
from hermes_cloud.email.contracts import EmailBinding, EmailDeliveryReceipt


class BindingRevisionChanged(RuntimeError):
    """An approval targets a binding that is no longer active."""


class EmailBindingStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    def activate(self, binding: EmailBinding, *, now: float | None = None) -> EmailBinding:
        now = self.clock() if now is None else now
        if binding.revision < 1:
            raise ValueError("email binding revision must be positive")
        if not binding.identity_id or not binding.provider or "@" not in binding.address:
            raise ValueError("email binding is incomplete")
        with self.db.write() as connection:
            existing = connection.execute(
                "SELECT * FROM email_bindings WHERE identity_id = ? AND revision = ?",
                (binding.identity_id, binding.revision),
            ).fetchone()
            if existing is not None and (
                existing["provider"] != binding.provider
                or existing["address"].casefold() != binding.address.casefold()
                or existing["provider_ref"] != binding.provider_ref
                or tuple(json.loads(existing["secret_names_json"])) != binding.secret_names
            ):
                raise ValueError("email binding revision is immutable")
            connection.execute(
                "UPDATE email_bindings SET state = 'superseded', updated_at = ?"
                " WHERE state = 'active' AND (identity_id != ? OR revision != ?)",
                (now, binding.identity_id, binding.revision),
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO email_bindings (identity_id, revision, provider, address,"
                    " provider_ref, secret_names_json, state, activated_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    (
                        binding.identity_id,
                        binding.revision,
                        binding.provider,
                        binding.address,
                        binding.provider_ref,
                        json.dumps(binding.secret_names, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE email_bindings SET state = 'active', updated_at = ?"
                    " WHERE identity_id = ? AND revision = ?",
                    (now, binding.identity_id, binding.revision),
                )
        return binding

    def current(self) -> EmailBinding | None:
        row = self.db.query_one("SELECT * FROM email_bindings WHERE state = 'active'")
        if row is None:
            return None
        return EmailBinding(
            identity_id=row["identity_id"],
            revision=row["revision"],
            provider=row["provider"],
            address=row["address"],
            provider_ref=row["provider_ref"],
            secret_names=tuple(json.loads(row["secret_names_json"])),
        )

    def require_current(self, identity_id: str, revision: int) -> EmailBinding:
        current = self.current()
        if (
            current is None
            or current.identity_id != identity_id
            or current.revision != revision
        ):
            raise BindingRevisionChanged(
                "email binding changed after approval; cancel and stage a new message"
            )
        return current


class EmailSendStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    def begin(
        self,
        *,
        effect_id: str,
        approval_id: str,
        binding: EmailBinding,
        request_sha256: str,
        message_id: str,
        now: float | None = None,
    ) -> tuple[str, bool]:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            inserted = connection.execute(
                "INSERT INTO email_sends (effect_id, approval_id, binding_identity_id,"
                " binding_revision, request_sha256, provider_idempotency_key, state,"
                " message_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?,"
                " 'pending', ?, ?, ?) ON CONFLICT (effect_id) DO NOTHING",
                (
                    effect_id,
                    approval_id,
                    binding.identity_id,
                    binding.revision,
                    request_sha256,
                    effect_id,
                    message_id,
                    now,
                    now,
                ),
            ).rowcount == 1
        row = self.db.query_one("SELECT * FROM email_sends WHERE effect_id = ?", (effect_id,))
        assert row is not None
        if (
            row["request_sha256"] != request_sha256
            or row["binding_identity_id"] != binding.identity_id
            or row["binding_revision"] != binding.revision
        ):
            raise ValueError("effect_id is already bound to another email request")
        return str(row["state"]), inserted

    def settle(
        self,
        receipt: EmailDeliveryReceipt,
        *,
        error_code: str | None = None,
        now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE email_sends SET state = ?, provider_ref = ?, error_code = ?,"
                " updated_at = ? WHERE effect_id = ?",
                (
                    receipt.status,
                    receipt.provider_ref,
                    error_code,
                    now,
                    receipt.effect_id,
                ),
            )
            connection.execute(
                "INSERT INTO email_delivery_receipts (effect_id, approval_id, message_id,"
                " provider_ref, state, accepted_at, reconciled_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (effect_id) DO UPDATE SET provider_ref = excluded.provider_ref,"
                " state = excluded.state, accepted_at = excluded.accepted_at,"
                " reconciled_at = excluded.reconciled_at, updated_at = excluded.updated_at",
                (
                    receipt.effect_id,
                    receipt.approval_id,
                    receipt.message_id,
                    receipt.provider_ref,
                    receipt.status,
                    receipt.accepted_at,
                    now if receipt.status != "accepted" else None,
                    now,
                    now,
                ),
            )

    def get(self, effect_id: str) -> EmailDeliveryReceipt | None:
        row = self.db.query_one(
            "SELECT * FROM email_delivery_receipts WHERE effect_id = ?", (effect_id,)
        )
        if row is None:
            return None
        return EmailDeliveryReceipt(
            effect_id=row["effect_id"],
            approval_id=row["approval_id"],
            message_id=row["message_id"],
            provider_ref=row["provider_ref"],
            accepted_at=row["accepted_at"],
            status=row["state"],
        )
