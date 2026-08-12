from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.crypto import SecretMaterial
from control_plane.provisioning.contracts import (
    InspectState,
    OutcomeUnknown,
    ProviderRateLimited,
    ProviderRejected,
)
from control_plane.provisioning.fly import FlyRuntimeProvisioner, _authorization_header
from control_plane.provisioning.manifest import (
    ActorsV1,
    ChannelBindingV1,
    ChannelsV1,
    ConsentAuthorityV1,
    ConsentReceiptV1,
    DesiredHouseholdSpecV1,
    EmailV1,
)
from control_plane.provisioning.secrets import FlySecretSink, SecretInstallError

IMAGE = "registry.example.test/abrolia@sha256:" + "a" * 64


def _spec() -> DesiredHouseholdSpecV1:
    return DesiredHouseholdSpecV1(
        household_id="00000000-0000-4000-8000-000000000042",
        config_revision=3,
        family_language="en",
        timezone="Europe/Prague",
        country_code="CZ",
        residency_mode="eu-app",
        actors=ActorsV1(owner="synthetic-owner", family=("synthetic-owner",)),
        channels=ChannelsV1(primary="telegram"),
        channel_bindings=(
            ChannelBindingV1(
                channel="telegram",
                actor_id="synthetic-owner",
                chat_id="synthetic-chat",
                external_ref="synthetic-channel-ref",
            ),
        ),
        email=EmailV1(
            agent_inbox="agent@assistant.test", fallback="owner@family.test"
        ),
        consent=ConsentAuthorityV1(
            required_purposes=("whatsapp_channel_privacy",),
            receipts=(
                ConsentReceiptV1(
                    receipt_id="synthetic-consent-receipt",
                    purpose="whatsapp_channel_privacy",
                    text_version="phase-1",
                    text_sha256="b" * 64,
                ),
            ),
        ),
        provider_refs={"email": "synthetic-email-ref"},
    ).with_hash()


def _provisioner(handler) -> FlyRuntimeProvisioner:
    return FlyRuntimeProvisioner(
        api_token="fly-api-secret-canary",
        org_slug="synthetic-org",
        image_digest=IMAGE,
        bootstrap_url="https://app.example.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, b"", b""
        ),
    )


def _synthetic_macaroon(version: str = "2") -> str:
    """Build a non-secret Fly-shaped test value without scanner-like literals."""
    return f"fm{version}_synthetic-macaroon"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("synthetic-oauth-token", "Bearer synthetic-oauth-token"),
        ("Bearer synthetic-oauth-token", "Bearer synthetic-oauth-token"),
        (_synthetic_macaroon(), f"FlyV1 {_synthetic_macaroon()}"),
        ("FlyV1 fm1a_one,fm2_two", "FlyV1 fm1a_one,fm2_two"),
        (
            f"synthetic-oauth,{_synthetic_macaroon('1r')}",
            f"FlyV1 {_synthetic_macaroon('1r')}",
        ),
    ],
)
def test_fly_authorization_header_matches_flaps_token_scheme(
    token: str, expected: str
) -> None:
    assert _authorization_header(token) == expected


