"""Forwarding health (plan Phase 4, Strategy A).

The relay mails the agent Gmail a daily check; Gmail forwards it straight back
(spike S3). A check that does not return within two hours is a miss, two
misses declare forwarding stale — one alert, one chat notice — and the next
returning check ends the episode. The family can ask for a check now.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.ingest.forwarding import (
    CHECK_SUBJECT_PREFIX,
    STALE_GUIDE,
    ForwardingHealth,
    ForwardingRelay,
    ForwardingStateStore,
    check_token,
)
from hermes_cloud.runner.tools import REGISTRY, Services
from hermes_cloud.runtime.service import RuntimeService
from tests.test_gmail_forwarding_runtime import (
    AGENT,
    INBOX_ID,
    RELAY,
    RelayInbox,
    _active_gmail_relay_runtime,
    _deliver,
    fixture,
)

BINDING = EmailBinding("identity-1", 7, "gmail", AGENT)
SECRET = "synthetic-webhook-signing-value"
DAY = 1_800_000_000.0  # 2027-01-15 08:00 UTC
HOUR = 3600.0


class ComposeRecorder:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def compose_email(self, **kwargs):
        self.sent.append(kwargs)
        return {"message_id": f"m-{len(self.sent)}", "status": "queued"}


def _health(tmp_path: Path, *, env=None):
    database = open_database(tmp_path / "runtime.db")
    store = ForwardingStateStore(database)
    store.ensure_pending(BINDING, now=DAY)
    store.mark_active(BINDING, letter=True, now=DAY)
    client = ComposeRecorder()
    health = ForwardingHealth(
        store,
        binding=BINDING,
        relay=ForwardingRelay(agent_address=AGENT, relay_address=RELAY, state=store),
        client=client,
        inbox_id=INBOX_ID,
        household_id="33333333-3333-4333-8333-333333333333",
        secret=SECRET,
        env={"HERMES_EMAIL_SEND": "1", "ABROLIA_HMAC_KEY": "k" * 24, **(env or {})},
        logger=logging.getLogger("forwarding-health-test"),
    )
    return store, client, health


def _returned(store: ForwardingStateStore, client: ComposeRecorder, *, now: float) -> None:
    """Gmail forwarded the last check back: the worker records its return."""
    token = client.sent[-1]["subject"].removeprefix(CHECK_SUBJECT_PREFIX)
    assert token == store.outstanding_check_token(BINDING)
    assert store.record_check_return(BINDING, token, received_at=now) is True


def test_a_check_is_sent_once_a_day_from_the_relay_to_the_agent(tmp_path: Path) -> None:
    store, client, health = _health(tmp_path)

    assert health.tick(now=DAY) == "sent"
    assert health.tick(now=DAY + HOUR) is None
    assert len(client.sent) == 1
    sent = client.sent[0]
    assert sent["inbox_id"] == INBOX_ID and sent["to"] == AGENT
    assert sent["subject"] == f"{CHECK_SUBJECT_PREFIX}{check_token(SECRET, '2027-01-15', 1)}"
    assert sent["idempotency_key"].startswith("forwarding-check:identity-1:")
    assert sent["attachments"] == [] and sent["html"] is None

    _returned(store, client, now=DAY + 20)
    assert health.tick(now=DAY + 3 * HOUR) is None
    assert store.row(BINDING)["misses"] == 0
    assert health.tick(now=DAY + 24 * HOUR) == "sent"
    assert len(client.sent) == 2


def test_two_missed_checks_declare_stale_once_and_a_return_recovers(
    tmp_path: Path, caplog
) -> None:
    store, client, health = _health(tmp_path)
    caplog.set_level(logging.WARNING, logger="forwarding-health-test")

    health.tick(now=DAY)
    # Two hours pass and nothing came back: one miss, still active.
    assert health.tick(now=DAY + 2 * HOUR + 1) is None
    assert store.row(BINDING)["misses"] == 1
    assert store.state(BINDING) == "active"
    assert health.tick(now=DAY + 3 * HOUR) is None, "a miss is counted once per check"
    assert store.row(BINDING)["misses"] == 1

    # The next day's check goes out, and it does not come back either.
    assert health.tick(now=DAY + 24 * HOUR) == "sent"
    assert health.tick(now=DAY + 26 * HOUR + 1) == "stale"
    assert store.state(BINDING) == "stale"
    alerts = [r for r in caplog.records if "ALERT gmail_forwarding_stale" in r.getMessage()]
    assert len(alerts) == 1
    assert "33333333" not in alerts[0].getMessage()
    assert store.take_unshown_stale_notice(BINDING, now=DAY + 27 * HOUR) is True
    assert store.take_unshown_stale_notice(BINDING, now=DAY + 27 * HOUR) is False

    # Days go by stale: no second alert, checks keep going out.
    assert health.tick(now=DAY + 48 * HOUR) == "sent"
    assert health.tick(now=DAY + 50 * HOUR + 1) is None
    assert len([r for r in caplog.records if "ALERT gmail_forwarding_stale" in r.getMessage()]) == 1

    # The family fixes forwarding and the check comes back inside its window.
    _returned(store, client, now=DAY + 49 * HOUR)
    row = store.row(BINDING)
    assert (row["state"], row["misses"], row["stale_since"], row["stale_notified_at"]) == (
        "active", 0, None, None,
    )
    # A later episode notifies again.
    health.tick(now=DAY + 72 * HOUR)
    health.tick(now=DAY + 74 * HOUR + 1)
    health.tick(now=DAY + 96 * HOUR)
    assert health.tick(now=DAY + 98 * HOUR + 1) == "stale"
    assert store.take_unshown_stale_notice(BINDING) is True


def test_a_pending_household_never_goes_stale(tmp_path: Path) -> None:
    """Forwarding that never worked is `pending` and owes the confirmation
    guide, not an outage notice."""
    database = open_database(tmp_path / "runtime.db")
    store = ForwardingStateStore(database)
    store.ensure_pending(BINDING, now=DAY)
    client = ComposeRecorder()
    health = ForwardingHealth(
        store, binding=BINDING,
        relay=ForwardingRelay(agent_address=AGENT, relay_address=RELAY, state=store),
        client=client, inbox_id=INBOX_ID, household_id="hh", secret=SECRET,
        env={"HERMES_EMAIL_SEND": "1"},
    )
    for day in range(4):
        health.tick(now=DAY + day * 24 * HOUR)
        health.tick(now=DAY + day * 24 * HOUR + 3 * HOUR)
    assert store.state(BINDING) == "pending"
    assert store.row(BINDING)["misses"] == 4
    assert len(client.sent) == 4


def test_the_outgoing_mail_switch_blocks_the_check(tmp_path: Path) -> None:
    store, client, health = _health(tmp_path, env={"HERMES_EMAIL_SEND": "0"})

    assert health.tick(now=DAY) == "blocked"

    assert client.sent == []
    assert store.row(BINDING)["last_check_sent_at"] is None
    assert store.state(BINDING) == "active"


def test_the_check_interval_is_configurable(tmp_path: Path) -> None:
    store, client, health = _health(tmp_path, env={"ABROLIA_GMAIL_FORWARD_CHECK_HOURS": "6"})
    health.tick(now=DAY)
    _returned(store, client, now=DAY + 10)
    assert health.tick(now=DAY + 5 * HOUR) is None
    assert health.tick(now=DAY + 6 * HOUR) == "sent"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "1", "2", "abc", ""])
def test_an_unusable_interval_falls_back_to_the_default(tmp_path: Path, value: str) -> None:
    """`nan`/`inf` would make a check never due again; an interval inside the
    two-hour return window would replace the outstanding check before it
    could be missed. Either is a monitor that cannot fail."""
    _store, _client, health = _health(tmp_path, env={"ABROLIA_GMAIL_FORWARD_CHECK_HOURS": value})
    assert health._check_interval() == 24 * HOUR


class IdempotentComposeRecorder(ComposeRecorder):
    """Nerve's compose is idempotent on the key: a repeated key sends nothing."""

    def compose_email(self, **kwargs):
        if any(sent["idempotency_key"] == kwargs["idempotency_key"] for sent in self.sent):
            return {"message_id": "m-dup", "status": "queued"}
        return super().compose_email(**kwargs)


