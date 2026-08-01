from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Signal

from .base import AgentAvailability, AgentDescriptor, AgentDriver, AgentProfile

DriverFactory = Callable[[AgentProfile], AgentDriver]
DriverProbe = Callable[[AgentProfile], AgentAvailability]


@dataclass(slots=True, frozen=True)
class DriverRegistration:
    kind: str
    factory: DriverFactory
    probe: DriverProbe


@dataclass(slots=True, frozen=True)
class AgentRegistration:
    """Compatibility registration for one profile backed by one driver kind."""

    descriptor: AgentDescriptor
    factory: Callable[[str], AgentDriver]
    probe: Callable[[str | None], AgentAvailability]


class AgentRegistry(QObject):
    """Registry of driver implementations and configured agent profiles."""

    profilesChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._drivers: dict[str, DriverRegistration] = {}
        self._profiles: dict[str, AgentProfile] = {}
        self._registrations: dict[str, AgentRegistration] = {}

    def register_driver(self, registration: DriverRegistration) -> None:
        if not registration.kind or registration.kind in self._drivers:
            raise ValueError(f"Agent driver is already registered: {registration.kind!r}")
        self._drivers[registration.kind] = registration

    def add_profile(self, profile: AgentProfile) -> None:
        if not profile.id or profile.id in self._profiles:
            raise ValueError(f"Agent profile is already registered: {profile.id!r}")
        if profile.driver_kind not in self._drivers:
            raise ValueError(f"Unknown driver kind: {profile.driver_kind!r}")
        self._profiles[profile.id] = profile
        self.profilesChanged.emit()

    def replace_profile(self, profile: AgentProfile) -> None:
        if profile.id not in self._profiles:
            raise KeyError(profile.id)
        if profile.driver_kind not in self._drivers:
            raise ValueError(f"Unknown driver kind: {profile.driver_kind!r}")
        self._profiles[profile.id] = profile
        self.profilesChanged.emit()

    def remove_profile(self, profile_id: str) -> None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return
        if profile.built_in:
            raise ValueError("Встроенный профиль нельзя удалить")
        self._profiles.pop(profile_id, None)
        self.profilesChanged.emit()

    def register(self, registration: AgentRegistration) -> None:
        """Register the pre-profile API as a built-in profile."""
        agent_id = registration.descriptor.id
        if agent_id in self._registrations or agent_id in self._profiles:
            raise ValueError(f"Agent driver is already registered: {agent_id!r}")
        self._registrations[agent_id] = registration

        def factory(profile: AgentProfile) -> AgentDriver:
            return registration.factory(profile.executable)

        def probe(profile: AgentProfile) -> AgentAvailability:
            return registration.probe(profile.executable or None)

        self.register_driver(DriverRegistration(agent_id, factory, probe))
        self.add_profile(
            AgentProfile(
                id=agent_id,
                driver_kind=agent_id,
                display_name=registration.descriptor.display_name,
                executable=registration.descriptor.executable_name,
                description=registration.descriptor.description,
                built_in=True,
            )
        )

    def profiles(self) -> list[AgentProfile]:
        return list(self._profiles.values())

    def profile(self, profile_id: str) -> AgentProfile | None:
        return self._profiles.get(profile_id)

    def descriptors(self) -> list[AgentDescriptor]:
        return [
            AgentDescriptor(
                profile.id,
                profile.display_name,
                profile.executable,
                profile.description,
            )
            for profile in self._profiles.values()
        ]

    def registration(self, profile_id: str) -> AgentRegistration | None:
        return self._registrations.get(profile_id)

    def probe(self, profile_id: str, executable: str | None = None) -> AgentAvailability:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return AgentAvailability(False, error=f"Неизвестный агент: {profile_id}")
        if executable:
            profile = replace(profile, executable=executable)
        driver = self._drivers.get(profile.driver_kind)
        if driver is None:
            return AgentAvailability(False, error=f"Неизвестный драйвер: {profile.driver_kind}")
        return driver.probe(profile)

    def create(self, profile_id: str, executable: str | None = None) -> AgentDriver:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        if executable:
            profile = replace(profile, executable=executable)
        driver = self._drivers.get(profile.driver_kind)
        if driver is None:
            raise KeyError(profile.driver_kind)
        return driver.factory(profile)