def test_fly_request_uses_macaroon_authorization_scheme() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"status": "ok"}, request=request)

    provisioner = FlyRuntimeProvisioner(
        api_token=_synthetic_macaroon(),
        org_slug="synthetic-org",
        image_digest=IMAGE,
        bootstrap_url="https://app.example.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provisioner._request("GET", "/status") == {"status": "ok"}
    assert seen == [f"FlyV1 {_synthetic_macaroon()}"]


def test_config_uses_private_http_only_for_flycast(tmp_path: Path) -> None:
    base = replace(
        ControlPlaneConfig.for_test(tmp_path),
        runtime_provider="fly-runtime",
        fly_api_token="synthetic-fly-token",
        fly_org_slug="synthetic-org",
        runtime_image_digest=IMAGE,
    )
    flycast = FlyRuntimeProvisioner.from_config(
        replace(base, internal_bootstrap_host="control-plane.flycast")
    )
    public = FlyRuntimeProvisioner.from_config(
        replace(base, internal_bootstrap_host="control-plane.example.test")
    )
    try:
        assert flycast.bootstrap_url == "http://control-plane.flycast"
        assert public.bootstrap_url == "https://control-plane.example.test"
    finally:
        flycast.client.close()
        public.client.close()


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, "ok"),
        (401, ProviderRejected),
        (403, ProviderRejected),
        (404, ProviderRejected),
        (422, ProviderRejected),
        (500, OutcomeUnknown),
        (503, OutcomeUnknown),
    ],
)
def test_fly_http_status_taxonomy(status_code: int, expected: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if status_code == 200:
            return httpx.Response(200, json={"status": "ok"}, request=request)
        return httpx.Response(status_code, json={"detail": "secret-provider-body"}, request=request)

    provisioner = _provisioner(handler)
    if expected == "ok":
        assert provisioner._request("GET", "/status") == {"status": "ok"}
    else:
        with pytest.raises(expected) as raised:
            provisioner._request("GET", "/status")
        assert "secret-provider-body" not in str(raised.value)
        assert "fly-api-secret-canary" not in str(raised.value)


def test_fly_404_can_be_an_idempotent_absence() -> None:
    provisioner = _provisioner(
        lambda request: httpx.Response(404, json={}, request=request)
    )
    assert provisioner._request("GET", "/missing", allow_not_found=True) is None


def test_fly_rate_limit_and_network_or_unreadable_response_are_typed() -> None:
    limited = _provisioner(
        lambda request: httpx.Response(
            429, headers={"Retry-After": "17.5"}, request=request
        )
    )
    with pytest.raises(ProviderRateLimited) as raised:
        limited._request("POST", "/limited")
    assert raised.value.retry_after == 17.5

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network-secret-canary", request=request)

    with pytest.raises(OutcomeUnknown) as unknown:
        _provisioner(timeout)._request("POST", "/timeout")
    assert "network-secret-canary" not in str(unknown.value)

    unreadable = _provisioner(
        lambda request: httpx.Response(200, content=b"not-json", request=request)
    )
    with pytest.raises(OutcomeUnknown, match="unreadable"):
        unreadable._request("GET", "/unreadable")


def test_stable_app_name_is_lowercase_base32_household_uuid() -> None:
    assert FlyRuntimeProvisioner.stable_app_name(_spec().household_id) == (
        "abrolia-hh-aaaaaaaaabaabaaaaaaaaaaaii"
    )


class StatefulFly:
    def __init__(
        self,
        *,
        conflict_on_app_create: bool = False,
        machine_delete_lag: int = 0,
        app_delete_lag: int = 0,
        ignore_volume_update: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.query_params: list[tuple[str, str, dict[str, str]]] = []
        self.app: dict[str, Any] | None = None
        self.volume: dict[str, Any] | None = None
        self.machine: dict[str, Any] | None = None
        self.conflict_on_app_create = conflict_on_app_create
        self.machine_delete_lag = machine_delete_lag
        self.app_delete_lag = app_delete_lag
        self.ignore_volume_update = ignore_volume_update
        self.machine_delete_requested = False
        self.app_delete_requested = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.query_params.append((request.method, path, dict(request.url.params)))
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/v1/apps" and request.method == "POST":
            self.app = {"name": body["app_name"]}
            if self.conflict_on_app_create:
                self.conflict_on_app_create = False
                return httpx.Response(409, json={}, request=request)
            return httpx.Response(201, json=self.app, request=request)
        if "/volumes" not in path and "/machines" not in path:
            if request.method == "DELETE":
                self.app_delete_requested = True
                return httpx.Response(202, json={"status": "deleting"}, request=request)
            if self.app_delete_requested:
                if self.app_delete_lag:
                    self.app_delete_lag -= 1
                else:
                    self.app = None
                    self.volume = None
                    self.machine = None
            status = 200 if self.app else 404
            return httpx.Response(status, json=self.app or {}, request=request)
        if "/volumes/" in path:
            if request.method == "PUT":
                assert self.volume is not None
                if not self.ignore_volume_update:
                    self.volume.update(body)
                return httpx.Response(200, json=self.volume, request=request)
            if request.method == "DELETE":
                self.volume = None
                return httpx.Response(204, request=request)
        if path.endswith("/volumes"):
            if request.method == "GET":
                return httpx.Response(
                    200, json=[] if self.volume is None else [self.volume], request=request
                )
            self.volume = {"id": "vol-synthetic", **body}
            return httpx.Response(201, json=self.volume, request=request)
        if "/machines/" in path:
            if request.method == "POST":
                self.machine = {
                    "id": "machine-synthetic",
                    "state": "started",
                    **body,
                }
                return httpx.Response(200, json=self.machine, request=request)
            if request.method == "DELETE":
                self.machine_delete_requested = True
                return httpx.Response(202, json={"status": "destroying"}, request=request)
        if path.endswith("/machines"):
            if request.method == "GET":
                if self.machine_delete_requested:
                    if self.machine_delete_lag:
                        self.machine_delete_lag -= 1
                    else:
                        self.machine = None
                return httpx.Response(
                    200, json=[] if self.machine is None else [self.machine], request=request
                )
            self.machine = {"id": "machine-synthetic", "state": "started", **body}
            return httpx.Response(201, json=self.machine, request=request)
        raise AssertionError(f"unexpected Fly call: {request.method} {path}")


@pytest.mark.parametrize("conflict_on_app_create", [False, True])
def test_ensure_inspects_before_create_orders_writes_and_is_idempotent(
    conflict_on_app_create: bool,
) -> None:
    fly = StatefulFly(conflict_on_app_create=conflict_on_app_create)
    provisioner = _provisioner(fly)
    spec = _spec()
    intent = {"manifest": spec.model_dump(mode="json")}

    prepared = provisioner.prepare(intent, "intent-one")
    app_name = provisioner.stable_app_name(spec.household_id)
    assert prepared.external_ref == app_name
    assert prepared.public_result["stage"] == "prepared"
    assert not any(
        method == "POST" and path.endswith("/machines")
        for method, path, _body in fly.calls
    )
    fly.calls.append(("STAGE_SECRETS", app_name, None))
    first = provisioner.launch(intent, prepared, "intent-one")
    post_count = len([call for call in fly.calls if call[0] == "POST"])
    second = provisioner.ensure(intent, "intent-two")

    assert first == second
    assert len([call for call in fly.calls if call[0] == "POST"]) == post_count
    assert first.external_ref == app_name
    ordered_writes = [
        (method, path)
        for method, path, _body in fly.calls
        if method in {"POST", "STAGE_SECRETS"}
    ]
    assert ordered_writes.index(("POST", "/v1/apps")) < ordered_writes.index(
        ("POST", f"/v1/apps/{app_name}/volumes")
    ) < ordered_writes.index(("STAGE_SECRETS", app_name)) < ordered_writes.index(
        ("POST", f"/v1/apps/{app_name}/machines")
    )

    volume_payload = next(
        body
        for method, path, body in fly.calls
        if method == "POST" and path.endswith("/volumes")
    )
    assert volume_payload == {
        "name": "abrolia_data",
        "region": "ams",
        "size_gb": 1,
        "encrypted": True,
        "auto_backup_enabled": True,
        "snapshot_retention": 5,
    }
    machine_payload = next(
        body
        for method, path, body in fly.calls
        if method == "POST" and path.endswith("/machines")
    )
    config = machine_payload["config"]
    assert machine_payload["region"] == "ams"
    assert config["image"] == IMAGE
    assert config["mounts"] == [{"volume": "vol-synthetic", "path": "/data"}]
    assert config["guest"] == {"cpu_kind": "shared", "cpus": 1, "memory_mb": 512}
    assert config["env"] == {
        "HERMES_HOUSEHOLD_ID": spec.household_id,
        "HERMES_CONFIG_REVISION": str(spec.config_revision),
        "HERMES_CONFIG_SHA256": spec.config_sha256,
        "HERMES_CONTROL_PLANE_URL": "https://app.example.test",
        "HERMES_RUNTIME_REF": first.external_ref,
        "HERMES_DB": "/data/hermes.db",
    }
    encoded = json.dumps(machine_payload, sort_keys=True)
    assert "fly-api-secret-canary" not in encoded
    assert spec.email.fallback not in encoded
    assert "ABROLIA_" not in encoded


def test_secret_namespace_ensures_only_app_before_runtime_prepare() -> None:
    fly = StatefulFly()
    provisioner = _provisioner(fly)
    spec = _spec()

    first = provisioner.ensure_secret_namespace(spec.household_id, "namespace-one")
    second = provisioner.ensure_secret_namespace(spec.household_id, "namespace-two")

    assert first == second
    assert first.public_result["stage"] == "secret_namespace_ready"
    assert [call[:2] for call in fly.calls if call[0] == "POST"] == [
        ("POST", "/v1/apps")
    ]
    assert not any(path.endswith("/volumes") for _method, path, _body in fly.calls)
    assert not any(path.endswith("/machines") for _method, path, _body in fly.calls)

    provisioner.prepare({"manifest": spec.model_dump(mode="json")}, "runtime-one")
    assert len(
        [call for call in fly.calls if call[0] == "POST" and call[1] == "/v1/apps"]
    ) == 1
    assert any(path.endswith("/volumes") for _method, path, _body in fly.calls)
    assert not any(path.endswith("/machines") for _method, path, _body in fly.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", None),
        ("region", "iad"),
        ("encrypted", None),
        ("encrypted", False),
    ],
)
def test_existing_volume_immutable_policy_is_fail_closed(
    field: str,
    value: object,
) -> None:
    fly = StatefulFly()
    fly.app = {"name": FlyRuntimeProvisioner.stable_app_name(_spec().household_id)}
    fly.volume = {
        "id": "vol-synthetic",
        "name": "abrolia_data",
        "region": "ams",
        "encrypted": True,
        "auto_backup_enabled": True,
        "snapshot_retention": 5,
    }
    if value is None:
        fly.volume.pop(field)
    else:
        fly.volume[field] = value

    with pytest.raises(ProviderRejected, match="volume"):
        _provisioner(fly).prepare(
            {"manifest": _spec().model_dump(mode="json")},
            "immutable-drift",
        )
    assert not any(method == "PUT" for method, _path, _body in fly.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auto_backup_enabled", None),
        ("auto_backup_enabled", False),
        ("snapshot_retention", None),
        ("snapshot_retention", 3),
    ],
)
def test_existing_volume_backup_drift_converges_then_reinspects(
    field: str,
    value: object,
) -> None:
    fly = StatefulFly()
    fly.app = {"name": FlyRuntimeProvisioner.stable_app_name(_spec().household_id)}
    fly.volume = {
        "id": "vol-synthetic",
        "name": "abrolia_data",
        "region": "ams",
        "encrypted": True,
        "auto_backup_enabled": True,
        "snapshot_retention": 5,
    }
    if value is None:
        fly.volume.pop(field)
    else:
        fly.volume[field] = value

    prepared = _provisioner(fly).prepare(
        {"manifest": _spec().model_dump(mode="json")},
        "mutable-drift",
    )

    assert prepared.public_result["volume_ref"] == "vol-synthetic"
    assert fly.volume["auto_backup_enabled"] is True
    assert fly.volume["snapshot_retention"] == 5
    assert len([call for call in fly.calls if call[0] == "PUT"]) == 1
    assert len([call for call in fly.calls if call[1].endswith("/volumes")]) >= 2


