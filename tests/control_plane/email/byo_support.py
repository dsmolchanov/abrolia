"""Shared fake Nerve admin and BYO selection helper for the C2 test modules.

The Phase C plan runs test_byo_reload_resume.py, test_byo_dns_advance.py and
test_byo_domain_race.py as separate commands, so the fakes they share live
here rather than in any one of them.
"""

from __future__ import annotations

from control_plane.models import StepKind
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.provisioning.contracts import OutcomeUnknown

ORG_ID = "10000000-0000-4000-8000-000000000001"
DOMAIN_ID = "10000000-0000-4000-8000-000000000002"
INBOX_ID = "10000000-0000-4000-8000-000000000003"
KEY_ID = "10000000-0000-4000-8000-000000000004"
WEBHOOK_ID = "10000000-0000-4000-8000-000000000005"
BASE_TIME = 1_800_000_000.0
_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)


class FakeByoNerveAdmin:
    def __init__(self) -> None:
        self.active = False
        self.checks = {"ownership": False, "mx": False, "spf": False, "dkim": False}
        self.deleted: list[str] = []
        self.inbox_calls = 0
        self.fail_domain_delete_once = False
        self.org_external_refs: list[str] = []
        self.dns_records = [{
            "type": "TXT",
            "host": "_nerve.family.example.test",
            "value": "synthetic-domain-proof",
            "purpose": (
                "DMARC policy. Required by Gmail and other major providers for reliable "
                "inbox delivery. Start with p=none, then tighten to p=quarantine or "
                "p=reject once delivery is confirmed."
            ),
            "required": True,
        }]

    def ensure_org(self, *, household_id, identity_id):
        self.org_external_refs.append(
            f"arbolia:household:{household_id}:email:{identity_id}"
        )
        return {"org_id": ORG_ID}

    def get_org(self, *, external_ref):
        self.org_external_refs.append(external_ref)
        return {} if f"/v1/orgs/{ORG_ID}" in self.deleted else {"org_id": ORG_ID}

    def ensure_domain(self, *, org_id, domain, external_ref):
        return {"domain": {
            "id": DOMAIN_ID,
            "domain": domain,
            "external_ref": external_ref,
            "status": "active" if self.active else "pending_dns",
        }}

    def domain_dns(self, *, org_id, domain_id):
        return {"domain_id": domain_id, "dns_records": self.dns_records}

    def verify_domain(self, *, org_id, domain_id):
        return {
            "domain": {"id": domain_id, "status": "active" if self.active else "pending_dns"},
            "checks": self.checks,
        }

    def ensure_inbox(self, *, org_id, address, external_ref, domain_id=None):
        self.inbox_calls += 1
        return {"inbox": {
            "id": INBOX_ID,
            "address": address,
            "external_ref": external_ref,
            "org_domain_id": domain_id,
        }}

    def issue_key(self, *, org_id, external_ref):
        return {
            "id": KEY_ID,
            "key": "synthetic-byo-key",
            "secret_available": True,
            "external_ref": external_ref,
        }

    def ensure_webhook(self, *, org_id, url, external_ref):
        return {
            "id": WEBHOOK_ID,
            "secret": "synthetic-byo-signing-key",
            "secret_available": True,
            "external_ref": external_ref,
        }

    def rotate_webhook(self, *, org_id, webhook_id):
        return {"id": webhook_id, "secret": "synthetic-byo-signing-key"}

    def delete(self, path, *, org_id=None):
        del org_id
        if self.fail_domain_delete_once and path == f"/v1/domains/{DOMAIN_ID}":
            self.fail_domain_delete_once = False
            raise OutcomeUnknown("synthetic provider unavailable")
        self.deleted.append(path)


class LostDomainResponseNerveAdmin(FakeByoNerveAdmin):
    def __init__(self) -> None:
        super().__init__()
        self.domain_calls = 0

    def ensure_domain(self, *, org_id, domain, external_ref):
        self.domain_calls += 1
        if self.domain_calls == 1:
            raise OutcomeUnknown("synthetic lost domain response")
        return super().ensure_domain(
            org_id=org_id, domain=domain, external_ref=external_ref
        )


