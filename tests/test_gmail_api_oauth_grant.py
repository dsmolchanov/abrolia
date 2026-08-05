import json
from pathlib import Path

import httpx
import pytest

from hermes_cloud.cli import main
from hermes_cloud.core.db import open_database
from hermes_cloud.email.google_grant import (
    GoogleGrantError,
    GoogleGrantStore,
    RefreshedAccess,
)


class Refresher:
    def refresh(self, refresh_credential):
        assert refresh_credential == "refresh-one"
        return RefreshedAccess("memory-only-access", 200.0, "refresh-two")


def test_grant_is_encrypted_and_rotates_atomically(tmp_path: Path) -> None:
    database = open_database(tmp_path / "runtime.db")
    store = GoogleGrantStore(database, {1: b"k" * 32}, active_version=1, clock=lambda: 10.0)
    store.put(
        identity_id="identity-1",
        revision=1,
        refresh_credential="refresh-one",
        provider_subject="subject-1",
        scopes=("gmail.readonly", "gmail.send"),
    )
    assert all(
        b"refresh-one" not in path.read_bytes()
        for path in tmp_path.glob("runtime.db*")
        if path.is_file()
    )
    access = store.access_token("identity-1", 1, Refresher())
    assert access.access_token == "memory-only-access"
    assert store.load("identity-1", 1).refresh_credential == "refresh-two"
    assert all(
        b"refresh-two" not in path.read_bytes()
        for path in tmp_path.glob("runtime.db*")
        if path.is_file()
    )

    store.revoke("identity-1", 1)
    with pytest.raises(GoogleGrantError):
        store.load("identity-1", 1)


def test_ciphertext_is_bound_to_identity_and_revision(tmp_path: Path) -> None:
    database = open_database(tmp_path / "runtime.db")
    store = GoogleGrantStore(database, {1: b"k" * 32}, active_version=1)
    store.put(
        identity_id="identity-1",
        revision=1,
        refresh_credential="refresh-one",
        provider_subject="subject-1",
        scopes=("gmail.send",),
    )
    with database.write() as connection:
        connection.execute(
            "UPDATE oauth_grants SET binding_identity_id = 'identity-2'"
            " WHERE binding_identity_id = 'identity-1'"
        )
    with pytest.raises(GoogleGrantError):
        store.load("identity-2", 1)


@pytest.mark.parametrize(("status", "expected"), [(200, 0), (400, 0), (500, 4)])
def test_revoke_google_grant_cli_is_redacted_and_idempotent(
    monkeypatch, capsys, status: int, expected: int
) -> None:
    secret = "refresh-secret-canary"
    seen: list[str] = []

    def post(url, *, params, timeout):
        assert url == "https://oauth2.googleapis.com/revoke"
        assert timeout == 20.0
        seen.append(params["token"])
        return httpx.Response(status)

    monkeypatch.setenv(
        "ABROLIA_GMAIL_OAUTH_GRANT",
        json.dumps({"refresh_credential": secret}),
    )
    monkeypatch.setattr(httpx, "post", post)

    assert main(["revoke-google-grant"]) == expected
    captured = capsys.readouterr()
    assert seen == [secret]
    assert secret not in captured.out
    assert secret not in captured.err


def test_revoke_google_grant_cli_rejects_bad_bundle_without_network(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("ABROLIA_GMAIL_OAUTH_GRANT", "not-json-secret-canary")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    assert main(["revoke-google-grant"]) == 2
    captured = capsys.readouterr()
    assert "not-json-secret-canary" not in captured.err