def test_existing_volume_incomplete_convergence_is_a_conflict() -> None:
    fly = StatefulFly(ignore_volume_update=True)
    fly.app = {"name": FlyRuntimeProvisioner.stable_app_name(_spec().household_id)}
    fly.volume = {
        "id": "vol-synthetic",
        "name": "abrolia_data",
        "region": "ams",
        "encrypted": True,
        "auto_backup_enabled": False,
        "snapshot_retention": 2,
    }

    with pytest.raises(ProviderRejected, match="did not converge"):
        _provisioner(fly).prepare(
            {"manifest": _spec().model_dump(mode="json")},
            "incomplete-convergence",
        )


def test_inspect_fails_closed_on_existing_volume_policy_drift() -> None:
    fly = StatefulFly()
    provisioner = _provisioner(fly)
    intent = {"manifest": _spec().model_dump(mode="json")}
    runtime = provisioner.ensure(intent, "intent")
    assert fly.volume is not None
    fly.volume.pop("auto_backup_enabled")

    inspected = provisioner.inspect(runtime.public_result)

    assert inspected.state is InspectState.FAILED
    assert inspected.error_code == "fly_volume_backup_policy_mismatch"


def test_inspect_accepts_job_intent_or_exact_ref_and_rejects_metadata_drift() -> None:
    spec = _spec()
    intent = {"manifest": spec.model_dump(mode="json")}
    fly = StatefulFly()
    provisioner = _provisioner(fly)
    runtime = provisioner.ensure(intent, "intent")

    assert provisioner.inspect(intent).state is InspectState.READY
    exact = provisioner.inspect(runtime.public_result)
    assert exact.state is InspectState.READY
    assert exact.result is not None
    assert exact.result.public_result["machine_ref"] == "machine-synthetic"

    assert fly.machine is not None
    assert fly.machine["config"]["metadata"]["fly_platform_version"] == "v2"
    assert fly.machine["config"]["metadata"]["fly_process_group"] == "app"
    fly.machine["config"]["metadata"].update(
        {
            "fly_release_id": "rel_synthetic",
            "fly_release_version": "5",
        }
    )
    assert provisioner.inspect(intent).state is InspectState.READY
    fly.machine["config"]["mounts"][0].update(
        {
            "encrypted": True,
            "size_gb": 1,
            "name": "abrolia_data",
        }
    )
    assert provisioner.inspect(intent).state is InspectState.READY

    original_bootstrap_url = fly.machine["config"]["env"]["HERMES_CONTROL_PLANE_URL"]
    fly.machine["config"]["env"]["HERMES_CONTROL_PLANE_URL"] = (
        "https://stale.example.test"
    )
    mutable_drift = provisioner.inspect(intent)
    assert mutable_drift.state is InspectState.PENDING
    assert mutable_drift.error_code == "fly_machine_config_drift"
    fly.machine["config"]["env"]["HERMES_CONTROL_PLANE_URL"] = original_bootstrap_url

    fly.machine["config"]["metadata"]["abrolia_revision"] = "999"
    drifted = provisioner.inspect(intent)
    assert drifted.state is InspectState.FAILED
    assert drifted.error_code == "fly_machine_metadata_mismatch"


