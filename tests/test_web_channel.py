"""Minimal Web channel: authenticated chat + PWA manifest, push fake-only."""

import json
from pathlib import Path

from hermes_cloud.channels.web import PUSH_ENABLED, endpoint_hash, handle_web_message
from hermes_cloud.core.runcontext import build_run_context, Household


def test_manifest_exists_and_is_installable() -> None:
    manifest = Path("web/manifest.json")
    assert manifest.is_file()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["name"] and data["start_url"] and data["display"] == "standalone"


def test_web_chat_requires_verified_context() -> None:
    household = Household(owner="owner1", family=frozenset({"owner1"}), allowed_chats=frozenset({"web-chat"}))
    unknown = build_run_context(household=household, actor_id="stranger", chat_id="web-chat")
    known = build_run_context(household=household, actor_id="owner1", chat_id="web-chat")
    from hermes_cloud.channels.web import WebChannelMessage

    assert "не знаю" in handle_web_message(WebChannelMessage(actor_id="stranger", text="hi"), context=unknown)
    assert "Web: hi" in handle_web_message(WebChannelMessage(actor_id="owner1", text="hi"), context=known)


def test_push_stays_fake_only_and_binding_shape() -> None:
    assert PUSH_ENABLED is False
    # endpoint stored as hash in channel_binding external_id
    assert len(endpoint_hash("https://push.example/endpoint/123")) == 24
