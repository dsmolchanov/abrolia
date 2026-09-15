"""A Gmail household receives through a Nerve relay that Gmail forwards into.

Plan Phase 2 (`thoughts/shared/plans/2026-09-13-gmail-send-only-forwarding.md`):
the manifest carries the relay, the runtime reads it through the Nerve path it
already has, Google's confirmation and Abrolia's own check are settled without
ever becoming events, and a forwarded letter is an ordinary letter whose reply
goes to the original sender and leaves through Gmail.
"""

from __future__ import annotations

import base64
import email
import hashlib
import hmac
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane.privacy.consent import consent_version_and_sha
from hermes_cloud import cli
from hermes_cloud.core.config import load_config
from hermes_cloud.core.db import open_database
from hermes_cloud.core.runtime_manifest import (
    ManifestError,
    compute_config_sha256,
    parse_runtime_manifest,
)
from hermes_cloud.execute.gmail_api_send import GmailSendProvider
from hermes_cloud.ingest.forwarding import (
    CONFIRMATION_SENDER,
    Check,
    Confirmation,
    ForwardingStateStore,
    Letter,
    classify,
)
from hermes_cloud.ingest.nerve_webhook import NerveDiverted, NerveMaterialized
from hermes_cloud.runtime.bootstrap import ActivationState, atomic_write, write_activation_state
from hermes_cloud.runtime.service import RuntimeNotReady, RuntimeService

FIXTURES = Path(__file__).parent / "fixtures" / "email" / "gmail_forwarding"
AGENT = "family.agent@example.com"
RELAY = "fwd-synthetic-relay@abrolia.example"
SECRET = "synthetic-webhook-signing-value"
ORG_ID = "00000000-0000-4000-8000-000000000001"
INBOX_ID = "00000000-0000-4000-8000-000000000002"
THREAD_ID = "00000000-0000-4000-8000-000000000003"
RUNTIME_REF = "fly:runtime"

_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
_HOUSEHOLD_VERSION, _HOUSEHOLD_SHA = consent_version_and_sha(
    "special_category_household_content"
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.nerve.json").read_text(encoding="utf-8"))


# --- classification ----------------------------------------------------------


def test_googles_confirmation_yields_only_the_confirm_link() -> None:
    kind = classify(fixture("confirmation"), agent_address=AGENT, relay_address=RELAY)

    assert isinstance(kind, Confirmation)
    assert kind.link.startswith("https://mail-settings.google.com/mail/vf-")
    assert "/mail/uf-" not in kind.link


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m: m["from"].__setitem__("email", "forwarding-noreply@example.com"), id="foreign-sender"),
        pytest.param(
            lambda m: m.__setitem__("text", m["text"].replace("mail-settings.google.com", "mail-settings.example.com")),
            id="foreign-link-host",
        ),
        pytest.param(
            lambda m: m.__setitem__("text", m["text"].replace("/mail/vf-", "/mail/xx-")),
            id="not-a-confirm-path",
        ),
        pytest.param(
            lambda m: m.__setitem__("text", m["text"] + "\nhttps://mail-settings.google.com/mail/vf-second\n"),
            id="two-confirm-links",
        ),
        pytest.param(
            lambda m: m.__setitem__("text", m["text"].replace(AGENT, "someone.else@example.com")),
            id="another-mailbox",
        ),
    ],
)
def test_a_confirmation_that_fails_any_check_is_a_letter(mutate) -> None:
    """Spoofed or malformed, it goes where every untrusted message goes."""
    message = fixture("confirmation")
    mutate(message)

    assert isinstance(classify(message, agent_address=AGENT, relay_address=RELAY), Letter)


def test_our_own_check_is_recognised_by_sender_and_subject() -> None:
    kind = classify(fixture("canary_return"), agent_address=AGENT, relay_address=RELAY)
    assert kind == Check("0000synthetic")

    forged = fixture("canary_return")
    forged["from"]["email"] = "someone@example.com"
    assert isinstance(classify(forged, agent_address=AGENT, relay_address=RELAY), Letter)


def test_a_forwarded_letter_is_a_letter() -> None:
    assert isinstance(
        classify(fixture("forwarded_letter"), agent_address=AGENT, relay_address=RELAY), Letter
    )
    assert CONFIRMATION_SENDER == "forwarding-noreply@google.com"


