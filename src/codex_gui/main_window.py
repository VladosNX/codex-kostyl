from __future__ import annotations

import mimetypes
import shutil
import sys
import uuid
from dataclasses import dataclass, field as dataclass_field
from importlib.resources import files
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, QElapsedTimer, QEvent, QObject, QPoint, QProcess, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QImageReader,
    QKeyEvent,
    QPixmap,
    QKeySequence,
    QResizeEvent,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSystemTrayIcon,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .agents.base import (
    AgentAvailability,
    AgentCapabilities,
    AgentConfigOption,
    AgentDescriptor,
    AgentManifest,
    AgentProfile,
    AgentPrompt,
    AgentRunMode,
    AuthMethod,
    FeatureId,
    FeatureState,
    PermissionOption,
    PermissionRequest,
)
from .agent_settings import AcpProfileDialog, AgentSettingsDialog
from .integrations import AgentIntegrationManager
from .models import PLAN_MODE_VALUE, AccessMode, Attachment, ModelInfo, ThreadSummary, weekly_limit_from_payload
from .rendering import MarkdownRenderer, plain_pre
from .settings import AppSettings

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
STREAM_RENDER_INTERVAL_MS = 60
MAX_RENDERED_MESSAGE_CHARS = 160_000
MAX_ACTIVITY_CONTENT_CHARS = 120_000
MAX_HISTORY_TURNS = 40
MAX_HISTORY_ITEMS = 300
SIDEBAR_AUTO_HIDE_WIDTH = 980
SIDEBAR_AUTO_SHOW_WIDTH = 1100
THREAD_SEARCH_ROLE = int(Qt.ItemDataRole.UserRole) + 2
THREAD_TITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 3


def asset_icon(name: str) -> QIcon:
    """Return an application-owned icon for consistent cross-desktop rendering."""
    return QIcon(str(files("codex_gui").joinpath("assets", name)))


def localized_status(status: object) -> str:
    value = str(status or "")
    return {
        "starting": "запускается",
        "inProgress": "выполняется",
        "completed": "завершено",
        "failed": "ошибка",
        "interrupted": "остановлено",
        "cancelled": "отменено",
        "canceled": "отменено",
    }.get(value, value or "неизвестно")


def effort_title(effort: object) -> str:
    value = str(effort or "").strip()
    return {
        "none": "No effort",
        "minimal": "Minimal effort",
        "low": "Low effort",
        "medium": "Medium effort",
        "high": "High effort",
        "xhigh": "Extra high effort",
    }.get(value, f"{value} effort" if value else "Effort")


@dataclass(slots=True)
class QueuedMessage:
    text: str
    attachments: list[Attachment]
    model: str
    effort: str | None
    access_mode: AccessMode
    collaboration_mode: str | None
    queue_syntax: str | None = None
    config: dict[str, str | bool] = dataclass_field(default_factory=dict)
    run_mode_id: str = ""


@dataclass(slots=True)
class QueuedCommand:
    name: str
    arguments: str = ""

    @property
    def syntax(self) -> str:
        return f"/{self.name}" + (f" {self.arguments}" if self.arguments else "")


@dataclass(slots=True)
class ApprovalPrompt:
    request_id: object
    method: str
    params: dict[str, Any]
    title: str
    detail: str
    options: tuple[Any, ...] = ()


@dataclass(slots=True, frozen=True)
class SlashCommand:
    name: str
    description: str
    accepts_arguments: bool = False
    needs_thread: bool = False

    @property
    def syntax(self) -> str:
        return f"/{self.name}"


SLASH_COMMANDS = (
    SlashCommand("compact", "Сжать историю текущего чата", needs_thread=True),
    SlashCommand(
        "review",
        "Проверить изменения или выполнить ревью по инструкции",
        accepts_arguments=True,
        needs_thread=True,
    ),
    SlashCommand("fork", "Создать копию текущего чата и открыть её", needs_thread=True),
    SlashCommand(
        "plan",
        "Переключиться в Plan Mode или отправить запрос на планирование",
        accepts_arguments=True,
    ),
    SlashCommand("new", "Создать новый чат в текущей рабочей папке"),
    SlashCommand("help", "Показать полный список slash-команд"),
)
SLASH_COMMANDS_BY_NAME = {command.name: command for command in SLASH_COMMANDS}


