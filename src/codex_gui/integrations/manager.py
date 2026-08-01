from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..agents.base import AgentProfile
from ..agents.registry import AgentRegistry
from .models import (
    InstalledIntegration,
    IntegrationCandidate,
    IntegrationStatus,
    RuntimeSpec,
)
from .sources import AcpRegistrySource, GitHubReleaseSource, SourceCallback
from .store import IntegrationStore, IntegrationStoreError


class AgentIntegrationManager(QObject):
    integrationsChanged = Signal(object)
    operationFailed = Signal(str)

    def __init__(
        self,
        registry: AgentRegistry,
        settings: Any,
        store: IntegrationStore,
        github_source: GitHubReleaseSource,
        registry_source: AcpRegistrySource,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.registry = registry
        self.settings = settings
        self.store = store
        self.github_source = github_source
        self.registry_source = registry_source
        self._installed: dict[str, InstalledIntegration] = {}
        self.load_installed()

    @property
    def installed(self) -> list[InstalledIntegration]:
        return sorted(
            self._installed.values(),
            key=lambda item: item.manifest.name.casefold(),
        )

    def integration_for_profile(self, profile_id: str) -> InstalledIntegration | None:
        return self._installed.get(profile_id)

    def load_installed(self) -> None:
        self._installed.clear()
        for item in self.store.load_all():
            self._installed[item.profile_id] = item
            self._register_profile(item)
        self.integrationsChanged.emit(self.installed)

    def status(self, item: InstalledIntegration) -> IntegrationStatus:
        runtime = item.manifest.runtime_for()
        if runtime is None:
            return IntegrationStatus(
                "unsupported-platform",
                False,
                "Интеграция не поддерживает текущую платформу",
            )
        executable = self._main_executable(item, runtime)
        if not executable:
            return IntegrationStatus(
                "missing-cli",
                False,
                item.manifest.install_help.message
                or "Установите агент и укажите путь к его executable",
            )
        resolved = _resolve_executable(executable)
        if not resolved:
            return IntegrationStatus(
                "missing-cli",
                False,
                item.manifest.install_help.message
                or f"Исполняемый файл не найден: {executable}",
                executable,
            )
        for requirement in runtime.requirements:
            requirement_path = self._requirement_executable(item, requirement.id, requirement.commands)
            if not requirement_path:
                return IntegrationStatus(
                    f"missing-requirement:{requirement.id}",
                    False,
                    f"Не найдена зависимость {requirement.id}. Установите CLI или укажите путь.",
                    resolved,
                )
        return IntegrationStatus("ready", True, "Готов к запуску", resolved)

    def profile(self, item: InstalledIntegration) -> AgentProfile:
        runtime = item.manifest.runtime_for()
        if runtime is None:
            return AgentProfile(
                item.profile_id,
                "acp",
                item.manifest.name,
                "",
                description=item.manifest.description,
                unavailable_reason="Интеграция не поддерживает текущую платформу",
            )
        executable = self._main_executable(item, runtime)
        environment = dict(runtime.environment)
        for requirement in runtime.requirements:
            resolved = self._requirement_executable(item, requirement.id, requirement.commands)
            if resolved:
                environment[requirement.export_as] = resolved
        availability = self.status(item)
        return AgentProfile(
            item.profile_id,
            "acp",
            item.manifest.name,
            executable,
            runtime.arguments,
            f"{item.manifest.description} · {self.source_title(item)} · {item.manifest.version}",
            False,
            tuple(environment.items()),
            "" if availability.available else availability.message,
        )

    def install(self, candidate: IntegrationCandidate, callback: SourceCallback) -> None:
        existing = next(
            (
                item
                for item in self._installed.values()
                if item.source_kind == candidate.source_kind
                and item.source_ref.casefold() == candidate.source_ref.casefold()
                and item.manifest.id == candidate.manifest.id
            ),
            None,
        )
        override = existing.executable_override if existing is not None else ""

        def commit(artifact_data: bytes | None) -> None:
            try:
                item = self.store.install(candidate, artifact_data, override)
                self._installed[item.profile_id] = item
                self._register_profile(item)
            except (OSError, IntegrationStoreError, ValueError) as exc:
                self.operationFailed.emit(str(exc))
                callback(None, str(exc))
                return
            self.integrationsChanged.emit(self.installed)
            callback(item, "")

        runtime = candidate.manifest.runtime_for()
        if runtime is None:
            callback(None, "Интеграция не поддерживает текущую платформу")
            return
        if runtime.artifact is None:
            commit(None)
            return

        def artifact_loaded(value: Any | None, error: str) -> None:
            if error or not isinstance(value, bytes):
                self.operationFailed.emit(error or "Не удалось загрузить адаптер")
                callback(None, error or "Не удалось загрузить адаптер")
                return
            commit(value)

        self.github_source.download_artifact(candidate, artifact_loaded)

    def uninstall(self, profile_id: str) -> None:
        item = self._installed.get(profile_id)
        if item is None:
            return
        self.store.remove(item)
        self._installed.pop(profile_id, None)
        if self.registry.profile(profile_id) is not None:
            self.registry.remove_profile(profile_id)
        remove_settings = getattr(self.settings, "remove_agent_settings", None)
        if callable(remove_settings):
            remove_settings(profile_id)
        self.integrationsChanged.emit(self.installed)

    def set_executable(self, profile_id: str, executable: str) -> IntegrationStatus:
        item = self._installed.get(profile_id)
        if item is None:
            raise KeyError(profile_id)
        updated = self.store.set_executable_override(item, executable)
        self._installed[profile_id] = updated
        setter = getattr(self.settings, "agent_set", None)
        if callable(setter):
            setter(profile_id, "executable", executable)
        self._register_profile(updated)
        self.integrationsChanged.emit(self.installed)
        return self.status(updated)

    def set_requirement_executable(
        self,
        profile_id: str,
        requirement_id: str,
        executable: str,
    ) -> IntegrationStatus:
        item = self._installed.get(profile_id)
        if item is None:
            raise KeyError(profile_id)
        setter = getattr(self.settings, "agent_set", None)
        if callable(setter):
            setter(profile_id, f"requirements/{requirement_id}", executable)
        self._register_profile(item)
        self.integrationsChanged.emit(self.installed)
        return self.status(item)

    def check_update(self, item: InstalledIntegration, callback: SourceCallback) -> None:
        if item.source_kind == "github":
            def github_loaded(value: Any | None, error: str) -> None:
                if error or not isinstance(value, IntegrationCandidate):
                    callback(None, error or "Не удалось проверить GitHub Release")
                    return
                if value.manifest.id != item.manifest.id:
                    callback(None, "Новый Release изменил id пакета; автоматическое обновление запрещено")
                    return
                callback(value, "")

            self.github_source.preview(item.source_ref, github_loaded)
            return
        if item.source_kind == "acp-registry":
            def catalog_loaded(value: Any | None, error: str) -> None:
                if error or not isinstance(value, list):
                    callback(None, error or "Не удалось обновить ACP Registry")
                    return
                candidate = next(
                    (
                        row
                        for row in value
                        if isinstance(row, IntegrationCandidate)
                        and row.manifest.id == item.manifest.id
                    ),
                    None,
                )
                if candidate is None:
                    callback(None, "Агент больше не найден в ACP Registry")
                else:
                    callback(candidate, "")

            self.registry_source.fetch(catalog_loaded)
            return
        callback(None, "Источник интеграции не поддерживает обновления")

    def requirement_needing_path(self, item: InstalledIntegration) -> str:
        runtime = item.manifest.runtime_for()
        if runtime is None:
            return ""
        for requirement in runtime.requirements:
            if not self._requirement_executable(item, requirement.id, requirement.commands):
                return requirement.id
        return ""

    @staticmethod
    def source_title(item: InstalledIntegration) -> str:
        if item.source_kind == "acp-registry":
            return "ACP Registry"
        if item.source_kind == "github":
            return f"GitHub: {item.source_ref}"
        return item.source_ref

    def _register_profile(self, item: InstalledIntegration) -> None:
        profile = self.profile(item)
        if self.registry.profile(profile.id) is None:
            self.registry.add_profile(profile)
        else:
            self.registry.replace_profile(profile)

    def _main_executable(self, item: InstalledIntegration, runtime: RuntimeSpec) -> str:
        if runtime.artifact_command:
            return str(self.store.payload_path(item, runtime.artifact_command))
        if item.executable_override:
            return item.executable_override
        getter = getattr(self.settings, "agent_get", None)
        if callable(getter):
            configured = str(getter(item.profile_id, "executable", ""))
            if configured:
                return configured
        for command in runtime.system_commands:
            resolved = shutil.which(command)
            if resolved:
                return resolved
        return runtime.system_commands[0] if runtime.system_commands else ""

    def _requirement_executable(
        self,
        item: InstalledIntegration,
        requirement_id: str,
        commands: tuple[str, ...],
    ) -> str:
        getter = getattr(self.settings, "agent_get", None)
        if callable(getter):
            configured = str(
                getter(item.profile_id, f"requirements/{requirement_id}", "")
            )
            if configured:
                return _resolve_executable(configured)
        for command in commands:
            resolved = shutil.which(command)
            if resolved:
                return resolved
        return ""


def _resolve_executable(value: str) -> str:
    if not value:
        return ""
    resolved = shutil.which(value)
    if resolved:
        return str(Path(resolved).resolve())
    path = Path(value).expanduser()
    if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
        return str(path.resolve())
    return ""