# --- manifest -----------------------------------------------------------------


def _manifest(
    *,
    provider_kind: str = "gmail",
    inbound: dict[str, str] | None = None,
) -> str:
    inbound_lines = "".join(f"{key} = '{value}'\n" for key, value in (inbound or {}).items())
    binding_ref = (
        "email-identity-1"
        if provider_kind == "gmail"
        else json.dumps({"org_id": ORG_ID, "inbox_id": INBOX_ID, "address": AGENT})
    )
    secret = "ABROLIA_GMAIL_OAUTH_GRANT" if provider_kind == "gmail" else "ABROLIA_NERVE_EMAIL_CREDENTIALS"
    content = f'''\
schema_version = 1
household_id = "33333333-3333-4333-8333-333333333333"
config_revision = 7
family_language = "English"
timezone = "Europe/Prague"
country_code = "CZ"
residency_mode = "eu-app"

[actors]
owner = "owner"
family = ["owner"]
guests = []

[channels]
primary = "telegram"

[[channel_bindings]]
channel = "telegram"
actor_id = "owner"
chat_id = "chat-1"
verified = true

[[channel_bindings]]
channel = "web"
actor_id = "owner"
chat_id = "web:owner"
verified = true

[email]
agent_inbox = "{AGENT}"
fallback = "owner@example.test"
provider_kind = "{provider_kind}"
provider_binding_ref = '{binding_ref}'
secret_binding_ref = "{secret}"
{inbound_lines}
[consent]
authority = "control_plane"
enforcement = "required"
required_purposes = ["special_category_content_restriction", "special_category_household_content"]

[[consent.receipts]]
receipt_id = "10000000-0000-4000-8000-000000000032"
purpose = "special_category_content_restriction"
text_version = "{_RESTRICTION_VERSION}"
text_sha256 = "{_RESTRICTION_SHA}"

[[consent.receipts]]
receipt_id = "10000000-0000-4000-8000-000000000035"
purpose = "special_category_household_content"
text_version = "{_HOUSEHOLD_VERSION}"
text_sha256 = "{_HOUSEHOLD_SHA}"
'''
    digest = compute_config_sha256(content)
    return content.replace(
        "schema_version = 1\n", f'schema_version = 1\nconfig_sha256 = "{digest}"\n'
    )


RELAY_FIELDS = {
    "inbound_provider_kind": "nerve",
    "inbound_binding_ref": json.dumps({"org_id": ORG_ID, "inbox_id": INBOX_ID, "address": RELAY}),
    "inbound_secret_binding_ref": "ABROLIA_NERVE_EMAIL_CREDENTIALS",
}


def test_a_gmail_manifest_carries_the_relay() -> None:
    manifest = parse_runtime_manifest(_manifest(inbound=RELAY_FIELDS))
    assert manifest.email.relay is True
    assert manifest.email.inbound_provider_kind == "nerve"
    assert manifest.email.inbound_secret_binding_ref == "ABROLIA_NERVE_EMAIL_CREDENTIALS"

    without = parse_runtime_manifest(_manifest())
    assert without.email.relay is False


@pytest.mark.parametrize(
    ("provider_kind", "inbound", "match"),
    [
        pytest.param("nerve-managed", RELAY_FIELDS, "only a gmail household", id="managed-with-relay"),
        pytest.param(
            "gmail", {k: v for k, v in RELAY_FIELDS.items() if k != "inbound_secret_binding_ref"},
            "required together", id="missing-secret",
        ),
        pytest.param(
            "gmail", {"inbound_provider_kind": "nerve"}, "required together", id="kind-alone"
        ),
        pytest.param(
            "gmail", {**RELAY_FIELDS, "inbound_provider_kind": "gmail"}, "expected 'nerve'", id="wrong-kind"
        ),
    ],
)
def test_a_relay_is_all_three_fields_on_gmail_or_nothing(provider_kind, inbound, match) -> None:
    with pytest.raises(ManifestError, match=match):
        parse_runtime_manifest(_manifest(provider_kind=provider_kind, inbound=inbound))


# --- runtime ------------------------------------------------------------------