def format_duration(milliseconds: int) -> str:
    milliseconds = max(0, milliseconds)
    seconds = milliseconds / 1000
    if seconds < 10:
        return f"{seconds:.1f}".replace(".", ",") + " сек"
    rounded_seconds = int(round(seconds))
    if rounded_seconds < 60:
        return f"{rounded_seconds} сек"
    minutes, remaining_seconds = divmod(rounded_seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {remaining_seconds} сек"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours} ч {remaining_minutes} мин {remaining_seconds} сек"


def context_usage(token_usage: dict[str, Any]) -> tuple[int, int, int] | None:
    try:
        used = int(token_usage["last"]["totalTokens"])
        window = int(token_usage["modelContextWindow"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if used < 0 or window <= 0:
        return None
    percent = round(min(1.0, used / window) * 100)
    return percent, used, window


def stream_render_interval(content_size: int) -> int:
    if content_size >= 100_000:
        return 250
    if content_size >= 40_000:
        return 120
    return STREAM_RENDER_INTERVAL_MS


def recent_thread_items(
    thread: dict[str, Any],
    max_turns: int = MAX_HISTORY_TURNS,
    max_items: int = MAX_HISTORY_ITEMS,
) -> tuple[list[dict[str, Any]], int, int]:
    """Return a bounded, recent subset suitable for synchronous Qt rendering."""
    turns = [turn for turn in thread.get("turns", []) if isinstance(turn, dict)]
    omitted_turns = max(0, len(turns) - max_turns)
    selected_turns = turns[-max_turns:]
    items = [
        item
        for turn in selected_turns
        for item in turn.get("items", [])
        if isinstance(item, dict)
    ]
    omitted_items = max(0, len(items) - max_items)
    return items[-max_items:], omitted_turns, omitted_items


class Composer(QTextEdit):
    MIN_HEIGHT = 54
    MAX_HEIGHT = 180

    sendRequested = Signal()
    filesDropped = Signal(list)
    slashNavigate = Signal(int)
    slashComplete = Signal()
    slashActivate = Signal()
    slashDismiss = Signal()
    newChatRequested = Signal()
    accessModeRequested = Signal()
    attachmentRequested = Signal()
    requestSettingsRequested = Signal()
    latestActivityRequested = Signal()
    stopRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setObjectName("composer")
        self.setPlaceholderText("Попросите Codex изменить код, найти ошибку или объяснить проект…")
        self.setToolTip("Enter — отправить · Shift+Enter — новая строка")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.document().documentLayout().documentSizeChanged.connect(self._adjust_height)
        self.setFixedHeight(self.MIN_HEIGHT)
        self._slash_menu_visible = False
        self._slash_has_selection = False

    def set_slash_menu_state(self, visible: bool, has_selection: bool) -> None:
        self._slash_menu_visible = visible
        self._slash_has_selection = has_selection

    def _adjust_height(self, _size: Any = None) -> None:
        document_height = int(self.document().size().height())
        target = max(self.MIN_HEIGHT, min(self.MAX_HEIGHT, document_height + 18))
        self.setFixedHeight(target)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if target >= self.MAX_HEIGHT
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._slash_menu_visible:
            if event.key() == Qt.Key.Key_Up:
                self.slashNavigate.emit(-1)
                return
            if event.key() == Qt.Key.Key_Down:
                self.slashNavigate.emit(1)
                return
            if event.key() == Qt.Key.Key_Tab:
                self.slashComplete.emit()
                return
            if event.key() == Qt.Key.Key_Escape:
                self.slashDismiss.emit()
                return
            if (
                event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and self._slash_has_selection
            ):
                self.slashActivate.emit()
                return
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            shortcut_signals = {
                Qt.Key.Key_K: self.newChatRequested,
                Qt.Key.Key_M: self.accessModeRequested,
                Qt.Key.Key_O: self.attachmentRequested,
                Qt.Key.Key_I: self.requestSettingsRequested,
                Qt.Key.Key_T: self.latestActivityRequested,
            }
            signal = shortcut_signals.get(event.key())
            if signal is not None:
                signal.emit()
                return
        if (
            event.key() == Qt.Key.Key_Escape
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            self.stopRequested.emit()
            return
        if (
            event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            self.sendRequested.emit()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class SlashCommandPanel(QFrame):
    commandActivated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("slashCommandPanel")
        self.setMaximumWidth(860)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(0)
        self.list = QListWidget()
        self.list.setObjectName("slashCommandList")
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setSpacing(1)
        self.list.itemClicked.connect(self._item_clicked)
        layout.addWidget(self.list)
        self.setVisible(False)

    def set_commands(
        self,
        commands: list[tuple[SlashCommand, bool, str]],
    ) -> None:
        previous = self.selected_command()
        self.list.clear()
        first_enabled: QListWidgetItem | None = None
        selected: QListWidgetItem | None = None
        for command, available, reason in commands:
            detail = command.description
            if reason:
                detail += f" · {reason}"
            item = QListWidgetItem(f"{command.syntax}\n{detail}")
            item.setData(Qt.ItemDataRole.UserRole, command.name)
            item.setData(int(Qt.ItemDataRole.UserRole) + 1, available)
            item.setSizeHint(QSize(0, 48))
            if available:
                if first_enabled is None:
                    first_enabled = item
                if command.name == previous:
                    selected = item
            else:
                item.setForeground(QColor("#666b67"))
                item.setToolTip(reason)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
            self.list.addItem(item)
        target = selected or first_enabled
        if target is not None:
            self.list.setCurrentItem(target)
        self.list.setFixedHeight(min(300, max(52, len(commands) * 50 + 4)))

    def selected_command(self) -> str | None:
        item = self.list.currentItem()
        if item is None or not bool(item.data(int(Qt.ItemDataRole.UserRole) + 1)):
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def move_selection(self, delta: int) -> None:
        if not self.list.count():
            return
        current = self.list.currentRow()
        for offset in range(1, self.list.count() + 1):
            row = (current + delta * offset) % self.list.count()
            item = self.list.item(row)
            if bool(item.data(int(Qt.ItemDataRole.UserRole) + 1)):
                self.list.setCurrentItem(item)
                return

    def activate_selected(self) -> None:
        command = self.selected_command()
        if command:
            self.commandActivated.emit(command)

    def _item_clicked(self, item: QListWidgetItem) -> None:
        if bool(item.data(int(Qt.ItemDataRole.UserRole) + 1)):
            self.commandActivated.emit(str(item.data(Qt.ItemDataRole.UserRole)))


class NumberedChoiceMenu(QMenu):
    """A menu whose visible actions can be selected with number keys."""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        text = event.text()
        if len(text) == 1 and text in "123456789":
            actions = [
                action
                for action in self.actions()
                if action.isVisible() and not action.isSeparator()
            ]
            index = int(text) - 1
            if index < len(actions) and actions[index].isEnabled():
                action = actions[index]
                submenu = action.menu()
                if submenu is not None:
                    self.setActiveAction(action)
                    action_rect = self.actionGeometry(action)
                    submenu.popup(
                        self.mapToGlobal(
                            QPoint(action_rect.right(), action_rect.top())
                        )
                    )
                    submenu.setFocus()
                else:
                    action.trigger()
                    menu: QWidget | None = self
                    while isinstance(menu, QMenu):
                        parent = menu.parentWidget()
                        menu.close()
                        menu = parent
                return
        super().keyPressEvent(event)


class ShortcutPushButton(QPushButton):
    def __init__(
        self,
        text: str,
        shortcut_text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.shortcut_label = QLabel(shortcut_text, self)
        self.shortcut_label.setObjectName("inlineShortcutLabel")
        self.shortcut_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.shortcut_label.adjustSize()
        self.shortcut_label.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.shortcut_label.adjustSize()
        self.shortcut_label.move(
            max(0, self.width() - self.shortcut_label.width() - 10),
            max(0, (self.height() - self.shortcut_label.height()) // 2),
        )
        self.shortcut_label.raise_()


class ShortcutToolButton(QToolButton):
    def __init__(
        self,
        shortcut_text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.shortcut_label = QLabel(shortcut_text, self)
        self.shortcut_label.setObjectName("inlineShortcutLabel")
        self.shortcut_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.shortcut_label.adjustSize()
        self.shortcut_label.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.shortcut_label.adjustSize()
        self.shortcut_label.move(
            max(0, self.width() - self.shortcut_label.width() - 10),
            max(0, (self.height() - self.shortcut_label.height()) // 2),
        )
        self.shortcut_label.raise_()


class ShortcutComboBox(QComboBox):
    popupRequested = Signal()

    def __init__(self, shortcut_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.shortcut_label = QLabel(shortcut_text, self)
        self.shortcut_label.setObjectName("inlineShortcutLabel")
        self.shortcut_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.shortcut_label.adjustSize()
        self.shortcut_label.raise_()

    def showPopup(self) -> None:
        self.popupRequested.emit()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.shortcut_label.adjustSize()
        self.shortcut_label.move(
            max(0, self.width() - self.shortcut_label.width() - 25),
            max(0, (self.height() - self.shortcut_label.height()) // 2),
        )
        self.shortcut_label.raise_()


class MessageCard(QFrame):
    editRequested = Signal(str)

    def __init__(self, role: str, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self.text = text
        self.renderer = MarkdownRenderer()
        self._last_rendered_text: str | None = None
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(STREAM_RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._render_text)
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._sync_body_height)
        self.setObjectName("userCard" if role == "user" else "agentCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16 if role == "user" else 4, 12, 16 if role == "user" else 4, 12)
        layout.setSpacing(5)
        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMaximumWidth(760 if role == "user" else 840)
        layout.addWidget(self.body)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        actions.addStretch(1)
        self.copy_button = QToolButton()
        self.copy_button.setObjectName("messageActionButton")
        self.copy_button.setIcon(asset_icon("copy.svg"))
        self.copy_button.setToolTip("Скопировать полный текст сообщения")
        self.copy_button.setAccessibleName("Скопировать сообщение")
        self.copy_button.setFixedSize(27, 25)
        actions.addWidget(self.copy_button)
        self.edit_button: QToolButton | None = None
        if role == "user":
            self.edit_button = QToolButton()
            self.edit_button.setObjectName("messageActionButton")
            self.edit_button.setIcon(asset_icon("edit.svg"))
            self.edit_button.setToolTip("Перенести текст в поле ввода для редактирования")
            self.edit_button.setAccessibleName("Редактировать сообщение")
            self.edit_button.setFixedSize(27, 25)
            actions.addWidget(self.edit_button)
        layout.addLayout(actions)
        self.copy_button.clicked.connect(self.copy_text)
        if self.edit_button is not None:
            self.edit_button.clicked.connect(lambda: self.editRequested.emit(self.text))
        self.set_text(text)

    def copy_text(self) -> None:
        QApplication.clipboard().setText(self.text)

    def set_text(self, text: str) -> None:
        self.text = text
        self._render_timer.stop()
        self._render_text()

    def _render_text(self) -> None:
        text = self._bounded_text(self.text)
        if text == self._last_rendered_text:
            return
        self._last_rendered_text = text
        self.body.setHtml(self.renderer.render(text or "…"))
        self._sync_body_height()
        self._height_timer.start(0)

    @staticmethod
    def _bounded_text(text: str) -> str:
        if len(text) <= MAX_RENDERED_MESSAGE_CHARS:
            return text
        half = MAX_RENDERED_MESSAGE_CHARS // 2
        omitted = len(text) - (half * 2)
        marker = f"\n\n> … Скрыто {omitted:,} символов для стабильной работы интерфейса …\n\n"
        return text[:half] + marker + text[-half:]

    def _sync_body_height(self) -> None:
        width = max(260, self.body.viewport().width())
        self.body.document().setTextWidth(width)
        self.body.setFixedHeight(max(34, int(self.body.document().size().height()) + 10))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._height_timer.start(0)

    def append(self, delta: str) -> None:
        self.text += delta
        if not self._render_timer.isActive():
            self._render_timer.start(stream_render_interval(len(self.text)))


class ThinkingIndicator(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("thinkingIndicator")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 3, 5, 3)
        self._activity = "ИИ думает"
        self.label = QLabel()
        self.label.setObjectName("thinkingLabel")
        layout.addWidget(self.label)
        layout.addStretch(1)
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(420)
        self._timer.timeout.connect(self._animate)
        self._render_frame()

    def start(self) -> None:
        self._frame = 0
        self._activity = "ИИ думает"
        self._render_frame()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _animate(self) -> None:
        self._frame = (self._frame + 1) % 4
        self._render_frame()

    def set_activity(self, activity: str) -> None:
        self._activity = activity.strip() or "ИИ думает"
        self._frame = 0
        self._render_frame()

    def _render_frame(self) -> None:
        dots = "." * self._frame
        self.label.setText(f"✦  {self._activity}{dots:<3}")


class ActivityCard(QFrame):
    def __init__(self, title: str, content: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content = ""
        self._content_truncated = False
        self._last_rendered_content: tuple[str, bool] | None = None
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(STREAM_RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._render_content)
        self.setObjectName("activityCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 7)
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setObjectName("activityToggle")
        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.body.setVisible(False)
        self.body.setMaximumHeight(280)
        self.toggle.toggled.connect(self._toggle)
        layout.addWidget(self.toggle)
        layout.addWidget(self.body)
        self.set_content(content)

    def _toggle(self, checked: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.body.setVisible(checked)

    def set_content(self, content: str) -> None:
        self.content, self._content_truncated = self._bounded_content(content)
        self._render_timer.stop()
        self._render_content()

    @staticmethod
    def _bounded_content(content: str) -> tuple[str, bool]:
        if len(content) <= MAX_ACTIVITY_CONTENT_CHARS:
            return content, False
        return content[-MAX_ACTIVITY_CONTENT_CHARS:], True

    def _render_content(self) -> None:
        state = (self.content, self._content_truncated)
        if state == self._last_rendered_content:
            return
        self._last_rendered_content = state
        prefix = "… Показан только конец большого вывода …\n\n" if self._content_truncated else ""
        self.body.setHtml(plain_pre(prefix + (self.content or "Выполняется…")))

    def append(self, delta: str) -> None:
        content, truncated = self._bounded_content(self.content + delta)
        self.content = content
        self._content_truncated = self._content_truncated or truncated
        if not self._render_timer.isActive():
            self._render_timer.start(stream_render_interval(len(self.content)))


class ActivityGroupCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("activityGroup")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.activities: list[ActivityCard] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = ShortcutToolButton("Ctrl+T")
        self.header.setText("ДЕЙСТВИЯ")
        self.header.setObjectName("activityGroupTitle")
        self.header.setCheckable(True)
        self.header.setChecked(False)
        self.header.setArrowType(Qt.ArrowType.RightArrow)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self.header)
        self.items_container = QWidget()
        self.items_container.setObjectName("activityGroupItems")
        self.items_layout = QVBoxLayout(self.items_container)
        self.items_layout.setContentsMargins(0, 0, 0, 0)
        self.items_layout.setSpacing(0)
        self.items_container.setVisible(False)
        layout.addWidget(self.items_container)
        self.header.toggled.connect(self._toggle)

    def _toggle(self, checked: bool) -> None:
        self.header.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self.items_container.setVisible(checked)

    def add_activity(self, activity: ActivityCard) -> None:
        self.activities.append(activity)
        self.items_layout.addWidget(activity)
        count = len(self.activities)
        self.header.setText(f"ДЕЙСТВИЯ  ·  {count}")


class ExecutionPlanCard(QFrame):
    STATUS_ICONS = {
        "pending": "○",
        "inProgress": "◉",
        "completed": "✓",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("executionPlanCard")
        self.setMaximumWidth(840)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 12)
        layout.setSpacing(7)
        title = QLabel("ПЛАН ВЫПОЛНЕНИЯ")
        title.setObjectName("executionPlanTitle")
        layout.addWidget(title)
        self.explanation = QLabel()
        self.explanation.setObjectName("executionPlanExplanation")
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        self.steps_layout = QVBoxLayout()
        self.steps_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_layout.setSpacing(4)
        layout.addLayout(self.steps_layout)

    def set_plan(self, explanation: str, plan: list[dict[str, Any]]) -> None:
        self.explanation.setText(explanation.strip())
        self.explanation.setVisible(bool(explanation.strip()))
        while self.steps_layout.count():
            item = self.steps_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for entry in plan:
            status = str(entry.get("status", "pending"))
            step = str(entry.get("step", "")).strip()
            label = QLabel(f"{self.STATUS_ICONS.get(status, '○')}  {step}")
            label.setObjectName("executionPlanStep")
            label.setProperty("status", status)
            label.setWordWrap(True)
            self.steps_layout.addWidget(label)


class InlineUserInputCard(QFrame):
    submitted = Signal(dict)
    canceled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("questionCard")
        self.setMaximumWidth(860)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._inputs: dict[str, tuple[QComboBox | None, QLineEdit]] = {}
        self._question_widgets: list[QWidget] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 14, 13)
        layout.setSpacing(9)
        self.title = QLabel("Агент запрашивает ответ")
        self.title.setObjectName("questionTitle")
        layout.addWidget(self.title)
        self.questions_layout = QVBoxLayout()
        self.questions_layout.setSpacing(10)
        layout.addLayout(self.questions_layout)
        self.error = QLabel()
        self.error.setObjectName("questionError")
        self.error.setVisible(False)
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("approvalSecondaryButton")
        self.submit_button = QPushButton("Ответить")
        self.submit_button.setObjectName("approvalPrimaryButton")
        self.cancel_button.clicked.connect(self.canceled.emit)
        self.submit_button.clicked.connect(self._submit)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.submit_button)
        layout.addLayout(actions)

    def set_request(self, params: dict[str, Any]) -> None:
        self._clear_questions()
        questions = [item for item in params.get("questions", []) if isinstance(item, dict)]
        if len(questions) == 1 and questions[0].get("header"):
            self.title.setText(str(questions[0]["header"]))
        else:
            self.title.setText("Агент запрашивает ответы")
        for number, question in enumerate(questions, start=1):
            frame = QWidget()
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(0, 0, 0, 0)
            frame_layout.setSpacing(5)
            prompt = QLabel(str(question.get("question") or f"Вопрос {number}"))
            prompt.setObjectName("questionPrompt")
            prompt.setWordWrap(True)
            frame_layout.addWidget(prompt)
            options = question.get("options")
            combo: QComboBox | None = None
            answer = QLineEdit()
            answer.setObjectName("questionAnswer")
            answer.setPlaceholderText("Введите ответ…")
            if question.get("isSecret"):
                answer.setEchoMode(QLineEdit.EchoMode.Password)
            if isinstance(options, list) and options:
                combo = QComboBox()
                combo.setObjectName("questionOptions")
                for option in options:
                    if not isinstance(option, dict):
                        continue
                    label = str(option.get("label", ""))
                    combo.addItem(label, label)
                    combo.setItemData(
                        combo.count() - 1,
                        str(option.get("description", "")),
                        Qt.ItemDataRole.ToolTipRole,
                    )
                if question.get("isOther"):
                    combo.addItem("Другой ответ…", None)
                answer.setVisible(combo.currentData() is None)
                combo.currentIndexChanged.connect(
                    lambda _index, selector=combo, field=answer: field.setVisible(
                        selector.currentData() is None
                    )
                )
                frame_layout.addWidget(combo)
            frame_layout.addWidget(answer)
            question_id = str(question.get("id") or f"question_{number}")
            self._inputs[question_id] = (combo, answer)
            self._question_widgets.append(frame)
            self.questions_layout.addWidget(frame)
        self.error.clear()
        self.error.setVisible(False)

    def _clear_questions(self) -> None:
        self._inputs.clear()
        for widget in self._question_widgets:
            self.questions_layout.removeWidget(widget)
            widget.deleteLater()
        self._question_widgets.clear()

    def _submit(self) -> None:
        answers: dict[str, list[str]] = {}
        for question_id, (combo, field) in self._inputs.items():
            value = combo.currentData() if combo is not None else None
            if combo is None or value is None:
                value = field.text().strip()
            if not value:
                self.error.setText("Ответьте на все вопросы")
                self.error.setVisible(True)
                return
            answers[question_id] = [str(value)]
        self.submitted.emit(answers)


class PlanConfirmationCard(QFrame):
    implementRequested = Signal()
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("planConfirmationCard")
        self.setMaximumWidth(860)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 14, 12)
        text = QVBoxLayout()
        title = QLabel("План готов")
        title.setObjectName("planConfirmationTitle")
        detail = QLabel("Проверьте план выше. Начать его реализацию?")
        detail.setObjectName("planConfirmationDetail")
        text.addWidget(title)
        text.addWidget(detail)
        layout.addLayout(text, 1)
        self.dismiss_button = QPushButton("Оставить план")
        self.dismiss_button.setObjectName("approvalSecondaryButton")
        self.implement_button = QPushButton("Начать реализацию")
        self.implement_button.setObjectName("approvalPrimaryButton")
        self.dismiss_button.clicked.connect(self.dismissed.emit)
        self.implement_button.clicked.connect(self.implementRequested.emit)
        layout.addWidget(self.dismiss_button)
        layout.addWidget(self.implement_button)


class InlineApprovalCard(QFrame):
    decisionSelected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("approvalCard")
        self.setMaximumWidth(860)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 14, 13)
        layout.setSpacing(9)

        heading = QHBoxLayout()
        icon = QLabel("!")
        icon.setObjectName("approvalIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(24, 24)
        self.title = QLabel("Требуется подтверждение")
        self.title.setObjectName("approvalTitle")
        heading.addWidget(icon)
        heading.addWidget(self.title)
        heading.addStretch(1)
        layout.addLayout(heading)

        self.detail = QLabel()
        self.detail.setObjectName("approvalDetail")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.actions = QHBoxLayout()
        layout.addLayout(self.actions)

    def set_request(self, title: str, detail: str, options: tuple[Any, ...] = ()) -> None:
        self.title.setText(title)
        self.detail.setText(detail.strip())
        while self.actions.count():
            item = self.actions.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        rows = options or (
            PermissionOption("decline", "Запретить", "reject"),
            PermissionOption(
                "acceptForSession",
                "Разрешить до закрытия чата",
                "allow_always",
            ),
            PermissionOption("accept", "Разрешить один раз", "allow_once"),
        )
        stop_button = QPushButton("Остановить выполнение")
        stop_button.setObjectName("approvalDangerButton")
        stop_button.setToolTip("Остановить текущий ход и отклонить запрос")
        stop_button.clicked.connect(
            lambda _checked=False: self.decisionSelected.emit("cancel")
        )
        self.actions.addWidget(stop_button)
        self.actions.addStretch(1)
        for option in rows:
            option_id = str(getattr(option, "id", ""))
            label = str(getattr(option, "label", option_id))
            kind = str(getattr(option, "kind", ""))
            object_name = (
                "approvalPrimaryButton"
                if kind in {"allow_once", "accept"} or option_id == "accept"
                else "approvalSecondaryButton"
            )
            button = QPushButton(label)
            button.setObjectName(object_name)
            button.setAccessibleName(label)
            button.clicked.connect(
                lambda _checked=False, value=option_id: self.decisionSelected.emit(value)
            )
            self.actions.addWidget(button)


class ApiKeyDialog(QDialog):
    def __init__(self, agent_name: str = "Codex", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Вход с API-ключом")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Ключ передаётся {agent_name} и не сохраняется приложением."))
        self.input = QLineEdit()
        self.input.setEchoMode(QLineEdit.EchoMode.Password)
        self.input.setPlaceholderText("sk-…")
        layout.addWidget(self.input)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(
        self,
        service: Any,
        settings: AppSettings,
        stop_server: Any,
        integration_manager: AgentIntegrationManager | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.settings = settings
        self.stop_server = stop_server
        self.integration_manager = integration_manager
        self._agent_settings_dialog: AgentSettingsDialog | None = None
        self.models: list[ModelInfo] = []
        self.agent_config_options: tuple[AgentConfigOption, ...] = ()
        manifest = getattr(service, "manifest", None)
        self.agent_manifest = manifest if isinstance(manifest, AgentManifest) else AgentManifest()
        self.attachments: list[Attachment] = []
        self.cards: dict[str, MessageCard | ActivityCard] = {}
        self._execution_plan_cards: dict[str, ExecutionPlanCard] = {}
        self._last_activity_group: ActivityGroupCard | None = None
        self._latest_activity_card: ActivityCard | None = None
        self._latest_activity_group: ActivityGroupCard | None = None
        self._approval_queue: list[ApprovalPrompt] = []
        self._current_approval: ApprovalPrompt | None = None
        self._user_input_queue: list[tuple[object, dict[str, Any]]] = []
        self._current_user_input: tuple[object, dict[str, Any]] | None = None
        self._message_queue: list[QueuedMessage | QueuedCommand] = []
        self._editing_queued_message: QueuedMessage | None = None
        self._queue_edit_draft_text = ""
        self._queue_edit_draft_attachments: list[Attachment] = []
        self._queue_paused = False
        self._queue_action_pending = False
        self._turn_active = False
        self._turn_timer = QElapsedTimer()
        self._active_collaboration_mode: str | None = None
        self._thinking_indicator: ThinkingIndicator | None = None
        self._pending_plan_text = ""
        self._slash_dismissed_text: str | None = None
        self._slash_help_visible = False
        self._auto_follow = True
        self._danger_acknowledged = False
        self._loading_existing_session = False
        self._run_mode_context: tuple[str, str] | None = None
        self._pending_run_mode_id = ""
        self._sidebar_user_hidden = False
        self._sidebar_auto_hidden = False
        self._closing = False
        self._build_ui()
        self._build_notifications()
        self._connect_service()
        self._clear_timeline("Добавьте рабочую папку, чтобы начать работу с AI-агентом.")
        self._load_settings()

    def _build_ui(self) -> None:
        self.setWindowTitle("Codex")
        self.setMinimumSize(760, 640)
        self.resize(1280, 820)
        self.main_splitter = QSplitter()
        self.main_splitter.setObjectName("mainSplitter")
        self.main_splitter.setHandleWidth(1)
        self.main_splitter.setChildrenCollapsible(False)
        self.sidebar_panel = self._build_sidebar()
        self.main_splitter.addWidget(self.sidebar_panel)
        self.main_splitter.addWidget(self._build_chat())
        self.main_splitter.setSizes([272, 1008])
        self.setCentralWidget(self.main_splitter)
        self.sidebar_shortcut = QShortcut(QKeySequence("Ctrl+B"), self)
        self.sidebar_shortcut.activated.connect(self._toggle_sidebar)
        self.new_chat_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self.new_chat_shortcut.activated.connect(self._shortcut_new_chat)
        self.access_mode_shortcut = QShortcut(QKeySequence("Ctrl+M"), self)
        self.access_mode_shortcut.activated.connect(self._show_access_mode_menu)
        self.attach_file_shortcut = QShortcut(QKeySequence("Ctrl+O"), self)
        self.attach_file_shortcut.activated.connect(self._shortcut_choose_attachments)
        self.request_settings_shortcut = QShortcut(QKeySequence("Ctrl+I"), self)
        self.request_settings_shortcut.activated.connect(
            self._show_request_settings_menu
        )
        self.latest_activity_shortcut = QShortcut(QKeySequence("Ctrl+T"), self)
        self.latest_activity_shortcut.activated.connect(
            self._toggle_latest_activity
        )
        self.stop_shortcut = QShortcut(QKeySequence("Esc"), self)
        self.stop_shortcut.setEnabled(False)
        self.stop_shortcut.activated.connect(self._confirm_interrupt)
        self.statusBar().showMessage("Запуск AI-агента…")

    def _build_notifications(self) -> None:
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_icon.setToolTip("Codex Kostyl")
        self.tray_icon.messageClicked.connect(self._show_from_notification)
        self.tray_icon.activated.connect(
            lambda _reason: self._show_from_notification()
        )
        self._tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if self._tray_available:
            self.tray_icon.show()

    def _show_from_notification(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _show_desktop_notification(self, title: str, message: str) -> None:
        if self._tray_available and QSystemTrayIcon.supportsMessages():
            self.tray_icon.showMessage(
                title,
                message,
                QSystemTrayIcon.MessageIcon.Information,
                7000,
            )
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            QProcess.startDetached(
                "notify-send",
                ["--app-name=Codex Kostyl", title, message],
            )
        QApplication.alert(self, 4000)

    def _toggle_sidebar(self) -> None:
        visible = not self.sidebar_panel.isHidden()
        self._sidebar_user_hidden = visible
        self._sidebar_auto_hidden = False
        self._set_sidebar_visible(not visible)
        self.settings.set("sidebar_hidden", self._sidebar_user_hidden)

    def _shortcut_new_chat(self) -> None:
        if not self.new_chat_button.isEnabled():
            self._show_notice(
                "Новый чат нельзя открыть во время активного хода или обработки очереди.",
                "warning",
            )
            return
        self._new_chat()

    def _shortcut_choose_attachments(self) -> None:
        if not self.attach_button.isEnabled():
            self._show_notice(
                self.attach_button.toolTip() or "Добавление файлов сейчас недоступно.",
                "warning",
            )
            return
        self._choose_attachments()

    def _toggle_latest_activity(self) -> None:
        group = self._latest_activity_group
        card = self._latest_activity_card
        if group is None or card is None:
            return
        expanded = group.header.isChecked() and card.toggle.isChecked()
        group.header.setChecked(not expanded)
        card.toggle.setChecked(not expanded)
        self.scroll.ensureWidgetVisible(group, 0, 24)

    def _confirm_interrupt(self) -> None:
        if not self._turn_active:
            return
        answer = QMessageBox.question(
            self,
            "Остановить генерацию?",
            "Текущий ответ будет прерван. Сообщения в очереди останутся на месте.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        cancel_run = getattr(self.service, "cancel_run", None)
        if callable(cancel_run):
            cancel_run()
        else:
            self.service.interrupt()

    def _set_sidebar_visible(self, visible: bool) -> None:
        self.sidebar_panel.setVisible(visible)
        if visible:
            self.main_splitter.setSizes([272, max(488, self.width() - 272)])
        self.sidebar_toggle.setToolTip(
            ("Скрыть" if visible else "Показать")
            + " боковую панель · Ctrl+B"
        )

    def _show_notice(self, message: str, level: str = "info", timeout: int = 4500) -> None:
        self.notice_label.setText(message)
        self.notice_banner.setProperty("level", level)
        self.notice_banner.style().unpolish(self.notice_banner)
        self.notice_banner.style().polish(self.notice_banner)
        self.notice_banner.setVisible(True)
        self._notice_timer.start(timeout)

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sidebar")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(10)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(6, 0, 4, 4)
        mark = QLabel("✦")
        mark.setObjectName("brandMark")
        brand = QLabel("CODEX KOSTYL")
        brand.setObjectName("brandName")
        brand_row.addWidget(mark)
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)

        self.new_chat_button = ShortcutPushButton("Новый чат", "Ctrl+K")
        self.new_chat_button.setObjectName("newChatButton")
        self.new_chat_button.setIcon(asset_icon("new-chat.svg"))
        self.new_chat_button.setAccessibleName("Создать новый чат")
        self.new_chat_button.setToolTip("Создать новый чат · Ctrl+K")
        self.new_chat_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_chat_shortcut_label = self.new_chat_button.shortcut_label
        self.thread_search = QLineEdit()
        self.thread_search.setObjectName("threadSearch")
        self.thread_search.setPlaceholderText("Поиск по чатам…")
        self.thread_search.setClearButtonEnabled(True)
        self.thread_search.setAccessibleName("Поиск по истории чатов")
        self.thread_list = QListWidget()
        self.thread_list.setObjectName("threadList")
        self.thread_list.setSpacing(3)
        self.thread_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.new_chat_button)
        section = QLabel("НЕДАВНИЕ ЧАТЫ")
        section.setObjectName("sectionLabel")
        layout.addWidget(section)
        layout.addWidget(self.thread_search)
        layout.addWidget(self.thread_list, 1)
        self.account_button = QPushButton("  ◉   Аккаунт")
        self.account_button.setObjectName("accountButton")
        layout.addWidget(self.account_button)
        self.new_chat_button.clicked.connect(self._new_chat)
        self.thread_search.textChanged.connect(self._filter_threads)
        self.thread_list.itemClicked.connect(self._thread_activated)
        self.account_button.clicked.connect(self._account_menu)
        return panel

    def _build_chat(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("chatPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        topbar = QWidget()
        topbar.setObjectName("topbar")
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.setContentsMargins(16, 13, 22, 13)
        self.sidebar_toggle = QToolButton()
        self.sidebar_toggle.setObjectName("sidebarToggle")
        self.sidebar_toggle.setIcon(asset_icon("sidebar.svg"))
        self.sidebar_toggle.setText("Ctrl+B")
        self.sidebar_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.sidebar_toggle.setToolTip("Скрыть или показать боковую панель · Ctrl+B")
        self.sidebar_toggle.setAccessibleName("Переключить боковую панель")
        self.sidebar_toggle.setFixedSize(76, 32)
        self.sidebar_toggle.clicked.connect(self._toggle_sidebar)
        topbar_layout.addWidget(self.sidebar_toggle)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.chat_title = QLabel("Новый чат")
        self.chat_title.setObjectName("chatTitle")
        self.chat_context = QLabel("Выберите рабочую папку")
        self.chat_context.setObjectName("chatContext")
        titles.addWidget(self.chat_title)
        titles.addWidget(self.chat_context)
        topbar_layout.addLayout(titles)
        topbar_layout.addStretch(1)
        self.agent_combo = QComboBox()
        self.agent_combo.setObjectName("optionCombo")
        self.agent_combo.setMinimumWidth(110)
        self.agent_combo.setAccessibleName("AI-агент")
        self.agent_combo.setToolTip("Активный AI-агент")
        topbar_layout.addWidget(self.agent_combo)
        self.header_status = QLabel("●  Готов")
        self.header_status.setObjectName("readyStatus")
        topbar_layout.addWidget(self.header_status)
        layout.addWidget(topbar)

        self.model_combo = QComboBox(panel)
        self.model_combo.setObjectName("optionCombo")
        self.model_combo.setMinimumWidth(160)
        self.model_combo.setAccessibleName("Модель AI-агента")
        self.model_combo.setVisible(False)
        self.effort_combo = QComboBox(panel)
        self.effort_combo.setObjectName("optionCombo")
        self.effort_combo.setAccessibleName("Глубина рассуждений")
        self.effort_combo.setVisible(False)
        self.access_combo = ShortcutComboBox("Ctrl+M")
        self.access_combo.setObjectName("accessCombo")
        self.access_combo.setAccessibleName("Режим агента")
        self.access_combo.setMinimumWidth(180)
        self.access_shortcut_label = self.access_combo.shortcut_label
        self.access_combo.popupRequested.connect(self._show_access_mode_menu)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("conversationScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(STREAM_RENDER_INTERVAL_MS)
        self._scroll_timer.timeout.connect(self._apply_scroll_bottom)
        scroll_bar = self.scroll.verticalScrollBar()
        scroll_bar.valueChanged.connect(self._scroll_value_changed)
        scroll_bar.rangeChanged.connect(self._scroll_range_changed)
        self.scroll.viewport().installEventFilter(self)
        self.scroll_down_button = QToolButton(self.scroll.viewport())
        self.scroll_down_button.setObjectName("scrollDownButton")
        self.scroll_down_button.setText("↓")
        self.scroll_down_button.setToolTip("Прокрутить к последнему сообщению")
        self.scroll_down_button.setFixedSize(38, 38)
        self.scroll_down_button.setVisible(False)
        self.scroll_down_button.clicked.connect(self._force_scroll_bottom)
        self.timeline = QWidget()
        self.timeline.setObjectName("timeline")
        timeline_outer = QHBoxLayout(self.timeline)
        timeline_outer.setContentsMargins(28, 0, 28, 0)
        timeline_outer.addStretch(1)
        self.timeline_column = QWidget()
        self.timeline_column.setObjectName("timelineColumn")
        self.timeline_column.setMaximumWidth(860)
        self.timeline_column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.timeline_layout = QVBoxLayout(self.timeline_column)
        self.timeline_layout.setContentsMargins(0, 30, 0, 24)
        self.timeline_layout.setSpacing(14)
        self.timeline_layout.addStretch(1)
        timeline_outer.addWidget(self.timeline_column, 10)
        timeline_outer.addStretch(1)
        self.scroll.setWidget(self.timeline)
        self.scroll_down_button.raise_()
        layout.addWidget(self.scroll, 1)

        composer_shell = QWidget()
        composer_shell.setObjectName("composerShell")
        shell_layout = QVBoxLayout(composer_shell)
        shell_layout.setContentsMargins(28, 10, 28, 18)
        shell_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        composer_area = QWidget()
        composer_area.setObjectName("composerArea")
        composer_area.setMaximumWidth(860)
        composer_area.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        composer_area_layout = QVBoxLayout(composer_area)
        composer_area_layout.setContentsMargins(0, 0, 0, 0)
        composer_area_layout.setSpacing(6)

        composer_panel = QFrame()
        composer_panel.setObjectName("composerPanel")
        composer_panel.setMaximumWidth(860)
        composer_panel_layout = QVBoxLayout(composer_panel)
        composer_panel_layout.setContentsMargins(14, 8, 12, 10)
        composer_panel_layout.setSpacing(4)

        self.project_bubble = QFrame()
        self.project_bubble.setObjectName("projectBubble")
        project_layout = QHBoxLayout(self.project_bubble)
        project_layout.setContentsMargins(8, 2, 3, 2)
        project_layout.setSpacing(3)
        project_icon = QLabel("Проект")
        project_icon.setObjectName("projectBubbleIcon")
        project_layout.addWidget(project_icon)
        self.project_combo = QComboBox()
        self.project_combo.setObjectName("projectCombo")
        self.project_combo.setToolTip("Рабочая папка для нового чата")
        self.project_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.project_combo.setMinimumContentsLength(12)
        self.project_combo.setMaximumWidth(300)
        project_layout.addWidget(self.project_combo)
        self.add_project_button = QToolButton()
        add_project = self.add_project_button
        add_project.setObjectName("projectBubbleButton")
        add_project.setIcon(asset_icon("folder-plus.svg"))
        add_project.setToolTip("Добавить рабочую папку")
        add_project.setAccessibleName("Добавить рабочую папку")
        add_project.setFixedSize(25, 25)
        project_layout.addWidget(add_project)
        composer_area_layout.addWidget(
            self.project_bubble,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )

        self.slash_panel = SlashCommandPanel()
        composer_area_layout.addWidget(self.slash_panel)

        self.notice_banner = QFrame()
        self.notice_banner.setObjectName("noticeBanner")
        notice_layout = QHBoxLayout(self.notice_banner)
        notice_layout.setContentsMargins(11, 7, 11, 7)
        self.notice_label = QLabel()
        self.notice_label.setObjectName("noticeLabel")
        self.notice_label.setWordWrap(True)
        notice_layout.addWidget(self.notice_label)
        self.notice_banner.setVisible(False)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(lambda: self.notice_banner.setVisible(False))
        composer_area_layout.addWidget(self.notice_banner)

        self.queue_edit_banner = QFrame()
        self.queue_edit_banner.setObjectName("queueEditBanner")
        queue_edit_layout = QHBoxLayout(self.queue_edit_banner)
        queue_edit_layout.setContentsMargins(11, 8, 8, 8)
        queue_edit_layout.setSpacing(9)
        queue_edit_icon = QLabel("✎")
        queue_edit_icon.setObjectName("queueEditIcon")
        queue_edit_layout.addWidget(queue_edit_icon)
        queue_edit_text = QVBoxLayout()
        queue_edit_text.setSpacing(1)
        queue_edit_title = QLabel("РЕДАКТИРОВАНИЕ СООБЩЕНИЯ ИЗ ОЧЕРЕДИ")
        queue_edit_title.setObjectName("queueEditTitle")
        self.queue_edit_detail = QLabel()
        self.queue_edit_detail.setObjectName("queueEditDetail")
        queue_edit_text.addWidget(queue_edit_title)
        queue_edit_text.addWidget(self.queue_edit_detail)
        queue_edit_layout.addLayout(queue_edit_text, 1)
        self.queue_edit_cancel_button = QPushButton("Отменить")
        self.queue_edit_cancel_button.setObjectName("queueEditCancel")
        self.queue_edit_cancel_button.setToolTip(
            "Отменить редактирование и вернуть предыдущий черновик"
        )
        self.queue_edit_cancel_button.clicked.connect(self._cancel_queue_edit)
        queue_edit_layout.addWidget(self.queue_edit_cancel_button)
        self.queue_edit_banner.setVisible(False)
        composer_panel_layout.addWidget(self.queue_edit_banner)

        self.attachment_row = QHBoxLayout()
        self.attachment_row.addStretch(1)
        composer_panel_layout.addLayout(self.attachment_row)

        self.composer = Composer()
        composer_panel_layout.addWidget(self.composer)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(4)
        self.attach_button = QPushButton()
        attach = self.attach_button
        attach.setObjectName("attachButton")
        attach.setIcon(asset_icon("attach.svg"))
        attach.setToolTip("Прикрепить изображение или файл")
        attach.setAccessibleName("Прикрепить файл")
        attach.setFixedSize(32, 32)
        controls_row.addWidget(attach)
        controls_row.addWidget(self.access_combo)
        controls_row.addStretch(1)

        self.context_usage_widget = QWidget()
        self.context_usage_widget.setObjectName("contextUsage")
        context_layout = QHBoxLayout(self.context_usage_widget)
        context_layout.setContentsMargins(5, 0, 5, 0)
        context_layout.setSpacing(7)
        self.context_usage_label = QLabel("Контекст —")
        self.context_usage_label.setObjectName("contextUsageLabel")
        self.context_usage_bar = QProgressBar()
        self.context_usage_bar.setObjectName("contextUsageBar")
        self.context_usage_bar.setRange(0, 100)
        self.context_usage_bar.setValue(0)
        self.context_usage_bar.setTextVisible(False)
        self.context_usage_bar.setFixedSize(82, 5)
        context_layout.addWidget(self.context_usage_label)
        context_layout.addWidget(self.context_usage_bar)
        self.context_usage_widget.setVisible(False)
        controls_row.addWidget(self.context_usage_widget)

        self.weekly_limit = QWidget()
        self.weekly_limit.setObjectName("weeklyLimit")
        limit_layout = QHBoxLayout(self.weekly_limit)
        limit_layout.setContentsMargins(5, 0, 5, 0)
        limit_layout.setSpacing(7)
        self.weekly_limit_label = QLabel("Неделя —")
        self.weekly_limit_label.setObjectName("weeklyLimitLabel")
        self.weekly_limit_bar = QProgressBar()
        self.weekly_limit_bar.setObjectName("weeklyLimitProgress")
        self.weekly_limit_bar.setRange(0, 100)
        self.weekly_limit_bar.setValue(0)
        self.weekly_limit_bar.setTextVisible(False)
        self.weekly_limit_bar.setFixedSize(66, 5)
        self.weekly_limit_bar.setProperty("level", "unavailable")
        limit_layout.addWidget(self.weekly_limit_label)
        limit_layout.addWidget(self.weekly_limit_bar)
        self.weekly_limit.setToolTip("Недельный лимит доступен при входе через ChatGPT")
        controls_row.addWidget(self.weekly_limit)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setIcon(asset_icon("settings.svg"))
        self.settings_button.setText("Ctrl+I")
        self.settings_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.settings_button.setToolTip(
            "Выбрать модель и усилие рассуждений · Ctrl+I"
        )
        self.settings_button.setAccessibleName("Настройки запроса")
        self.settings_button.setFixedSize(76, 32)
        controls_row.addWidget(self.settings_button)

        self.send_button = QPushButton()
        self.send_button.setObjectName("sendButton")
        self.send_button.setIcon(asset_icon("send.svg"))
        self.send_button.setToolTip("Отправить · Enter (Shift+Enter — новая строка)")
        self.send_button.setAccessibleName("Отправить сообщение")
        self.send_button.setFixedSize(34, 34)
        self.stop_button = QPushButton()
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setIcon(asset_icon("stop.svg"))
        self.stop_button.setToolTip("Остановить выполнение · Esc")
        self.stop_button.setAccessibleName("Остановить выполнение")
        self.stop_button.setFixedSize(34, 34)
        self.stop_button.setVisible(False)
        controls_row.addWidget(self.send_button)
        controls_row.addWidget(self.stop_button)
        composer_panel_layout.addLayout(controls_row)

        self.queue_panel = QFrame()
        self.queue_panel.setObjectName("queuePanel")
        self.queue_panel.setMaximumWidth(860)
        queue_layout = QVBoxLayout(self.queue_panel)
        queue_layout.setContentsMargins(14, 10, 14, 10)
        queue_layout.setSpacing(6)
        queue_header = QHBoxLayout()
        self.queue_label = QLabel("Очередь")
        self.queue_label.setObjectName("queueTitle")
        self.queue_resume_button = QPushButton("Продолжить")
        self.queue_resume_button.setObjectName("queueActionButton")
        self.queue_clear_button = QPushButton("Очистить")
        self.queue_clear_button.setObjectName("queueActionButton")
        queue_header.addWidget(self.queue_label)
        queue_header.addStretch(1)
        queue_header.addWidget(self.queue_resume_button)
        queue_header.addWidget(self.queue_clear_button)
        queue_layout.addLayout(queue_header)
        self.queue_items_layout = QVBoxLayout()
        self.queue_items_layout.setContentsMargins(0, 0, 0, 0)
        self.queue_items_layout.setSpacing(3)
        queue_layout.addLayout(self.queue_items_layout)
        self.queue_panel.setVisible(False)

        self.approval_card = InlineApprovalCard()
        self.approval_card.setVisible(False)
        self.approval_card.decisionSelected.connect(self._answer_inline_approval)
        self.user_input_card = InlineUserInputCard()
        self.user_input_card.setVisible(False)
        self.user_input_card.submitted.connect(self._answer_inline_user_input)
        self.user_input_card.canceled.connect(self._cancel_inline_user_input)
        self.plan_confirmation_card = PlanConfirmationCard()
        self.plan_confirmation_card.setVisible(False)
        self.plan_confirmation_card.implementRequested.connect(self._implement_plan)
        self.plan_confirmation_card.dismissed.connect(self._dismiss_plan_confirmation)
        shell_layout.addWidget(self.approval_card)
        shell_layout.addWidget(self.user_input_card)
        shell_layout.addWidget(self.plan_confirmation_card)
        shell_layout.addWidget(self.queue_panel)
        composer_area_layout.addWidget(composer_panel)
        shell_layout.addWidget(composer_area)

        self.composer_hint = QLabel("Codex может ошибаться. Проверяйте команды и изменения файлов.")
        self.composer_hint.setObjectName("composerHint")
        self.composer_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        shell_layout.addWidget(self.composer_hint)
        layout.addWidget(composer_shell)

        attach.clicked.connect(self._choose_attachments)
        add_project.clicked.connect(self._add_project)
        self.project_combo.currentIndexChanged.connect(self._project_changed)
        self.composer.filesDropped.connect(self._add_attachments)
        self.composer.sendRequested.connect(self._send)
        self.composer.textChanged.connect(self._composer_text_changed)
        self.composer.cursorPositionChanged.connect(self._update_slash_panel)
        self.composer.slashNavigate.connect(self._navigate_slash_commands)
        self.composer.slashComplete.connect(self._complete_slash_command)
        self.composer.slashActivate.connect(self.slash_panel.activate_selected)
        self.composer.slashDismiss.connect(self._dismiss_slash_panel)
        self.composer.newChatRequested.connect(self._shortcut_new_chat)
        self.composer.accessModeRequested.connect(self._show_access_mode_menu)
        self.composer.attachmentRequested.connect(self._shortcut_choose_attachments)
        self.composer.requestSettingsRequested.connect(
            self._show_request_settings_menu
        )
        self.composer.latestActivityRequested.connect(
            self._toggle_latest_activity
        )
        self.composer.stopRequested.connect(self._confirm_interrupt)
        self.slash_panel.commandActivated.connect(self._activate_slash_command)
        self.send_button.clicked.connect(self._send)
        self.stop_button.clicked.connect(self._confirm_interrupt)
        self.queue_resume_button.clicked.connect(self._resume_queue)
        self.queue_clear_button.clicked.connect(self._clear_message_queue)
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        self.effort_combo.currentIndexChanged.connect(self._effort_changed)
        self.access_combo.currentIndexChanged.connect(self._access_changed)
        self.settings_button.clicked.connect(self._show_request_settings_menu)
        self.agent_combo.currentIndexChanged.connect(self._agent_changed)
        return panel

    def _connect_service(self) -> None:
        self.service.ready.connect(
            lambda: self.statusBar().showMessage(
                f"{self._agent_name()} подключен", 3000
            )
        )
        self.service.modelsUpdated.connect(self._set_models)
        self.service.accountUpdated.connect(self._set_account)
        self.service.rateLimitsUpdated.connect(self._set_rate_limits)
        self.service.threadsUpdated.connect(self._set_threads)
        self.service.threadLoaded.connect(self._render_thread)
        self.service.itemUpdated.connect(self._upsert_item)
        self.service.agentDelta.connect(self._agent_delta)
        self.service.reasoningDelta.connect(self._reasoning_delta)
        self.service.commandDelta.connect(self._command_delta)
        self.service.planDelta.connect(self._plan_delta)
        self.service.turnPlanUpdated.connect(self._turn_plan_updated)
        self.service.tokenUsageUpdated.connect(self._set_context_usage)
        self.service.turnStateChanged.connect(self._turn_state)
        self.service.errorOccurred.connect(self._show_error)
        self.service.loginStarted.connect(self._login_started)
        permission_requested = getattr(self.service, "permissionRequested", None)
        if permission_requested is not None:
            permission_requested.connect(self._permission_requested)
        else:
            self.service.approvalRequested.connect(self._approval_requested)
        self.service.userInputRequested.connect(self._user_input_requested)
        self.service.serverRequestResolved.connect(self._server_request_resolved)
        current_thread_changed = getattr(self.service, "currentThreadChanged", None)
        if current_thread_changed is not None:
            current_thread_changed.connect(lambda _thread_id: self._update_slash_panel())
        agents_changed = getattr(self.service, "agentsChanged", None)
        if agents_changed is not None:
            agents_changed.connect(self._populate_agents)
        active_agent_changed = getattr(self.service, "activeAgentChanged", None)
        if active_agent_changed is not None:
            active_agent_changed.connect(self._active_agent_changed)
        availability_changed = getattr(self.service, "availabilityChanged", None)
        if availability_changed is not None:
            availability_changed.connect(self._agent_availability_changed)
        manifest_updated = getattr(self.service, "manifestUpdated", None)
        if manifest_updated is not None:
            manifest_updated.connect(self._manifest_updated)
        config_updated = getattr(self.service, "configOptionsUpdated", None)
        if config_updated is not None:
            config_updated.connect(self._set_config_options)
        feature_states_changed = getattr(self.service, "featureStatesChanged", None)
        if feature_states_changed is not None:
            feature_states_changed.connect(lambda _states: self._apply_agent_capabilities())

    def _agent_id(self) -> str:
        return str(getattr(self.service, "active_agent_id", "") or "codex")

    def _agent_name(self) -> str:
        descriptor = getattr(self.service, "descriptor", None)
        return str(getattr(descriptor, "display_name", "") or "Codex")

    def _agent_setting(self, key: str, default: str = "") -> str:
        getter = getattr(self.settings, "agent_get", None)
        if callable(getter):
            return str(getter(self._agent_id(), key, default))
        return self.settings.get(key, default)

    def _set_agent_setting(self, key: str, value: object) -> None:
        setter = getattr(self.settings, "agent_set", None)
        if callable(setter):
            setter(self._agent_id(), key, value)
        else:
            self.settings.set(key, value)

    def _populate_agents(self, descriptors: object | None = None) -> None:
        rows = descriptors
        if rows is None:
            rows = getattr(self.service, "available_agents", None)
        agents = [item for item in (rows or []) if isinstance(item, AgentDescriptor)]
        if not agents:
            agents = [AgentDescriptor("codex", "Codex", "codex")]
        selected = str(
            getattr(self.service, "active_agent_id", "")
            or getattr(self.settings, "selected_agent_id", "")
            or "codex"
        )
        self.agent_combo.blockSignals(True)
        self.agent_combo.clear()
        for descriptor in agents:
            self.agent_combo.addItem(descriptor.display_name, descriptor.id)
            index = self.agent_combo.count() - 1
            self.agent_combo.setItemData(index, descriptor.description, Qt.ItemDataRole.ToolTipRole)
            availability_for = getattr(self.service, "availability_for", None)
            if callable(availability_for):
                availability = availability_for(descriptor.id)
                model_item = self.agent_combo.model().item(index)
                if model_item is not None and not availability.available:
                    model_item.setEnabled(False)
                    model_item.setToolTip(availability.error)
        index = self.agent_combo.findData(selected)
        self.agent_combo.setCurrentIndex(max(0, index))
        self.agent_combo.blockSignals(False)

    def _agent_changed(self, index: int) -> None:
        agent_id = str(self.agent_combo.itemData(index) or "")
        activate = getattr(self.service, "activate", None)
        if not agent_id or not callable(activate):
            return
        if self._turn_active or self._message_queue or self._queue_action_pending:
            self._show_notice(
                "Сначала завершите ход и очистите очередь перед сменой агента.",
                "warning",
            )
            self._populate_agents()
            return
        if activate(agent_id):
            if hasattr(type(self.settings), "selected_agent_id"):
                self.settings.selected_agent_id = agent_id
            else:
                self.settings.set("selected_agent_id", agent_id)
        else:
            # Activation is atomic: keep the selector aligned with the driver
            # that remains active when a candidate is unavailable.
            self._populate_agents()

    def _active_agent_changed(self, agent_id: str) -> None:
        self._run_mode_context = None
        self._pending_run_mode_id = ""
        index = self.agent_combo.findData(agent_id)
        if index >= 0 and index != self.agent_combo.currentIndex():
            self.agent_combo.blockSignals(True)
            self.agent_combo.setCurrentIndex(index)
            self.agent_combo.blockSignals(False)
        name = self._agent_name()
        self.composer.setPlaceholderText(
            f"Попросите {name} изменить код, найти ошибку или объяснить проект…"
        )
        self.composer_hint.setText(
            f"{name} может ошибаться. Проверяйте команды и изменения файлов."
        )
        manifest = getattr(self.service, "manifest", None)
        self.agent_manifest = manifest if isinstance(manifest, AgentManifest) else AgentManifest()
        self.agent_config_options = self.agent_manifest.config_options
        self.models = []
        self.model_combo.clear()
        self.effort_combo.clear()
        self._reset_context_usage()
        self._set_run_modes(
            self.agent_manifest.run_modes,
            self.agent_manifest.current_run_mode_id,
        )
        self._apply_agent_capabilities()

    def _agent_availability_changed(self, availability: object) -> None:
        if not isinstance(availability, AgentAvailability):
            return
        if availability.available:
            detail = f" · {availability.version}" if availability.version else ""
            self.agent_combo.setToolTip(f"{self._agent_name()} доступен{detail}")
            self.composer.setEnabled(not self._turn_active)
        else:
            self.agent_combo.setToolTip(availability.error)
            self.composer.setEnabled(False)

    def _manifest_updated(self, manifest: object) -> None:
        if not isinstance(manifest, AgentManifest):
            return
        self.agent_manifest = manifest
        self._set_run_modes(manifest.run_modes, manifest.current_run_mode_id)
        self._set_config_options(manifest.config_options)
        self._apply_agent_capabilities()
        self._update_slash_panel()

    def _feature_state(
        self,
        feature: FeatureId,
        legacy_supported: bool = False,
    ) -> FeatureState:
        getter = getattr(self.service, "feature_state", None)
        if callable(getter):
            state = getter(feature)
            if isinstance(state, FeatureState):
                return state
        return FeatureState(
            legacy_supported,
            legacy_supported,
            "" if legacy_supported else f"Не поддерживается агентом {self._agent_name()}",
        )

    def _capabilities(self) -> AgentCapabilities:
        value = getattr(self.service, "capabilities", None)
        if isinstance(value, AgentCapabilities):
            return value
        # Legacy/fake services used by tests represent the fully featured Codex UI.
        return AgentCapabilities(*([True] * 15))

    def _apply_agent_capabilities(self) -> None:
        capabilities = self._capabilities()
        name = self._agent_name()
        model_state = self._feature_state(FeatureId.CONFIG_MODEL, capabilities.models)
        thought_state = self._feature_state(
            FeatureId.CONFIG_THOUGHT_LEVEL,
            capabilities.reasoning_effort,
        )
        settings_supported = model_state.supported or thought_state.supported or bool(
            self.agent_config_options
        )
        settings_enabled = (
            (model_state.enabled or thought_state.enabled or bool(self.agent_config_options))
            and self._editing_queued_message is None
        )
        self.settings_button.setEnabled(settings_enabled)
        if not self.settings_button.isEnabled():
            reason = model_state.reason or thought_state.reason
            self.settings_button.setToolTip(
                reason if settings_supported and reason else f"Настройки модели не поддерживаются агентом {name}"
            )
        else:
            self.settings_button.setToolTip(
                "Настройки текущего запроса · Ctrl+I"
            )
        access_state = self._feature_state(FeatureId.ACCESS_MODES, capabilities.access_modes)
        has_modes = any(
            str(self.access_combo.itemData(index) or "")
            for index in range(self.access_combo.count())
        )
        self.access_combo.setEnabled(
            access_state.enabled and has_modes and self._editing_queued_message is None
        )
        self.access_shortcut_label.setEnabled(self.access_combo.isEnabled())
        self._refresh_access_shortcut_style()
        if access_state.enabled and has_modes:
            access_tip = self.access_combo.toolTip() or "Режим агента для следующего запроса"
            if "Ctrl+M" not in access_tip:
                access_tip += "\nCtrl+M — выбрать режим"
            self.access_combo.setToolTip(access_tip)
        else:
            self.access_combo.setToolTip(
                access_state.reason or f"{name} не объявил доступные режимы"
            )
        attachment_state = self._feature_state(FeatureId.INPUT_FILES, capabilities.attachments)
        self.attach_button.setEnabled(
            attachment_state.enabled and self._editing_queued_message is None
        )
        self.attach_button.setToolTip(
            "Добавить вложение · Ctrl+O"
            if attachment_state.enabled
            else attachment_state.reason
        )
        quota_state = self._feature_state(FeatureId.USAGE_QUOTA, capabilities.rate_limits)
        self.weekly_limit.setEnabled(quota_state.enabled)
        if not quota_state.enabled:
            self.weekly_limit_label.setText("Лимит —")
            self.weekly_limit.setToolTip(quota_state.reason)
        context_state = self._feature_state(FeatureId.USAGE_CONTEXT, capabilities.context_usage)
        self.context_usage_widget.setEnabled(context_state.enabled)
        if not context_state.enabled:
            self.context_usage_label.setText("Контекст —")
            self.context_usage_widget.setToolTip(context_state.reason)
            self.context_usage_widget.setVisible(True)
        self.account_button.setToolTip(
            "Управление аккаунтом и исполняемым файлом агента"
            if capabilities.authentication
            else f"Авторизация не поддерживается агентом {name}; можно изменить путь к CLI"
        )

    def _load_settings(self) -> None:
        self._populate_agents()
        self._set_run_modes(
            self.agent_manifest.run_modes,
            self.agent_manifest.current_run_mode_id,
        )
        for path in self.settings.projects:
            self.project_combo.addItem(Path(path).name, path)
        saved_project = self.settings.get("last_project")
        index = self.project_combo.findData(saved_project)
        if index >= 0:
            self.project_combo.setCurrentIndex(index)
        self._refresh_access_style()
        geometry, state = self.settings.restore_geometry()
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
        if not state.isEmpty():
            self.restoreState(state)
        self._sidebar_user_hidden = self.settings.get(
            "sidebar_hidden", "false"
        ).lower() in {"1", "true", "yes"}
        if self._sidebar_user_hidden:
            self._set_sidebar_visible(False)
        self._apply_agent_capabilities()

    def _add_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите рабочую папку")
        if not path:
            return
        path = str(Path(path).resolve())
        index = self.project_combo.findData(path)
        if index < 0:
            self.project_combo.addItem(Path(path).name, path)
            projects = self.settings.projects
            projects.append(path)
            self.settings.projects = projects
            index = self.project_combo.count() - 1
        if (
            self.project_combo.currentIndex() == index
            and getattr(self.service, "current_project", "") != path
        ):
            self._project_changed(index)
        elif self.project_combo.currentIndex() != index:
            self.project_combo.setCurrentIndex(index)

    def _project_changed(self, index: int) -> None:
        path = self.project_combo.itemData(index)
        if not path:
            return
        self.chat_context.setText(str(path))
        self.settings.set("last_project", path)
        self._clear_message_queue()
        self._reset_context_usage()
        self.service.set_project(path)
        self.service.prepare_new_thread()
        self._clear_timeline("Выберите сохраненный чат или начните новый.")

    def _set_threads(self, threads: list[ThreadSummary]) -> None:
        selected = self.service.current_thread_id
        self.thread_list.blockSignals(True)
        self.thread_list.clear()
        for thread in threads:
            folder = Path(thread.cwd).name if thread.cwd else "Без рабочей папки"
            metadata = folder
            if thread.updated_at > 0:
                updated = QDateTime.fromSecsSinceEpoch(thread.updated_at).toLocalTime()
                metadata += "  ·  " + updated.toString("dd MMM, HH:mm")
            active = thread.status not in {"", "notLoaded", "idle", "completed"}
            title = ("●  " if active else "") + thread.title
            item = QListWidgetItem(f"{title}\n{metadata}")
            item.setSizeHint(QSize(0, 54))
            item.setData(Qt.ItemDataRole.UserRole, thread.id)
            item.setData(int(Qt.ItemDataRole.UserRole) + 1, thread.cwd)
            item.setData(
                THREAD_SEARCH_ROLE,
                f"{thread.title} {folder} {thread.cwd} {localized_status(thread.status)}".casefold(),
            )
            item.setData(THREAD_TITLE_ROLE, thread.title)
            item.setToolTip(f"{thread.cwd}\n{thread.id}")
            self.thread_list.addItem(item)
            if thread.id == selected:
                self.thread_list.setCurrentItem(item)
        self.thread_list.blockSignals(False)
        self._filter_threads(self.thread_search.text())

    def _filter_threads(self, query: str) -> None:
        normalized = " ".join(query.casefold().split())
        for index in range(self.thread_list.count()):
            item = self.thread_list.item(index)
            haystack = str(item.data(THREAD_SEARCH_ROLE) or item.text()).casefold()
            item.setHidden(bool(normalized) and normalized not in haystack)

    def _thread_activated(self, item: QListWidgetItem | None) -> None:
        if item:
            thread_id = item.data(Qt.ItemDataRole.UserRole)
            if thread_id and thread_id != self.service.current_thread_id:
                cwd = str(item.data(int(Qt.ItemDataRole.UserRole) + 1) or "")
                if not self._switch_to_thread_project(cwd):
                    return
                self.chat_title.setText(str(item.data(THREAD_TITLE_ROLE) or item.text().splitlines()[0]))
                self._loading_existing_session = True
                self._pending_run_mode_id = ""
                self.service.open_thread(thread_id)

    def _switch_to_thread_project(self, cwd: str) -> bool:
        if not cwd:
            self._show_error("У сохраненного чата не указана рабочая директория")
            return False
        path = Path(cwd).expanduser()
        if not path.is_dir():
            self._show_error(f"Рабочая директория чата больше не существует:\n{path}")
            return False
        resolved = str(path.resolve())
        self.project_combo.blockSignals(True)
        index = self.project_combo.findData(resolved)
        if index < 0:
            self.project_combo.addItem(path.name or resolved, resolved)
            projects = self.settings.projects
            projects.append(resolved)
            self.settings.projects = projects
            index = self.project_combo.count() - 1
        self.project_combo.setCurrentIndex(index)
        self.project_combo.blockSignals(False)
        self.settings.set("last_project", resolved)
        self._clear_message_queue()
        self._reset_context_usage()
        self.service.set_project(resolved)
        return True

    def _new_chat(self) -> None:
        self._prepare_new_chat(clear_queue=True)

    def _prepare_new_chat(self, clear_queue: bool) -> bool:
        if not self.service.current_project:
            self._add_project()
            if not self.service.current_project:
                return False
        self.service.prepare_new_thread()
        if clear_queue:
            self._clear_message_queue()
        self._reset_context_usage()
        self.thread_list.clearSelection()
        self.chat_title.setText("Новый чат")
        self.chat_context.setText(self.service.current_project or "Рабочая папка не выбрана")
        self._clear_timeline("Новый чат готов. Опишите задачу ниже.")
        self.composer.setFocus()
        self._update_slash_panel()
        return True

    def _set_models(self, models: list[ModelInfo]) -> None:
        self.models = models
        saved = self._agent_setting("model")
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in models:
            self.model_combo.addItem(model.display_name, model.id)
        index = self.model_combo.findData(saved)
        if index < 0:
            index = next((i for i, model in enumerate(models) if model.is_default), 0)
        if self.model_combo.count():
            self.model_combo.setCurrentIndex(index)
        self.model_combo.blockSignals(False)
        self._model_changed(self.model_combo.currentIndex())

    @staticmethod
    def _legacy_codex_run_modes() -> tuple[AgentRunMode, ...]:
        """Compatibility modes for old services that only expose booleans."""
        return (
            AgentRunMode(
                AccessMode.READ_ONLY.value,
                AccessMode.READ_ONLY.title,
                "Codex не сможет изменять файлы",
                "safe",
            ),
            AgentRunMode(
                AccessMode.WORKSPACE_WRITE.value,
                AccessMode.WORKSPACE_WRITE.title,
                "Изменения разрешены только внутри проекта",
                "workspace",
            ),
            AgentRunMode(
                AccessMode.FULL_ACCESS.value,
                AccessMode.FULL_ACCESS.title,
                "Команды выполняются без дополнительных подтверждений",
                "danger",
                True,
            ),
            AgentRunMode(
                PLAN_MODE_VALUE,
                "Режим планирования",
                "Анализ без изменения файлов",
                "plan",
            ),
        )

    def _set_run_modes(self, modes: object, current_mode_id: str = "") -> None:
        raw_modes = modes if isinstance(modes, (list, tuple)) else ()
        rows = tuple(
            mode for mode in raw_modes
            if isinstance(mode, AgentRunMode)
        )
        if not rows and self._capabilities().access_modes:
            rows = self._legacy_codex_run_modes()

        default_id = current_mode_id or (
            AccessMode.WORKSPACE_WRITE.value
            if any(mode.id == AccessMode.WORKSPACE_WRITE.value for mode in rows)
            else (rows[0].id if rows else "")
        )
        saved_id = self._agent_setting("run_mode", default_id)
        supported_ids = {mode.id for mode in rows}
        context = (
            self._agent_id(),
            str(getattr(self.service, "current_session_id", "") or ""),
        )
        new_context = bool(rows) and context != self._run_mode_context
        if self._pending_run_mode_id in supported_ids:
            selected_id = self._pending_run_mode_id
        elif self._loading_existing_session and current_mode_id in supported_ids:
            selected_id = current_mode_id
        elif new_context and saved_id in supported_ids:
            selected_id = saved_id
        elif current_mode_id in supported_ids:
            selected_id = current_mode_id
        elif saved_id in supported_ids:
            selected_id = saved_id
        else:
            selected_id = default_id if default_id in supported_ids else ""

        self.access_combo.blockSignals(True)
        self.access_combo.clear()
        if rows:
            for mode in rows:
                self.access_combo.addItem(mode.title, mode.id)
                index = self.access_combo.count() - 1
                self.access_combo.setItemData(
                    index,
                    mode.description,
                    Qt.ItemDataRole.ToolTipRole,
                )
            index = self.access_combo.findData(selected_id)
            self.access_combo.setCurrentIndex(max(0, index))
        else:
            self.access_combo.addItem("Режим по умолчанию", "")
        self.access_combo.blockSignals(False)

        if (
            rows
            and new_context
            and not self._loading_existing_session
            and selected_id
            and selected_id != current_mode_id
            and not getattr(self.service, "current_run_id", "")
        ):
            setter = getattr(self.service, "set_run_mode", None)
            if callable(setter):
                setter(selected_id)
        if rows:
            self._run_mode_context = context
        else:
            self._run_mode_context = None
        if current_mode_id and current_mode_id == self._pending_run_mode_id:
            self._pending_run_mode_id = ""
        self._loading_existing_session = False
        self._refresh_access_style()

    def _set_config_options(self, options: object) -> None:
        if not isinstance(options, (list, tuple)):
            return
        self.agent_config_options = tuple(
            option for option in options if isinstance(option, AgentConfigOption)
        )
        model_option = next(
            (option for option in self.agent_config_options if option.category == "model"),
            None,
        )
        thought_option = next(
            (
                option
                for option in self.agent_config_options
                if option.category == "thought_level"
            ),
            None,
        )
        efforts = [value.value for value in thought_option.values] if thought_option else []
        self.models = (
            [
                ModelInfo(
                    value.value,
                    value.label,
                    efforts=list(efforts),
                    default_effort=str(thought_option.current_value) if thought_option else None,
                    modalities={"text", "image"}
                    if self._feature_state(FeatureId.INPUT_IMAGES).supported
                    else {"text"},
                    is_default=value.value == str(model_option.current_value),
                )
                for value in model_option.values
            ]
            if model_option
            else []
        )
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in self.models:
            self.model_combo.addItem(model.display_name, model.id)
        if model_option:
            index = self.model_combo.findData(str(model_option.current_value))
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
        self.model_combo.blockSignals(False)
        self.effort_combo.blockSignals(True)
        self.effort_combo.clear()
        if thought_option:
            for value in thought_option.values:
                self.effort_combo.addItem(value.label, value.value)
            index = self.effort_combo.findData(str(thought_option.current_value))
            if index >= 0:
                self.effort_combo.setCurrentIndex(index)
        self.effort_combo.blockSignals(False)
        self._apply_agent_capabilities()

    def _current_agent_config(self) -> dict[str, str | bool]:
        config: dict[str, str | bool] = {}
        for option in self.agent_config_options:
            saved = self._agent_setting(f"config/{option.id}", str(option.current_value))
            if option.kind == "boolean":
                config[option.id] = saved.lower() in {"1", "true", "yes", "on"}
            else:
                config[option.id] = saved
        categories = {option.category for option in self.agent_config_options}
        if "model" not in categories and self.model_combo.currentData():
            config["model"] = str(self.model_combo.currentData())
        if "thought_level" not in categories and self.effort_combo.currentData():
            config["thought_level"] = str(self.effort_combo.currentData())
        return config

    def _set_config_value(self, option: AgentConfigOption, value: str | bool) -> None:
        self._set_agent_setting(f"config/{option.id}", value)
        setter = getattr(self.service, "set_config_option", None)
        if callable(setter):
            setter(option.id, value)

    def _build_request_settings_menu(self) -> NumberedChoiceMenu:
        menu = NumberedChoiceMenu(self)
        menu.setObjectName("requestSettingsMenu")
        arrow_path = str(
            files("codex_gui").joinpath("assets", "chevron-right.svg")
        ).replace("\\", "/")
        menu.setStyleSheet(
            f"""
            QMenu#requestSettingsMenu::item {{
                min-width: 170px;
                padding: 8px 36px 8px 12px;
            }}
            QMenu#requestSettingsMenu::right-arrow {{
                image: url("{arrow_path}");
                width: 12px;
                height: 12px;
                subcontrol-origin: padding;
                subcontrol-position: right center;
                right: 10px;
            }}
            """
        )

        selected_model = self._selected_model()
        model_title = selected_model.display_name if selected_model else "Модель"
        model_menu = NumberedChoiceMenu(
            f"1   {model_title}",
            menu,
        )
        model_menu.setObjectName("modelSettingsMenu")
        menu.addMenu(model_menu)
        for index, model in enumerate(self.models):
            prefix = f"{index + 1}   " if index < 9 else ""
            action = model_menu.addAction(prefix + model.display_name)
            action.setCheckable(True)
            action.setChecked(index == self.model_combo.currentIndex())
            action.triggered.connect(
                lambda _checked=False, target=index: self.model_combo.setCurrentIndex(target)
            )
        if not self.models:
            unavailable = model_menu.addAction("1   Модели ещё загружаются…")
            unavailable.setEnabled(False)

        current_effort = self.effort_combo.currentData()
        effort_menu = NumberedChoiceMenu(
            f"2   {effort_title(current_effort)}",
            menu,
        )
        effort_menu.setObjectName("effortSettingsMenu")
        menu.addMenu(effort_menu)
        for index in range(self.effort_combo.count()):
            value = self.effort_combo.itemData(index)
            prefix = f"{index + 1}   " if index < 9 else ""
            action = effort_menu.addAction(prefix + effort_title(value))
            action.setCheckable(True)
            action.setChecked(index == self.effort_combo.currentIndex())
            action.triggered.connect(
                lambda _checked=False, target=index: self.effort_combo.setCurrentIndex(target)
            )
        if not self.effort_combo.count():
            unavailable = effort_menu.addAction(
                "1   Недоступно для текущей модели"
            )
            unavailable.setEnabled(False)
        generic_menus: list[QMenu] = []
        for option in self.agent_config_options:
            if option.category in {"model", "thought_level", "mode"}:
                continue
            menu_number = len(menu.actions()) + 1
            title_prefix = f"{menu_number}   " if menu_number <= 9 else ""
            option_menu = NumberedChoiceMenu(title_prefix + option.name, menu)
            menu.addMenu(option_menu)
            generic_menus.append(option_menu)
            if option.kind == "boolean":
                for index, (label, value) in enumerate(
                    (("Включено", True), ("Выключено", False))
                ):
                    action = option_menu.addAction(f"{index + 1}   {label}")
                    action.setCheckable(True)
                    action.setChecked(option.current_value is value)
                    action.triggered.connect(
                        lambda _checked=False, target=option, selected=value: self._set_config_value(target, selected)
                    )
            else:
                for index, value in enumerate(option.values):
                    prefix = f"{index + 1}   " if index < 9 else ""
                    action = option_menu.addAction(prefix + value.label)
                    action.setCheckable(True)
                    action.setChecked(str(option.current_value) == value.value)
                    action.setToolTip(value.description)
                    action.triggered.connect(
                        lambda _checked=False, target=option, selected=value.value: self._set_config_value(target, selected)
                    )
        # Keep Python wrappers alive for the lifetime of the parent menu.
        # PySide can otherwise release dynamically created submenu wrappers.
        menu._request_submenus = (model_menu, effort_menu, *generic_menus)  # type: ignore[attr-defined]
        return menu

    def _show_request_settings_menu(self) -> None:
        if not self.settings_button.isEnabled():
            self._show_notice(
                self.settings_button.toolTip()
                or "Настройки запроса сейчас недоступны.",
                "warning",
            )
            return
        menu = self._build_request_settings_menu()
        menu.ensurePolished()
        size = menu.sizeHint()
        position = self.settings_button.mapToGlobal(
            QPoint(self.settings_button.width() - size.width(), -size.height() - 6)
        )
        menu.exec(position)

    def _model_changed(self, index: int) -> None:
        if index < 0 or index >= len(self.models):
            return
        model = self.models[index]
        self._set_agent_setting("model", model.id)
        option = next(
            (item for item in self.agent_config_options if item.category == "model"),
            None,
        )
        if option is not None:
            setter = getattr(self.service, "set_config_option", None)
            if callable(setter):
                setter(option.id, model.id)
        saved_effort = self._agent_setting("effort", model.default_effort or "")
        self.effort_combo.blockSignals(True)
        self.effort_combo.clear()
        for effort in model.efforts:
            self.effort_combo.addItem(effort, effort)
        effort_index = self.effort_combo.findData(saved_effort)
        if effort_index < 0 and model.default_effort:
            effort_index = self.effort_combo.findData(model.default_effort)
        if effort_index >= 0:
            self.effort_combo.setCurrentIndex(effort_index)
        elif self.effort_combo.count():
            self.effort_combo.setCurrentIndex(0)
        self.effort_combo.blockSignals(False)
        self._effort_changed(self.effort_combo.currentIndex())
        self._note_next_request_setting()

    def _effort_changed(self, _index: int) -> None:
        self._set_agent_setting("effort", self.effort_combo.currentData() or "")
        option = next(
            (
                item
                for item in self.agent_config_options
                if item.category == "thought_level"
            ),
            None,
        )
        if option is not None and self.effort_combo.currentData():
            setter = getattr(self.service, "set_config_option", None)
            if callable(setter):
                setter(option.id, str(self.effort_combo.currentData()))
        self._note_next_request_setting()

    def _build_access_mode_menu(self) -> NumberedChoiceMenu:
        menu = NumberedChoiceMenu(self)
        menu.setObjectName("accessModeShortcutMenu")
        for index in range(min(9, self.access_combo.count())):
            mode_id = str(self.access_combo.itemData(index) or "")
            if not mode_id:
                continue
            action = menu.addAction(
                f"{len(menu.actions()) + 1}   {self.access_combo.itemText(index)}"
            )
            action.setCheckable(True)
            action.setChecked(index == self.access_combo.currentIndex())
            description = self.access_combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
            if description:
                action.setToolTip(str(description))
            action.triggered.connect(
                lambda _checked=False, target=index: self.access_combo.setCurrentIndex(target)
            )
        return menu

    def _show_access_mode_menu(self) -> None:
        if not self.access_combo.isEnabled():
            self._show_notice(
                self.access_combo.toolTip() or "Смена режима сейчас недоступна.",
                "warning",
            )
            return
        menu = self._build_access_mode_menu()
        if not menu.actions():
            self._show_notice("Текущий агент не предоставил режимы работы.", "warning")
            return
        menu.ensurePolished()
        size = menu.sizeHint()
        position = self.access_combo.mapToGlobal(
            QPoint(0, -size.height() - 6)
        )
        menu.exec(position)

    def _access_changed(self, _index: int) -> None:
        selected = str(self.access_combo.currentData() or "")
        mode = next(
            (item for item in self.agent_manifest.run_modes if item.id == selected),
            None,
        )
        if mode is None:
            mode = next(
                (item for item in self._legacy_codex_run_modes() if item.id == selected),
                None,
            )
        if mode is None:
            return
        if mode.dangerous and not self._danger_acknowledged:
            answer = QMessageBox.warning(
                self,
                "Полный доступ",
                f"{self._agent_name()} сможет читать и изменять файлы вне рабочей папки, а также выполнять "
                "команды без дополнительных подтверждений. Включить полный доступ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                fallback = self.access_combo.findData(self.agent_manifest.current_run_mode_id)
                if fallback < 0:
                    fallback = self.access_combo.findData(AccessMode.WORKSPACE_WRITE.value)
                self.access_combo.blockSignals(True)
                self.access_combo.setCurrentIndex(max(0, fallback))
                self.access_combo.blockSignals(False)
                self._refresh_access_style()
                return
            self._danger_acknowledged = True
        self._set_agent_setting("run_mode", mode.id)
        if mode.id in {item.value for item in AccessMode}:
            self._set_agent_setting("access_mode", mode.id)
        if getattr(self.service, "current_run_id", ""):
            self._pending_run_mode_id = mode.id
        else:
            self._pending_run_mode_id = ""
            setter = getattr(self.service, "set_run_mode", None)
            if callable(setter):
                setter(mode.id)
        self._refresh_access_style()
        self._note_next_request_setting()

    def _refresh_access_style(self) -> None:
        selected = str(self.access_combo.currentData() or "")
        descriptor = next(
            (item for item in self.agent_manifest.run_modes if item.id == selected),
            None,
        )
        if descriptor is None:
            descriptor = next(
                (item for item in self._legacy_codex_run_modes() if item.id == selected),
                None,
            )
        tone = descriptor.tone if descriptor is not None else "neutral"
        description = descriptor.description if descriptor is not None else ""
        suffix = "\nПрименится к следующему сообщению" if self._turn_active else ""
        self.access_combo.setToolTip(description + suffix + "\nCtrl+M — выбрать режим")
        self.access_combo.setProperty("mode", tone)
        self.access_combo.setProperty("nextTurn", self._turn_active)
        self.access_shortcut_label.setProperty("accessTone", tone)
        self.access_combo.style().unpolish(self.access_combo)
        self.access_combo.style().polish(self.access_combo)
        self._refresh_access_shortcut_style()

    def _refresh_access_shortcut_style(self) -> None:
        tone = str(self.access_shortcut_label.property("accessTone") or "neutral")
        color = {
            "safe": "#b8d8c2",
            "workspace": "#d4ddd7",
            "neutral": "#d2d5d2",
            "plan": "#d2c9eb",
            "danger": "#efb8b2",
        }.get(tone, "#d2d5d2")
        if not self.access_shortcut_label.isEnabled():
            color = "#606662"
        self.access_shortcut_label.setStyleSheet(
            f"color: {color}; font-size: 9px;"
        )

    def _note_next_request_setting(self) -> None:
        if self._turn_active:
            self._show_notice(
                "Настройка сохранена и применится к следующему сообщению.",
                "info",
                4500,
            )

    def _composer_text_changed(self) -> None:
        text = self.composer.toPlainText()
        if self._slash_dismissed_text is not None and text != self._slash_dismissed_text:
            self._slash_dismissed_text = None
        self._slash_help_visible = False
        self._update_slash_panel()

    def _matching_slash_commands(self) -> list[tuple[SlashCommand, bool, str]]:
        text = self.composer.toPlainText()
        if self._slash_help_visible and not text:
            prefix = "/"
        else:
            cursor_position = self.composer.textCursor().position()
            prefix = text[:cursor_position]
            # setPlainText() leaves the cursor at the start. Keep programmatic
            # command insertion useful when the whole value is a command prefix.
            if cursor_position == 0 and text.startswith("/") and not any(
                character.isspace() for character in text
            ):
                prefix = text
            if not prefix.startswith("/") or any(
                character.isspace() for character in prefix
            ):
                return []
            if text == self._slash_dismissed_text:
                return []
        has_thread = bool(getattr(self.service, "current_thread_id", ""))
        matches: list[tuple[SlashCommand, bool, str]] = []
        capabilities = self._capabilities()
        feature_by_command = {
            "compact": self._feature_state(FeatureId.SESSION_COMPACT, capabilities.compact),
            "review": self._feature_state(FeatureId.SESSION_REVIEW, capabilities.review),
            "fork": self._feature_state(FeatureId.SESSION_FORK, capabilities.fork),
            "plan": self._feature_state(FeatureId.RUN_PLAN, capabilities.plan_mode),
        }
        action_state = getattr(self.service, "action_state", None)
        for command in self._all_slash_commands():
            if not command.syntax.startswith(prefix):
                continue
            available = not command.needs_thread or has_thread
            reason = "" if available else "Сначала создайте текущий чат"
            state = feature_by_command.get(command.name)
            if state is None and command.name not in SLASH_COMMANDS_BY_NAME and callable(action_state):
                candidate = action_state(command.name)
                state = candidate if isinstance(candidate, FeatureState) else None
            if state is not None and not state.enabled:
                available = False
                reason = state.reason
            matches.append((command, available, reason))
        return matches

    def _all_slash_commands(self) -> tuple[SlashCommand, ...]:
        existing = set(SLASH_COMMANDS_BY_NAME)
        dynamic = tuple(
            SlashCommand(
                action.id,
                action.description or action.title,
                accepts_arguments=bool(action.argument_hint),
                needs_thread=action.requires_session,
            )
            for action in self.agent_manifest.actions
            if action.id not in existing
        )
        return SLASH_COMMANDS + dynamic

    def _slash_commands_by_name(self) -> dict[str, SlashCommand]:
        return {command.name: command for command in self._all_slash_commands()}

    def _update_slash_panel(self) -> None:
        matches = self._matching_slash_commands()
        self.slash_panel.set_commands(matches)
        visible = bool(matches)
        self.slash_panel.setVisible(visible)
        self.composer.set_slash_menu_state(
            visible,
            self.slash_panel.selected_command() is not None,
        )

    def _navigate_slash_commands(self, delta: int) -> None:
        self.slash_panel.move_selection(delta)
        self.composer.set_slash_menu_state(
            not self.slash_panel.isHidden(),
            self.slash_panel.selected_command() is not None,
        )

    def _complete_slash_command(self) -> None:
        selected = self.slash_panel.selected_command()
        if not selected:
            return
        self._insert_slash_completion(selected)

    def _insert_slash_completion(self, name: str) -> str:
        command = self._slash_commands_by_name()[name]
        text = self.composer.toPlainText()
        cursor_position = self.composer.textCursor().position()
        if cursor_position == 0 and text.startswith("/") and not any(
            character.isspace() for character in text
        ):
            cursor_position = len(text)
        suffix = text[cursor_position:]
        if command.accepts_arguments:
            completion = command.syntax + " "
            suffix = suffix.lstrip(" ")
        else:
            completion = command.syntax
            if suffix and not suffix[0].isspace():
                completion += " "
        completed_text = completion + suffix
        self.composer.setPlainText(completed_text)
        cursor = self.composer.textCursor()
        cursor.setPosition(len(completion))
        self.composer.setTextCursor(cursor)
        return completed_text

    def _dismiss_slash_panel(self) -> None:
        self._slash_dismissed_text = self.composer.toPlainText()
        self._slash_help_visible = False
        self.slash_panel.setVisible(False)
        self.composer.set_slash_menu_state(False, False)

    def _activate_slash_command(self, name: str) -> None:
        self._insert_slash_completion(name)
        self._send()

    def _show_slash_help(self) -> None:
        self.composer.clear()
        self._slash_dismissed_text = None
        self._slash_help_visible = True
        self._update_slash_panel()
        self.composer.setFocus()

    def _parse_slash_command(self, text: str) -> tuple[str, str] | None:
        if not text.startswith("/"):
            return None
        token, separator, arguments = text.partition(" ")
        name = token[1:]
        if name not in self._slash_commands_by_name():
            return None
        return name, arguments.strip() if separator else ""

    def _choose_attachments(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Прикрепить файлы")
        self._add_attachments(paths)

    def _add_attachments(self, paths: list[str]) -> None:
        known = {item.path for item in self.attachments}
        for raw in paths:
            path = Path(raw).resolve()
            if not path.is_file() or path in known:
                continue
            is_image = path.suffix.lower() in IMAGE_EXTENSIONS or (mimetypes.guess_type(path)[0] or "").startswith("image/")
            self.attachments.append(Attachment(path, is_image))
            known.add(path)
        self._render_attachments()

    def _render_attachments(self) -> None:
        while self.attachment_row.count() > 1:
            item = self.attachment_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for attachment in self.attachments:
            button = QToolButton()
            button.setText(f"{attachment.name}  ×")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            if attachment.is_image:
                reader = QImageReader(str(attachment.path))
                reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid():
                    size.scale(40, 40, Qt.AspectRatioMode.KeepAspectRatio)
                    reader.setScaledSize(size)
                image = reader.read()
                if not image.isNull():
                    button.setIcon(QIcon(QPixmap.fromImage(image)))
                    button.setIconSize(QSize(40, 40))
            else:
                button.setText(f"📄 {attachment.name}  ×")
            button.setToolTip(str(attachment.path))
            button.clicked.connect(lambda _checked=False, target=attachment: self._remove_attachment(target))
            self.attachment_row.insertWidget(self.attachment_row.count() - 1, button)

    def _remove_attachment(self, attachment: Attachment) -> None:
        if attachment in self.attachments:
            self.attachments.remove(attachment)
            self._render_attachments()

    def _send(self) -> None:
        if self._editing_queued_message is not None:
            self._save_queue_edit()
            return
        text = self.composer.toPlainText().strip()
        if not text and not self.attachments:
            return
        slash = self._parse_slash_command(text)
        if slash is not None:
            name, arguments = slash
            self._execute_slash_command(name, arguments, text)
            return
        self._submit_message(text)

    def _execute_slash_command(self, name: str, arguments: str, syntax: str) -> None:
        command = self._slash_commands_by_name()[name]
        capabilities = self._capabilities()
        state = {
            "compact": self._feature_state(FeatureId.SESSION_COMPACT, capabilities.compact),
            "review": self._feature_state(FeatureId.SESSION_REVIEW, capabilities.review),
            "fork": self._feature_state(FeatureId.SESSION_FORK, capabilities.fork),
            "plan": self._feature_state(FeatureId.RUN_PLAN, capabilities.plan_mode),
        }.get(name)
        action_state = getattr(self.service, "action_state", None)
        if state is None and name not in SLASH_COMMANDS_BY_NAME and callable(action_state):
            candidate = action_state(name)
            state = candidate if isinstance(candidate, FeatureState) else None
        if state is not None and not state.enabled:
            self._show_notice(
                state.reason,
                "warning",
            )
            return
        if command.needs_thread and not getattr(self.service, "current_thread_id", ""):
            self._show_notice(
                "Команда недоступна: сначала создайте или откройте чат.",
                "warning",
            )
            return
        if name == "help":
            self._show_slash_help()
            return
        if name == "plan" and not arguments:
            self.composer.clear()
            self._dismiss_slash_panel()
            index = self.access_combo.findData(PLAN_MODE_VALUE)
            if index >= 0:
                self.access_combo.setCurrentIndex(index)
            return
        if name == "plan":
            self._submit_message(arguments, force_plan=True, queue_syntax=syntax)
            return

        self.composer.clear()
        self._dismiss_slash_panel()
        queued = QueuedCommand(name, arguments)
        if self._turn_active or self._queue_action_pending or not self.plan_confirmation_card.isHidden():
            self._message_queue.append(queued)
            self._render_message_queue()
            return
        self._dispatch_command(queued)

    def _submit_message(
        self,
        text: str,
        *,
        force_plan: bool = False,
        queue_syntax: str | None = None,
    ) -> None:
        if getattr(self.service, "connected", True) is False:
            self._show_error(f"{self._agent_name()} ещё не подключен")
            return
        if not getattr(self.service, "current_project", ""):
            self._add_project()
            if not getattr(self.service, "current_project", ""):
                self._show_notice(
                    "Сообщение сохранено в редакторе — выберите рабочую папку.",
                    "warning",
                    6000,
                )
                return
        invalid = [str(item.path) for item in self.attachments if not item.path.is_file()]
        if invalid:
            self._show_error("Файлы больше не существуют:\n" + "\n".join(invalid))
            return
        model = self._selected_model()
        if any(item.is_image for item in self.attachments) and model and "image" not in model.modalities:
            self._show_error(f"Модель {model.display_name} не поддерживает изображения")
            return
        attachments = list(self.attachments)
        self.composer.clear()
        self.attachments.clear()
        self._render_attachments()
        selected_mode = str(self.access_combo.currentData() or "")
        run_mode = next(
            (item for item in self.agent_manifest.run_modes if item.id == selected_mode),
            None,
        )
        if run_mode is None:
            run_mode = next(
                (item for item in self._legacy_codex_run_modes() if item.id == selected_mode),
                None,
            )
        collaboration_mode = (
            PLAN_MODE_VALUE
            if force_plan or (run_mode is not None and run_mode.tone == "plan")
            else None
        )
        if collaboration_mode:
            access_mode = AccessMode.READ_ONLY
        else:
            try:
                access_mode = AccessMode(selected_mode)
            except ValueError:
                access_mode = AccessMode.WORKSPACE_WRITE
        message = QueuedMessage(
            text,
            attachments,
            self.model_combo.currentData() or "",
            self.effort_combo.currentData(),
            access_mode,
            collaboration_mode,
            queue_syntax,
            self._current_agent_config(),
            selected_mode,
        )
        if self._turn_active or self._queue_action_pending:
            self._message_queue.append(message)
            self._render_message_queue()
            return
        if not self.plan_confirmation_card.isHidden():
            self._dismiss_plan_confirmation(send_queued=False)
        self._dispatch_message(message)

    def _dispatch_message(self, message: QueuedMessage) -> bool:
        if getattr(self.service, "connected", True) is False:
            self._show_notice(
                f"Очередь приостановлена: {self._agent_name()} не подключён.",
                "warning",
                6000,
            )
            self._queue_paused = True
            self._render_message_queue()
            return False
        missing = [str(item.path) for item in message.attachments if not item.path.is_file()]
        if missing:
            self._show_error("Файлы из очереди больше не существуют:\n" + "\n".join(missing))
            self._queue_paused = True
            self._render_message_queue()
            return False
        self._active_collaboration_mode = message.collaboration_mode
        submit_prompt = getattr(self.service, "submit_prompt", None)
        if callable(submit_prompt):
            config = dict(message.config)
            option_by_category = {
                option.category: option for option in self.agent_config_options
            }
            if message.model:
                model_option = option_by_category.get("model")
                config[model_option.id if model_option else "model"] = message.model
            if message.effort:
                thought_option = option_by_category.get("thought_level")
                config[
                    thought_option.id if thought_option else "thought_level"
                ] = message.effort
            submit_prompt(
                AgentPrompt(
                    text=message.text,
                    attachments=tuple(message.attachments),
                    working_directory=getattr(self.service, "current_project", ""),
                    config=config,
                    mode=message.collaboration_mode or "",
                    access_mode=message.access_mode,
                    run_mode_id=message.run_mode_id,
                )
            )
        else:
            self.service.send_message(
                message.text,
                message.attachments,
                message.model,
                message.effort,
                message.access_mode,
                message.collaboration_mode,
            )
        return True

    def _dispatch_command(self, command: QueuedCommand) -> None:
        invoke = getattr(self.service, "invoke_action", None)
        if command.name == "compact":
            if callable(invoke):
                invoke("compact", command.arguments)
            else:
                self.service.compact_thread()
            return
        if command.name == "review":
            if callable(invoke):
                invoke("review", command.arguments)
            else:
                self.service.start_review(command.arguments)
            return
        if command.name == "fork":
            self._queue_action_pending = True
            self._render_message_queue()
            if callable(invoke):
                invoke("fork", command.arguments, self._fork_finished)
            else:
                self.service.fork_thread(self._fork_finished)
            return
        if command.name == "new":
            if not self._prepare_new_chat(clear_queue=False):
                self._queue_paused = True
                self._render_message_queue()
                return
            QTimer.singleShot(0, self._send_next_queued)
            return
        if callable(invoke):
            invoke(command.name, command.arguments)

    def _fork_finished(self, success: bool) -> None:
        self._queue_action_pending = False
        if not success:
            self._queue_paused = True
        self._render_message_queue()
        if success:
            QTimer.singleShot(0, self._send_next_queued)

    def _send_next_queued(self) -> None:
        if (
            self._turn_active
            or self._queue_action_pending
            or self._queue_paused
            or self._editing_queued_message is not None
            or not self._message_queue
        ):
            return
        if not self.plan_confirmation_card.isHidden() or not self.user_input_card.isHidden():
            return
        queued = self._message_queue[0]
        if isinstance(queued, QueuedCommand):
            self._message_queue.pop(0)
            self._render_message_queue()
            self._dispatch_command(queued)
        else:
            if self._dispatch_message(queued):
                self._message_queue.pop(0)
                self._render_message_queue()

    def _resume_queue(self) -> None:
        self._queue_paused = False
        self._render_message_queue()
        self._send_next_queued()

    def _remove_queued_message(self, index: int) -> None:
        if 0 <= index < len(self._message_queue):
            if self._message_queue[index] is self._editing_queued_message:
                self._finish_queue_edit(resume_queue=False)
            self._message_queue.pop(index)
            self._render_message_queue()

    def _edit_queued_message(self, index: int) -> None:
        if not (0 <= index < len(self._message_queue)):
            return
        queued = self._message_queue[index]
        if isinstance(queued, QueuedCommand):
            return
        if queued is self._editing_queued_message:
            self.composer.setFocus()
            return
        if self._editing_queued_message is not None:
            self._finish_queue_edit(resume_queue=False)
        self._editing_queued_message = queued
        self._queue_edit_draft_text = self.composer.toPlainText()
        self._queue_edit_draft_attachments = list(self.attachments)
        self.attachments.clear()
        self._render_attachments()
        self.composer.setPlainText(queued.text)
        cursor = self.composer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.composer.setTextCursor(cursor)
        queue_index = self._message_queue.index(queued) + 1
        attachment_note = (
            f" · Вложений: {len(queued.attachments)}"
            if queued.attachments
            else ""
        )
        self.queue_edit_detail.setText(
            f"Сообщение №{queue_index}{attachment_note} · Enter — сохранить · Shift+Enter — новая строка"
        )
        self.queue_edit_banner.setVisible(True)
        self.attach_button.setEnabled(False)
        self.access_combo.setEnabled(False)
        self.access_shortcut_label.setEnabled(False)
        self._refresh_access_shortcut_style()
        self.settings_button.setEnabled(False)
        composer_panel = self.composer.parentWidget()
        if composer_panel is not None:
            composer_panel.setProperty("editingQueue", True)
            composer_panel.style().unpolish(composer_panel)
            composer_panel.style().polish(composer_panel)
        self._refresh_send_button()
        self._render_message_queue()
        self.composer.setFocus()

    def _save_queue_edit(self) -> None:
        queued = self._editing_queued_message
        if queued is None or queued not in self._message_queue:
            self._finish_queue_edit()
            return
        index = self._message_queue.index(queued)
        if self._update_queued_message(index, self.composer.toPlainText()):
            self._finish_queue_edit()

    def _cancel_queue_edit(self) -> None:
        self._finish_queue_edit()

    def _finish_queue_edit(self, *, resume_queue: bool = True) -> None:
        if self._editing_queued_message is None:
            return
        self._editing_queued_message = None
        self.queue_edit_banner.setVisible(False)
        self.composer.setPlainText(self._queue_edit_draft_text)
        self.attachments = list(self._queue_edit_draft_attachments)
        self._queue_edit_draft_text = ""
        self._queue_edit_draft_attachments.clear()
        self._render_attachments()
        self.attach_button.setEnabled(True)
        self.access_combo.setEnabled(True)
        self.access_shortcut_label.setEnabled(True)
        self._refresh_access_shortcut_style()
        self.settings_button.setEnabled(True)
        composer_panel = self.composer.parentWidget()
        if composer_panel is not None:
            composer_panel.setProperty("editingQueue", False)
            composer_panel.style().unpolish(composer_panel)
            composer_panel.style().polish(composer_panel)
        self._refresh_send_button()
        self._render_message_queue()
        self._apply_agent_capabilities()
        if resume_queue:
            QTimer.singleShot(0, self._send_next_queued)

    def _update_queued_message(self, index: int, text: str) -> bool:
        if not (0 <= index < len(self._message_queue)):
            return False
        queued = self._message_queue[index]
        if isinstance(queued, QueuedCommand):
            return False
        normalized = text.strip()
        if not normalized and not queued.attachments:
            self._show_notice(
                "Сообщение без текста или вложений нельзя сохранить.",
                "warning",
            )
            return False
        queued.text = normalized
        if queued.queue_syntax and queued.queue_syntax.startswith("/plan"):
            queued.queue_syntax = "/plan" + (f" {normalized}" if normalized else "")
        self._render_message_queue()
        return True

    def _clear_message_queue(self) -> None:
        self._finish_queue_edit(resume_queue=False)
        self._message_queue.clear()
        self._queue_paused = False
        self._render_message_queue()

    def _render_message_queue(self) -> None:
        while self.queue_items_layout.count():
            item = self.queue_items_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        count = len(self._message_queue)
        state = (
            "РЕДАКТИРОВАНИЕ"
            if self._editing_queued_message is not None
            else "ПРИОСТАНОВЛЕНА"
            if self._queue_paused
            else "ОЧЕРЕДЬ"
        )
        self.queue_label.setText(f"{state}  ·  {count}")
        for index, queued in enumerate(self._message_queue):
            if isinstance(queued, QueuedCommand):
                preview = queued.syntax
                full_text = preview
            else:
                preview = queued.queue_syntax or " ".join(queued.text.split()) or "Вложения"
                full_text = queued.queue_syntax or queued.text or "Вложения"
                if queued.attachments:
                    suffix = f" · {len(queued.attachments)} влож."
                    preview += suffix
            if len(preview) > 90:
                preview = preview[:87] + "…"
            row = QFrame()
            row.setObjectName("queueItem")
            editing = queued is self._editing_queued_message
            row.setProperty("editing", editing)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 5, 5, 5)
            row_layout.setSpacing(7)
            number = QLabel(str(index + 1))
            number.setObjectName("queueItemIndex")
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            number.setFixedSize(22, 22)
            row_layout.addWidget(number)
            text = QLabel(preview)
            text.setObjectName("queueItemText")
            text.setToolTip(full_text)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row_layout.addWidget(text, 1)
            if not isinstance(queued, QueuedCommand):
                edit = QToolButton()
                edit.setObjectName("queueItemAction")
                edit.setIcon(asset_icon("edit.svg"))
                edit.setToolTip("Редактировать сообщение")
                edit.setAccessibleName(f"Редактировать сообщение {index + 1}")
                edit.setFixedSize(28, 28)
                edit.setEnabled(not editing)
                edit.clicked.connect(
                    lambda _checked=False, target=index: self._edit_queued_message(target)
                )
                row_layout.addWidget(edit)
            remove = QToolButton()
            remove.setObjectName("queueItemAction")
            remove.setIcon(asset_icon("remove.svg"))
            remove.setToolTip("Удалить из очереди")
            remove.setAccessibleName(f"Удалить элемент {index + 1} из очереди")
            remove.setFixedSize(28, 28)
            remove.setEnabled(not editing)
            remove.clicked.connect(
                lambda _checked=False, target=index: self._remove_queued_message(target)
            )
            row_layout.addWidget(remove)
            self.queue_items_layout.addWidget(row)
        self.queue_resume_button.setEnabled(
            bool(count)
            and not self._turn_active
            and not self._queue_action_pending
            and self._editing_queued_message is None
        )
        self.queue_panel.setVisible(bool(count))
        navigation_enabled = not self._turn_active and not self._queue_action_pending
        self.new_chat_button.setEnabled(navigation_enabled)
        self.new_chat_shortcut_label.setEnabled(navigation_enabled)
        self.thread_list.setEnabled(navigation_enabled)
        self.project_combo.setEnabled(navigation_enabled)
        self.agent_combo.setEnabled(navigation_enabled and not bool(count))

    def _selected_model(self) -> ModelInfo | None:
        model_id = self.model_combo.currentData()
        return next((item for item in self.models if item.id == model_id), None)

    def _render_thread(self, thread: dict[str, Any]) -> None:
        items, omitted_turns, omitted_items = recent_thread_items(thread)
        self._clear_timeline(
            "Новый чат готов. Опишите задачу ниже." if not items else ""
        )
        if omitted_turns or omitted_items:
            details: list[str] = []
            if omitted_turns:
                details.append(f"{omitted_turns} старых ходов")
            if omitted_items:
                details.append(f"{omitted_items} старых элементов")
            notice = QLabel("Для быстрого открытия не показаны " + " и ".join(details) + ".")
            notice.setObjectName("historyNotice")
            notice.setWordWrap(True)
            notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.timeline_layout.insertWidget(0, notice)
        for item in items:
            self._upsert_item(item, True)
        self._scroll_bottom()

    def _clear_timeline(self, hint: str = "") -> None:
        if self._thinking_indicator is not None:
            self._thinking_indicator.stop()
            self._thinking_indicator = None
        self.cards.clear()
        self._execution_plan_cards.clear()
        self._last_activity_group = None
        self._latest_activity_card = None
        self._latest_activity_group = None
        self._auto_follow = True
        if hasattr(self, "scroll_down_button"):
            self.scroll_down_button.setVisible(False)
        self._pending_plan_text = ""
        self.plan_confirmation_card.setVisible(False)
        while self.timeline_layout.count() > 1:
            item = self.timeline_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        if hint:
            empty = QFrame()
            empty.setObjectName("emptyHint")
            empty.setMaximumWidth(560)
            empty_layout = QVBoxLayout(empty)
            empty_layout.setContentsMargins(24, 40, 24, 40)
            empty_layout.setSpacing(10)
            glyph = QLabel("✦")
            glyph.setObjectName("emptyGlyph")
            glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title = QLabel("Чем займёмся?")
            title.setObjectName("emptyTitle")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            description = QLabel(hint)
            description.setObjectName("emptyDescription")
            description.setAlignment(Qt.AlignmentFlag.AlignCenter)
            description.setWordWrap(True)
            empty_layout.addWidget(glyph)
            empty_layout.addWidget(title)
            empty_layout.addWidget(description)
            starter_title = QLabel("БЫСТРЫЙ СТАРТ")
            starter_title.setObjectName("emptyStarterTitle")
            starter_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_layout.addWidget(starter_title)
            for label, prompt in (
                (
                    "Объяснить структуру проекта",
                    "Изучи проект и объясни его структуру, основные компоненты и точки входа.",
                ),
                (
                    "Найти потенциальные ошибки",
                    "Проверь проект на потенциальные ошибки и предложи исправления.",
                ),
                (
                    "Проверить текущие изменения",
                    "Проверь текущие изменения в рабочей папке и перечисли найденные проблемы.",
                ),
            ):
                button = QPushButton(label)
                button.setObjectName("starterButton")
                button.setAccessibleName(label)
                button.clicked.connect(
                    lambda _checked=False, value=prompt: self._use_starter_prompt(value)
                )
                empty_layout.addWidget(button)
            self.timeline_layout.insertWidget(0, empty, 1, Qt.AlignmentFlag.AlignCenter)

    def _use_starter_prompt(self, prompt: str) -> None:
        self.composer.setPlainText(prompt)
        cursor = self.composer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.composer.setTextCursor(cursor)
        self.composer.setFocus()

    def _remove_empty_hint(self) -> None:
        for index in range(self.timeline_layout.count() - 1):
            widget = self.timeline_layout.itemAt(index).widget()
            if widget and widget.objectName() == "emptyHint":
                self.timeline_layout.takeAt(index)
                widget.hide()
                widget.deleteLater()
                return

    def _upsert_item(self, item: dict[str, Any], complete: bool) -> None:
        item_id = str(item.get("id") or uuid.uuid4())
        kind = str(item.get("kind") or item.get("type", "unknown"))
        subtype = str(item.get("subtype") or item.get("type", ""))
        card = self.cards.get(item_id)
        if kind in {"userMessage", "user_message"}:
            text = self._user_message_text(item.get("content", []))
            if not isinstance(card, MessageCard):
                card = MessageCard("user", text)
                self._add_card(item_id, card)
            else:
                card.set_text(text)
        elif kind in {"agentMessage", "assistant_message"}:
            self._set_thinking_activity("ИИ пишет ответ")
            text = str(item.get("text", ""))
            if not isinstance(card, MessageCard):
                card = MessageCard("agent", text)
                self._add_card(item_id, card)
            elif complete or text:
                card.set_text(text)
        elif kind == "plan":
            self._set_thinking_activity("ИИ составляет план")
            text = str(item.get("text", ""))
            if not isinstance(card, MessageCard):
                card = MessageCard("agent", text)
                self._add_card(item_id, card)
            elif complete or text:
                card.set_text(text)
            if (
                complete
                and self._turn_active
                and self._active_collaboration_mode == PLAN_MODE_VALUE
            ):
                self._pending_plan_text = text
        elif kind == "reasoning":
            self._set_thinking_activity("ИИ анализирует")
            summary = item.get("summary", [])
            text = "\n".join(summary) if isinstance(summary, list) else str(summary or item.get("content", ""))
            self._activity(item_id, "Размышления", text)
        elif kind in {"commandExecution", "command"}:
            self._set_thinking_activity("ИИ выполняет команду")
            command = item.get("command", "Команда")
            if isinstance(command, list):
                command = " ".join(map(str, command))
            output = str(item.get("aggregatedOutput") or "")
            status = item.get("status", "inProgress")
            self._activity(
                item_id,
                f"Терминал · {localized_status(status)}: {command}",
                output,
            )
        elif kind in {"fileChange", "file_change"}:
            self._set_thinking_activity("ИИ изменяет файлы")
            changes = item.get("changes", [])
            paths = [str(change.get("path", "")) for change in changes if isinstance(change, dict)]
            diffs = [str(change.get("diff", "")) for change in changes if isinstance(change, dict)]
            self._activity(item_id, "Изменения файлов: " + ", ".join(paths), "\n".join(diffs))
        elif kind == "contextCompaction" or (
            kind == "system_activity" and subtype == "contextCompaction"
        ):
            self._set_thinking_activity("ИИ сжимает контекст")
            self._activity(
                item_id,
                "Контекст сжат",
                self._compact_item(item) or "История чата сжата для продолжения работы.",
            )
        elif kind == "enteredReviewMode" or (
            kind == "system_activity" and subtype == "enteredReviewMode"
        ):
            self._set_thinking_activity("ИИ проверяет изменения")
            self._activity(
                item_id,
                "Режим ревью запущен",
                self._compact_item(item) or "Агент проверяет выбранные изменения.",
            )
        elif kind == "exitedReviewMode" or (
            kind == "system_activity" and subtype == "exitedReviewMode"
        ):
            self._activity(
                item_id,
                "Режим ревью завершён",
                self._compact_item(item) or "Агент завершил проверку изменений.",
            )
        elif kind == "tool_call" or kind in {
            "mcpToolCall",
            "dynamicToolCall",
            "webSearch",
            "collabToolCall",
        }:
            self._set_thinking_activity(
                "ИИ ищет в интернете"
                if subtype == "webSearch" or kind == "webSearch"
                else "ИИ использует инструмент"
            )
            advertised_kind = "" if subtype == "acp_tool_call" else subtype
            title = str(
                item.get("tool")
                or item.get("query")
                or advertised_kind
                or item.get("title")
                or kind
            )
            self._activity(item_id, title, self._compact_item(item))
        self._scroll_bottom()

    def _add_card(self, item_id: str, card: MessageCard | ActivityCard) -> None:
        self._remove_empty_hint()
        self.cards[item_id] = card
        self._last_activity_group = None
        if isinstance(card, MessageCard):
            card.editRequested.connect(self._edit_message)
        if isinstance(card, MessageCard) and card.role == "user":
            self.timeline_layout.insertWidget(
                self.timeline_layout.count() - 1,
                card,
                0,
                Qt.AlignmentFlag.AlignRight,
            )
        else:
            self.timeline_layout.insertWidget(self.timeline_layout.count() - 1, card)
        self._move_thinking_to_bottom()

    def _edit_message(self, text: str) -> None:
        self.composer.setPlainText(text)
        cursor = self.composer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.composer.setTextCursor(cursor)
        self.composer.setFocus()
        self.statusBar().showMessage("Текст сообщения перенесён в поле ввода", 3000)

    def _activity(self, item_id: str, title: str, content: str) -> ActivityCard:
        card = self.cards.get(item_id)
        if not isinstance(card, ActivityCard):
            previous_card = self._latest_activity_card
            previous_group = self._latest_activity_group
            follow_latest = bool(
                previous_card is not None
                and previous_group is not None
                and previous_group.header.isChecked()
                and previous_card.toggle.isChecked()
            )
            card = ActivityCard(title, content)
            self.cards[item_id] = card
            self._remove_empty_hint()
            if self._last_activity_group is None:
                if previous_group is not None:
                    previous_group.header.shortcut_label.setVisible(
                        False
                    )
                self._last_activity_group = ActivityGroupCard()
                self._latest_activity_group = self._last_activity_group
                self.timeline_layout.insertWidget(
                    self.timeline_layout.count() - 1,
                    self._last_activity_group,
                )
                self._move_thinking_to_bottom()
            self._last_activity_group.add_activity(card)
            self._latest_activity_card = card
            if follow_latest:
                if previous_card is not None:
                    previous_card.toggle.setChecked(False)
                if previous_group is not None and previous_group is not self._last_activity_group:
                    previous_group.header.setChecked(False)
                self._last_activity_group.header.setChecked(True)
                card.toggle.setChecked(True)
        else:
            card.toggle.setText(title)
            card.set_content(content)
        return card

    def _agent_delta(self, item_id: str, delta: str) -> None:
        self._set_thinking_activity("ИИ пишет ответ")
        card = self.cards.get(item_id)
        if not isinstance(card, MessageCard):
            card = MessageCard("agent")
            self._add_card(item_id, card)
        card.append(delta)
        self._scroll_bottom()

    def _plan_delta(self, item_id: str, delta: str) -> None:
        self._set_thinking_activity("ИИ составляет план")
        card = self.cards.get(item_id)
        if not isinstance(card, MessageCard):
            card = MessageCard("agent")
            self._add_card(item_id, card)
        card.append(delta)
        self._scroll_bottom()

    def _turn_plan_updated(self, params: dict[str, Any]) -> None:
        self._set_thinking_activity("ИИ обновляет план")
        turn_id = str(
            params.get("turnId")
            or getattr(self.service, "current_turn_id", "")
            or uuid.uuid4()
        )
        plan = [item for item in params.get("plan", []) if isinstance(item, dict)]
        card = self._execution_plan_cards.get(turn_id)
        if card is None:
            if not plan:
                return
            card = ExecutionPlanCard()
            self._execution_plan_cards[turn_id] = card
            self._remove_empty_hint()
            self._last_activity_group = None
            self.timeline_layout.insertWidget(self.timeline_layout.count() - 1, card)
        card.set_plan(str(params.get("explanation") or ""), plan)
        self._move_thinking_to_bottom()
        self._scroll_bottom()

    def _reasoning_delta(self, item_id: str, delta: str) -> None:
        self._set_thinking_activity("ИИ анализирует")
        card = self.cards.get(item_id)
        if not isinstance(card, ActivityCard):
            card = self._activity(item_id, "Размышления", "")
        card.append(delta)

    def _command_delta(self, item_id: str, delta: str) -> None:
        self._set_thinking_activity("ИИ выполняет команду")
        card = self.cards.get(item_id)
        if not isinstance(card, ActivityCard):
            card = self._activity(item_id, "Терминал", "")
        card.append(delta)

    def _turn_state(self, status: str) -> None:
        active = status in {"starting", "inProgress"}
        was_active = self._turn_active
        self._turn_active = active
        if active and not was_active:
            self._turn_timer.start()
            self._pending_plan_text = ""
            self.plan_confirmation_card.setVisible(False)
        self._set_thinking(active)
        self.header_status.setText("●  Выполняется" if active else "●  Готов")
        self.header_status.setProperty("active", active)
        self.header_status.style().unpolish(self.header_status)
        self.header_status.style().polish(self.header_status)
        self.send_button.setVisible(True)
        self._refresh_send_button()
        self.stop_button.setVisible(active)
        self.stop_shortcut.setEnabled(active)
        self.composer.setEnabled(True)
        self.new_chat_button.setEnabled(not active)
        self.new_chat_shortcut_label.setEnabled(not active)
        self.thread_list.setEnabled(not active)
        self.project_combo.setEnabled(not active)
        self.agent_combo.setEnabled(not active and not bool(self._message_queue))
        self.model_combo.setEnabled(True)
        self.effort_combo.setEnabled(True)
        editing_queue = self._editing_queued_message is not None
        self.attach_button.setEnabled(not editing_queue)
        self.access_combo.setEnabled(not editing_queue)
        self.settings_button.setEnabled(not editing_queue)
        self._refresh_access_style()
        self._apply_agent_capabilities()
        self._render_message_queue()
        self.statusBar().showMessage(
            f"{self._agent_name()} работает…" if active else f"Ход: {localized_status(status)}",
            4000,
        )
        if was_active and not active:
            self._clear_server_requests()
            elapsed_ms = self._turn_timer.elapsed() if self._turn_timer.isValid() else 0
            self._turn_timer.invalidate()
            self._add_turn_duration(status, elapsed_ms)
            if status == "failed":
                self._show_desktop_notification(self._agent_name(), "Запрос завершился с ошибкой")
            elif status in {"interrupted", "cancelled", "canceled"}:
                self._show_desktop_notification(self._agent_name(), "Выполнение запроса остановлено")
            else:
                self._show_desktop_notification(self._agent_name(), "Выполнение запроса завершено")
            if status in {"failed", "interrupted", "cancelled", "canceled"}:
                if self._message_queue:
                    self._queue_paused = True
                    self._render_message_queue()
            else:
                if self._pending_plan_text:
                    self.plan_confirmation_card.setVisible(True)
                else:
                    QTimer.singleShot(0, self._send_next_queued)
            self._active_collaboration_mode = None

    def _refresh_send_button(self) -> None:
        if self._editing_queued_message is not None:
            self.send_button.setIcon(asset_icon("save.svg"))
            self.send_button.setToolTip(
                "Сохранить изменения · Enter (Shift+Enter — новая строка)"
            )
            self.send_button.setAccessibleName(
                "Сохранить изменения сообщения в очереди"
            )
            return
        self.send_button.setIcon(
            asset_icon("queue.svg" if self._turn_active else "send.svg")
        )
        self.send_button.setToolTip(
            "Добавить сообщение в очередь · Enter (Shift+Enter — новая строка)"
            if self._turn_active
            else "Отправить · Enter (Shift+Enter — новая строка)"
        )
        self.send_button.setAccessibleName(
            "Добавить сообщение в очередь"
            if self._turn_active
            else "Отправить сообщение"
        )

    def _add_turn_duration(self, status: str, elapsed_ms: int) -> None:
        if status == "failed":
            text = f"Ошибка через {format_duration(elapsed_ms)}"
            result = "failed"
        elif status in {"interrupted", "cancelled", "canceled"}:
            text = f"Остановлено через {format_duration(elapsed_ms)}"
            result = "stopped"
        else:
            text = f"Готово за {format_duration(elapsed_ms)}"
            result = "completed"
        label = QLabel(text)
        label.setObjectName("turnDuration")
        label.setProperty("result", result)
        self.timeline_layout.insertWidget(
            self.timeline_layout.count() - 1,
            label,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        self._last_activity_group = None

    def _set_thinking(self, active: bool) -> None:
        if active:
            if self._thinking_indicator is None:
                self._remove_empty_hint()
                self._thinking_indicator = ThinkingIndicator()
                self.timeline_layout.insertWidget(
                    self.timeline_layout.count() - 1,
                    self._thinking_indicator,
                )
            self._thinking_indicator.start()
            self._move_thinking_to_bottom()
            self._scroll_bottom()
            return
        if self._thinking_indicator is not None:
            self._thinking_indicator.stop()
            self.timeline_layout.removeWidget(self._thinking_indicator)
            self._thinking_indicator.hide()
            self._thinking_indicator.deleteLater()
            self._thinking_indicator = None

    def _set_thinking_activity(self, activity: str) -> None:
        if self._turn_active and self._thinking_indicator is not None:
            self._thinking_indicator.set_activity(activity)
            self._move_thinking_to_bottom()

    def _move_thinking_to_bottom(self) -> None:
        if self._thinking_indicator is None:
            return
        self.timeline_layout.removeWidget(self._thinking_indicator)
        self.timeline_layout.insertWidget(
            self.timeline_layout.count() - 1,
            self._thinking_indicator,
        )

    def _user_message_text(self, content: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for value in content:
            kind = value.get("type")
            if kind == "text":
                parts.append(str(value.get("text", "")))
            elif kind == "localImage":
                path = str(value.get("path", ""))
                parts.append(f"![{Path(path).name}]({QUrl.fromLocalFile(path).toString()})\n\n`{path}`")
            elif kind == "image":
                parts.append(f"🖼 `{value.get('url')}`")
            elif kind == "mention":
                parts.append(f"📄 `{value.get('path')}`")
        return "\n\n".join(parts)

    def _compact_item(self, item: dict[str, Any]) -> str:
        omitted = {"id", "type", "kind", "subtype", "status"}
        return "\n".join(f"{key}: {value}" for key, value in item.items() if key not in omitted)

    def _scroll_bottom(self) -> None:
        if self._auto_follow and not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _apply_scroll_bottom(self) -> None:
        if not self._auto_follow:
            return
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._update_scroll_down_button()

    def _force_scroll_bottom(self) -> None:
        self._auto_follow = True
        self._scroll_timer.stop()
        self._apply_scroll_bottom()

    def _scroll_value_changed(self, _value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        self._auto_follow = bar.maximum() - bar.value() <= 24
        self._update_scroll_down_button()

    def _scroll_range_changed(self, _minimum: int, _maximum: int) -> None:
        if self._auto_follow:
            self._scroll_bottom()
        self._update_scroll_down_button()

    def _update_scroll_down_button(self) -> None:
        bar = self.scroll.verticalScrollBar()
        away_from_bottom = bar.maximum() - bar.value() > 24
        self.scroll_down_button.setVisible(bar.maximum() > bar.minimum() and away_from_bottom)
        if not self.scroll_down_button.isHidden():
            self.scroll_down_button.raise_()
            self._position_scroll_down_button()

    def _position_scroll_down_button(self) -> None:
        viewport = self.scroll.viewport()
        x = max(0, (viewport.width() - self.scroll_down_button.width()) // 2)
        y = max(0, viewport.height() - self.scroll_down_button.height() - 14)
        self.scroll_down_button.move(x, y)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "sidebar_panel"):
            return
        width = event.size().width()
        if width <= SIDEBAR_AUTO_HIDE_WIDTH and not self.sidebar_panel.isHidden():
            self._sidebar_auto_hidden = True
            self._set_sidebar_visible(False)
        elif (
            width >= SIDEBAR_AUTO_SHOW_WIDTH
            and self._sidebar_auto_hidden
            and not self._sidebar_user_hidden
        ):
            self._sidebar_auto_hidden = False
            self._set_sidebar_visible(True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            hasattr(self, "scroll")
            and watched is self.scroll.viewport()
            and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}
            and hasattr(self, "scroll_down_button")
        ):
            self._position_scroll_down_button()
        return super().eventFilter(watched, event)

    def _set_account(self, payload: dict[str, Any]) -> None:
        account = payload.get("account")
        if not account:
            self.account_button.setText("  ◉   Войти в аккаунт")
            return
        if account.get("type") == "chatgpt":
            self.account_button.setText("  ◉   " + (account.get("email") or "ChatGPT"))
        elif account.get("type") == "acp":
            self.account_button.setText(
                "  ◉   " + str(account.get("name") or self._agent_name())
            )
        else:
            self.account_button.setText("  ◉   API key")

    def _set_rate_limits(self, payload: dict[str, Any]) -> None:
        window = weekly_limit_from_payload(payload)
        if window is None:
            self.weekly_limit_label.setText("Неделя —")
            self.weekly_limit_bar.setValue(0)
            level = "unavailable"
            state = self._feature_state(
                FeatureId.USAGE_QUOTA,
                self._capabilities().rate_limits,
            )
            tooltip = state.reason or "Данные о недельном лимите временно недоступны"
        else:
            remaining = window.remaining_percent
            self.weekly_limit_label.setText(f"Неделя {remaining}%")
            self.weekly_limit_bar.setValue(remaining)
            level = "danger" if remaining <= 15 else "warning" if remaining <= 35 else "normal"
            tooltip = f"Осталось {remaining}% недельного лимита"
            if window.resets_at is not None:
                reset = QDateTime.fromSecsSinceEpoch(window.resets_at).toLocalTime()
                tooltip += f"\nСброс: {reset.toString('dd MMM, HH:mm')}"
        self.weekly_limit.setToolTip(tooltip)
        self.weekly_limit_bar.setProperty("level", level)
        self.weekly_limit_bar.style().unpolish(self.weekly_limit_bar)
        self.weekly_limit_bar.style().polish(self.weekly_limit_bar)

    def _set_context_usage(self, token_usage: dict[str, Any]) -> None:
        usage = context_usage(token_usage)
        if usage is None:
            self._reset_context_usage()
            return
        percent, used, window = usage
        self.context_usage_label.setText(f"Контекст {percent}%")
        self.context_usage_bar.setValue(percent)
        level = "danger" if percent >= 85 else "warning" if percent >= 65 else "normal"
        self.context_usage_bar.setProperty("level", level)
        self.context_usage_bar.style().unpolish(self.context_usage_bar)
        self.context_usage_bar.style().polish(self.context_usage_bar)
        used_text = f"{used:,}".replace(",", " ")
        window_text = f"{window:,}".replace(",", " ")
        self.context_usage_widget.setToolTip(
            f"Использовано {used_text} из {window_text} токенов по последнему обновлению {self._agent_name()}"
        )
        self.context_usage_widget.setVisible(True)

    def _reset_context_usage(self) -> None:
        self.context_usage_label.setText("Контекст —")
        self.context_usage_bar.setValue(0)
        state = self._feature_state(
            FeatureId.USAGE_CONTEXT,
            self._capabilities().context_usage,
        )
        self.context_usage_widget.setToolTip(
            "Ожидание данных об использовании контекста" if state.supported else state.reason
        )
        self.context_usage_widget.setVisible(True)

    def _account_menu(self) -> None:
        menu = QMenu(self)
        account = self.service.account
        capabilities = self._capabilities()
        auth_methods = self.agent_manifest.auth_methods
        if account and capabilities.authentication:
            title = account.get("email") or account.get("type", "Аккаунт")
            info = menu.addAction(str(title))
            info.setEnabled(False)
            menu.addSeparator()
            if callable(getattr(self.service, "logout", None)):
                menu.addAction("Выйти", self.service.logout)
        elif auth_methods:
            for method in auth_methods:
                menu.addAction(
                    method.name,
                    lambda _checked=False, target=method: self._authenticate_method(target),
                )
        elif capabilities.authentication:
            menu.addAction("Войти через ChatGPT", self.service.login_chatgpt)
            menu.addAction("Войти с API-ключом", self._api_key_login)
        else:
            unavailable = menu.addAction(
                f"Авторизация не поддерживается агентом {self._agent_name()}"
            )
            unavailable.setEnabled(False)
        if callable(getattr(self.service, "set_executable", None)):
            menu.addSeparator()
            menu.addAction("Указать путь к CLI…", self._choose_agent_executable)
        if callable(getattr(self.service, "add_profile", None)):
            menu.addAction("Добавить ACP-агента…", self._add_acp_profile)
            profile = getattr(self.service, "profile", None)
            managed = (
                self.integration_manager.integration_for_profile(profile.id)
                if self.integration_manager is not None and isinstance(profile, AgentProfile)
                else None
            )
            if isinstance(profile, AgentProfile) and not profile.built_in and managed is None:
                menu.addAction(
                    f"Удалить профиль «{profile.display_name}»",
                    self._remove_current_profile,
                )
        if self.integration_manager is not None:
            menu.addSeparator()
            menu.addAction("Настройки агентов…", self._open_agent_settings)
        menu.exec(self.account_button.mapToGlobal(self.account_button.rect().bottomLeft()))

    def _open_agent_settings(self) -> None:
        if self.integration_manager is None:
            return
        if self._agent_settings_dialog is None:
            self._agent_settings_dialog = AgentSettingsDialog(
                self.service,
                self.settings,
                self.integration_manager,
                self,
            )
        self._agent_settings_dialog.refresh()
        self._agent_settings_dialog.show()
        self._agent_settings_dialog.raise_()
        self._agent_settings_dialog.activateWindow()

    def _authenticate_method(self, method: AuthMethod) -> None:
        secret = ""
        if method.kind == "secret":
            dialog = ApiKeyDialog(self._agent_name(), self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            secret = dialog.input.text().strip()
            dialog.input.clear()
            if not secret:
                return
        authenticate = getattr(self.service, "authenticate", None)
        if callable(authenticate):
            authenticate(method.id, secret)

    def _add_acp_profile(self) -> None:
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
            self._show_error(str(exc))
            return
        self._populate_agents()
        index = self.agent_combo.findData(profile.id)
        if index >= 0:
            self.agent_combo.setCurrentIndex(index)

    def _remove_current_profile(self) -> None:
        profile = getattr(self.service, "profile", None)
        if not isinstance(profile, AgentProfile) or profile.built_in:
            return
        answer = QMessageBox.question(
            self,
            "Удалить профиль агента",
            f"Удалить профиль «{profile.display_name}»? История самого агента не удаляется.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.service.remove_profile(profile.id)
        self._populate_agents()
        if self.agent_combo.count():
            next_id = str(self.agent_combo.itemData(0) or "")
            if next_id and self.service.activate(next_id):
                self.agent_combo.setCurrentIndex(0)
                if hasattr(type(self.settings), "selected_agent_id"):
                    self.settings.selected_agent_id = next_id

    def _choose_agent_executable(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            f"Исполняемый файл {self._agent_name()}",
        )
        if not path:
            return
        setter = getattr(self.service, "set_executable", None)
        if callable(setter) and setter(self._agent_id(), path):
            self._show_notice(
                f"Путь к {self._agent_name()} сохранён.",
                "info",
            )

    def _api_key_login(self) -> None:
        dialog = ApiKeyDialog(self._agent_name(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.input.text().strip():
            key = dialog.input.text().strip()
            dialog.input.clear()
            self.service.login_api_key(key)

    def _login_started(self, result: dict[str, Any]) -> None:
        if result.get("authUrl"):
            QDesktopServices.openUrl(QUrl(str(result["authUrl"])))
            self._show_notice("Завершите вход в открывшемся браузере.", "info", 8000)

    @staticmethod
    def _permission_summary(permissions: object) -> str:
        if not isinstance(permissions, dict) or not permissions:
            return "Область дополнительных разрешений не указана."
        labels = {
            "network": "Доступ к сети",
            "fileSystem": "Доступ к файловой системе",
            "filesystem": "Доступ к файловой системе",
            "writableRoots": "Дополнительные папки для записи",
        }
        lines: list[str] = []
        for key, value in permissions.items():
            label = labels.get(str(key), str(key))
            if isinstance(value, dict):
                enabled = value.get("enabled")
                detail = "разрешён" if enabled is True else "запрещён" if enabled is False else "запрошен"
                paths = value.get("writableRoots") or value.get("paths")
                if isinstance(paths, list) and paths:
                    detail += ": " + ", ".join(map(str, paths))
            elif isinstance(value, list):
                detail = ", ".join(map(str, value))
            elif isinstance(value, bool):
                detail = "разрешён" if value else "запрещён"
            else:
                detail = str(value)
            lines.append(f"• {label}: {detail}")
        return "\n".join(lines)

    def _approval_requested(self, request_id: object, method: str, params: dict[str, Any]) -> None:
        self._set_thinking_activity("ИИ ждёт подтверждения")
        project = getattr(self.service, "current_project", "") or "не выбрана"
        reason = str(params.get("reason") or "Причина не указана.").strip()
        if "commandExecution" in method:
            command = params.get("command") or "Команда не указана"
            if isinstance(command, list):
                command = " ".join(map(str, command))
            title = "Выполнение команды"
            detail = f"Команда:\n{command}\n\nРабочая папка: {project}\n\nЗачем это нужно:\n{reason}"
        elif "fileChange" in method:
            title = "Изменение файлов"
            paths = params.get("paths") or params.get("files") or []
            affected = "\n".join(f"• {path}" for path in paths) if isinstance(paths, list) else str(paths)
            scope = affected or f"Внутри проекта: {project}"
            detail = f"{self._agent_name()} запрашивает изменение файлов.\n\nОбласть:\n{scope}\n\nЗачем это нужно:\n{reason}"
        else:
            title = "Дополнительные разрешения"
            detail = (
                f"{self._agent_name()} запрашивает доступ за пределами обычного режима.\n\n"
                f"Запрошено:\n{self._permission_summary(params.get('permissions', {}))}"
                f"\n\nЗачем это нужно:\n{reason}"
            )
        self._approval_queue.append(
            ApprovalPrompt(request_id, method, params, title, detail)
        )
        self._show_next_approval()
        summary = " ".join(detail.split())
        if len(summary) > 150:
            summary = summary[:147] + "…"
        self._show_desktop_notification(f"{self._agent_name()} ждёт подтверждения", summary)

    def _permission_requested(self, request: object) -> None:
        if not isinstance(request, PermissionRequest):
            return
        self._set_thinking_activity("ИИ ждёт подтверждения")
        self._approval_queue.append(
            ApprovalPrompt(
                request.request_id,
                "",
                {},
                request.title,
                request.detail,
                request.options,
            )
        )
        self._show_next_approval()
        summary = " ".join(request.detail.split())
        if len(summary) > 150:
            summary = summary[:147] + "…"
        self._show_desktop_notification(
            f"{self._agent_name()} ждёт подтверждения",
            summary,
        )

    def _show_next_approval(self) -> None:
        if self._current_approval is not None or not self._approval_queue:
            return
        self._current_approval = self._approval_queue.pop(0)
        self.approval_card.set_request(
            self._current_approval.title,
            self._current_approval.detail,
            self._current_approval.options,
        )
        self.approval_card.setVisible(True)

    def _answer_inline_approval(self, decision: str) -> None:
        if self._current_approval is None:
            return
        prompt = self._current_approval
        self._current_approval = None
        self.approval_card.setVisible(False)
        resolver = getattr(self.service, "resolve_permission", None)
        if not prompt.method and callable(resolver):
            resolver(prompt.request_id, decision)
        else:
            self.service.answer_approval(
                prompt.request_id,
                decision,
                prompt.method,
                prompt.params,
            )
        self._show_next_approval()

    def _user_input_requested(self, request_id: object, params: dict[str, Any]) -> None:
        self._set_thinking_activity("ИИ ждёт ответа")
        self._user_input_queue.append((request_id, params))
        self._show_next_user_input()
        questions = params.get("questions", [])
        prompt = f"{self._agent_name()} ожидает подтверждение или ответ"
        if questions and isinstance(questions[0], dict):
            prompt = str(questions[0].get("question") or prompt)
        self._show_desktop_notification(f"{self._agent_name()} ждёт ответа", prompt)

    def _show_next_user_input(self) -> None:
        if self._current_user_input is not None or not self._user_input_queue:
            return
        self._current_user_input = self._user_input_queue.pop(0)
        _request_id, params = self._current_user_input
        self.user_input_card.set_request(params)
        self.user_input_card.setVisible(True)

    def _answer_inline_user_input(self, answers: dict[str, list[str]]) -> None:
        if self._current_user_input is None:
            return
        request_id, _params = self._current_user_input
        self._current_user_input = None
        self.user_input_card.setVisible(False)
        self.service.answer_user_input(request_id, answers)
        self._show_next_user_input()

    def _cancel_inline_user_input(self) -> None:
        if self._current_user_input is None:
            return
        request_id, _params = self._current_user_input
        self._current_user_input = None
        self.user_input_card.setVisible(False)
        self.service.cancel_server_request(request_id)
        self._show_next_user_input()

    def _server_request_resolved(self, request_id: object) -> None:
        self._approval_queue = [
            item for item in self._approval_queue if item.request_id != request_id
        ]
        if (
            self._current_approval is not None
            and self._current_approval.request_id == request_id
        ):
            self._current_approval = None
            self.approval_card.setVisible(False)
            self._show_next_approval()
        self._user_input_queue = [
            item for item in self._user_input_queue if item[0] != request_id
        ]
        if self._current_user_input is not None and self._current_user_input[0] == request_id:
            self._current_user_input = None
            self.user_input_card.setVisible(False)
            self._show_next_user_input()

    def _clear_server_requests(self) -> None:
        self._approval_queue.clear()
        self._current_approval = None
        self.approval_card.setVisible(False)
        self._user_input_queue.clear()
        self._current_user_input = None
        self.user_input_card.setVisible(False)

    def _implement_plan(self) -> None:
        self.plan_confirmation_card.setVisible(False)
        self._pending_plan_text = ""
        index = self.access_combo.findData(AccessMode.WORKSPACE_WRITE.value)
        if index >= 0:
            self.access_combo.setCurrentIndex(index)
        message = QueuedMessage(
            "Реализуй утверждённый план.",
            [],
            self.model_combo.currentData() or "",
            self.effort_combo.currentData(),
            AccessMode.WORKSPACE_WRITE,
            None,
            run_mode_id=AccessMode.WORKSPACE_WRITE.value,
        )
        if self._turn_active:
            self._message_queue.insert(0, message)
            self._render_message_queue()
        else:
            self._dispatch_message(message)

    def _dismiss_plan_confirmation(self, send_queued: bool = True) -> None:
        self.plan_confirmation_card.setVisible(False)
        self._pending_plan_text = ""
        if send_queued:
            QTimer.singleShot(0, self._send_next_queued)

    def _show_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)
        QMessageBox.warning(self, self._agent_name(), message)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self.tray_icon.hide()
        self.settings.save_geometry(self.saveGeometry(), self.saveState())
        self.stop_server()
        super().closeEvent(event)