def test_deprovision_waits_for_post_delete_404_and_uses_exact_ids() -> None:
    spec = _spec()
    fly = StatefulFly(machine_delete_lag=1, app_delete_lag=1)
    provisioner = _provisioner(fly)
    runtime = provisioner.ensure(
        {"manifest": spec.model_dump(mode="json")}, "intent"
    )

    first = provisioner.deprovision(runtime.public_result)
    assert first.state is InspectState.PENDING
    assert first.error_code == "fly_delete_pending"
    assert (
        "DELETE",
        f"/v1/apps/{runtime.external_ref}/machines/machine-synthetic",
    ) in [(method, path) for method, path, _body in fly.calls]
    assert not any(
        method == "DELETE" and "/volumes/" in path
        for method, path, _body in fly.calls
    )

    second = provisioner.deprovision(runtime.public_result)
    assert second.state is InspectState.ABSENT
    assert (
        "DELETE",
        f"/v1/apps/{runtime.external_ref}/volumes/vol-synthetic",
    ) in [(method, path) for method, path, _body in fly.calls]
    assert ("DELETE", f"/v1/apps/{runtime.external_ref}") in [
        (method, path) for method, path, _body in fly.calls
    ]
    assert (
        "DELETE",
        f"/v1/apps/{runtime.external_ref}/machines/machine-synthetic",
        {"force": "true"},
    ) in fly.query_params
    assert (
        "DELETE",
        f"/v1/apps/{runtime.external_ref}",
        {},
    ) in fly.query_params

    # The pinned flyctl fallback handles staged-secret apps, but success is
    # still established only by the authoritative GET observing 404.
    third = provisioner.deprovision(runtime.public_result)
    assert third.state is InspectState.ABSENT


