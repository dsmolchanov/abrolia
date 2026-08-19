"""FastAPI composition for the same-origin onboarding control plane."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from control_plane.api import auth, email, households, internal_bootstrap, onboarding, privacy, web
from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.db import new_id
from control_plane.observability import HealthReporter, HealthSnapshot
from control_plane.onboarding.contracts import WorkflowConflict
from control_plane.privacy.consent import consent_version_and_sha, consent_version_and_text
from control_plane.repositories.households import HouseholdNotFound

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
PWA_ROOT = Path(__file__).resolve().parents[2] / "web"
MAXIMUM_BACKUP_AGE_SECONDS = 26 * 60 * 60
SAFE_PROVIDER_STATUSES = frozenset({"configured", "disabled", "unavailable"})
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'; object-src 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


def create_app(
    config: ControlPlaneConfig | None = None,
    *,
    active_container: ControlPlaneContainer | None = None,
) -> FastAPI:
    owns_container = active_container is None
    if active_container is None:
        active_container = ControlPlaneContainer.build(
            config or ControlPlaneConfig.from_env(), acquire_process_lock=True
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owns_container:
            active_container.close()

    app = FastAPI(
        title="Abrolia onboarding control plane",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.container = active_container
    app.state.bootstrap = active_container.bootstrap
    app.state.health_reporter = HealthReporter(active_container.database)
    app.state.owns_container = owns_container
    templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
    app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
    # PWA shell from top-level web/ (Phase E E6) — serve manifest, sw, icons
    if PWA_ROOT.is_dir():
        app.mount("/pwa", StaticFiles(directory=str(PWA_ROOT)), name="pwa")
        # Also serve PWA static at same place as control_plane static for manifest compat
        pwa_static = PWA_ROOT / "static"
        if pwa_static.is_dir():
            # expose icons via same /static path via additional mount at /pwa-static
            app.mount("/pwa-static", StaticFiles(directory=str(pwa_static)), name="pwa-static")

    @app.middleware("http")
    async def secure_response(request: Request, call_next):
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        request_host = (request.url.hostname or "").casefold()
        if forwarded_proto == "http" and not request_host.endswith(".flycast"):
            return RedirectResponse(
                str(request.url.replace(scheme="https")),
                status_code=status.HTTP_308_PERMANENT_REDIRECT,
            )
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        if request.url.path.startswith(("/api/", "/internal/", "/auth/", "/onboarding")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.exception_handler(WorkflowConflict)
    async def workflow_conflict(_request: Request, error: WorkflowConflict) -> JSONResponse:
        return JSONResponse(
            {"detail": str(error), "code": error.__class__.__name__},
            status_code=status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(HouseholdNotFound)
    async def household_not_found(_request: Request, _error: HouseholdNotFound) -> JSONResponse:
        return JSONResponse(
            {"detail": "household not found"}, status_code=status.HTTP_404_NOT_FOUND
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default includes the submitted input. That is unsafe for
        # magic-link/bootstrap token request bodies, so only structural facts survive.
        safe_errors = [
            {"loc": item.get("loc", ()), "type": item.get("type"), "msg": item.get("msg")}
            for item in error.errors()
        ]
        return JSONResponse(
            {"detail": safe_errors}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/start", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/start", include_in_schema=False)
    def start(request: Request):
        return templates.TemplateResponse(
            request, "start.html", {"sent": request.query_params.get("sent") == "1"}
        )

    @app.get("/auth/verify", include_in_schema=False)
    def verify(request: Request):
        return templates.TemplateResponse(request, "verify.html", {})

    @app.get("/onboarding", include_in_schema=False)
    def onboarding_page(request: Request):
        try:
            principal = active_container.sessions.authenticate(
                request.cookies.get(active_container.config.session_cookie_name, "")
            )
            household = active_container.households.current_for_account(principal.account_id)
            account = active_container.accounts.get(principal.account_id)
        except (PermissionError, HouseholdNotFound):
            return RedirectResponse("/start", status_code=status.HTTP_303_SEE_OTHER)
        snapshot = active_container.onboarding_repository.snapshot(household.id)
        restriction_version, restriction_text = consent_version_and_text(
            "special_category_content_restriction"
        )
        _, restriction_sha = consent_version_and_sha(
            "special_category_content_restriction"
        )
        household_version, household_text = consent_version_and_text(
            "special_category_household_content"
        )
        _, household_sha = consent_version_and_sha(
            "special_category_household_content"
        )
        return templates.TemplateResponse(
            request,
            "onboarding.html",
            {
                "snapshot": snapshot,
                "recovery_email": account.masked_email if account else "unavailable",
                "csrf_token": request.cookies.get(active_container.config.csrf_cookie_name, ""),
                "idempotency_key": new_id(),
                "error": request.query_params.get("error"),
                "google_confirm": request.query_params.get("google") == "confirm",
                "special_category_restriction_version": restriction_version,
                "special_category_restriction_text": restriction_text,
                "special_category_restriction_sha256": restriction_sha,
                "special_category_household_version": household_version,
                "special_category_household_text": household_text,
                "special_category_household_sha256": household_sha,
                # Per OPTION, from the same predicate the gate uses — not from
                # the managed-rollout flag, which says nothing about Gmail.
                "household_consent_required": {
                    option: active_container.onboarding
                    .email_option_processes_real_content(option)
                    for option in ("abrolia_managed", "gmail_agent", "family_domain")
                },
            },
        )

    def provider_health() -> dict[str, str]:
        try:
            raw_status = active_container.providers.health()
        except Exception:  # provider exceptions and response bodies are never probe output
            return {"registry": "unavailable"}
        return {
            str(name): value if value in SAFE_PROVIDER_STATUSES else "unavailable"
            for name, value in sorted(raw_status.items())
        }

    def probe_payload(
        snapshot: HealthSnapshot,
        *,
        status_value: str,
        blockers: tuple[str, ...],
        provider_status: dict[str, str],
    ) -> dict:
        if snapshot.backup_age_seconds is None:
            backup_status = "not_observed"
        elif snapshot.backup_age_seconds > MAXIMUM_BACKUP_AGE_SECONDS:
            backup_status = "stale"
        else:
            backup_status = "fresh"
        return {
            "status": status_value,
            "mode": "synthetic-only",
            "checks": {
                "database": "ok" if snapshot.database_ok else "unavailable",
                "volume": "ok" if snapshot.volume_ok else "unavailable",
                "workers": "paused" if snapshot.workers_paused else "running",
                "backup": backup_status,
                "providers": provider_status,
            },
            "metrics": {
                "volume_free_bytes": snapshot.volume_free_bytes,
                "backup_age_seconds": snapshot.backup_age_seconds,
                "pending_jobs": snapshot.pending_jobs,
                "stale_leases": snapshot.stale_leases,
                "unknown_outcomes": snapshot.unknown_outcomes,
                "expired_bootstrap": snapshot.expired_bootstrap_tokens,
            },
            "blockers": list(blockers),
        }

    def health_snapshot() -> HealthSnapshot:
        reporter = app.state.health_reporter
        return reporter.snapshot(
            backup_completed_at=reporter.latest_backup_completed_at()
        )

    @app.get("/healthz")
    def health() -> JSONResponse:
        snapshot = health_snapshot()
        blockers = snapshot.liveness_blockers()
        providers = provider_health()
        return JSONResponse(
            probe_payload(
                snapshot,
                status_value="healthy" if not blockers else "unhealthy",
                blockers=blockers,
                provider_status=providers,
            ),
            status_code=(
                status.HTTP_200_OK
                if not blockers
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )

    @app.get("/readyz")
    def ready() -> JSONResponse:
        snapshot = health_snapshot()
        providers = provider_health()
        blockers = list(snapshot.readiness_blockers(
            maximum_backup_age_seconds=MAXIMUM_BACKUP_AGE_SECONDS
        ))
        if any(value == "unavailable" for value in providers.values()):
            blockers.append("provider_registry_unavailable")
        blocker_tuple = tuple(blockers)
        return JSONResponse(
            probe_payload(
                snapshot,
                status_value="ready" if not blocker_tuple else "not_ready",
                blockers=blocker_tuple,
                provider_status=providers,
            ),
            status_code=(
                status.HTTP_200_OK
                if not blocker_tuple
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )

    app.include_router(auth.router)
    app.include_router(households.router)
    app.include_router(onboarding.router)
    app.include_router(email.router)
    app.include_router(privacy.router)
    app.include_router(web.router)
    app.include_router(internal_bootstrap.router)

    return app