@pytest.mark.parametrize("trigger", ["recheck", "subdaily"])
def test_a_second_check_on_the_same_day_is_a_new_message(tmp_path: Path, trigger: str) -> None:
    """Token and idempotency key derive from the attempt too. Without that a
    same-day recheck reused both: Nerve sent nothing, the seen token already
    matched, and the recheck reported a success nobody verified."""
    env = {"ABROLIA_GMAIL_FORWARD_CHECK_HOURS": "3"} if trigger == "subdaily" else {}
    store, _client, health = _health(tmp_path, env=env)
    client = IdempotentComposeRecorder()
    health.client = client
    health.tick(now=DAY)
    _returned(store, client, now=DAY + 10)

    if trigger == "recheck":
        store.request_check(BINDING, now=DAY + HOUR)
        assert health.tick(now=DAY + HOUR + 1) == "sent"
    else:
        assert health.tick(now=DAY + 3 * HOUR) == "sent"

    assert len(client.sent) == 2
    first, second = client.sent
    assert first["subject"] != second["subject"]
    assert first["idempotency_key"] != second["idempotency_key"]
    assert second["subject"].endswith(check_token(SECRET, "2027-01-15", 2))
    # The second attempt is outstanding on its own: not back → a miss.
    later = (DAY + HOUR + 1 if trigger == "recheck" else DAY + 3 * HOUR) + 2 * HOUR + 1
    health.tick(now=later)
    assert store.row(BINDING)["misses"] == 1


