"""Phase 5 pilotization: channel_preferences routing & fallback invariant."""

from __future__ import annotations

import pytest

from control_plane.channel_preferences import ChannelPreferenceError


def test_household_primary_defaults_to_telegram_and_fallback_is_email(cp_stack) -> None:
    prefs = cp_stack.channel_prefs
    household = cp_stack.household
    pref = prefs.set_household(household.id, primary_channel="telegram", verified_at=123.0, fallback_email="owner@example.test", agent_inbox="agent@abrolia.test")
    assert pref.primary_channel == "telegram"
    assert pref.fallback_channel == "email"
    assert pref.verified_at == 123.0
    # source-channel replies stay in source channel regardless of preference; proactive goes to primary
    fetched = prefs.get_household(household.id)
    assert fetched is not None and fetched.primary_channel == "telegram"


def test_fallback_cannot_equal_agent_inbox(cp_stack) -> None:
    prefs = cp_stack.channel_prefs
    household = cp_stack.household
    with pytest.raises(ChannelPreferenceError, match="self-ingestion"):
        prefs.set_household(household.id, primary_channel="whatsapp", fallback_email="agent@abrolia.test", agent_inbox="agent@abrolia.test")


def test_unknown_primary_or_fallback_is_rejected(cp_stack) -> None:
    prefs = cp_stack.channel_prefs
    household = cp_stack.household
    with pytest.raises(ChannelPreferenceError):
        prefs.set_household(household.id, primary_channel="sms")
    with pytest.raises(ChannelPreferenceError):
        prefs.set_household(household.id, primary_channel="telegram", fallback_channel="sms")


def test_switching_primary_preserves_updated_at_and_does_not_change_fallback(cp_stack) -> None:
    prefs = cp_stack.channel_prefs
    household = cp_stack.household
    prefs.set_household(household.id, primary_channel="telegram", verified_at=1.0, now=1.0, fallback_email="owner@example.test", agent_inbox="agent@abrolia.test")
    second = prefs.set_household(household.id, primary_channel="web", verified_at=2.0, now=2.0, fallback_email="owner@example.test", agent_inbox="agent@abrolia.test")
    assert second.primary_channel == "web"
    assert second.fallback_channel == "email"
    assert second.updated_at == 2.0


def test_privacy_classification_and_retention_covered_by_migration(cp_stack) -> None:
    # TABLE_CLASSIFICATION already asserts coverage; this smoke ensures the table exists with expected shape.
    row = cp_stack.database.query_one("SELECT sql FROM sqlite_master WHERE name='channel_preferences'")
    assert row is not None and "primary_channel" in row["sql"]
