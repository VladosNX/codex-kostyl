from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings

from .models import AccessMode


class AppSettings:
    def __init__(self) -> None:
        self._settings = QSettings("CodexKostyl", "CodexKostyl")

    @property
    def projects(self) -> list[str]:
        raw = self._settings.value("projects", [])
        if isinstance(raw, str):
            raw = [raw]
        return [str(Path(value).resolve()) for value in raw if Path(value).is_dir()]

    @projects.setter
    def projects(self, values: list[str]) -> None:
        self._settings.setValue("projects", list(dict.fromkeys(values)))

    def get(self, key: str, default: str = "") -> str:
        return str(self._settings.value(key, default))

    def set(self, key: str, value: object) -> None:
        self._settings.setValue(key, value)

    @property
    def access_mode(self) -> AccessMode:
        try:
            return AccessMode(self.get("access_mode", AccessMode.WORKSPACE_WRITE.value))
        except ValueError:
            return AccessMode.WORKSPACE_WRITE

    @access_mode.setter
    def access_mode(self, value: AccessMode) -> None:
        self.set("access_mode", value.value)

    def save_geometry(self, geometry: QByteArray, window_state: QByteArray) -> None:
        self.set("geometry", geometry)
        self.set("window_state", window_state)

    def restore_geometry(self) -> tuple[QByteArray, QByteArray]:
        geometry = self._settings.value("geometry", QByteArray())
        state = self._settings.value("window_state", QByteArray())
        return geometry, state

