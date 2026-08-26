from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane.api.app import create_app
from control_plane.auth.mailer import MemoryMailer
from control_plane.auth.sessions import IssuedSession, SessionService
from control_plane.channel_preferences import ChannelPreferencesRepository
from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.crypto import FieldCipher, LookupHasher
from control_plane.db import ControlPlaneDatabase
from control_plane.email.repository import EmailIdentityRepository
from control_plane.email.service import EmailIdentityService
from control_plane.models import ProfileInput
from control_plane.onboarding.contracts import CommandContext
from control_plane.onboarding.service import OnboardingService
from control_plane.provisioning.contracts import ProviderRegistry
from control_plane.provisioning.fakes import synthetic_provider_registry
from control_plane.provisioning.planner import DesiredSpecPlanner
from control_plane.provisioning.secrets import InMemorySecretSink
from control_plane.provisioning.worker import ProvisioningWorker
from control_plane.repositories.accounts import AccountRecord, AccountsRepository
from control_plane.repositories.auth import AuthRepository
from control_plane.repositories.bindings import ChannelBindingsRepository
from control_plane.repositories.configs import ConfigRepository
from control_plane.repositories.households import HouseholdRecord, HouseholdsRepository
from control_plane.repositories.jobs import JobsRepository
from control_plane.repositories.onboarding import OnboardingRepository

BASE_TIME = 1_800_000_000.0


@dataclass
class ControlPlaneStack:
    config: ControlPlaneConfig
    database: ControlPlaneDatabase
    cipher: FieldCipher
    lookup: LookupHasher
    token_hasher: LookupHasher
    accounts: AccountsRepository
    auth: AuthRepository
    households: HouseholdsRepository
    onboarding: OnboardingRepository
    jobs: JobsRepository
    configs: ConfigRepository
    bindings: ChannelBindingsRepository
    email_identities: EmailIdentityRepository
    channel_prefs: ChannelPreferencesRepository
    sessions: SessionService
    service: OnboardingService
    account: AccountRecord
    household: HouseholdRecord
    session: IssuedSession
    _request_sequence: itertools.count = field(default_factory=lambda: itertools.count(1))

    def context(
        self,
        *,
        key: str | None = None,
        expected_version: int | None = None,
        account_id: str | None = None,
        session_id: str | None = None,
    ) -> CommandContext:
        sequence = next(self._request_sequence)
        if expected_version is None:
            expected_version = self.onboarding.workflow_for_household(self.household.id).version
        return CommandContext(
            account_id=account_id or self.account.id,
            session_id=session_id or self.session.id,
            request_id=f"synthetic-request-{sequence}",
            idempotency_key=key or f"synthetic-key-{sequence}",
            expected_version=expected_version,
        )

    @staticmethod
    def valid_profile(**changes: object) -> ProfileInput:
        values: dict[str, object] = {
            "first_name": "Test",
            "last_name": "Family",
            "family_language": "en",
            "timezone": "Europe/Prague",
            "country_code": "DE",
            "residency_mode": "eu-app",
        }
        values.update(changes)
        return ProfileInput.model_validate(values)

    def complete_profile(
        self, *, now: float = BASE_TIME + 1, provision_namespace: bool = True
    ) -> None:
        self.service.save_profile(
            self.household.id,
            self.valid_profile(),
            context=self.context(),
            now=now,
        )
        if provision_namespace:
            result = self.make_worker(now=now + 0.5).run_once()
            assert result is not None and result.status == "succeeded"

    def make_worker(
        self,
        *,
        providers: ProviderRegistry | None = None,
        secret_sink: InMemorySecretSink | None = None,
        model_api_key: str | None = None,
        now: float = BASE_TIME + 100,
    ) -> ProvisioningWorker:
        planner = DesiredSpecPlanner(
            self.accounts,
            self.households,
            self.onboarding,
            self.configs,
            self.bindings,
        )
        return ProvisioningWorker(
            jobs=self.jobs,
            onboarding=self.onboarding,
            households=self.households,
            configs=self.configs,
            planner=planner,
            providers=providers or synthetic_provider_registry(),
            secret_sink=secret_sink or InMemorySecretSink(),
            email_identities=self.email_identities,
            worker_id="synthetic-worker",
            # The brake is subtractive: the live env can only ever turn real
            # email OFF, never authorize it. Tests that drive Nerve adapters
            # need this household in the boot-time allowlist as well as the env
            # var, because dispatch asks about THIS household and not about
            # real email in general.
            real_email_authorized_households=frozenset({self.household.id}),
            model_api_key=model_api_key,
            clock=lambda: now,
        )