def test_a_check_that_comes_back_after_its_deadline_proves_nothing(tmp_path: Path) -> None:
    """Forwarding that takes three hours is forwarding that does not work: a
    late return neither resets the miss count nor clears a stale state."""
    store, client, health = _health(tmp_path)
    health.tick(now=DAY)
    assert health.tick(now=DAY + 2 * HOUR + 1) is None  # miss 1
    token = client.sent[-1]["subject"].removeprefix(CHECK_SUBJECT_PREFIX)

    assert store.record_check_return(BINDING, token, received_at=DAY + 3 * HOUR) is False
    assert store.row(BINDING)["misses"] == 1

    health.tick(now=DAY + 24 * HOUR)
    assert health.tick(now=DAY + 26 * HOUR + 1) == "stale"
    token = client.sent[-1]["subject"].removeprefix(CHECK_SUBJECT_PREFIX)
    assert store.record_check_return(BINDING, token, received_at=DAY + 27 * HOUR) is False
    assert store.state(BINDING) == "stale"

    # A return inside the window ends the episode.
    health.tick(now=DAY + 48 * HOUR)
    token = client.sent[-1]["subject"].removeprefix(CHECK_SUBJECT_PREFIX)
    assert store.record_check_return(BINDING, token, received_at=DAY + 49 * HOUR) is True
    assert store.state(BINDING) == "active"


def test_a_requested_check_goes_out_now(tmp_path: Path) -> None:
    store, client, health = _health(tmp_path)
    health.tick(now=DAY)
    _returned(store, client, now=DAY + 10)

    store.request_check(BINDING, now=DAY + HOUR)

    assert health.tick(now=DAY + HOUR + 1) == "sent"
    assert len(client.sent) == 2
    assert store.row(BINDING)["check_requested_at"] is None


# --- through the runtime ---------------------------------------------------------


class RelayInboxThatSends(RelayInbox):
    def __init__(self, message: dict) -> None:
        super().__init__(message)
        self.sent: list[dict] = []

    def compose_email(self, **kwargs):
        self.sent.append(kwargs)
        return {"message_id": f"m-{len(self.sent)}", "status": "queued"}


