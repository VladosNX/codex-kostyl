from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings

from .models import AccessMode
from .agents.base import AgentProfile


class AppSettings:
    def __init__(self) -> None:
        self._settings = QSettings("CodexKostyl", "CodexKostyl")
        self._migrate_codex_settings()

    def _migrate_codex_settings(self) -> None:
        """Move legacy request settings into the Codex driver namespace."""
        for key in ("model", "effort", "run_mode", "access_mode"):
            target = f"agents/codex/{key}"
            if not self._settings.contains(target) and self._settings.contains(key):
                self._settings.setValue(target, self._settings.value(key))

    @property
    def projects(self) -> list[str]:
        raw = self._settings.value("projects", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        projects: list[str] = []
        for value in raw:
            if not isinstance(value, (str, Path)):
                continue
            path = Path(value).expanduser()
            if path.is_dir():
                projects.append(str(path.resolve()))
        return list(dict.fromkeys(projects))

    @projects.setter
    def projects(self, values: list[str]) -> None:
        self._settings.setValue("projects", list(dict.fromkeys(values)))

    def get(self, key: str, default: str = "") -> str:
        value = self._settings.value(key, default)
        return default if value is None else str(value)

    def set(self, key: str, value: object) -> None:
        self._settings.setValue(key, value)

    @property
    def selected_agent_id(self) -> str:
        return self.get("selected_agent_id", "codex") or "codex"

    @selected_agent_id.setter
    def selected_agent_id(self, value: str) -> None:
        self.set("selected_agent_id", value)

    def agent_get(self, agent_id: str, key: str, default: str = "") -> str:
        return self.get(f"agents/{agent_id}/{key}", default)

    def agent_set(self, agent_id: str, key: str, value: object) -> None:
        self.set(f"agents/{agent_id}/{key}", value)

    @property
    def agent_profiles(self) -> list[AgentProfile]:
        raw = self.get("agent_profiles_json", "[]")
        try:
            rows = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(rows, list):
            return []
        profiles: list[AgentProfile] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            profile_id = str(row.get("id", "")).strip()
            executable = str(row.get("executable", "")).strip()
            if not profile_id or not executable:
                continue
            arguments = row.get("arguments", [])
            if not isinstance(arguments, list):
                arguments = []
            profiles.append(
                AgentProfile(
                    profile_id,
                    str(row.get("driver_kind") or "acp"),
                    str(row.get("display_name") or profile_id),
                    executable,
                    tuple(str(value) for value in arguments),
                    str(row.get("description") or ""),
                )
            )
        return profiles

    def save_agent_profile(self, profile: AgentProfile) -> None:
        profiles = {item.id: item for item in self.agent_profiles}
        profiles[profile.id] = profile
        payload = [
            {
                "id": item.id,
                "driver_kind": item.driver_kind,
                "display_name": item.display_name,
                "executable": item.executable,
                "arguments": list(item.arguments),
                "description": item.description,
            }
            for item in profiles.values()
            if not item.built_in
        ]
        self.set("agent_profiles_json", json.dumps(payload, ensure_ascii=False))

    def remove_agent_profile(self, profile_id: str) -> None:
        profiles = [item for item in self.agent_profiles if item.id != profile_id]
        self.set(
            "agent_profiles_json",
            json.dumps(
                [
                    {
                        "id": item.id,
                        "driver_kind": item.driver_kind,
                        "display_name": item.display_name,
                        "executable": item.executable,
                        "arguments": list(item.arguments),
                        "description": item.description,
                    }
                    for item in profiles
                ],
                ensure_ascii=False,
            ),
        )

    @property
    def access_mode(self) -> AccessMode:
        try:
            return AccessMode(
                self.agent_get(
                    "codex",
                    "access_mode",
                    self.get("access_mode", AccessMode.WORKSPACE_WRITE.value),
                )
            )
        except ValueError:
            return AccessMode.WORKSPACE_WRITE

    @access_mode.setter
    def access_mode(self, value: AccessMode) -> None:
        self.agent_set("codex", "access_mode", value.value)

    def save_geometry(self, geometry: QByteArray, window_state: QByteArray) -> None:
        self.set("geometry", geometry)
        self.set("window_state", window_state)

    def restore_geometry(self) -> tuple[QByteArray, QByteArray]:
        geometry = self._settings.value("geometry", QByteArray())
        state = self._settings.value("window_state", QByteArray())
        if not isinstance(geometry, QByteArray):
            geometry = QByteArray()
        if not isinstance(state, QByteArray):
            state = QByteArray()
        return geometry, state
