from __future__ import annotations

import re
import shlex
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .agents.base import AgentProfile
from .integrations import AgentIntegrationManager, InstalledIntegration, IntegrationCandidate
from .settings import AppSettings


class AcpProfileDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить локального ACP-агента")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Например, Goose")
        form.addRow("Название", self.name_input)
        executable_row = QHBoxLayout()
        self.executable_input = QLineEdit()
        self.executable_input.setPlaceholderText("/путь/к/agent")
        browse = QPushButton("Обзор…")
        browse.clicked.connect(self._browse)
        executable_row.addWidget(self.executable_input, 1)
        executable_row.addWidget(browse)
        form.addRow("Исполняемый файл", executable_row)
        self.arguments_input = QLineEdit()
        self.arguments_input.setPlaceholderText("Аргументы запуска, если нужны")
        form.addRow("Аргументы", self.arguments_input)
        layout.addLayout(form)
        self.error_label = QLabel()
        self.error_label.setObjectName("questionError")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Исполняемый файл ACP-агента")
        if path:
            self.executable_input.setText(path)

    def _accept_if_valid(self) -> None:
        if not self.name_input.text().strip() or not self.executable_input.text().strip():
            self.error_label.setText("Укажите название и исполняемый файл.")
            self.error_label.setVisible(True)
            return
        try:
            shlex.split(self.arguments_input.text())
        except ValueError as exc:
            self.error_label.setText(f"Некорректные аргументы: {exc}")
            self.error_label.setVisible(True)
            return
        self.accept()

    def profile(self, existing_ids: set[str]) -> AgentProfile:
        name = self.name_input.text().strip()
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "acp-agent"
        profile_id = base
        suffix = 2
        while profile_id in existing_ids:
            profile_id = f"{base}-{suffix}"
            suffix += 1
        return AgentProfile(
            profile_id,
            "acp",
            name,
            self.executable_input.text().strip(),
            tuple(shlex.split(self.arguments_input.text())),
            "ACP v1 через локальный stdio",
        )


class GitHubUrlDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Установить интеграцию из GitHub")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Ссылка на публичный GitHub-репозиторий"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://github.com/owner/repository")
        layout.addWidget(self.url_input)
        hint = QLabel(
            "Будет прочитан последний стабильный Release и asset "
            "codex-kostyl-agent.json. Репозиторий не клонируется."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(520, 160)


class InstallPreviewDialog(QDialog):
    def __init__(
        self,
        candidate: IntegrationCandidate,
        parent: QWidget | None = None,
        *,
        updating: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Обновить интеграцию" if updating else "Установить интеграцию")
        manifest = candidate.manifest
        runtime = manifest.runtime_for()
        layout = QVBoxLayout(self)
        title = QLabel(manifest.name)
        title.setStyleSheet("font-size: 18px; font-weight: 650;")
        layout.addWidget(title)
        details = QTextBrowser()
        details.setOpenExternalLinks(True)
        command = ""
        if runtime is not None:
            if runtime.artifact_command:
                command = runtime.artifact_command
            else:
                command = runtime.system_commands[0] if runtime.system_commands else "будет выбран позже"
            command = " ".join([command, *runtime.arguments]).strip()
        source = (
            "ACP Registry"
            if candidate.source_kind == "acp-registry"
            else f"GitHub: {candidate.source_ref}"
        )
        details.setPlainText(
            f"{manifest.description}\n\n"
            f"Источник: {source}\n"
            f"Версия: {manifest.version} ({candidate.release_tag or 'без тега'})\n"
            f"Тип: {manifest.kind}\n"
            f"Команда: {command or 'не настроена'}\n"
            f"Зависимости: {', '.join(item.id for item in (runtime.requirements if runtime else ())) or 'нет'}"
        )
        details.setMaximumHeight(230)
        layout.addWidget(details)
        if manifest.kind == "acp-adapter":
            warning = QLabel(
                "⚠ ZIP содержит исполняемый адаптер. Он будет запущен отдельным процессом, "
                "но получит те же права пользователя, что и приложение. Устанавливайте только "
                "из источника, которому доверяете."
            )
            warning.setWordWrap(True)
            warning.setObjectName("questionError")
            layout.addWidget(warning)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Обновить" if updating else "Установить"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 420)


class AcpCatalogDialog(QDialog):
    def __init__(
        self,
        manager: AgentIntegrationManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self.candidates: list[IntegrationCandidate] = []
        self.setWindowTitle("Каталог ACP-агентов")
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти агента…")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._accept_selected())
        layout.addWidget(self.list, 1)
        self.status = QLabel("Загрузка официального ACP Registry…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.install_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.install_button.setText("Выбрать")
        self.install_button.setEnabled(False)
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        self.list.currentRowChanged.connect(
            lambda row: self.install_button.setEnabled(row >= 0)
        )
        layout.addWidget(buttons)
        self.resize(620, 560)
        manager.registry_source.fetch(self._loaded)

    @property
    def selected_candidate(self) -> IntegrationCandidate | None:
        item = self.list.currentItem()
        if item is None:
            return None
        index = item.data(Qt.ItemDataRole.UserRole)
        return self.candidates[index] if isinstance(index, int) else None

    def _loaded(self, value: Any | None, error: str) -> None:
        if error or not isinstance(value, list):
            self.status.setText(error or "Не удалось загрузить каталог")
            return
        self.candidates = [item for item in value if isinstance(item, IntegrationCandidate)]
        self.status.setText(f"Доступно интеграций: {len(self.candidates)}")
        self._filter()

    def _filter(self) -> None:
        query = self.search.text().strip().casefold()
        self.list.clear()
        for index, candidate in enumerate(self.candidates):
            manifest = candidate.manifest
            haystack = f"{manifest.name} {manifest.description} {manifest.id}".casefold()
            if query and query not in haystack:
                continue
            runtime = manifest.runtime_for()
            suffix = ""
            if runtime is None:
                suffix = " · нет сборки для этой платформы"
            elif runtime.configuration_required:
                suffix = " · потребуется указать executable"
            item = QListWidgetItem(f"{manifest.name} · {manifest.version}{suffix}")
            item.setToolTip(manifest.description)
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list.addItem(item)

    def _accept_selected(self) -> None:
        if self.selected_candidate is not None:
            self.accept()


class AgentSettingsDialog(QDialog):
    def __init__(
        self,
        service: Any,
        settings: AppSettings,
        manager: AgentIntegrationManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.settings = settings
        self.manager = manager
        self.setWindowTitle("Настройки")
        self.setModal(False)
        root = QHBoxLayout(self)
        navigation = QListWidget()
        navigation.setFixedWidth(150)
        navigation.addItem("Агенты")
        navigation.setCurrentRow(0)
        root.addWidget(navigation)
        pages = QStackedWidget()
        root.addWidget(pages, 1)
        page = QWidget()
        pages.addWidget(page)
        page_layout = QVBoxLayout(page)
        heading = QLabel("<h1>Агенты</h1>")
        page_layout.addWidget(heading)
        toolbar = QHBoxLayout()
        self.catalog_button = QPushButton("Каталог ACP…")
        self.github_button = QPushButton("Установить из GitHub…")
        self.local_button = QPushButton("Добавить локальный ACP…")
        toolbar.addWidget(self.catalog_button)
        toolbar.addWidget(self.github_button)
        toolbar.addWidget(self.local_button)
        toolbar.addStretch(1)
        page_layout.addLayout(toolbar)
        splitter = QSplitter()
        self.agent_list = QListWidget()
        splitter.addWidget(self.agent_list)
        details = QWidget()
        details_layout = QVBoxLayout(details)
        self.name_label = QLabel("Выберите агента")
        self.name_label.setStyleSheet("font-size: 18px; font-weight: 650;")
        self.description_label = QLabel()
        self.description_label.setWordWrap(True)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        details_layout.addWidget(self.name_label)
        details_layout.addWidget(self.description_label)
        details_layout.addWidget(self.status_label)
        details_layout.addWidget(self.source_label)
        details_layout.addStretch(1)
        actions = QHBoxLayout()
        self.help_button = QPushButton("Инструкция")
        self.path_button = QPushButton("Указать CLI…")
        self.check_button = QPushButton("Проверить")
        self.update_button = QPushButton("Проверить обновление")
        self.remove_button = QPushButton("Удалить")
        for button in (
            self.help_button,
            self.path_button,
            self.check_button,
            self.update_button,
            self.remove_button,
        ):
            actions.addWidget(button)
        details_layout.addLayout(actions)
        splitter.addWidget(details)
        splitter.setSizes([300, 560])
        page_layout.addWidget(splitter, 1)
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.close)
        page_layout.addWidget(close_buttons)
        self.resize(980, 650)

        navigation.currentRowChanged.connect(pages.setCurrentIndex)
        self.agent_list.currentItemChanged.connect(lambda *_args: self._show_selected())
        self.catalog_button.clicked.connect(self._install_from_catalog)
        self.github_button.clicked.connect(self._install_from_github)
        self.local_button.clicked.connect(self._add_local)
        self.help_button.clicked.connect(self._open_help)
        self.path_button.clicked.connect(self._choose_path)
        self.check_button.clicked.connect(self._check)
        self.update_button.clicked.connect(self._check_update)
        self.remove_button.clicked.connect(self._remove)
        manager.integrationsChanged.connect(lambda _items: self.refresh())
        profiles_changed = getattr(service, "profilesChanged", None)
        if profiles_changed is not None:
            profiles_changed.connect(lambda _items: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        selected = self._selected_profile_id()
        self.agent_list.blockSignals(True)
        self.agent_list.clear()
        profiles = getattr(self.service, "available_profiles", [])
        for profile in profiles:
            if not isinstance(profile, AgentProfile):
                continue
            installed = self.manager.integration_for_profile(profile.id)
            if profile.built_in:
                source = "Встроенный"
            elif installed is not None:
                source = self.manager.source_title(installed)
            else:
                source = "Локальный профиль"
            status = self.manager.status(installed) if installed is not None else None
            suffix = "" if status is None or status.available else " · требует настройки"
            item = QListWidgetItem(f"{profile.display_name}\n{source}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, profile.id)
            self.agent_list.addItem(item)
            if profile.id == selected:
                self.agent_list.setCurrentItem(item)
        if self.agent_list.currentRow() < 0 and self.agent_list.count():
            self.agent_list.setCurrentRow(0)
        self.agent_list.blockSignals(False)
        self._show_selected()

    def _selected_profile_id(self) -> str:
        item = self.agent_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _selected(self) -> tuple[AgentProfile | None, InstalledIntegration | None]:
        profile_id = self._selected_profile_id()
        profile = next(
            (
                item
                for item in getattr(self.service, "available_profiles", [])
                if isinstance(item, AgentProfile) and item.id == profile_id
            ),
            None,
        )
        return profile, self.manager.integration_for_profile(profile_id)

    def _show_selected(self) -> None:
        profile, installed = self._selected()
        if profile is None:
            for button in (
                self.help_button,
                self.path_button,
                self.check_button,
                self.update_button,
                self.remove_button,
            ):
                button.setEnabled(False)
            return
        self.name_label.setText(profile.display_name)
        self.description_label.setText(profile.description)
        if installed is None:
            self.source_label.setText("Источник: встроенный" if profile.built_in else "Источник: локальный профиль")
            availability = self.service.availability_for(profile.id)
            self.status_label.setText(
                "Состояние: готов" if availability.available else f"Состояние: {availability.error}"
            )
        else:
            status = self.manager.status(installed)
            self.status_label.setText(f"Состояние: {status.message}")
            self.source_label.setText(
                f"Источник: {self.manager.source_title(installed)}\n"
                f"Версия: {installed.manifest.version} · Тип: {installed.manifest.kind}"
            )
        self.help_button.setEnabled(bool(installed and installed.manifest.install_help.url))
        runtime = installed.manifest.runtime_for() if installed is not None else None
        adapter_requirements = runtime.requirements if runtime is not None else ()
        self.path_button.setEnabled(
            installed is None
            or installed.manifest.kind == "acp-command"
            or bool(adapter_requirements)
        )
        self.path_button.setText(
            "Указать зависимость…"
            if installed is not None
            and installed.manifest.kind == "acp-adapter"
            and adapter_requirements
            else "Указать CLI…"
        )
        self.check_button.setEnabled(True)
        self.update_button.setEnabled(installed is not None)
        self.remove_button.setEnabled(not profile.built_in)

    def _install_from_catalog(self) -> None:
        dialog = AcpCatalogDialog(self.manager, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_candidate:
            self._preview_and_install(dialog.selected_candidate)

    def _install_from_github(self) -> None:
        dialog = GitHubUrlDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        repository = dialog.url_input.text().strip()
        if not repository:
            return
        self.status_label.setText("Получение последнего стабильного GitHub Release…")
        self.github_button.setEnabled(False)

        def loaded(value: Any | None, error: str) -> None:
            self.github_button.setEnabled(True)
            if error or not isinstance(value, IntegrationCandidate):
                QMessageBox.critical(self, "Не удалось прочитать интеграцию", error)
                return
            self._preview_and_install(value)

        self.manager.github_source.preview(repository, loaded)

    def _preview_and_install(
        self,
        candidate: IntegrationCandidate,
        *,
        updating: bool = False,
    ) -> None:
        preview = InstallPreviewDialog(candidate, self, updating=updating)
        if preview.exec() != QDialog.DialogCode.Accepted:
            return
        replacing = next(
            (
                item
                for item in self.manager.installed
                if item.source_kind == candidate.source_kind
                and item.source_ref.casefold() == candidate.source_ref.casefold()
                and item.manifest.id == candidate.manifest.id
            ),
            None,
        )
        if (
            replacing is not None
            and getattr(self.service, "active_profile_id", "") == replacing.profile_id
        ):
            if not self.service.activate("codex"):
                QMessageBox.critical(
                    self,
                    "Не удалось обновить",
                    "Сначала остановите активного агента и переключитесь на Codex.",
                )
                return
            self.settings.selected_agent_id = "codex"
        self.status_label.setText("Установка интеграции…")

        def installed(value: Any | None, error: str) -> None:
            if error or not isinstance(value, InstalledIntegration):
                QMessageBox.critical(self, "Ошибка установки", error)
                return
            self.refresh()
            self._select_profile(value.profile_id)
            status = self.manager.status(value)
            if status.available:
                QMessageBox.information(self, "Интеграция установлена", "Агент готов к запуску.")
            else:
                QMessageBox.information(
                    self,
                    "Интеграция установлена",
                    f"Интеграция сохранена, но пока недоступна.\n\n{status.message}",
                )

        self.manager.install(candidate, installed)

    def _add_local(self) -> None:
        dialog = AcpProfileDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profiles = getattr(self.service, "available_profiles", [])
        profile = dialog.profile(
            {item.id for item in profiles if isinstance(item, AgentProfile)}
        )
        try:
            self.service.add_profile(profile)
        except ValueError as exc:
            QMessageBox.critical(self, "Не удалось добавить агента", str(exc))
            return
        self.refresh()
        self._select_profile(profile.id)

    def _choose_path(self) -> None:
        profile, installed = self._selected()
        if profile is None:
            return
        requirement_id = (
            self.manager.requirement_needing_path(installed) if installed is not None else ""
        )
        if (
            installed is not None
            and not requirement_id
            and installed.manifest.kind == "acp-adapter"
        ):
            runtime = installed.manifest.runtime_for()
            if runtime is not None and runtime.requirements:
                requirement_id = runtime.requirements[0].id
        caption = (
            f"Исполняемый файл зависимости {requirement_id}"
            if requirement_id
            else f"Исполняемый файл {profile.display_name}"
        )
        path, _filter = QFileDialog.getOpenFileName(self, caption)
        if not path:
            return
        try:
            if installed is not None and requirement_id:
                status = self.manager.set_requirement_executable(
                    profile.id, requirement_id, path
                )
            elif installed is not None:
                status = self.manager.set_executable(profile.id, path)
            else:
                success = self.service.set_executable(profile.id, path)
                if not success:
                    return
                status = None
        except (KeyError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Некорректный executable", str(exc))
            return
        self.refresh()
        if status is not None:
            self.status_label.setText(f"Состояние: {status.message}")

    def _check(self) -> None:
        profile, installed = self._selected()
        if profile is None:
            return
        if installed is not None:
            status = self.manager.status(installed)
            self.status_label.setText(f"Состояние: {status.message}")
        else:
            availability = self.service.availability_for(profile.id)
            self.status_label.setText(
                "Состояние: готов" if availability.available else f"Состояние: {availability.error}"
            )

    def _check_update(self) -> None:
        _profile, installed = self._selected()
        if installed is None:
            return
        self.status_label.setText("Проверка обновления…")

        def checked(value: Any | None, error: str) -> None:
            if error or not isinstance(value, IntegrationCandidate):
                QMessageBox.critical(self, "Не удалось проверить обновление", error)
                return
            if value.release_id == installed.release_id and value.manifest.version == installed.manifest.version:
                QMessageBox.information(self, "Обновления", "Установлена актуальная версия.")
                self._show_selected()
                return
            self._preview_and_install(value, updating=True)

        self.manager.check_update(installed, checked)

    def _remove(self) -> None:
        profile, installed = self._selected()
        if profile is None or profile.built_in:
            return
        answer = QMessageBox.question(
            self,
            "Удалить интеграцию",
            f"Удалить «{profile.display_name}»? Внешний CLI и его история не удаляются.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if getattr(self.service, "active_profile_id", "") == profile.id:
            if not self.service.activate("codex"):
                QMessageBox.critical(self, "Не удалось удалить", "Сначала переключитесь на Codex.")
                return
            self.settings.selected_agent_id = "codex"
        try:
            if installed is not None:
                self.manager.uninstall(profile.id)
            else:
                self.service.remove_profile(profile.id)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Не удалось удалить", str(exc))
        self.refresh()

    def _open_help(self) -> None:
        _profile, installed = self._selected()
        if installed and installed.manifest.install_help.url:
            QDesktopServices.openUrl(QUrl(installed.manifest.install_help.url))

    def _select_profile(self, profile_id: str) -> None:
        for index in range(self.agent_list.count()):
            item = self.agent_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == profile_id:
                self.agent_list.setCurrentItem(item)
                return