def _gmail_grant() -> str:
    return json.dumps(
        {
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "client-secret-canary",
            "refresh_credential": "refresh-credential-canary",
            "provider_subject": "google-subject-1",
            "scopes": ["openid", "email", "https://www.googleapis.com/auth/gmail.send"],
            "wrapping_key": base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode(),
        }
    )


class RelayInbox:
    """Nerve REST as the worker sees it: one thread holding one fixture message."""

    def __init__(self, message: dict) -> None:
        self.message = message
        self.fetched = 0

    def get_thread(self, inbox_id: str, thread_id: str):
        self.fetched += 1
        assert inbox_id == INBOX_ID and thread_id == THREAD_ID
        return {"thread": {"id": THREAD_ID, "inbox_id": INBOX_ID}, "messages": [self.message]}

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        raise AssertionError("no fixture carries an attachment")

    def close(self) -> None:
        pass


def _webhook(message: dict) -> bytes:
    return json.dumps(
        {
            "event": "email.received",
            "org_id": ORG_ID,
            "inbox_id": INBOX_ID,
            "thread_id": THREAD_ID,
            "message_id": message["id"],
            "attachment_count": 0,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _signature(body: bytes, timestamp: int) -> str:
    digest = hmac.new(SECRET.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _active_gmail_relay_runtime(tmp_path: Path, message: dict, *, env_extra=None) -> RuntimeService:
    content = _manifest(inbound=RELAY_FIELDS)
    manifest = parse_runtime_manifest(content)
    manifest_path = atomic_write(tmp_path / "household.toml", content.encode())
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            updated_at=1.0,
        ),
    )
    inbox = RelayInbox(message)
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={
            "HERMES_DB": str(tmp_path / "runtime.db"),
            "ABROLIA_GMAIL_OAUTH_GRANT": _gmail_grant(),
            "ABROLIA_NERVE_EMAIL_CREDENTIALS": json.dumps(
                {"api_key": "synthetic-api-key", "webhook_signing_key": SECRET}
            ),
            **(env_extra or {}),
        },
        nerve_client_factory=lambda **_kwargs: inbox,
    )
    service.inbox = inbox  # type: ignore[attr-defined]
    return service


def _deliver(service: RuntimeService, message: dict):
    """Nerve's webhook for `message`, then one worker pass over it."""
    body = _webhook(message)
    seen: list[str] = []
    result = service(
        {
            "PATH_INFO": "/v1/email/nerve/webhook",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": "application/json",
            "HTTP_X_NERVE_SIGNATURE": _signature(body, int(time.time())),
            "wsgi.input": io.BytesIO(body),
        },
        lambda status, _headers: seen.append(status),
    )
    assert seen[0] == "200 OK", json.loads(result[0])
    return service.run_nerve_once()


def _events(service: RuntimeService) -> list:
    with open_database(service.database_path) as database:
        return list(database.query("SELECT source, raw FROM events"))


def test_a_relay_binding_activates_pending_and_readyz_says_so(tmp_path: Path) -> None:
    service = _active_gmail_relay_runtime(tmp_path, fixture("forwarded_letter"))

    ready = service.readyz()

    assert ready.status_code == 200
    assert ready.payload["email_provider"] == "gmail"
    assert ready.payload["email_health"] == {"status": "send_only", "forwarding": "pending"}


def test_a_gmail_manifest_without_a_relay_reports_no_forwarding(tmp_path: Path) -> None:
    content = _manifest()
    manifest = parse_runtime_manifest(content)
    manifest_path = atomic_write(tmp_path / "household.toml", content.encode())
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            updated_at=1.0,
        ),
    )
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={"HERMES_DB": str(tmp_path / "runtime.db"), "ABROLIA_GMAIL_OAUTH_GRANT": _gmail_grant()},
    )

    assert service.readyz().payload["email_health"] == {"status": "send_only", "forwarding": "none"}
    with pytest.raises(RuntimeNotReady, match="not Nerve"):
        service.run_nerve_once()


def test_a_relay_whose_secret_is_missing_is_not_ready(tmp_path: Path) -> None:
    service = _active_gmail_relay_runtime(tmp_path, fixture("forwarded_letter"))
    del service.env["ABROLIA_NERVE_EMAIL_CREDENTIALS"]

    ready = service.readyz()

    assert ready.status_code == 503
    assert ready.payload["reason"] == "email_provider_unavailable"


