"""The runtime half of Art. 7(3) withdrawal.

The control plane can revoke a receipt and revoke every configuration revision,
and an instance that is ALREADY serving will carry on regardless: it re-reads a
local manifest whose embedded purpose, version and digest stay valid-looking
forever. Marking the database is invisible to it.

So withdrawal has to reach the instance, and these tests pin the properties that
make that trustworthy — above all that the endpoint works when the runtime is in
an awkward state, because a withdrawal that fails on a runtime with an
unparseable manifest is a withdrawal that did not happen.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cloud.core.runtime_manifest import (
    compute_config_sha256,
    load_runtime_manifest,
    parse_runtime_manifest,
)
from hermes_cloud.runtime.bootstrap import (
    ActivationState,
    atomic_write,
    load_activation_state,
    write_activation_state,
)
from hermes_cloud.runtime.service import (
    ENV_RUNTIME_DSAR_TOKEN,
    RuntimeNotReady,
    RuntimeService,
)

from .test_runtime_service import RUNTIME_REF, manifest_toml

DSAR_TOKEN = "runtime-dsar-token-canary"
REVOKE_PATH = "/internal/v1/consent/revoke"


def active_service(tmp_path: Path, *, manifest: bool = True) -> RuntimeService:
    activation_path = tmp_path / "activation.json"
    manifest_path = tmp_path / "household.toml"
    if manifest:
        content = manifest_toml()
        parsed = parse_runtime_manifest(content)
        atomic_write(manifest_path, content.encode())
        write_activation_state(
            activation_path,
            ActivationState(
                status="active",
                runtime_ref=RUNTIME_REF,
                household_id=parsed.household_id,
                config_revision=parsed.config_revision,
                config_sha256=parsed.config_sha256,
                updated_at=1.0,
            ),
        )
    return RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={ENV_RUNTIME_DSAR_TOKEN: DSAR_TOKEN},
    )


def revoke(service: RuntimeService, *, token: str = DSAR_TOKEN, method: str = "POST"):
    return service._consent_revoke(method, f"Bearer {token}")


def test_a_serving_runtime_stops_after_withdrawal(tmp_path: Path) -> None:
    """The property the whole feature exists for."""
    service = active_service(tmp_path)
    assert service.readyz().status_code == 200

    assert revoke(service).status_code == 200

    assert service.readyz().status_code == 503
    assert service.readyz().payload["reason"] == "consent_withdrawn"
    assert service.can_start_workers is False
    with pytest.raises(RuntimeNotReady, match="consent_withdrawn"):
        service.require_ready()


def test_withdrawal_survives_a_restart(tmp_path: Path) -> None:
    """A marker in memory would be undone by the next deploy."""
    service = active_service(tmp_path)
    assert revoke(service).status_code == 200

    restarted = active_service(tmp_path)
    assert restarted.readyz().payload["reason"] == "consent_withdrawn"


def test_withdrawal_succeeds_on_a_runtime_that_is_not_ready(tmp_path: Path) -> None:
    """Art. 7(3) is a right, not an attempt.

    A runtime with no manifest, or an unparseable one, must still accept the
    withdrawal — otherwise the awkward states are exactly the ones where the
    family cannot exercise it.
    """
    service = active_service(tmp_path, manifest=False)
    assert service.readyz().status_code == 503

    assert revoke(service).status_code == 200
    assert service.readyz().payload["reason"] == "consent_withdrawn"


def test_withdrawal_is_idempotent(tmp_path: Path) -> None:
    """The control plane retries until the runtime confirms."""
    service = active_service(tmp_path)
    assert revoke(service).status_code == 200
    assert revoke(service).status_code == 200
    assert service.readyz().payload["reason"] == "consent_withdrawn"


def test_withdrawal_requires_the_runtime_credential(tmp_path: Path) -> None:
    service = active_service(tmp_path)

    assert revoke(service, token="wrong-token").status_code == 401
    assert service.readyz().status_code == 200


def test_withdrawal_is_not_reachable_without_a_configured_token(tmp_path: Path) -> None:
    service = RuntimeService(
        manifest_path=tmp_path / "household.toml",
        activation_path=tmp_path / "activation.json",
        runtime_ref=RUNTIME_REF,
        env={},
    )
    assert revoke(service).status_code == 404


def test_withdrawal_rejects_a_get(tmp_path: Path) -> None:
    service = active_service(tmp_path)
    assert revoke(service, method="GET").status_code == 404
    assert service.readyz().status_code == 200


def test_the_route_is_wired_into_the_wsgi_app(tmp_path: Path) -> None:
    """The handler is useless if nothing dispatches to it."""
    service = active_service(tmp_path)
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status

    body = service(
        {
            "PATH_INFO": REVOKE_PATH,
            "REQUEST_METHOD": "POST",
            "HTTP_AUTHORIZATION": f"Bearer {DSAR_TOKEN}",
        },
        start_response,
    )
    assert str(captured["status"]).startswith("200")
    assert b"consent_withdrawn" in b"".join(body)
    assert service.readyz().payload["reason"] == "consent_withdrawn"


def test_deletion_still_takes_precedence(tmp_path: Path) -> None:
    """A deleted runtime reports deletion, not withdrawal."""
    service = active_service(tmp_path)
    atomic_write(service.deletion_marker, b'{"status":"deleted"}')
    assert revoke(service).status_code == 200
    assert service.readyz().payload["reason"] == "runtime_deleted"


def test_an_unknown_consent_purpose_fails_closed_without_raising(
    tmp_path: Path,
) -> None:
    """A manifest may name a purpose this build does not know.

    During a rolling addition the control plane issues a new purpose before
    every runtime carries the copy for it, and the manifest parser accepts any
    non-empty string. Looking the copy up raised `KeyError`, which is worse than
    it sounds: `can_start_workers` reads readiness OUTSIDE the worker loops'
    exception handlers, so the raise terminated ingress worker threads instead
    of producing a fail-closed 503.
    """
    # The digest covers the body, so the purpose has to be added BEFORE it is
    # computed — editing a finished manifest only produces a hash mismatch,
    # which is a different failure and would not exercise the lookup at all.
    stripped = re.sub(r'config_sha256 = "[0-9a-f]*"\n', "", manifest_toml())
    body = re.sub(
        r"required_purposes = \[(.*?)\]",
        lambda m: f'required_purposes = [{m.group(1)}, "a_purpose_added_after_this_build"]',
        stripped,
        count=1,
    )
    # The parser requires a receipt for every required purpose, and so does the
    # real rolling addition: the control plane issues the new purpose WITH its
    # receipt. What the older runtime lacks is the copy to verify it against.
    body += """
