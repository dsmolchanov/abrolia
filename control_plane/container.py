"""Explicit dependency graph for the single-writer Phase 1 deployable."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from control_plane.auth.mailer import Mailer, MemoryMailer, ResendMailer
from control_plane.auth.rate_limit import RateLimiter
from control_plane.auth.sessions import SessionService
from control_plane.auth.tokens import MagicLinkService
from control_plane.config import ControlPlaneConfig
from control_plane.crypto import FieldCipher, LookupHasher
from control_plane.db import ControlPlaneDatabase
from control_plane.email.repository import EmailIdentityRepository
from control_plane.email.service import EmailIdentityService
from control_plane.observability import StructuredLogger
from control_plane.onboarding.service import OnboardingService
from control_plane.privacy.delete import (
    DeletionService,
    RuntimeDeleter,
    SyntheticRuntimeDeleter,
)
from control_plane.privacy.export import (
    HouseholdExporter,
    RuntimeExporter,
    SyntheticRuntimeExporter,
)
from control_plane.privacy.retention import RetentionService
from control_plane.privacy.runtime import PrivateRuntimeDsarClient
from control_plane.providers.email.google_oauth import (
    GoogleOAuthClient,
    GoogleOAuthProvisioner,
    GoogleOAuthService,
)
from control_plane.provisioning.bootstrap import BootstrapService
from control_plane.provisioning.fakes import synthetic_provider_registry
from control_plane.provisioning.planner import DesiredSpecPlanner
from control_plane.provisioning.runtime_health import RuntimeReadinessMonitor
from control_plane.provisioning.secrets import FlySecretSink, InMemorySecretSink
from control_plane.provisioning.worker import ProvisioningWorker
from control_plane.repositories import (
    AccountsRepository,
    AuthRepository,
    ConfigRepository,
    HouseholdsRepository,
    JobsRepository,
    OnboardingRepository,
)
from control_plane.services.accounts import AccountService
from control_plane.services.households import HouseholdService


@dataclass
class ControlPlaneContainer:
    config: ControlPlaneConfig
    database: ControlPlaneDatabase
    cipher: FieldCipher
    lookup: LookupHasher
    token_hasher: LookupHasher
    accounts: AccountsRepository
    auth: AuthRepository
    households: HouseholdsRepository
    onboarding_repository: OnboardingRepository
    jobs: JobsRepository
    configs: ConfigRepository
    email_identities: EmailIdentityRepository
    email_identity_service: EmailIdentityService
    google_oauth: GoogleOAuthService
    sessions: SessionService
    magic_links: MagicLinkService
    account_service: AccountService
    household_service: HouseholdService
    onboarding: OnboardingService
    rate_limiter: RateLimiter
    providers: Any
    secret_sink: Any
    planner: DesiredSpecPlanner
    worker: ProvisioningWorker
    bootstrap: BootstrapService
    exporter: HouseholdExporter
    deletion: DeletionService
    retention: RetentionService
    runtime_health: RuntimeReadinessMonitor

    @classmethod
    def build(
        cls,
        config: ControlPlaneConfig,
        *,
        mailer: Mailer | None = None,
        acquire_process_lock: bool = False,
        runtime_exporter: RuntimeExporter | None = None,
        runtime_deleter: RuntimeDeleter | None = None,
    ) -> ControlPlaneContainer:
        config.validate()
        database = ControlPlaneDatabase(config.database_path)
        if acquire_process_lock:
            database.acquire_process_lock()
        database.migrate()
        cipher = FieldCipher(config.encryption_keys, config.active_encryption_key_version)
        lookup = LookupHasher(config.lookup_hmac_key)
        token_hasher = LookupHasher(config.token_hmac_key)
        accounts = AccountsRepository(database, cipher, lookup)
        auth = AuthRepository(database, cipher, lookup, token_hasher)
        households = HouseholdsRepository(database, cipher, lookup)
        onboarding_repository = OnboardingRepository(database, cipher, lookup)
        jobs = JobsRepository(database, cipher, lookup)
        configs = ConfigRepository(database, cipher, lookup, token_hasher)
        email_identities = EmailIdentityRepository(database, cipher, lookup)
        email_identity_service = EmailIdentityService(email_identities)
        sessions = SessionService(auth)
        if mailer is None:
            mailer = (
                ResendMailer(
                    api_key=config.resend_api_key,
                    sender=config.magic_link_from,
                )
                if config.magic_link_delivery_enabled
                and config.resend_api_key
                and config.magic_link_from
                else MemoryMailer()
            )
        magic_links = MagicLinkService(auth, mailer, config.public_origin)
        account_service = AccountService(auth, accounts, households, sessions)
        household_service = HouseholdService(households)
        rate_limiter = RateLimiter(database, lookup)
        providers = synthetic_provider_registry()
        fly_provider = None
        if config.runtime_provider == "fly-runtime":
            from control_plane.provisioning.fly import FlyRuntimeProvisioner

            fly_provider = FlyRuntimeProvisioner.from_config(config)
            providers.register("fly-runtime", fly_provider)
            secret_sink = FlySecretSink()
        else:
            secret_sink = InMemorySecretSink()
        google_client = (
            GoogleOAuthClient(
                client_id=config.google_oauth_client_id,
                client_secret=config.google_oauth_client_secret,
            )
            if config.google_oauth_client_id and config.google_oauth_client_secret
            else None
        )
        google_oauth = GoogleOAuthService(
            config=config,
            client=google_client,
            identities=email_identities,
            accounts=accounts,
            jobs=jobs,
            secret_sink=secret_sink,
            token_hasher=token_hasher,
        )
        google_provider = GoogleOAuthProvisioner(google_oauth)
        providers.register("google-oauth", google_provider)
        if config.real_email_enabled:
            from control_plane.providers.email.nerve_byo_domain import (
                NerveByoDomainProvisioner,
            )
            from control_plane.providers.email.nerve_client import (
                NerveAdminClient,
                NerveAdminSettings,
            )
            from control_plane.providers.email.nerve_managed import (
                NerveManagedEmailProvisioner,
            )

            nerve_client = NerveAdminClient(NerveAdminSettings(
                base_url=config.nerve_base_url or "",
                admin_key=config.nerve_admin_key or "",
                platform_org_id=config.nerve_platform_org_id or "",
                platform_domain_id=config.nerve_platform_domain_id or "",
            ))
            providers.register(
                "nerve-managed", NerveManagedEmailProvisioner(nerve_client)
            )
            providers.register(
                "nerve-byo-domain", NerveByoDomainProvisioner(nerve_client)
            )
        email_provider = "nerve-managed" if config.real_email_enabled else "fake-email"
        byo_domain_provider = (
            "nerve-byo-domain" if config.real_email_enabled else "fake-email"
        )
        onboarding = OnboardingService(
            households,
            onboarding_repository,
            jobs,
            runtime_provider=config.runtime_provider,
            gmail_provider="google-oauth",
            email_provider=email_provider,
            byo_domain_provider=byo_domain_provider,
            allow_real_email_domains=config.real_email_enabled,
            real_email_enabled=config.real_email_enabled,
            real_email_household_allowlist=config.real_email_household_allowlist,
            email_identities=email_identity_service,
        )
        planner = DesiredSpecPlanner(accounts, households, onboarding_repository, configs)
        worker = ProvisioningWorker(
            jobs=jobs,
            onboarding=onboarding_repository,
            households=households,
            configs=configs,
            planner=planner,
            providers=providers,
            secret_sink=secret_sink,
            email_identities=email_identities,
            runtime_provider=config.runtime_provider,
            bootstrap_ttl_seconds=config.bootstrap_ttl_seconds,
            logger=StructuredLogger(sys.stderr),
        )
        bootstrap = BootstrapService(configs, onboarding_repository, jobs)
        runtime_boundary = (
            PrivateRuntimeDsarClient(token_hasher) if config.runtime_provider == "fly-runtime" else None
        )
        if runtime_boundary is not None:
            google_provider.revoker = runtime_boundary
            google_provider.namespace_revoker = fly_provider
        if runtime_exporter is None:
            runtime_exporter = (
                SyntheticRuntimeExporter()
                if config.runtime_provider == "dry-run-runtime"
                else runtime_boundary
            )
        if runtime_deleter is None:
            runtime_deleter = (
                SyntheticRuntimeDeleter()
                if config.runtime_provider == "dry-run-runtime"
                else runtime_boundary
            )
        assert runtime_exporter is not None and runtime_deleter is not None
        exporter = HouseholdExporter(
            accounts,
            households,
            onboarding_repository,
            jobs,
            runtime=runtime_exporter,
        )
        deletion = DeletionService(
            accounts,
            auth,
            households,
            jobs,
            providers,
            runtime=runtime_deleter,
        )
        retention = RetentionService(accounts)
        runtime_health = RuntimeReadinessMonitor(database)
        return cls(
            config,
            database,
            cipher,
            lookup,
            token_hasher,
            accounts,
            auth,
            households,
            onboarding_repository,
            jobs,
            configs,
            email_identities,
            email_identity_service,
            google_oauth,
            sessions,
            magic_links,
            account_service,
            household_service,
            onboarding,
            rate_limiter,
            providers,
            secret_sink,
            planner,
            worker,
            bootstrap,
            exporter,
            deletion,
            retention,
            runtime_health,
        )

    def close(self) -> None:
        self.database.close()

    def __enter__(self) -> ControlPlaneContainer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