def test_the_confirmation_is_kept_for_the_owner_and_never_becomes_an_event(
    tmp_path: Path,
) -> None:
    service = _active_gmail_relay_runtime(tmp_path, fixture("confirmation"))

    result = _deliver(service, fixture("confirmation"))

    assert result == NerveDiverted(result.nerve_event_id, "confirmation")
    assert _events(service) == []
    assert service.readyz().payload["email_health"]["forwarding"] == "pending"
    with open_database(service.database_path) as database:
        row = database.query_one("SELECT state, confirmation_link FROM gmail_forwarding_state")
        journal = database.query_one("SELECT state, canonical_event_id FROM nerve_webhook_events")
    assert row["state"] == "pending"
    assert row["confirmation_link"].startswith("https://mail-settings.google.com/mail/vf-")
    assert (journal["state"], journal["canonical_event_id"]) == ("diverted", None)

    # Shown to the owner on their next web turn, once, ahead of the reply;
    # never to another member.
    stub = SimpleNamespace(run=lambda context, text, **_: SimpleNamespace(text="reply"))
    service._web_chat_loop = lambda database, config: stub
    first = service.web_chat_turn("hi", actor_id="owner", chat_id="web:owner")
    assert first.startswith("Gmail asks you to confirm forwarding")
    assert row["confirmation_link"] in first
    assert "/mail/uf-" not in first
    assert first.endswith("\n\nreply")
    second = service.web_chat_turn("hi", actor_id="owner", chat_id="web:owner")
    assert second == "reply"


def test_a_spoofed_confirmation_is_an_ordinary_letter(tmp_path: Path) -> None:
    spoofed = fixture("confirmation")
    spoofed["from"]["email"] = "forwarding-noreply@example.com"
    service = _active_gmail_relay_runtime(tmp_path, spoofed)

    result = _deliver(service, spoofed)

    assert isinstance(result, NerveMaterialized)
    assert [row["source"] for row in _events(service)] == ["gmail-forward"]
    with open_database(service.database_path) as database:
        assert database.query_one("SELECT confirmation_link FROM gmail_forwarding_state")["confirmation_link"] is None


def test_a_forwarded_letter_is_ingested_with_the_original_sender_and_leaves_through_gmail(
    tmp_path: Path, monkeypatch
) -> None:
    letter = fixture("forwarded_letter")
    service = _active_gmail_relay_runtime(tmp_path, letter)

    result = _deliver(service, letter)

    assert isinstance(result, NerveMaterialized) and result.created is True
    events = _events(service)
    assert [row["source"] for row in events] == ["gmail-forward"]
    parsed = email.message_from_bytes(bytes(events[0]["raw"]))
    assert parsed["From"] == "sekretariat@schule.example"
    assert parsed["To"] == RELAY
    assert parsed["Subject"] == "Wandertag am Donnerstag"
    assert service.readyz().payload["email_health"]["forwarding"] == "active"

    # The send backend for this household is Gmail, from the agent address.
    for name, value in service.env.items():
        monkeypatch.setenv(name, value)
    config = load_config(env=service.env, manifest_path=service.manifest_path)
    with open_database(service.database_path) as database:
        binding = cli._email_binding(config, database)
        sender = cli._mail(config, database, binding)
        assert sender is not None
        assert isinstance(sender.backend, GmailSendProvider)
        assert sender.sender == AGENT
        sender.backend.client.close()


def test_our_check_message_marks_forwarding_active_without_an_event(tmp_path: Path) -> None:
    service = _active_gmail_relay_runtime(tmp_path, fixture("canary_return"))

    result = _deliver(service, fixture("canary_return"))

    assert result == NerveDiverted(result.nerve_event_id, "check")
    assert _events(service) == []
    assert service.readyz().payload["email_health"]["forwarding"] == "active"
    with open_database(service.database_path) as database:
        state = ForwardingStateStore(database)
        row = database.query_one("SELECT last_check_at, last_letter_at FROM gmail_forwarding_state")
    assert row["last_check_at"] is not None and row["last_letter_at"] is None
    assert state is not None
