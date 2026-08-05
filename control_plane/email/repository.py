from __future__ import annotations

import json
import sqlite3
import time

from control_plane.crypto import normalize_email, reject_secret_fields
from control_plane.db import new_id
from control_plane.email.local_part import normalize_local_part
from control_plane.email.models import EmailIdentityRecord, EmailIdentityStatus, EmailOption
from control_plane.repositories.base import Repository

RESERVATION_TTL_SECONDS = 15 * 60


def _mask_email(address: str) -> str:
    local, domain = normalize_email(address).split("@", 1)
    visible = local[:2]
    return f"{visible}{'*' * max(1, len(local) - len(visible))}@{domain}"


class EmailIdentityRepository(Repository):
    def _record(self, row) -> EmailIdentityRecord:
        address = None
        subject = None
        if row["address_ciphertext"] is not None:
            address = self.decrypt_json(
                "email_identities",
                row["id"],
                "address",
                row["address_ciphertext"],
                row["encryption_key_version"],
            )
        if row["provider_subject_ciphertext"] is not None:
            subject = self.decrypt_json(
                "email_identities",
                row["id"],
                "provider_subject",
                row["provider_subject_ciphertext"],
                row["encryption_key_version"],
            )
        return EmailIdentityRecord(
            id=row["id"],
            household_id=row["household_id"],
            option=EmailOption(row["option"]),
            status=EmailIdentityStatus(row["status"]),
            address=address,
            address_masked=row["address_masked"],
            provider_subject=subject,
            provider_resource_refs=json.loads(row["provider_resource_refs_json"]),
            secret_binding_ref=row["secret_binding_ref"],
            granted_scopes=tuple(json.loads(row["granted_scopes_json"] or "[]")),
            version=row["version"],
            verified_at=row["verified_at"],
            activated_at=row["activated_at"],
            disconnected_at=row["disconnected_at"],
        )

    def get(self, identity_id: str) -> EmailIdentityRecord | None:
        row = self.db.query_one("SELECT * FROM email_identities WHERE id = ?", (identity_id,))
        return self._record(row) if row else None

    def current_for_household(self, household_id: str) -> EmailIdentityRecord | None:
        row = self.db.query_one(
            "SELECT * FROM email_identities WHERE household_id = ?"
            " AND status NOT IN ('disconnecting','deleted')"
            " ORDER BY created_at DESC LIMIT 1",
            (household_id,),
        )
        return self._record(row) if row else None

    def create_selected(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
        option: EmailOption,
        address: str | None = None,
        reservation_ttl_seconds: int = RESERVATION_TTL_SECONDS,
        now: float | None = None,
    ) -> EmailIdentityRecord:
        now = time.time() if now is None else now
        identity_id = new_id()
        encrypted_address = None
        address_hmac = None
        address_masked = None
        normalized_address = None
        if address is not None:
            normalized_address = normalize_email(address)
            encrypted_address = self.encrypt_json(
                "email_identities", identity_id, "address", normalized_address
            )
            address_hmac = self.lookup.email(normalized_address)
            address_masked = _mask_email(normalized_address)
        key_version = (
            encrypted_address.key_version
            if encrypted_address is not None
            else self.cipher.active_version
        )
        connection.execute(
            "INSERT INTO email_identities (id, household_id, option, status,"
            " address_ciphertext, address_lookup_hmac, address_masked,"
            " encryption_key_version, created_at, updated_at)"
            " VALUES (?, ?, ?, 'selected', ?, ?, ?, ?, ?, ?)",
            (
                identity_id,
                household_id,
                option.value,
                encrypted_address.ciphertext if encrypted_address else None,
                address_hmac,
                address_masked,
                key_version,
                now,
                now,
            ),
        )
        if normalized_address is not None:
            local_part, domain = normalized_address.split("@", 1)
            reservation = connection.execute(
                "INSERT INTO email_address_reservations (id, normalized_domain,"
                " normalized_local_part, household_id, email_identity_id, status,"
                " expires_at, created_at) VALUES (?, ?, ?, ?, ?, 'held', ?, ?)"
                " ON CONFLICT (normalized_domain, normalized_local_part) DO UPDATE SET"
                " id = excluded.id, household_id = excluded.household_id,"
                " email_identity_id = excluded.email_identity_id, status = 'held',"
                " expires_at = excluded.expires_at, created_at = excluded.created_at,"
                " consumed_at = NULL WHERE email_address_reservations.status"
                " IN ('released','expired') OR (email_address_reservations.status = 'held'"
                " AND email_address_reservations.expires_at <= excluded.created_at)",
                (
                    new_id(),
                    domain,
                    normalize_local_part(local_part),
                    household_id,
                    identity_id,
                    now + reservation_ttl_seconds,
                    now,
                ),
            )
            if reservation.rowcount != 1:
                raise sqlite3.IntegrityError("email address is already reserved")
        row = connection.execute(
            "SELECT * FROM email_identities WHERE id = ?", (identity_id,)
        ).fetchone()
        return self._record(row)

    def address_available(
        self, domain: str, local_part: str, *, now: float | None = None
    ) -> bool:
        now = time.time() if now is None else now
        local_part = normalize_local_part(local_part)
        domain = domain.strip().casefold().rstrip(".")
        row = self.db.query_one(
            "SELECT status, expires_at FROM email_address_reservations"
            " WHERE normalized_domain = ? AND normalized_local_part = ?",
            (domain, local_part),
        )
        return row is None or row["status"] in {"released", "expired"} or (
            row["status"] == "held" and row["expires_at"] <= now
        )

    def mark_provisioning(
        self, connection: sqlite3.Connection, identity_id: str, *, now: float
    ) -> None:
        connection.execute(
            "UPDATE email_identities SET status = 'provisioning', version = version + 1,"
            " updated_at = ? WHERE id = ? AND status = 'selected'",
            (now, identity_id),
        )

    def retry_provisioning(
        self, connection: sqlite3.Connection, identity_id: str, *, now: float
    ) -> None:
        updated = connection.execute(
            "UPDATE email_identities SET status = 'provisioning', version = version + 1,"
            " updated_at = ? WHERE id = ?"
            " AND status IN ('provisioning','waiting_user','needs_attention','outcome_unknown')",
            (now, identity_id),
        )
        if updated.rowcount != 1:
            raise ValueError("email identity cannot be retried from its current state")

    def mark_problem(
        self,
        connection: sqlite3.Connection,
        identity_id: str,
        *,
        status: EmailIdentityStatus,
        now: float,
    ) -> None:
        if status not in {
            EmailIdentityStatus.WAITING_USER,
            EmailIdentityStatus.NEEDS_ATTENTION,
            EmailIdentityStatus.OUTCOME_UNKNOWN,
        }:
            raise ValueError("invalid email identity problem state")
        connection.execute(
            "UPDATE email_identities SET status = ?, version = version + 1, updated_at = ?"
            " WHERE id = ? AND status NOT IN ('disconnecting','deleted')",
            (status.value, now, identity_id),
        )

    def mark_verified(
        self,
        connection: sqlite3.Connection,
        identity_id: str,
        *,
        address: str,
        provider_subject: str | None,
        provider_refs: dict[str, str],
        secret_binding_ref: str | None,
        granted_scopes: list[str] | tuple[str, ...],
        now: float,
    ) -> None:
        reject_secret_fields({
            "provider_refs": provider_refs,
            "secret_binding_ref": secret_binding_ref,
            "granted_scopes": list(granted_scopes),
        })
        normalized = normalize_email(address)
        address_field = self.encrypt_json(
            "email_identities", identity_id, "address", normalized
        )
        subject_field = (
            self.encrypt_json(
                "email_identities", identity_id, "provider_subject", provider_subject
            )
            if provider_subject
            else None
        )
        updated = connection.execute(
            "UPDATE email_identities SET status = 'verified', address_ciphertext = ?,"
            " address_lookup_hmac = ?, address_masked = ?, provider_subject_ciphertext = ?,"
            " provider_resource_refs_json = ?, secret_binding_ref = ?,"
            " granted_scopes_json = ?, encryption_key_version = ?, verified_at = ?,"
            " version = version + 1, updated_at = ? WHERE id = ?"
            " AND status IN ('selected','provisioning','waiting_user','outcome_unknown')",
            (
                address_field.ciphertext,
                self.lookup.email(normalized),
                _mask_email(normalized),
                subject_field.ciphertext if subject_field else None,
                self.public_json(provider_refs),
                secret_binding_ref,
                self.public_json(sorted(set(granted_scopes))),
                address_field.key_version,
                now,
                now,
                identity_id,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("email identity is not verifiable from its current state")
        connection.execute(
            "UPDATE email_address_reservations SET status = 'consumed', consumed_at = ?"
            " WHERE email_identity_id = ? AND status = 'held'",
            (now, identity_id),
        )

    def begin_disconnect(
        self, connection: sqlite3.Connection, household_id: str, *, now: float
    ) -> None:
        connection.execute(
            "UPDATE email_identities SET status = 'disconnecting', version = version + 1,"
            " updated_at = ? WHERE household_id = ?"
            " AND status NOT IN ('disconnecting','deleted')",
            (now, household_id),
        )

    def begin_activation(
        self, connection: sqlite3.Connection, identity_id: str, *, now: float
    ) -> None:
        updated = connection.execute(
            "UPDATE email_identities SET status = 'activating', version = version + 1,"
            " updated_at = ? WHERE id = ? AND status = 'verified'",
            (now, identity_id),
        )
        if updated.rowcount != 1:
            raise ValueError("only a verified email identity can activate")

    def record_activation_receipt(
        self,
        connection: sqlite3.Connection,
        identity_id: str,
        *,
        desired_revision: int,
        runtime_ref: str,
        provider: str,
        inbound_check: str,
        outbound_check: str,
        receipt_digest: str,
        now: float,
    ) -> EmailIdentityStatus:
        checks = {inbound_check, outbound_check}
        if not checks <= {"pending", "healthy", "failed"}:
            raise ValueError("invalid activation check")
        if len(receipt_digest) != 64 or any(
            character not in "0123456789abcdef" for character in receipt_digest
        ):
            raise ValueError("invalid activation receipt digest")
        if "failed" in checks:
            status = EmailIdentityStatus.NEEDS_ATTENTION
        elif checks == {"healthy"}:
            status = EmailIdentityStatus.ACTIVE
        else:
            status = EmailIdentityStatus.ACTIVATING
        connection.execute(
            "INSERT INTO email_activation_receipts (email_identity_id, desired_revision,"
            " runtime_ref, provider, inbound_check, outbound_check, checked_at,"
            " receipt_digest, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (email_identity_id, desired_revision) DO UPDATE SET"
            " runtime_ref = excluded.runtime_ref, provider = excluded.provider,"
            " inbound_check = excluded.inbound_check, outbound_check = excluded.outbound_check,"
            " checked_at = excluded.checked_at, receipt_digest = excluded.receipt_digest,"
            " status = excluded.status",
            (
                identity_id,
                desired_revision,
                runtime_ref,
                provider,
                inbound_check,
                outbound_check,
                now,
                receipt_digest,
                status.value,
            ),
        )
        connection.execute(
            "UPDATE email_identities SET status = ?, activated_at = CASE WHEN ? = 'active'"
            " THEN ? ELSE activated_at END, version = version + 1, updated_at = ?"
            " WHERE id = ? AND status IN ('verified','activating','active','needs_attention')",
            (status.value, status.value, now, now, identity_id),
        )
        return status

    def finish_disconnect(
        self, connection: sqlite3.Connection, household_id: str, *, now: float
    ) -> None:
        connection.execute(
            "UPDATE email_identities SET status = 'deleted', disconnected_at = ?,"
            " version = version + 1, updated_at = ? WHERE household_id = ?"
            " AND status = 'disconnecting'",
            (now, now, household_id),
        )
        connection.execute(
            "UPDATE email_address_reservations SET status = 'released'"
            " WHERE household_id = ? AND status IN ('held','consumed')",
            (household_id,),
        )

    def expire_reservations(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self.db.write() as connection:
            result = connection.execute(
                "UPDATE email_address_reservations SET status = 'expired'"
                " WHERE status = 'held' AND expires_at <= ?",
                (now,),
            )
        return result.rowcount