def test_deprovision_flyctl_fallback_is_shell_free_and_keeps_token_out_of_argv(
) -> None:
    spec = _spec()
    fly = StatefulFly(app_delete_lag=100)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    provisioner = FlyRuntimeProvisioner(
        api_token=_synthetic_macaroon(),
        org_slug="synthetic-org",
        image_digest=IMAGE,
        bootstrap_url="https://app.example.test",
        client=httpx.Client(transport=httpx.MockTransport(fly)),
        runner=runner,
    )
    runtime = provisioner.ensure(
        {"manifest": spec.model_dump(mode="json")}, "intent"
    )

    pending = provisioner.deprovision(runtime.public_result)

    assert pending.state is InspectState.PENDING
    command, kwargs = calls[-1]
    assert command == ["fly", "apps", "destroy", runtime.external_ref, "--yes"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 30.0
    assert _synthetic_macaroon() not in command
    assert kwargs["env"]["FLY_ACCESS_TOKEN"] == _synthetic_macaroon()

    polled = provisioner.deprovision(runtime.public_result)
    assert polled.state is InspectState.PENDING
    assert len(calls) == 1


def test_deprovision_flyctl_timeout_is_an_unknown_outcome() -> None:
    spec = _spec()
    fly = StatefulFly(app_delete_lag=100)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    provisioner = FlyRuntimeProvisioner(
        api_token=_synthetic_macaroon(),
        org_slug="synthetic-org",
        image_digest=IMAGE,
        bootstrap_url="https://app.example.test",
        client=httpx.Client(transport=httpx.MockTransport(fly)),
        runner=runner,
    )
    runtime = provisioner.ensure(
        {"manifest": spec.model_dump(mode="json")}, "intent"
    )

    result = provisioner.deprovision(runtime.public_result)

    assert result.state is InspectState.UNKNOWN
    assert result.error_code == "fly_delete_unknown"
    assert calls[-1][1]["timeout"] == 30.0


def test_runtime_deprovision_preserves_app_secret_namespace() -> None:
    spec = _spec()
    fly = StatefulFly()
    provisioner = _provisioner(fly)
    runtime = provisioner.ensure(
        {"manifest": spec.model_dump(mode="json")}, "intent"
    )

    result = provisioner.deprovision_runtime(runtime.public_result)

    assert result.state is InspectState.ABSENT
    assert fly.app == {"name": runtime.external_ref}
    assert fly.machine is None
    assert fly.volume is None
    assert ("DELETE", f"/v1/apps/{runtime.external_ref}") not in [
        (method, path) for method, path, _body in fly.calls
    ]


def test_runtime_deprovision_ignores_historical_destroyed_machine() -> None:
    app_ref = FlyRuntimeProvisioner.stable_app_name(_spec().household_id)
    volume_present = True
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal volume_present
        seen.append(request)
        path = request.url.path
        if path.endswith("/machines"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "machine-synthetic",
                        "name": "abrolia-runtime",
                        "state": "destroyed",
                    }
                ],
                request=request,
            )
        if path.endswith("/volumes"):
            payload = (
                [
                    {
                        "id": "vol-synthetic",
                        "name": "abrolia_data",
                        "region": "ams",
                        "encrypted": True,
                    }
                ]
                if volume_present
                else []
            )
            return httpx.Response(200, json=payload, request=request)
        if path.endswith("/volumes/vol-synthetic") and request.method == "DELETE":
            volume_present = False
            return httpx.Response(204, request=request)
        if path == f"/v1/apps/{app_ref}":
            return httpx.Response(200, json={"name": app_ref}, request=request)
        raise AssertionError(f"unexpected Fly call: {request.method} {path}")

    result = _provisioner(handler).deprovision_runtime(
        {
            "app_ref": app_ref,
            "machine_ref": "machine-synthetic",
            "volume_ref": "vol-synthetic",
        }
    )

    assert result.state is InspectState.ABSENT
    assert not any(
        request.method == "DELETE" and "/machines/" in request.url.path
        for request in seen
    )
    assert any(
        request.method == "DELETE" and request.url.path.endswith("/volumes/vol-synthetic")
        for request in seen
    )