class LostCommittedStepResponseNerveAdmin(FakeByoNerveAdmin):
    def __init__(self, lost_step: str) -> None:
        super().__init__()
        self.lost_step = lost_step
        self.step_calls: dict[str, int] = {}

    def _commit_then_maybe_lose(self, step: str, result):
        self.step_calls[step] = self.step_calls.get(step, 0) + 1
        if step == self.lost_step and self.step_calls[step] == 1:
            raise OutcomeUnknown(f"synthetic lost {step} response")
        return result

    def ensure_org(self, *, household_id, identity_id):
        return self._commit_then_maybe_lose(
            "org",
            super().ensure_org(household_id=household_id, identity_id=identity_id),
        )

    def ensure_domain(self, *, org_id, domain, external_ref):
        return self._commit_then_maybe_lose(
            "domain",
            super().ensure_domain(
                org_id=org_id, domain=domain, external_ref=external_ref
            ),
        )

    def ensure_inbox(self, *, org_id, address, external_ref, domain_id=None):
        return self._commit_then_maybe_lose(
            "inbox",
            super().ensure_inbox(
                org_id=org_id,
                address=address,
                external_ref=external_ref,
                domain_id=domain_id,
            ),
        )

    def issue_key(self, *, org_id, external_ref):
        return self._commit_then_maybe_lose(
            "key", super().issue_key(org_id=org_id, external_ref=external_ref)
        )

    def ensure_webhook(self, *, org_id, url, external_ref):
        return self._commit_then_maybe_lose(
            "webhook",
            super().ensure_webhook(
                org_id=org_id, url=url, external_ref=external_ref
            ),
        )


class WrongMailboxNerveAdmin(FakeByoNerveAdmin):
    def ensure_inbox(self, *, org_id, address, external_ref, domain_id=None):
        envelope = super().ensure_inbox(
            org_id=org_id,
            address=address,
            external_ref=external_ref,
            domain_id=domain_id,
        )
        envelope["inbox"]["address"] = "assistant@other.example.test"
        return envelope


class LostVerifyResponseNerveAdmin(FakeByoNerveAdmin):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_verify = True

    def verify_domain(self, *, org_id, domain_id):
        if self.lose_next_verify:
            self.lose_next_verify = False
            raise OutcomeUnknown("synthetic lost verification response")
        return super().verify_domain(org_id=org_id, domain_id=domain_id)


class HardDeleteGrantNerveAdmin(FakeByoNerveAdmin):
    def __init__(self) -> None:
        super().__init__()
        self.grant_generation = 0
        self.current_grant_id = ""

    def ensure_grant(self, *, org_id, external_ref):
        del org_id, external_ref
        if not self.current_grant_id:
            self.grant_generation += 1
            self.current_grant_id = f"synthetic-grant-{self.grant_generation}"
        return {"id": self.current_grant_id}

    def issue_key(self, *, org_id, external_ref):
        result = super().issue_key(org_id=org_id, external_ref=external_ref)
        result["secret_available"] = True
        return result

    def ensure_webhook(self, *, org_id, url, external_ref):
        result = super().ensure_webhook(
            org_id=org_id, url=url, external_ref=external_ref
        )
        result["secret_available"] = True
        return result

    def attachment_feature_enabled(self, *, api_key, expected_org_id):
        del api_key, expected_org_id
        return True

    def list_keys(self, *, org_id):
        del org_id
        return []

    def delete(self, path, *, org_id=None):
        super().delete(path, org_id=org_id)
        if path == f"/v1/domain-grants/{self.current_grant_id}":
            self.current_grant_id = ""


def select_byo_domain(cp_stack) -> None:
    cp_stack.service.byo_domain_provider = "nerve-byo-domain"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "family_domain",
            "domain": "family.example.test",
            "local_part": "assistant",
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000022",
            "special_category_restriction_text_version": _RESTRICTION_VERSION,
            "special_category_restriction_text_sha256": _RESTRICTION_SHA,
        },
        context=cp_stack.context(),
    )