[[consent.receipts]]
receipt_id = "10000000-0000-4000-8000-000000000099"
purpose = "a_purpose_added_after_this_build"
text_version = "a-purpose-added-after-this-build-v1"
text_sha256 = "{}"
""".format("b" * 64)
    assert "a_purpose_added_after_this_build" in body
    digest = compute_config_sha256(body)
    content = body.replace(
        "schema_version = 1\n", f'schema_version = 1\nconfig_sha256 = "{digest}"\n'
    )
    manifest_path = tmp_path / "household.toml"
    atomic_write(manifest_path, content.encode())
    parsed = parse_runtime_manifest(content)
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=parsed.household_id,
            config_revision=parsed.config_revision,
            config_sha256=parsed.config_sha256,
            updated_at=1.0,
        ),
    )
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={ENV_RUNTIME_DSAR_TOKEN: DSAR_TOKEN},
    )

    probe = service.readyz()
    assert probe.status_code == 503
    assert probe.payload["reason"] == "consent_not_current"
    # The property that actually kept the workers alive.
    assert service.can_start_workers is False


def test_a_legacy_real_email_manifest_without_the_consent_is_suspended(
    tmp_path: Path,
) -> None:
    """Validating only DECLARED purposes let a legacy manifest keep serving.

    A schema-v1 manifest issued before `special_category_household_content`
    existed can name `nerve-managed` as its provider and declare only the
    restriction. Checking "every purpose the manifest declares" found nothing
    wrong with it, so real family content kept flowing with no Art. 9 condition
    at all. What is owed comes from the provider, as the control plane derives
    it — never read back from the document being validated.
    """
    content = manifest_toml(
        with_email_binding=True,
        email_provider="nerve-managed",
        with_household_consent=False,
    )
    assert 'provider_kind = "nerve-managed"' in content
    assert "special_category_household_content" not in content
    manifest_path = tmp_path / "household.toml"
    atomic_write(manifest_path, content.encode())
    parsed = parse_runtime_manifest(content)
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=parsed.household_id,
            config_revision=parsed.config_revision,
            config_sha256=parsed.config_sha256,
            updated_at=1.0,
        ),
    )
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={ENV_RUNTIME_DSAR_TOKEN: DSAR_TOKEN},
    )

    probe = service.readyz()
    assert probe.status_code == 503
    assert probe.payload["reason"] == "consent_not_current"


def test_withdrawal_does_not_change_what_dsar_answers(tmp_path: Path) -> None:
    """Art. 7(3) stops processing; it does not extinguish Art. 15 and Art. 17.

    Withdrawal made `_ready_manifest` return nothing, and both DSAR routes go
    through it — so exercising one right destroyed the others, and a household
    that withdrew could no longer get its data out or have it deleted.

    Asserted as an equivalence rather than a fixed status: whatever this runtime
    answers for export, it must answer the same after withdrawal. That pins the
    property without depending on how a seeded DSAR database would respond.
    """
    before_service = active_service(tmp_path / "before")
    before = before_service._dsar(
        "/internal/v1/dsar/export", "POST", f"Bearer {DSAR_TOKEN}"
    )

    after_service = active_service(tmp_path / "after")
    assert revoke(after_service).status_code == 200
    assert after_service.readyz().payload["reason"] == "consent_withdrawn"
    after = after_service._dsar(
        "/internal/v1/dsar/export", "POST", f"Bearer {DSAR_TOKEN}"
    )

    assert after.status_code == before.status_code
    # And specifically not the readiness refusal withdrawal used to cause.
    assert after.status_code != 503


def test_dsar_still_requires_its_credential_after_withdrawal(tmp_path: Path) -> None:
    """Relaxing readiness must not relax authentication with it."""
    service = active_service(tmp_path)
    assert revoke(service).status_code == 200

    denied = service._dsar("/internal/v1/dsar/export", "POST", "Bearer wrong")
    assert denied.status_code == 401


def test_dsar_survives_a_stale_consent_version(tmp_path: Path) -> None:
    """Bumping the restriction copy must not disarm export and deletion.

    Every runtime already carrying a v1 receipt became stale the moment v2
    shipped. Routing DSAR through the ordinary readiness path would have taken
    export and deletion down for exactly the households that had been running
    longest — the ones with the most data to exercise those rights over.
    """
    content = manifest_toml(content_restriction_sha="0" * 64)
    manifest_path = tmp_path / "household.toml"
    atomic_write(manifest_path, content.encode())
    parsed = parse_runtime_manifest(content)
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=parsed.household_id,
            config_revision=parsed.config_revision,
            config_sha256=parsed.config_sha256,
            updated_at=1.0,
        ),
    )
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={ENV_RUNTIME_DSAR_TOKEN: DSAR_TOKEN},
    )

    # Ordinary serving is correctly suspended by the stale receipt.
    assert service.readyz().payload["reason"] == "content_restriction_not_current"

    # The rights over data already held are not.
    export = service._dsar(
        "/internal/v1/dsar/export", "POST", f"Bearer {DSAR_TOKEN}"
    )
    assert export.status_code != 503


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        pytest.param(
            lambda state: replace(state, status="pending"),
            "activation_pending",
            id="activation-pending",
        ),
        pytest.param(
            lambda state: replace(state, runtime_ref="abrolia-hh-" + "z" * 26),
            "runtime_ref_mismatch",
            id="runtime-ref-mismatch",
        ),
        pytest.param(
            lambda state: replace(state, config_revision=state.config_revision + 1),
            "revision_mismatch",
            id="revision-mismatch",
        ),
    ],
)
def test_dsar_keeps_every_identity_check(tmp_path: Path, damage, reason) -> None:
    """Relaxing consent must not relax "whose runtime is this".

    The DSAR branch loaded the manifest and returned, skipping the activation
    status, runtime-reference and revision checks along with the consent checks
    it meant to skip. With a valid credential but a pending activation, a
    different `runtime_ref`, or a revision that does not match, export ran
    against a runtime that was never validly activated for this instance, and
    delete and Google revocation could act through a stale provider binding —
    wiping data while revoking the wrong credential and leaving the current one
    live.

    Consent is a question about processing; identity is a question about whose
    data this is, and a DSAR has to get the second one right.
    """
    service = active_service(tmp_path)
    state = load_activation_state(service.activation_path)
    assert state is not None
    write_activation_state(service.activation_path, damage(state))

    manifest, actual = service._ready_manifest(for_data_subject_request=True)

    assert manifest is None, "a DSAR was served from an unverified runtime"
    assert actual == reason


def test_dsar_still_answers_for_a_withdrawn_or_stale_consent(tmp_path: Path) -> None:
    """The two relaxations that are correct must survive the tightening.

    Withdrawal and consent staleness still have to leave export and deletion
    reachable — Art. 15 and Art. 17 are about data already held — so this pins
    the boundary from the other side.
    """
    service = active_service(tmp_path)
    assert revoke(service).status_code == 200
    assert service.readyz().payload["reason"] == "consent_withdrawn"

    manifest, reason = service._ready_manifest(for_data_subject_request=True)

    assert manifest is not None, "withdrawal took the data-subject rights with it"
    assert reason == "dsar"


def _revoke_body(service: RuntimeService, receipt_ids: list[str] | None):
    body = b"" if receipt_ids is None else json.dumps(
        {"receipt_ids": receipt_ids}
    ).encode()
    return service._consent_revoke("POST", f"Bearer {DSAR_TOKEN}", body)


def test_a_stop_for_a_superseded_generation_is_acknowledged_not_obeyed(
    tmp_path: Path,
) -> None:
    """A stale withdrawal must not suspend a re-consented runtime.

    Withdraw from revision A, leave that stop unreachable, re-consent and
    reprovision as revision B. The runtime reference is the household's stable
    app name, so the old job authenticates against B — and unconditionally
    marking meant a valid, currently-consented runtime was suspended
    indefinitely by a withdrawal nobody made against it.

    Acknowledged, so the job settles and stops retrying, and not obeyed, so the
    runtime keeps serving the consent it actually holds.
    """
    service = active_service(tmp_path)
    held = {
        receipt.receipt_id
        for receipt in load_runtime_manifest(
            service.manifest_path, env=service.env
        ).consent.receipts
    }
    assert held, "the fixture manifest declares no receipts"

    probe = _revoke_body(service, ["99999999-9999-4999-8999-999999999999"])

    assert probe.status_code == 200
    assert probe.payload["state"] == "superseded_generation"
    assert not service.consent_marker.exists(), "a stale stop suspended the runtime"
    assert service.readyz().payload.get("reason") != "consent_withdrawn"


def test_a_stop_naming_this_generation_still_suspends_it(tmp_path: Path) -> None:
    """The other half: a matching stop must work exactly as before."""
    service = active_service(tmp_path)
    held = sorted(
        receipt.receipt_id
        for receipt in load_runtime_manifest(
            service.manifest_path, env=service.env
        ).consent.receipts
    )

    probe = _revoke_body(service, held)

    assert probe.status_code == 200
    assert probe.payload["state"] == "consent_withdrawn"
    assert service.consent_marker.exists()
    assert service.readyz().payload["reason"] == "consent_withdrawn"


@pytest.mark.parametrize(
    ("receipt_ids", "why"),
    [
        pytest.param(None, "a control plane that predates the generation field", id="no-body"),
        pytest.param([], "an empty list names no generation", id="empty"),
    ],
)
def test_a_stop_that_names_no_generation_still_suspends(
    tmp_path: Path, receipt_ids, why
) -> None:
    """Only a POSITIVE mismatch declines. Everything else stops the runtime.

    Fail closed toward stopping: the cost of suspending a runtime that could
    have kept running is recoverable, and the cost of not suspending one is not.
    """
    service = active_service(tmp_path)

    probe = _revoke_body(service, receipt_ids)

    assert probe.status_code == 200, why
    assert service.consent_marker.exists(), why


def test_a_generation_that_cannot_be_decided_is_deferred_not_marked(
    tmp_path: Path,
) -> None:
    """The bootstrap window, which fail-closed-toward-stopping got wrong.

    Deliver revision A's stop after `serve_runtime` has opened its socket but
    BEFORE the background bootstrap has installed revision B's manifest. Folding
    "cannot tell" into "match" wrote the permanent marker, and bootstrap never
    clears or re-checks it — so a fully consented runtime is suspended for good
    by a withdrawal nobody made against it.

    Deferring costs no processing: a runtime whose manifest is unreadable is not
    serving anything, because `readyz`, `require_ready` and `can_start_workers`
    all go through `_ready_manifest` and fail. Marking the wrong generation
    stops processing that is lawful. The control plane treats a non-200/410 as
    `outcome_unknown` and comes back when the answer exists.
    """
    service = active_service(tmp_path)
    service.manifest_path.write_text("this is not a manifest yet", encoding="utf-8")

    probe = _revoke_body(service, ["99999999-9999-4999-8999-999999999999"])

    assert probe.status_code == 503
    assert probe.payload["status"] == "generation_undecidable"
    assert not service.consent_marker.exists(), (
        "a stop was obeyed before it could be attributed to a generation"
    )


def test_the_deferred_stop_lands_once_the_manifest_is_installed(tmp_path: Path) -> None:
    """Deferral must be a wait, not a refusal.

    The retry has to succeed once the manifest exists — otherwise "come back
    later" is just a withdrawal that never happens, which Art. 7(3) does not
    allow.
    """
    service = active_service(tmp_path)
    held = sorted(
        receipt.receipt_id
        for receipt in load_runtime_manifest(
            service.manifest_path, env=service.env
        ).consent.receipts
    )
    installed = service.manifest_path.read_text(encoding="utf-8")
    service.manifest_path.write_text("mid-bootstrap", encoding="utf-8")
    assert _revoke_body(service, held).status_code == 503

    # Bootstrap finishes.
    service.manifest_path.write_text(installed, encoding="utf-8")

    probe = _revoke_body(service, held)

    assert probe.status_code == 200
    assert probe.payload["state"] == "consent_withdrawn"
    assert service.consent_marker.exists()


def test_a_stop_naming_no_generation_still_stops_an_unreadable_runtime(
    tmp_path: Path,
) -> None:
    """The legacy path keeps the unconditional guarantee.

    A control plane that predates the generation field names nothing, so there
    is no question to defer for — and withdrawal must still succeed on a runtime
    in an awkward state, because Art. 7(3) gives the family a right to it rather
    than an attempt at it.
    """
    service = active_service(tmp_path)
    service.manifest_path.write_text("this is not a manifest", encoding="utf-8")

    probe = _revoke_body(service, None)

    assert probe.status_code == 200
    assert service.consent_marker.exists()


@pytest.mark.parametrize(
    "path",
    [
        "/internal/v1/consent/revoke",
        "/internal/v1/dsar/export",
        "/internal/v1/dsar/delete",
        "/internal/v1/email/google/revoke",
    ],
)
def test_every_internal_route_is_authenticated_before_its_handler(
    tmp_path: Path, path
) -> None:
    """The gate is outside the handler, so a handler is only ever reached
    authenticated.

    Each handler used to check its own credential. The comparisons were correct,
    and the rule they broke is checkable: authentication inside a handler is
    authentication a future early return, or a second dispatch path, can step
    over — with no signature to inspect that would show it missing. A route
    either appears in `INTERNAL_ROUTES` and is authenticated, or it does not
    exist.
    """
    service = active_service(tmp_path)
    assert path in service.INTERNAL_ROUTES, "a route with no declared gate"

    body: list[bytes] = []
    status: list[str] = []

    def start_response(code, headers):
        status.append(code)
        return body.append

    # The property is that the HANDLER IS NOT REACHED — not merely that a 401
    # comes back, which the handlers' own checks would also produce. Observing
    # the outcome cannot tell a gate from defence in depth.
    reached: list[str] = []
    for name in ("_consent_revoke", "_dsar", "_google_revoke"):
        original = getattr(type(service), name)

        def trap(*args, _name=name, _original=original, **kwargs):
            reached.append(_name)
            return _original(*args, **kwargs)

        setattr(service, name, trap.__get__(service, type(service)))

    service(
        {
            "PATH_INFO": path,
            "REQUEST_METHOD": "POST",
            "HTTP_AUTHORIZATION": "Bearer wrong-token",
        },
        start_response,
    )

    assert status and status[0].startswith("401"), status
    assert reached == [], f"an unauthenticated request reached {reached}"


def test_the_declared_internal_routes_are_the_dispatched_ones(tmp_path: Path) -> None:
    """A route added to dispatch but not to the table is an open route."""
    import inspect

    service = active_service(tmp_path)
    dispatched = {
        literal
        for literal in inspect.getsource(type(service).__call__).split('"')
        if literal.startswith("/internal/")
    }
    assert dispatched <= service.INTERNAL_ROUTES, dispatched - service.INTERNAL_ROUTES
