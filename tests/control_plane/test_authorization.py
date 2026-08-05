from __future__ import annotations

import uuid

import pytest

from control_plane.onboarding.contracts import CommandContext
from control_plane.repositories.auth import InvalidCredential
from control_plane.repositories.households import HouseholdNotFound
from control_plane.services.households import HouseholdService

BASE_TIME = 1_800_000_000.0


def test_household_lookup_is_scoped_to_active_membership(cp_stack) -> None:
    other_account = cp_stack.accounts.create_verified("other-owner@family.test")
    other_household = cp_stack.households.create_for_owner(other_account.id)
    service = HouseholdService(cp_stack.households)

    assert service.require(cp_stack.account.id, cp_stack.household.id).id == cp_stack.household.id
    for unavailable in (other_household.id, str(uuid.uuid4())):
        with pytest.raises(HouseholdNotFound):
            service.require(cp_stack.account.id, unavailable)


def test_foreign_account_cannot_mutate_workflow_even_with_valid_uuid(cp_stack) -> None:
    other_account = cp_stack.accounts.create_verified("other-workflow@family.test")
    other_household = cp_stack.households.create_for_owner(other_account.id)
    before = cp_stack.onboarding.snapshot(other_household.id)
    context = CommandContext(
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        request_id="foreign-household-request",
        idempotency_key="foreign-household-key",
        expected_version=before.version,
    )

    with pytest.raises(KeyError):
        cp_stack.service.save_profile(
            other_household.id,
            cp_stack.valid_profile(first_name="UnauthorizedMutationCanary"),
            context=context,
        )

    after = cp_stack.onboarding.snapshot(other_household.id)
    assert after == before
    assert cp_stack.households.profile(other_household.id) is None
    assert cp_stack.database.query(
        "SELECT * FROM idempotency_requests WHERE account_id = ?"
        " AND idempotency_key_hmac = ?",
        (cp_stack.account.id, cp_stack.lookup.digest(context.idempotency_key)),
    ) == []


def test_revoked_membership_is_indistinguishable_from_absent_household(cp_stack) -> None:
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE household_memberships SET status = 'revoked', revoked_at = ?"
            " WHERE account_id = ? AND household_id = ?",
            (BASE_TIME + 1, cp_stack.account.id, cp_stack.household.id),
        )
    with pytest.raises(HouseholdNotFound):
        cp_stack.households.authorized(cp_stack.account.id, cp_stack.household.id)
    with pytest.raises(HouseholdNotFound):
        cp_stack.households.authorized(cp_stack.account.id, str(uuid.uuid4()))


@pytest.mark.parametrize("status", ["locked", "deleting", "deleted"])
def test_non_active_account_cannot_authenticate_existing_session(cp_stack, status: str) -> None:
    cp_stack.accounts.set_status(cp_stack.account.id, status, now=BASE_TIME + 1)
    with pytest.raises(InvalidCredential):
        cp_stack.sessions.authenticate(cp_stack.session.token, now=BASE_TIME + 2)


def test_account_views_require_explicit_full_identifier_access(cp_stack) -> None:
    assert cp_stack.account.masked_email == "o***@family.test"
    assert cp_stack.account.recovery_email == "owner@family.test"
    assert cp_stack.account.masked_email != cp_stack.account.recovery_email
