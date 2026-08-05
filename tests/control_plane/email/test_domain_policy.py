from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from control_plane.email.domain_policy import canonicalize_domain, domain_guidance
from control_plane.models import EmailSelection


def test_domain_policy_canonicalizes_idna_and_recommends_subdomain_for_apex() -> None:
    assert canonicalize_domain("BÜCHER.Example.COM.") == "xn--bcher-kva.example.com"
    guidance = domain_guidance("example.com", allow_test=False)
    assert guidance.apex_mx_risk
    assert guidance.recommended_domain == "assistant.example.com"
    assert not domain_guidance("mail.example.com", allow_test=False).apex_mx_risk


@pytest.mark.parametrize(
    "domain",
    [
        "co.uk",
        "localhost",
        "127.0.0.1",
        "abrolia.com",
        "mail.abrolia.com",
        "family.example.test",
        "bad_label.example.com",
        "пример.рф",
    ],
)
def test_domain_policy_rejects_suffixes_and_controlled_or_local_domains(domain) -> None:
    with pytest.raises(ValueError):
        canonicalize_domain(domain, allow_test=False)


def test_real_apex_requires_explicit_mx_ack_but_rollout_gate_is_separate() -> None:
    adapter = TypeAdapter(EmailSelection)
    body = {
        "kind": "family_domain",
        "domain": "example.com",
        "local_part": "assistant",
    }
    with pytest.raises(ValidationError):
        adapter.validate_python(body, context={"allow_real_email_domains": True})
    accepted = adapter.validate_python(
        {**body, "mx_change_acknowledged": True},
        context={"allow_real_email_domains": True},
    )
    assert accepted.domain == "example.com"
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "kind": "family_domain",
                "domain": "family.example.test",
                "local_part": "assistant",
            },
            context={"allow_real_email_domains": True},
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {**body, "mx_change_acknowledged": True},
            context={"allow_real_email_domains": False},
        )