def _runtime(tmp_path: Path, message: dict) -> tuple[RuntimeService, RelayInboxThatSends]:
    service = _active_gmail_relay_runtime(tmp_path, message, env_extra={"HERMES_EMAIL_SEND": "1"})
    inbox = RelayInboxThatSends(message)
    service.nerve_client_factory = lambda **_kwargs: inbox
    return service, inbox


def test_the_runtime_loop_sends_the_check_and_recognises_its_return(tmp_path: Path) -> None:
    letter = fixture("forwarded_letter")
    service, inbox = _runtime(tmp_path, letter)
    _deliver(service, letter)  # forwarding is active
    assert inbox.sent, "no check went out with the first pass"
    token = inbox.sent[0]["subject"].removeprefix(CHECK_SUBJECT_PREFIX)

    check = fixture("canary_return")
    check["subject"] = f"{CHECK_SUBJECT_PREFIX}{token}"
    inbox.message = check
    _deliver(service, check)

    with open_database(service.database_path) as database:
        row = ForwardingStateStore(database).row(
            EmailBinding("email-identity-1", 7, "gmail", AGENT)
        )
    assert row["last_check_seen_token"] == token and row["misses"] == 0
    assert service.readyz().payload["email_health"]["forwarding"] == "active"


def test_a_check_with_a_foreign_token_is_diverted_but_proves_nothing(tmp_path: Path) -> None:
    letter = fixture("forwarded_letter")
    service, inbox = _runtime(tmp_path, letter)
    _deliver(service, letter)
    forged = fixture("canary_return")  # token 0000synthetic, never sent
    inbox.message = forged

    _deliver(service, forged)

    with open_database(service.database_path) as database:
        row = ForwardingStateStore(database).row(
            EmailBinding("email-identity-1", 7, "gmail", AGENT)
        )
        events = database.query_one("SELECT COUNT(*) AS n FROM events")["n"]
    assert row["last_check_seen_token"] is None
    assert events == 1, "the forged check must not become an event either"


def test_stale_is_shown_to_the_owner_once_and_the_recheck_tool_asks_for_a_check(
    tmp_path: Path,
) -> None:
    letter = fixture("forwarded_letter")
    service, inbox = _runtime(tmp_path, letter)
    _deliver(service, letter)
    binding = EmailBinding("email-identity-1", 7, "gmail", AGENT)
    with open_database(service.database_path) as database:
        ForwardingStateStore(database).mark_stale(binding)
    assert service.readyz().payload["email_health"]["forwarding"] == "stale"

    stub = SimpleNamespace(run=lambda context, text, **_: SimpleNamespace(text="reply"))
    service._web_chat_loop = lambda database, config, **_kwargs: stub
    first = service.web_chat_turn("hi", actor_id="owner", chat_id="web:owner")
    assert first == f"{STALE_GUIDE}\n\nreply"
    assert service.web_chat_turn("hi", actor_id="owner", chat_id="web:owner") == "reply"

    # The tool the model calls when the family says forwarding is back on.
    tool = REGISTRY._tools["forwarding_recheck"]
    context = SimpleNamespace(require=lambda capability: None)
    with open_database(service.database_path) as database:
        services = Services.on(database, email_binding=binding)
        assert tool(context, services, {}) == {
            "requested": True,
            "status": "проверка уйдёт в ближайшую минуту",
        }
        assert ForwardingStateStore(database).row(binding)["check_requested_at"] is not None
    sent_before = len(inbox.sent)
    service.run_nerve_once()
    assert len(inbox.sent) == sent_before + 1


def test_the_recheck_tool_refuses_without_a_gmail_relay(tmp_path: Path) -> None:
    tool = REGISTRY._tools["forwarding_recheck"]
    context = SimpleNamespace(require=lambda capability: None)
    with open_database(tmp_path / "runtime.db") as database:
        managed = EmailBinding("identity-1", 1, "nerve-managed", "family@abrolia.example")
        assert tool(context, Services.on(database, email_binding=managed), {}) == {
            "requested": False,
            "reason": "no_gmail_relay",
        }
        assert tool(context, Services.on(database), {})["requested"] is False
