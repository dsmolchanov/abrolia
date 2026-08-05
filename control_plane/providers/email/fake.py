from __future__ import annotations

from control_plane.crypto import SecretMaterial
from control_plane.email.models import EmailOption, EmailProvisionIntent
from control_plane.provisioning.contracts import ProvisionResult
from control_plane.provisioning.fakes import DeterministicFakeProvisioner


class FakeEmailIdentityProvisioner(DeterministicFakeProvisioner):
    """Synthetic provider exercising the one-time credential handoff contract."""

    def __init__(self, *, issue_secret: bool = False, behavior: str = "success") -> None:
        super().__init__("email", behavior=behavior)
        self.issue_secret = issue_secret

    def _result(self, intent: dict, key: str) -> ProvisionResult:
        typed = EmailProvisionIntent.model_validate(intent)
        selection = typed.selection
        if typed.option is EmailOption.MANAGED_ABROLIA:
            address = f"{selection['local_part']}@abrolia.com"
        elif typed.option is EmailOption.GMAIL:
            address = "synthetic-agent@gmail.test"
        else:
            address = f"agent@{selection['domain']}"
        material = (
            SecretMaterial.from_mapping({"ABROLIA_EMAIL_PROVIDER_KEY": "synthetic-key"})
            if self.issue_secret
            else SecretMaterial()
        )
        return ProvisionResult(
            external_ref=f"synthetic-email:{typed.identity_id}",
            public_result={
                "agent_inbox": address,
                "provider": "synthetic",
                "provider_refs": {"identity_id": typed.identity_id},
                "secret_binding_ref": "ABROLIA_EMAIL_PROVIDER_KEY" if self.issue_secret else None,
                "granted_scopes": [],
                "masked_external_ref": typed.identity_id[-8:],
            },
            secret_material=material,
        )
