"""Registration entry points for the native Codex driver."""

from ..service import CODEX_DESCRIPTOR, CodexDriver
from .base import AgentProfile
from .registry import AgentRegistration, DriverRegistration

CODEX_DRIVER_KIND = "codex-native"


def codex_driver_registration() -> DriverRegistration:
    return DriverRegistration(
        CODEX_DRIVER_KIND,
        CodexDriver.create,
        CodexDriver.check_availability,
    )


def codex_profile() -> AgentProfile:
    return AgentProfile(
        "codex",
        CODEX_DRIVER_KIND,
        "Codex",
        "codex",
        description=CODEX_DESCRIPTOR.description,
        built_in=True,
    )


def codex_registration() -> AgentRegistration:
    return AgentRegistration(
        CODEX_DESCRIPTOR,
        CodexDriver.create,
        CodexDriver.check_availability,
    )


__all__ = [
    "CODEX_DRIVER_KIND",
    "CodexDriver",
    "codex_driver_registration",
    "codex_profile",
    "codex_registration",
]
