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

import re
from pathlib import Path

import pytest

from hermes_cloud.core.runtime_manifest import (
    compute_config_sha256,
    parse_runtime_manifest,
)
from hermes_cloud.runtime.bootstrap import (
    ActivationState,
    atomic_write,
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
    body = stripped.replace(
        'required_purposes = ["special_category_content_restriction"]',
        'required_purposes = ["special_category_content_restriction",'
        ' "a_purpose_added_after_this_build"]',
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