def test_inspect_and_deprovision_do_not_claim_unknown_cleanup_complete() -> None:
    spec = _spec()
    fly = StatefulFly()
    provisioner = _provisioner(fly)
    runtime = provisioner.ensure({"manifest": spec.model_dump(mode="json")}, "intent")
    inspected = provisioner.inspect(runtime.external_ref)
    assert inspected.state is InspectState.READY

    def deletion_unknown(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/machines" not in request.url.path:
            return httpx.Response(200, json={"name": runtime.external_ref}, request=request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"id": "machine-synthetic", "name": "abrolia-runtime"}],
                request=request,
            )
        return httpx.Response(503, json={}, request=request)

    result = _provisioner(deletion_unknown).deprovision(runtime.external_ref)
    assert result.state is InspectState.UNKNOWN
    assert result.error_code == "fly_delete_unknown"


@pytest.mark.parametrize(
    ("exit_event", "expected"),
    [
        (
            {"exit_code": 0, "oom_killed": False, "requested_stop": False},
            InspectState.ABSENT,
        ),
        (
            {"exit_code": 1, "oom_killed": False, "requested_stop": False},
            InspectState.FAILED,
        ),
    ],
)
def test_ephemeral_google_revoker_proves_exit_before_cleanup(
    exit_event: dict[str, Any], expected: InspectState
) -> None:
    transaction_id = "a" * 32
    app = FlyRuntimeProvisioner.stable_app_name(_spec().household_id)
    machine: dict[str, Any] | None = None
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal machine
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path.endswith("/machines") and request.method == "GET":
            return httpx.Response(200, json=[] if machine is None else [machine], request=request)
        if path.endswith("/machines") and request.method == "POST":
            machine = {
                "id": "revoker-machine-id",
                "instance_id": "01JREVOCATION",
                "state": "started",
                **body,
            }
            return httpx.Response(201, json=machine, request=request)
        if path.endswith("/wait"):
            assert dict(request.url.params) == {
                "state": "stopped",
                "instance_id": "01JREVOCATION",
                "timeout": "30",
            }
            return httpx.Response(200, json={"ok": True}, request=request)
        if path.endswith("/revoker-machine-id") and request.method == "GET":
            assert machine is not None
            observed = {
                **machine,
                "state": "stopped",
                "events": [{"type": "exit", "request": {"exit_event": exit_event}}],
            }
            return httpx.Response(200, json=observed, request=request)
        if path.endswith("/revoker-machine-id") and request.method == "DELETE":
            assert dict(request.url.params) == {"force": "true"}
            machine = None
            return httpx.Response(204, request=request)
        raise AssertionError(f"unexpected Fly call: {request.method} {request.url}")

    state = _provisioner(handler).revoke_google_secret(app, transaction_id)

    assert state is expected
    payload = next(
        body
        for method, path, body in calls
        if method == "POST" and path.endswith("/machines")
    )
    assert payload == {
        "name": f"abrolia-google-revoker-{transaction_id[:12]}",
        "region": "ams",
        "skip_service_registration": True,
        "config": {
            "image": IMAGE,
            "init": {"exec": ["hermes-cloud", "revoke-google-grant"]},
            "auto_destroy": False,
            "metadata": {
                "abrolia_managed": "true",
                "abrolia_google_revoke": transaction_id,
                "fly_platform_version": "v2",
                "fly_process_group": "google-revoker",
            },
            "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 256},
            "restart": {"policy": "no"},
        },
    }
    encoded = json.dumps(payload)
    assert "ABROLIA_GMAIL_OAUTH_GRANT" not in encoded
    assert "refresh" not in encoded
    assert "mounts" not in encoded
    deleted = any(method == "DELETE" for method, _path, _body in calls)
    assert deleted is (expected is InspectState.ABSENT)


