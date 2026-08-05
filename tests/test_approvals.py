"""Контур подтверждений: каждая проверка закрывает конкретную атаку."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.core.approvals import (
    FAILED_MAX,
    STATUS_CANCELLED,
    STATUS_CLAIMED,
    STATUS_EXPIRED,
    ApprovalStore,
    PayloadTampered,
    RateLimited,
)
from hermes_cloud.core.db import open_database

PAYLOAD = {"kind": "reminder", "text": "оплатить 15 EUR", "due_at": 1_800_000_000.0}


@pytest.fixture()
def store(tmp_path: Path) -> ApprovalStore:
    return ApprovalStore(open_database(tmp_path / "hermes.db"))


def stage(store: ApprovalStore, **overrides):
    params = {
        "kind": "reminder", "payload": PAYLOAD, "chat": "990000001",
        "actor": "990000001", "thread": 7, "now": 1000.0,
    }
    params.update(overrides)
    return store.stage(**params)


def test_code_is_single_use(store: ApprovalStore) -> None:
    staged = stage(store)

    first = store.claim(code=staged.code, chat="990000001", thread=7,
                        actor="990000001", now=1010.0)
    assert first is not None and first.status == STATUS_CLAIMED

    second = store.claim(code=staged.code, chat="990000001", thread=7,
                         actor="990000001", now=1020.0)
    assert second is None, "код обязан работать ровно один раз"


def test_code_is_bound_to_chat_and_thread(store: ApprovalStore) -> None:
    """Код, подсмотренный в соседнем чате, не должен работать."""
    staged = stage(store)

    assert store.claim(code=staged.code, chat="990000002", thread=7,
                       actor="990000002", now=1010.0) is None
    assert store.claim(code=staged.code, chat="990000001", thread=9,
                       actor="990000001", now=1010.0) is None
    # В своём чате и треде — работает.
    assert store.claim(code=staged.code, chat="990000001", thread=7,
                       actor="990000001", now=1010.0) is not None


def test_plaintext_code_is_not_stored(store: ApprovalStore) -> None:
    staged = stage(store)
    dump = "".join(
        str(dict(row)) for row in store.db.query("SELECT * FROM approvals")
    )
    assert staged.code not in dump, "код обязан храниться только хэшем"


def test_expired_proposal_cannot_be_claimed(store: ApprovalStore) -> None:
    staged = stage(store, ttl_seconds=60)

    assert store.claim(code=staged.code, chat="990000001", thread=7,
                       actor="990000001", now=1061.0) is None
    assert store.get(staged.id).status == STATUS_EXPIRED


def test_wrong_codes_are_rate_limited(store: ApprovalStore) -> None:
    staged = stage(store)
    for attempt in range(FAILED_MAX):
        assert store.claim(code="0" * 16, chat="990000001", thread=7,
                           actor="990000001", now=1000.0 + attempt) is None

    with pytest.raises(RateLimited):
        store.claim(code=staged.code, chat="990000001", thread=7,
                    actor="990000001", now=1010.0)

    # Окно прошло — попытки снова открыты, и настоящий код работает.
    claimed = store.claim(code=staged.code, chat="990000001", thread=7,
                          actor="990000001", now=1000.0 + 601)
    assert claimed is not None


def test_rate_limit_is_per_actor_and_chat(store: ApprovalStore) -> None:
    for attempt in range(FAILED_MAX):
        store.claim(code="0" * 16, chat="990000001", thread=7,
                    actor="990000009", now=1000.0 + attempt)

    # Другой актор в том же чате не заблокирован чужими попытками.
    staged = stage(store)
    assert store.claim(code=staged.code, chat="990000001", thread=7,
                       actor="990000001", now=1010.0) is not None


def test_malformed_code_counts_as_a_failed_attempt(store: ApprovalStore) -> None:
    store.claim(code="не-код", chat="990000001", thread=7, actor="990000001", now=1000.0)
    assert store.failed_attempts(actor="990000001", chat="990000001", now=1000.0) == 1


def test_tampered_payload_is_refused(store: ApprovalStore) -> None:
    """Человек подтверждает конкретный payload, а не «действие вообще»."""
    staged = stage(store)
    with store.db.write() as connection:
        connection.execute(
            "UPDATE approvals SET payload = ? WHERE id = ?",
            ('{"kind": "reminder", "text": "перевести 1500 EUR"}', staged.id),
        )

    with pytest.raises(PayloadTampered):
        store.claim(code=staged.code, chat="990000001", thread=7,
                    actor="990000001", now=1010.0)


def test_cancel_invalidates_the_code(store: ApprovalStore) -> None:
    """✏️ отменяет предложение: новое предложение получает новый код."""
    staged = stage(store)

    assert store.cancel(staged.id, now=1005.0) is True
    assert store.get(staged.id).status == STATUS_CANCELLED
    assert store.claim(code=staged.code, chat="990000001", thread=7,
                       actor="990000001", now=1010.0) is None
    assert store.cancel(staged.id, now=1006.0) is False


def test_claim_by_id_follows_the_same_binding(store: ApprovalStore) -> None:
    staged = stage(store)

    assert store.claim_by_id(approval_id=staged.id, chat="990000002", thread=7,
                             actor="990000002", now=1010.0) is None
    claimed = store.claim_by_id(approval_id=staged.id, chat="990000001", thread=7,
                                actor="990000001", now=1010.0)
    assert claimed is not None and claimed.claimed_by == "990000001"


def test_pending_and_expiry_sweep(store: ApprovalStore) -> None:
    stage(store, ttl_seconds=60)
    stage(store, ttl_seconds=6000)

    assert len(store.pending_for("990000001", 7, now=1010.0)) == 2
    assert store.expire_stale(now=1100.0) == 1
    assert len(store.pending_for("990000001", 7, now=1100.0)) == 1
