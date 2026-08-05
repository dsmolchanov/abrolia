import base64
from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.email.service import EmailRuntimeService
from hermes_cloud.ingest.gmail_api import (
    GmailAuthRevoked,
    GmailHistoryExpired,
    GmailHistoryGap,
    GmailHistorySource,
    GmailMalformedMessage,
    GmailQuotaExceeded,
)

RAW = b"From: school@example.test\r\nTo: agent@example.test\r\nMessage-ID: <one@example.test>\r\nSubject: One\r\n\r\nBody"
BINDING = EmailBinding("identity-1", 1, "gmail", "agent@example.test")


class FakeGmail:
    def __init__(self):
        self.profile_id = "100"
        self.history_pages = []
        self.messages = {}
        self.list_page = {"messages": []}
        self.error = None

    def profile(self):
        return {"historyId": self.profile_id}

    def history(self, start_history_id, page_token=None):
        if self.error:
            raise self.error
        assert start_history_id == "100"
        return self.history_pages.pop(0)

    def message(self, message_id):
        return self.messages[message_id]

    def list_inbox(self, page_token=None, *, max_results):
        return self.list_page


def encoded(raw=RAW, *, labels=("INBOX",), message_id="m1"):
    return {
        "id": message_id,
        "labelIds": list(labels),
        "raw": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
    }


def source_world(tmp_path: Path):
    db = open_database(tmp_path / "runtime.db")
    client = FakeGmail()
    source = GmailHistorySource(db, BINDING, client, clock=lambda: 200.0, resync_limit=2)
    service = EmailRuntimeService(db, [source], clock=lambda: 200.0)
    service.activate(BINDING)
    return db, client, source, service


def test_initial_cursor_imports_no_history_then_advances_after_append(tmp_path: Path) -> None:
    db, client, _source, service = source_world(tmp_path)
    assert service.run_once() == 0
    assert db.query_one("SELECT cursor FROM email_sync_state")["cursor"] == "100"

    client.history_pages = [
        {
            "historyId": "102",
            "history": [{"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "sent"}}]}],
        }
    ]
    client.messages = {"m1": encoded(), "sent": encoded(labels=("SENT",), message_id="sent")}
    assert service.run_once() == 1
    assert db.query_one("SELECT cursor FROM email_sync_state")["cursor"] == "102"
    assert db.query_one("SELECT COUNT(*) AS n FROM email_ingress_receipts")["n"] == 1


def test_cursor_does_not_advance_when_durable_ingest_fails(tmp_path: Path, monkeypatch) -> None:
    db, client, _source, service = source_world(tmp_path)
    service.run_once()
    client.history_pages = [
        {"historyId": "102", "history": [{"messagesAdded": [{"message": {"id": "bad"}}]}]}
    ]
    client.messages = {"bad": encoded(message_id="bad")}
    monkeypatch.setattr(
        "hermes_cloud.email.service.ingest_rfc822",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk failure")),
    )
    with pytest.raises(RuntimeError, match="disk failure"):
        service.run_once()
    assert db.query_one("SELECT cursor FROM email_sync_state")["cursor"] == "100"


def test_expired_history_uses_bounded_resync_and_refuses_unbounded_gap(tmp_path: Path) -> None:
    db, client, source, service = source_world(tmp_path)
    service.run_once()
    client.error = GmailHistoryExpired("expired")
    client.profile_id = "120"
    client.list_page = {"messages": [{"id": "m1"}]}
    client.messages = {"m1": encoded()}
    assert service.run_once() == 1
    assert db.query_one("SELECT cursor FROM email_sync_state")["cursor"] == "120"

    client.list_page = {"messages": [{"id": "m1"}], "nextPageToken": "more"}
    with pytest.raises(GmailHistoryGap):
        source.poll()
    assert db.query_one("SELECT health FROM email_sync_state")["health"] == "needs_attention"


@pytest.mark.parametrize(
    ("error", "health"),
    [(GmailQuotaExceeded(5), "quota_backoff"), (GmailAuthRevoked("revoked"), "auth_revoked")],
)
def test_quota_and_revocation_are_visible_health(tmp_path: Path, error, health) -> None:
    db, client, source, service = source_world(tmp_path)
    service.run_once()
    client.error = error
    with pytest.raises(type(error)):
        source.poll()
    assert db.query_one("SELECT health FROM email_sync_state")["health"] == health


def test_stale_cursor_health_contains_no_mailbox_address(tmp_path: Path) -> None:
    db, _client, _source, service = source_world(tmp_path)
    service.run_once()
    later = EmailRuntimeService(db, clock=lambda: 500.0)
    reported = later.health()
    assert reported.status == "stale_cursor"
    assert "@" not in repr(reported)


def test_malformed_raw_does_not_advance_cursor(tmp_path: Path) -> None:
    db, client, source, service = source_world(tmp_path)
    service.run_once()
    client.history_pages = [
        {"historyId": "102", "history": [{"messagesAdded": [{"message": {"id": "bad"}}]}]}
    ]
    client.messages = {"bad": encoded(b"Subject: no identity\r\n\r\nBody", message_id="bad")}
    with pytest.raises(GmailMalformedMessage):
        source.poll()
    assert db.query_one("SELECT cursor FROM email_sync_state")["cursor"] == "100"