def test_ephemeral_google_revoker_wait_timeout_is_unknown() -> None:
    transaction_id = "b" * 32
    app = FlyRuntimeProvisioner.stable_app_name(_spec().household_id)
    desired = _provisioner(lambda request: httpx.Response(500, request=request))
    machine = {
        "id": "revoker-machine-id",
        "instance_id": "01JREVOCATION",
        "state": "started",
        **desired._google_revoker_payload(transaction_id),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/machines"):
            return httpx.Response(200, json=[machine], request=request)
        if request.url.path.endswith("/wait"):
            return httpx.Response(408, json={}, request=request)
        raise AssertionError(f"unexpected Fly call: {request.method} {request.url}")

    assert (
        _provisioner(handler).revoke_google_secret(app, transaction_id)
        is InspectState.UNKNOWN
    )


def test_fly_secret_sink_uses_stdin_only_and_redacts_failures() -> None:
    calls: list[dict[str, Any]] = []

    def success(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, b"", b"")

    secret = "runtime-secret-canary"
    material = SecretMaterial.from_mapping({"RUNTIME_API_KEY": secret})
    FlySecretSink(runner=success).install("synthetic-app", material)
    call = calls[0]
    assert call["shell"] is False
    assert secret.encode() in call["input"]
    assert secret not in " ".join(call["command"])
    assert material.is_empty

    def failure(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, b"", f"provider echoed {secret}".encode()
        )

    with pytest.raises(SecretInstallError) as raised:
        FlySecretSink(runner=failure).install(
            "synthetic-app", SecretMaterial.from_mapping({"RUNTIME_API_KEY": secret})
        )
    assert secret not in str(raised.value)