@dataclass(frozen=True)
class APIPrincipalWorld:
    account: AccountRecord
    household: HouseholdRecord
    session: IssuedSession


@dataclass
class APIHarness:
    config: ControlPlaneConfig
    container: ControlPlaneContainer
    mailer: MemoryMailer
    client: TestClient

    def create_principal(
        self,
        email: str = "api-owner@family.test",
        *,
        now: float | None = None,
    ) -> APIPrincipalWorld:
        now = time.time() if now is None else now
        account = self.container.accounts.create_verified(email, now=now)
        household = self.container.households.create_for_owner(account.id, now=now)
        session = self.container.sessions.issue(account.id, now=now)
        return APIPrincipalWorld(account, household, session)

    def authenticate(self, world: APIPrincipalWorld) -> None:
        self.client.cookies.set(self.config.session_cookie_name, world.session.token)
        self.client.cookies.set(self.config.csrf_cookie_name, world.session.csrf_token)

    @property
    def mutation_headers(self) -> dict[str, str]:
        return {
            "Origin": self.config.public_origin,
            "X-CSRF-Token": self.client.cookies.get(self.config.csrf_cookie_name),
        }


@pytest.fixture
def cp_stack(tmp_path: Path) -> ControlPlaneStack:
    config = ControlPlaneConfig.for_test(tmp_path)
    database = ControlPlaneDatabase(config.database_path)
    database.migrate()
    cipher = FieldCipher(config.encryption_keys, config.active_encryption_key_version)
    lookup = LookupHasher(config.lookup_hmac_key)
    token_hasher = LookupHasher(config.token_hmac_key)
    accounts = AccountsRepository(database, cipher, lookup)
    auth = AuthRepository(database, cipher, lookup, token_hasher)
    households = HouseholdsRepository(database, cipher, lookup)
    onboarding = OnboardingRepository(database, cipher, lookup)
    jobs = JobsRepository(database, cipher, lookup)
    configs = ConfigRepository(database, cipher, lookup, token_hasher)
    bindings = ChannelBindingsRepository(database, cipher, lookup, token_hasher)
    email_identities = EmailIdentityRepository(database, cipher, lookup)
    email_identity_service = EmailIdentityService(email_identities)
    channel_prefs = ChannelPreferencesRepository(database)
    sessions = SessionService(auth)
    account = accounts.create_verified("owner@family.test", now=BASE_TIME)
    household = households.create_for_owner(account.id, now=BASE_TIME)
    session = sessions.issue(account.id, now=BASE_TIME)
    stack = ControlPlaneStack(
        config=config,
        database=database,
        cipher=cipher,
        lookup=lookup,
        token_hasher=token_hasher,
        accounts=accounts,
        auth=auth,
        households=households,
        onboarding=onboarding,
        jobs=jobs,
        configs=configs,
        bindings=bindings,
        email_identities=email_identities,
        channel_prefs=channel_prefs,
        sessions=sessions,
        service=OnboardingService(
            households,
            onboarding,
            jobs,
            runtime_provider=config.runtime_provider,
            email_identities=email_identity_service,
        ),
        account=account,
        household=household,
        session=session,
    )
    try:
        yield stack
    finally:
        database.close()


@pytest.fixture
def api_harness(tmp_path: Path) -> APIHarness:
    config = ControlPlaneConfig.for_test(tmp_path)
    mailer = MemoryMailer()
    active = ControlPlaneContainer.build(config, mailer=mailer)
    app = create_app(active_container=active)
    try:
        with TestClient(app, base_url=config.public_origin) as client:
            yield APIHarness(config, active, mailer, client)
    finally:
        active.close()
