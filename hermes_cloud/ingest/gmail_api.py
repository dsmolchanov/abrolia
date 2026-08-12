"""Gmail History adapter with a durable, append-before-cursor contract."""

from __future__ import annotations

import base64
import time
from collections.abc import Sequence
from email import policy
from email.parser import BytesParser
from typing import Any, Protocol

from hermes_cloud.core.db import Database
from hermes_cloud.email.contracts import EmailBinding, InboundEmail


class GmailError(RuntimeError):
    pass


class GmailQuotaExceeded(GmailError):
    def __init__(self, retry_after: float = 60.0) -> None:
        super().__init__("gmail_quota")
        self.retry_after = retry_after


class GmailAuthRevoked(GmailError):
    pass


class GmailHistoryExpired(GmailError):
    pass


class GmailHistoryGap(GmailError):
    pass


class GmailMalformedMessage(GmailError):
    pass


class GmailApi(Protocol):
    def profile(self) -> dict[str, Any]: ...
    def history(self, start_history_id: str, page_token: str | None = None) -> dict[str, Any]: ...
    def message(self, message_id: str) -> dict[str, Any]: ...
    def list_inbox(self, page_token: str | None = None, *, max_results: int) -> dict[str, Any]: ...


class GmailHistorySource:
    provider = "gmail"

    def __init__(
        self,
        database: Database,
        binding: EmailBinding,
        client: GmailApi,
        *,
        clock=time.time,
        resync_limit: int = 100,
        quota_backoff: float = 60.0,
    ) -> None:
        if binding.provider != "gmail":
            raise ValueError("Gmail source requires a Gmail binding")
        self.db = database
        self.binding = binding
        self.client = client
        self.clock = clock
        self.resync_limit = resync_limit
        self.quota_backoff = quota_backoff
        self._pending_cursor: str | None = None

    def _state(self):
        return self.db.query_one(
            "SELECT * FROM email_sync_state WHERE binding_identity_id = ? AND binding_revision = ?",
            (self.binding.identity_id, self.binding.revision),
        )

    def _write_state(
        self,
        *,
        cursor: str | None,
        health: str,
        success: bool = False,
        backoff_until: float | None = None,
    ) -> None:
        now = self.clock()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO email_sync_state (binding_identity_id, binding_revision, cursor,"
                " connected_at, last_success_at, backoff_until, health, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (binding_identity_id, binding_revision) DO UPDATE SET"
                " cursor = excluded.cursor, last_success_at = CASE WHEN ? THEN excluded.last_success_at"
                " ELSE email_sync_state.last_success_at END, backoff_until = excluded.backoff_until,"
                " health = excluded.health, updated_at = excluded.updated_at",
                (
                    self.binding.identity_id,
                    self.binding.revision,
                    cursor,
                    now,
                    now if success else None,
                    backoff_until,
                    health,
                    now,
                    int(success),
                ),
            )

    @staticmethod
    def _decode(item: dict[str, Any]) -> InboundEmail | None:
        labels = {str(label).upper() for label in item.get("labelIds", [])}
        if "INBOX" not in labels or "SENT" in labels:
            return None
        message_id = str(item.get("id") or "")
        raw = item.get("raw")
        if not message_id or not isinstance(raw, str):
            raise GmailMalformedMessage("gmail_message_shape")
        try:
            data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (ValueError, TypeError) as error:
            raise GmailMalformedMessage("gmail_raw_invalid") from error
        if not data:
            raise GmailMalformedMessage("gmail_raw_empty")
        parsed = BytesParser(policy=policy.default).parsebytes(data, headersonly=True)
        if not parsed.get("Message-ID") or not parsed.get("From"):
            raise GmailMalformedMessage("gmail_raw_missing_identity_headers")
        return InboundEmail("gmail", f"gmail:{message_id}", data)

    def _bounded_resync(self) -> tuple[list[str], str]:
        ids: list[str] = []
        token: str | None = None
        while True:
            remaining = self.resync_limit - len(ids)
            if remaining <= 0:
                raise GmailHistoryGap("gmail_history_gap")
            page = self.client.list_inbox(token, max_results=min(100, remaining))
            ids.extend(str(item["id"]) for item in page.get("messages", []) if item.get("id"))
            token = page.get("nextPageToken")
            if len(ids) > self.resync_limit:
                raise GmailHistoryGap("gmail_history_gap")
            if not token:
                break
        cursor = str(self.client.profile().get("historyId") or "")
        if not cursor:
            raise GmailHistoryGap("gmail_profile_cursor_missing")
        return ids, cursor

    def poll(self) -> Sequence[InboundEmail]:
        state = self._state()
        if state is None:
            cursor = str(self.client.profile().get("historyId") or "")
            if not cursor:
                raise GmailHistoryGap("gmail_profile_cursor_missing")
            self._write_state(cursor=cursor, health="ready", success=True)
            return []
        if state["backoff_until"] is not None and state["backoff_until"] > self.clock():
            return []
        cursor = str(state["cursor"] or "")
        ids: list[str] = []
        latest = cursor
        token: str | None = None
        try:
            while True:
                page = self.client.history(cursor, token)
                latest = str(page.get("historyId") or latest)
                for record in page.get("history", []):
                    for added in record.get("messagesAdded", []):
                        message_id = str(added.get("message", {}).get("id") or "")
                        if message_id:
                            ids.append(message_id)
                token = page.get("nextPageToken")
                if not token:
                    break
        except GmailHistoryExpired:
            try:
                ids, latest = self._bounded_resync()
            except GmailHistoryGap:
                self._write_state(cursor=cursor, health="needs_attention")
                raise
        except GmailQuotaExceeded as error:
            self._write_state(
                cursor=cursor,
                health="quota_backoff",
                backoff_until=self.clock() + max(error.retry_after, self.quota_backoff),
            )
            raise
        except GmailAuthRevoked:
            self._write_state(cursor=cursor, health="auth_revoked")
            raise
        incoming = [
            item
            for message_id in dict.fromkeys(ids)
            if (item := self._decode(self.client.message(message_id)))
        ]
        self._pending_cursor = latest
        return incoming

    def ack(self) -> None:
        if self._pending_cursor is None:
            return
        self._write_state(cursor=self._pending_cursor, health="ready", success=True)
        self._pending_cursor = None
