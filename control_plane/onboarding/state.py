"""Complete transition table for commands accepted by the onboarding API."""

from __future__ import annotations

from control_plane.models import StepKind, StepStatus

SAVE_PROFILE = "save_profile"
SELECT = "select"
RETRY = "retry"
CHECK = "check"
VERIFY_RESULT = "verify_result"
RESET = "reset"
CANCEL = "cancel"

# State changes are data, not scattered HTTP handler conditionals. Worker-only
# transitions use VERIFY_RESULT; cancel is workflow-wide and checked separately.
TRANSITIONS: dict[tuple[StepKind, StepStatus, str], StepStatus] = {
    (StepKind.PROFILE, StepStatus.AVAILABLE, SAVE_PROFILE): StepStatus.VERIFIED,
    (StepKind.EMAIL, StepStatus.AVAILABLE, SELECT): StepStatus.PROVISIONING,
    (StepKind.EMAIL, StepStatus.FAILED, RETRY): StepStatus.PROVISIONING,
    (StepKind.EMAIL, StepStatus.WAITING_USER, CHECK): StepStatus.VERIFYING,
    (StepKind.EMAIL, StepStatus.PROVISIONING, VERIFY_RESULT): StepStatus.VERIFIED,
    (StepKind.EMAIL, StepStatus.WAITING_USER, VERIFY_RESULT): StepStatus.VERIFIED,
    (StepKind.EMAIL, StepStatus.VERIFYING, VERIFY_RESULT): StepStatus.VERIFIED,
    (StepKind.EMAIL, StepStatus.VERIFIED, RESET): StepStatus.AVAILABLE,
    (StepKind.WHATSAPP, StepStatus.AVAILABLE, SELECT): StepStatus.PROVISIONING,
    (StepKind.WHATSAPP, StepStatus.FAILED, RETRY): StepStatus.PROVISIONING,
    (StepKind.WHATSAPP, StepStatus.WAITING_USER, CHECK): StepStatus.VERIFYING,
    (StepKind.WHATSAPP, StepStatus.PROVISIONING, VERIFY_RESULT): StepStatus.VERIFIED,
    (StepKind.WHATSAPP, StepStatus.WAITING_USER, VERIFY_RESULT): StepStatus.VERIFIED,
    (StepKind.WHATSAPP, StepStatus.VERIFYING, VERIFY_RESULT): StepStatus.VERIFIED,
    (StepKind.WHATSAPP, StepStatus.VERIFIED, RESET): StepStatus.AVAILABLE,
    (StepKind.PRIMARY_CHANNEL, StepStatus.AVAILABLE, SELECT): StepStatus.PROVISIONING,
    (StepKind.PRIMARY_CHANNEL, StepStatus.FAILED, RETRY): StepStatus.PROVISIONING,
    (
        StepKind.PRIMARY_CHANNEL,
        StepStatus.WAITING_USER,
        CHECK,
    ): StepStatus.VERIFYING,
    (
        StepKind.PRIMARY_CHANNEL,
        StepStatus.PROVISIONING,
        VERIFY_RESULT,
    ): StepStatus.VERIFIED,
    (
        StepKind.PRIMARY_CHANNEL,
        StepStatus.WAITING_USER,
        VERIFY_RESULT,
    ): StepStatus.VERIFIED,
    (
        StepKind.PRIMARY_CHANNEL,
        StepStatus.VERIFYING,
        VERIFY_RESULT,
    ): StepStatus.VERIFIED,
    (StepKind.PRIMARY_CHANNEL, StepStatus.VERIFIED, RESET): StepStatus.AVAILABLE,
}


def next_status(kind: StepKind, current: StepStatus, command: str) -> StepStatus:
    try:
        return TRANSITIONS[(kind, current, command)]
    except KeyError as error:
        from control_plane.onboarding.contracts import InvalidTransition

        raise InvalidTransition(f"{command} is invalid for {kind.value}:{current.value}") from error
