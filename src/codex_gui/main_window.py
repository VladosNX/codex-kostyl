from __future__ import annotations

import json
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, QElapsedTimer, QEvent, QObject, QProcess, QSize, Qt, QTimer, QUrl, Signal
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
    QResizeEvent,
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

from .models import PLAN_MODE_VALUE, AccessMode, Attachment, ModelInfo, ThreadSummary, weekly_limit_from_payload
from .rendering import MarkdownRenderer, plain_pre
from .service import CodexService
from .settings import AppSettings

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
STREAM_RENDER_INTERVAL_MS = 60
MAX_RENDERED_MESSAGE_CHARS = 160_000
MAX_ACTIVITY_CONTENT_CHARS = 120_000
MAX_HISTORY_TURNS = 40
MAX_HISTORY_ITEMS = 300


@dataclass(slots=True)
class QueuedMessage:
    text: str
    attachments: list[Attachment]
    model: str
    effort: str | None
    access_mode: AccessMode
    collaboration_mode: str | None
    queue_syntax: str | None = None


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

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setObjectName("composer")
        self.setPlaceholderText("Попросите Codex изменить код, найти ошибку или объяснить проект…")
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
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
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
        self.copy_button.setText("⧉")
        self.copy_button.setToolTip("Скопировать полный текст сообщения")
        self.copy_button.setFixedSize(27, 25)
        actions.addWidget(self.copy_button)
        self.edit_button: QToolButton | None = None
        if role == "user":
            self.edit_button = QToolButton()
            self.edit_button.setObjectName("messageActionButton")
            self.edit_button.setText("✎")
            self.edit_button.setToolTip("Перенести текст в поле ввода для редактирования")
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
        self.label = QLabel("✦  ИИ думает   ")
        self.label.setObjectName("thinkingLabel")
        layout.addWidget(self.label)
        layout.addStretch(1)
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(420)
        self._timer.timeout.connect(self._animate)

    def start(self) -> None:
        self._frame = 0
        self.label.setText("✦  ИИ думает   ")
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _animate(self) -> None:
        self._frame = (self._frame + 1) % 4
        dots = "." * self._frame
        self.label.setText(f"✦  ИИ думает{dots:<3}")


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
        self.header = QToolButton()
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
        title = QLabel("UPDATED PLAN")
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
        self.title = QLabel("Codex запрашивает ответ")
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
            self.title.setText("Codex запрашивает ответы")
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

        actions = QHBoxLayout()
        actions.addStretch(1)
        for text, decision, object_name in (
            ("Отклонить", "decline", "approvalSecondaryButton"),
            ("Отменить ход", "cancel", "approvalDangerButton"),
            ("На сессию", "acceptForSession", "approvalSecondaryButton"),
            ("Разрешить", "accept", "approvalPrimaryButton"),
        ):
            button = QPushButton(text)
            button.setObjectName(object_name)
            button.clicked.connect(
                lambda _checked=False, value=decision: self.decisionSelected.emit(value)
            )
            actions.addWidget(button)
        layout.addLayout(actions)

    def set_request(self, title: str, detail: str) -> None:
        self.title.setText(title)
        self.detail.setText(detail.strip())


class ApiKeyDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Вход с API-ключом")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Ключ передается Codex и не сохраняется приложением."))
        self.input = QLineEdit()
        self.input.setEchoMode(QLineEdit.EchoMode.Password)
        self.input.setPlaceholderText("sk-…")
        layout.addWidget(self.input)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, service: CodexService, settings: AppSettings, stop_server: Any) -> None:
        super().__init__()
        self.service = service
        self.settings = settings
        self.stop_server = stop_server
        self.models: list[ModelInfo] = []
        self.attachments: list[Attachment] = []
        self.cards: dict[str, MessageCard | ActivityCard] = {}
        self._execution_plan_cards: dict[str, ExecutionPlanCard] = {}
        self._last_activity_group: ActivityGroupCard | None = None
        self._approval_queue: list[ApprovalPrompt] = []
        self._current_approval: ApprovalPrompt | None = None
        self._user_input_queue: list[tuple[object, dict[str, Any]]] = []
        self._current_user_input: tuple[object, dict[str, Any]] | None = None
        self._message_queue: list[QueuedMessage | QueuedCommand] = []
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
        self._closing = False
        self._build_ui()
        self._build_notifications()
        self._connect_service()
        self._clear_timeline("Добавьте рабочую папку, чтобы начать работу с Codex.")
        self._load_settings()

    def _build_ui(self) -> None:
        self.setWindowTitle("Codex")
        self.setMinimumSize(920, 640)
        self.resize(1280, 820)
        splitter = QSplitter()
        splitter.setObjectName("mainSplitter")
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_chat())
        splitter.setSizes([272, 1008])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("Запуск Codex app-server…")

    def _build_notifications(self) -> None:
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_icon.setToolTip("Codex")
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
        else:
            QProcess.startDetached(
                "notify-send",
                ["--app-name=Codex Kostyl", title, message],
            )
        QApplication.alert(self, 4000)

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

        self.new_chat_button = QPushButton("＋   Новый чат")
        self.new_chat_button.setObjectName("newChatButton")
        self.new_chat_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.thread_list = QListWidget()
        self.thread_list.setObjectName("threadList")
        self.thread_list.setSpacing(3)
        self.thread_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.new_chat_button)
        section = QLabel("НЕДАВНИЕ ЧАТЫ")
        section.setObjectName("sectionLabel")
        layout.addWidget(section)
        layout.addWidget(self.thread_list, 1)
        self.account_button = QPushButton("  ◉   Аккаунт")
        self.account_button.setObjectName("accountButton")
        layout.addWidget(self.account_button)
        self.new_chat_button.clicked.connect(self._new_chat)
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
        topbar_layout.setContentsMargins(28, 13, 22, 13)
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
        self.header_status = QLabel("●  Готов")
        self.header_status.setObjectName("readyStatus")
        topbar_layout.addWidget(self.header_status)
        layout.addWidget(topbar)

        self.model_combo = QComboBox()
        self.model_combo.setObjectName("optionCombo")
        self.model_combo.setMinimumWidth(180)
        self.effort_combo = QComboBox()
        self.effort_combo.setObjectName("optionCombo")
        self.access_combo = QComboBox()
        self.access_combo.setObjectName("optionCombo")
        for mode in AccessMode:
            self.access_combo.addItem(mode.title, mode.value)
        self.access_combo.addItem("Plan Mode", PLAN_MODE_VALUE)

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
        shell_layout.setContentsMargins(48, 10, 48, 22)
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
        project_icon = QLabel("⌂")
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
        add_project = QToolButton()
        add_project.setObjectName("projectBubbleButton")
        add_project.setText("＋")
        add_project.setToolTip("Добавить рабочую папку")
        add_project.setFixedSize(25, 25)
        project_layout.addWidget(add_project)
        composer_area_layout.addWidget(
            self.project_bubble,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )

        self.slash_panel = SlashCommandPanel()
        composer_area_layout.addWidget(self.slash_panel)

        self.attachment_row = QHBoxLayout()
        self.attachment_row.addStretch(1)
        composer_panel_layout.addLayout(self.attachment_row)

        self.composer = Composer()
        composer_panel_layout.addWidget(self.composer)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(4)
        attach = QPushButton("＋")
        attach.setObjectName("attachButton")
        attach.setToolTip("Прикрепить изображение или файл")
        attach.setFixedSize(32, 32)
        controls_row.addWidget(attach)
        controls_row.addWidget(self.model_combo)
        controls_row.addWidget(self.effort_combo)
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

        self.send_button = QPushButton("↑")
        self.send_button.setObjectName("sendButton")
        self.send_button.setToolTip("Отправить · Ctrl+Enter")
        self.send_button.setFixedSize(34, 34)
        self.stop_button = QPushButton("■")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setToolTip("Остановить выполнение")
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

        hint = QLabel("Codex может ошибаться. Проверяйте команды и изменения файлов.")
        hint.setObjectName("composerHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        shell_layout.addWidget(hint)
        layout.addWidget(composer_shell)

        attach.clicked.connect(self._choose_attachments)
        add_project.clicked.connect(self._add_project)
        self.project_combo.currentIndexChanged.connect(self._project_changed)
        self.composer.filesDropped.connect(self._add_attachments)
        self.composer.sendRequested.connect(self._send)
        self.composer.textChanged.connect(self._composer_text_changed)
        self.composer.slashNavigate.connect(self._navigate_slash_commands)
        self.composer.slashComplete.connect(self._complete_slash_command)
        self.composer.slashActivate.connect(self.slash_panel.activate_selected)
        self.composer.slashDismiss.connect(self._dismiss_slash_panel)
        self.slash_panel.commandActivated.connect(self._activate_slash_command)
        self.send_button.clicked.connect(self._send)
        self.stop_button.clicked.connect(self.service.interrupt)
        self.queue_resume_button.clicked.connect(self._resume_queue)
        self.queue_clear_button.clicked.connect(self._clear_message_queue)
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        self.effort_combo.currentIndexChanged.connect(self._effort_changed)
        self.access_combo.currentIndexChanged.connect(self._access_changed)
        return panel

    def _connect_service(self) -> None:
        self.service.ready.connect(lambda: self.statusBar().showMessage("Codex подключен", 3000))
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
        self.service.approvalRequested.connect(self._approval_requested)
        self.service.userInputRequested.connect(self._user_input_requested)
        self.service.serverRequestResolved.connect(self._server_request_resolved)
        current_thread_changed = getattr(self.service, "currentThreadChanged", None)
        if current_thread_changed is not None:
            current_thread_changed.connect(lambda _thread_id: self._update_slash_panel())

    def _load_settings(self) -> None:
        for path in self.settings.projects:
            self.project_combo.addItem(Path(path).name, path)
        saved_project = self.settings.get("last_project")
        index = self.project_combo.findData(saved_project)
        if index >= 0:
            self.project_combo.setCurrentIndex(index)
        saved_selection = self.settings.get("run_mode", self.settings.access_mode.value)
        mode_index = self.access_combo.findData(saved_selection)
        if mode_index >= 0:
            self.access_combo.blockSignals(True)
            self.access_combo.setCurrentIndex(mode_index)
            self.access_combo.blockSignals(False)
        if saved_selection == PLAN_MODE_VALUE:
            self.access_combo.setToolTip("Plan Mode: анализ и планирование без изменения файлов")
        geometry, state = self.settings.restore_geometry()
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
        if not state.isEmpty():
            self.restoreState(state)

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
        self._clear_timeline("Выберите сохраненный чат или начните новый.")

    def _set_threads(self, threads: list[ThreadSummary]) -> None:
        selected = self.service.current_thread_id
        self.thread_list.blockSignals(True)
        self.thread_list.clear()
        for thread in threads:
            folder = Path(thread.cwd).name if thread.cwd else "Без рабочей папки"
            item = QListWidgetItem(f"{thread.title}\n{folder}")
            item.setSizeHint(QSize(0, 54))
            item.setData(Qt.ItemDataRole.UserRole, thread.id)
            item.setData(int(Qt.ItemDataRole.UserRole) + 1, thread.cwd)
            item.setToolTip(f"{thread.cwd}\n{thread.id}")
            self.thread_list.addItem(item)
            if thread.id == selected:
                self.thread_list.setCurrentItem(item)
        self.thread_list.blockSignals(False)

    def _thread_activated(self, item: QListWidgetItem | None) -> None:
        if item:
            thread_id = item.data(Qt.ItemDataRole.UserRole)
            if thread_id and thread_id != self.service.current_thread_id:
                cwd = str(item.data(int(Qt.ItemDataRole.UserRole) + 1) or "")
                if not self._switch_to_thread_project(cwd):
                    return
                self.chat_title.setText(item.text().splitlines()[0])
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
        saved = self.settings.get("model")
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

    def _model_changed(self, index: int) -> None:
        if index < 0 or index >= len(self.models):
            return
        model = self.models[index]
        self.settings.set("model", model.id)
        saved_effort = self.settings.get("effort", model.default_effort or "")
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
        self.settings.set("effort", self.effort_combo.currentData() or "")
        self._note_next_request_setting()

    def _access_changed(self, _index: int) -> None:
        selected = str(self.access_combo.currentData())
        if selected == PLAN_MODE_VALUE:
            self.settings.set("run_mode", PLAN_MODE_VALUE)
            self.access_combo.setToolTip("Plan Mode: анализ и планирование без изменения файлов")
            self._note_next_request_setting()
            return
        try:
            mode = AccessMode(selected)
        except ValueError:
            return
        if mode is AccessMode.FULL_ACCESS and not self._danger_acknowledged:
            answer = QMessageBox.warning(
                self,
                "Полный доступ",
                "Codex сможет читать и изменять файлы вне рабочей папки, а также выполнять "
                "команды без дополнительных подтверждений. Включить полный доступ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.access_combo.setCurrentIndex(self.access_combo.findData(AccessMode.WORKSPACE_WRITE.value))
                return
            self._danger_acknowledged = True
        self.settings.access_mode = mode
        self.settings.set("run_mode", mode.value)
        self.access_combo.setToolTip("")
        self._note_next_request_setting()

    def _note_next_request_setting(self) -> None:
        if self._turn_active:
            self.statusBar().showMessage(
                "Настройка сохранена и применится к следующему сообщению",
                3500,
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
            if not text.startswith("/") or any(character.isspace() for character in text):
                return []
            if text == self._slash_dismissed_text:
                return []
            prefix = text
        has_thread = bool(getattr(self.service, "current_thread_id", ""))
        matches: list[tuple[SlashCommand, bool, str]] = []
        for command in SLASH_COMMANDS:
            if not command.syntax.startswith(prefix):
                continue
            available = not command.needs_thread or has_thread
            reason = "" if available else "Сначала создайте текущий чат"
            matches.append((command, available, reason))
        return matches

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
        command = SLASH_COMMANDS_BY_NAME[selected]
        completion = command.syntax + (" " if command.accepts_arguments else "")
        self.composer.setPlainText(completion)
        cursor = self.composer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.composer.setTextCursor(cursor)

    def _dismiss_slash_panel(self) -> None:
        self._slash_dismissed_text = self.composer.toPlainText()
        self._slash_help_visible = False
        self.slash_panel.setVisible(False)
        self.composer.set_slash_menu_state(False, False)

    def _activate_slash_command(self, name: str) -> None:
        self._execute_slash_command(name, "", f"/{name}")

    def _show_slash_help(self) -> None:
        self.composer.clear()
        self._slash_dismissed_text = None
        self._slash_help_visible = True
        self._update_slash_panel()
        self.composer.setFocus()

    @staticmethod
    def _parse_slash_command(text: str) -> tuple[str, str] | None:
        if not text.startswith("/"):
            return None
        token, separator, arguments = text.partition(" ")
        name = token[1:]
        if name not in SLASH_COMMANDS_BY_NAME:
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
        command = SLASH_COMMANDS_BY_NAME[name]
        if command.needs_thread and not getattr(self.service, "current_thread_id", ""):
            self.statusBar().showMessage("Команда недоступна: сначала создайте текущий чат", 5000)
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
            self.statusBar().showMessage(
                f"Команда добавлена в очередь · {len(self._message_queue)}",
                4000,
            )
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
            self._show_error("Codex app-server ещё не подключен")
            return
        if not getattr(self.service, "current_project", ""):
            self._add_project()
            if not getattr(self.service, "current_project", ""):
                self.statusBar().showMessage(
                    "Сообщение сохранено в редакторе — выберите рабочую папку",
                    5000,
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
        selected_mode = str(self.access_combo.currentData())
        collaboration_mode = (
            PLAN_MODE_VALUE
            if force_plan or selected_mode == PLAN_MODE_VALUE
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
        )
        if self._turn_active or self._queue_action_pending:
            self._message_queue.append(message)
            self._render_message_queue()
            self.statusBar().showMessage(
                f"Сообщение добавлено в очередь · {len(self._message_queue)}",
                4000,
            )
            return
        if not self.plan_confirmation_card.isHidden():
            self._dismiss_plan_confirmation(send_queued=False)
        self._dispatch_message(message)

    def _dispatch_message(self, message: QueuedMessage) -> bool:
        if getattr(self.service, "connected", True) is False:
            self.statusBar().showMessage("Очередь приостановлена: Codex не подключен", 5000)
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
        if command.name == "compact":
            self.service.compact_thread()
            return
        if command.name == "review":
            self.service.start_review(command.arguments)
            return
        if command.name == "fork":
            self._queue_action_pending = True
            self._render_message_queue()
            self.service.fork_thread(self._fork_finished)
            return
        if command.name == "new":
            if not self._prepare_new_chat(clear_queue=False):
                self._queue_paused = True
                self._render_message_queue()
                return
            QTimer.singleShot(0, self._send_next_queued)

    def _fork_finished(self, success: bool) -> None:
        self._queue_action_pending = False
        if not success:
            self._queue_paused = True
        self._render_message_queue()
        if success:
            QTimer.singleShot(0, self._send_next_queued)

    def _send_next_queued(self) -> None:
        if self._turn_active or self._queue_action_pending or self._queue_paused or not self._message_queue:
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
            self._message_queue.pop(index)
            self._render_message_queue()

    def _clear_message_queue(self) -> None:
        self._message_queue.clear()
        self._queue_paused = False
        self._render_message_queue()

    def _render_message_queue(self) -> None:
        while self.queue_items_layout.count():
            item = self.queue_items_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        count = len(self._message_queue)
        state = "ПРИОСТАНОВЛЕНА" if self._queue_paused else "ОЧЕРЕДЬ"
        self.queue_label.setText(f"{state}  ·  {count}")
        for index, queued in enumerate(self._message_queue):
            if isinstance(queued, QueuedCommand):
                preview = queued.syntax
            else:
                preview = queued.queue_syntax or " ".join(queued.text.split()) or "Вложения"
            if len(preview) > 90:
                preview = preview[:87] + "…"
            button = QToolButton()
            button.setObjectName("queueItemButton")
            button.setText(f"{index + 1}. {preview}   ×")
            button.setToolTip("Удалить это сообщение из очереди")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.clicked.connect(
                lambda _checked=False, target=index: self._remove_queued_message(target)
            )
            self.queue_items_layout.addWidget(button)
        self.queue_resume_button.setEnabled(
            bool(count) and not self._turn_active and not self._queue_action_pending
        )
        self.queue_panel.setVisible(bool(count))
        navigation_enabled = not self._turn_active and not self._queue_action_pending
        self.new_chat_button.setEnabled(navigation_enabled)
        self.thread_list.setEnabled(navigation_enabled)
        self.project_combo.setEnabled(navigation_enabled)

    def _selected_model(self) -> ModelInfo | None:
        model_id = self.model_combo.currentData()
        return next((item for item in self.models if item.id == model_id), None)

    def _render_thread(self, thread: dict[str, Any]) -> None:
        self._clear_timeline()
        items, omitted_turns, omitted_items = recent_thread_items(thread)
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
            self.timeline_layout.insertWidget(0, empty, 1, Qt.AlignmentFlag.AlignCenter)

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
        kind = str(item.get("type", "unknown"))
        card = self.cards.get(item_id)
        if kind == "userMessage":
            text = self._user_message_text(item.get("content", []))
            if not isinstance(card, MessageCard):
                card = MessageCard("user", text)
                self._add_card(item_id, card)
            else:
                card.set_text(text)
        elif kind == "agentMessage":
            text = str(item.get("text", ""))
            if not isinstance(card, MessageCard):
                card = MessageCard("agent", text)
                self._add_card(item_id, card)
            elif complete or text:
                card.set_text(text)
        elif kind == "plan":
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
            summary = item.get("summary", [])
            text = "\n".join(summary) if isinstance(summary, list) else str(summary or item.get("content", ""))
            self._activity(item_id, "Размышления", text)
        elif kind == "commandExecution":
            command = item.get("command", "Команда")
            if isinstance(command, list):
                command = " ".join(map(str, command))
            output = str(item.get("aggregatedOutput") or "")
            status = item.get("status", "inProgress")
            self._activity(item_id, f"Терминал · {status}: {command}", output)
        elif kind == "fileChange":
            changes = item.get("changes", [])
            paths = [str(change.get("path", "")) for change in changes if isinstance(change, dict)]
            diffs = [str(change.get("diff", "")) for change in changes if isinstance(change, dict)]
            self._activity(item_id, "Изменения файлов: " + ", ".join(paths), "\n".join(diffs))
        elif kind == "contextCompaction":
            self._activity(
                item_id,
                "Контекст сжат",
                self._compact_item(item) or "История чата сжата для продолжения работы.",
            )
        elif kind == "enteredReviewMode":
            self._activity(
                item_id,
                "Режим ревью запущен",
                self._compact_item(item) or "Codex проверяет выбранные изменения.",
            )
        elif kind == "exitedReviewMode":
            self._activity(
                item_id,
                "Режим ревью завершён",
                self._compact_item(item) or "Codex завершил проверку изменений.",
            )
        elif kind in {"mcpToolCall", "dynamicToolCall", "webSearch", "collabToolCall"}:
            title = str(item.get("tool") or item.get("query") or item.get("type"))
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
            card = ActivityCard(title, content)
            self.cards[item_id] = card
            self._remove_empty_hint()
            if self._last_activity_group is None:
                self._last_activity_group = ActivityGroupCard()
                self.timeline_layout.insertWidget(
                    self.timeline_layout.count() - 1,
                    self._last_activity_group,
                )
                self._move_thinking_to_bottom()
            self._last_activity_group.add_activity(card)
        else:
            card.toggle.setText(title)
            card.set_content(content)
        return card

    def _agent_delta(self, item_id: str, delta: str) -> None:
        card = self.cards.get(item_id)
        if not isinstance(card, MessageCard):
            card = MessageCard("agent")
            self._add_card(item_id, card)
        card.append(delta)
        self._scroll_bottom()

    def _plan_delta(self, item_id: str, delta: str) -> None:
        card = self.cards.get(item_id)
        if not isinstance(card, MessageCard):
            card = MessageCard("agent")
            self._add_card(item_id, card)
        card.append(delta)
        self._scroll_bottom()

    def _turn_plan_updated(self, params: dict[str, Any]) -> None:
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
        card = self.cards.get(item_id)
        if not isinstance(card, ActivityCard):
            card = self._activity(item_id, "Размышления", "")
        card.append(delta)

    def _command_delta(self, item_id: str, delta: str) -> None:
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
        self.send_button.setText("＋" if active else "↑")
        self.send_button.setToolTip(
            "Добавить сообщение в очередь · Ctrl+Enter"
            if active
            else "Отправить · Ctrl+Enter"
        )
        self.stop_button.setVisible(active)
        self.composer.setEnabled(True)
        self.new_chat_button.setEnabled(not active)
        self.thread_list.setEnabled(not active)
        self.project_combo.setEnabled(not active)
        self.model_combo.setEnabled(True)
        self.effort_combo.setEnabled(True)
        self.access_combo.setEnabled(True)
        self._render_message_queue()
        self.statusBar().showMessage("Codex работает…" if active else f"Ход: {status}", 4000)
        if was_active and not active:
            self._clear_server_requests()
            elapsed_ms = self._turn_timer.elapsed() if self._turn_timer.isValid() else 0
            self._turn_timer.invalidate()
            self._add_turn_duration(status, elapsed_ms)
            if status == "failed":
                self._show_desktop_notification("Codex", "Запрос завершился с ошибкой")
            elif status in {"interrupted", "cancelled", "canceled"}:
                self._show_desktop_notification("Codex", "Выполнение запроса остановлено")
            else:
                self._show_desktop_notification("Codex", "Выполнение запроса завершено")
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
        omitted = {"id", "type", "status"}
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
        else:
            self.account_button.setText("  ◉   API key")

    def _set_rate_limits(self, payload: dict[str, Any]) -> None:
        window = weekly_limit_from_payload(payload)
        if window is None:
            self.weekly_limit_label.setText("Неделя —")
            self.weekly_limit_bar.setValue(0)
            level = "unavailable"
            tooltip = "Недельный лимит недоступен для текущего типа авторизации"
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
            f"Использовано {used_text} из {window_text} токенов по последнему обновлению Codex"
        )
        self.context_usage_widget.setVisible(True)

    def _reset_context_usage(self) -> None:
        self.context_usage_label.setText("Контекст —")
        self.context_usage_bar.setValue(0)
        self.context_usage_widget.setVisible(False)

    def _account_menu(self) -> None:
        menu = QMenu(self)
        account = self.service.account
        if account:
            title = account.get("email") or account.get("type", "Аккаунт")
            info = menu.addAction(str(title))
            info.setEnabled(False)
            menu.addSeparator()
            menu.addAction("Выйти", self.service.logout)
        else:
            menu.addAction("Войти через ChatGPT", self.service.login_chatgpt)
            menu.addAction("Войти с API-ключом", self._api_key_login)
        menu.exec(self.account_button.mapToGlobal(self.account_button.rect().bottomLeft()))

    def _api_key_login(self) -> None:
        dialog = ApiKeyDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.input.text().strip():
            key = dialog.input.text().strip()
            dialog.input.clear()
            self.service.login_api_key(key)

    def _login_started(self, result: dict[str, Any]) -> None:
        if result.get("authUrl"):
            QDesktopServices.openUrl(QUrl(str(result["authUrl"])))
            self.statusBar().showMessage("Завершите вход в браузере")

    def _approval_requested(self, request_id: object, method: str, params: dict[str, Any]) -> None:
        if "commandExecution" in method:
            command = params.get("command") or "Команда не указана"
            if isinstance(command, list):
                command = " ".join(map(str, command))
            title = "Выполнение команды"
            detail = f"{command}\n\n{params.get('reason', '')}"
        elif "fileChange" in method:
            title = "Изменение файлов"
            detail = f"Codex запрашивает разрешение на изменение файлов.\n\n{params.get('reason', '')}"
        else:
            title = "Дополнительные разрешения"
            permissions = params.get("permissions", {})
            rendered_permissions = (
                json.dumps(permissions, ensure_ascii=False, indent=2)
                if isinstance(permissions, dict)
                else str(permissions)
            )
            detail = (
                "Codex запрашивает дополнительные разрешения.\n\n"
                f"{params.get('reason', '')}\n\nЗапрошено:\n{rendered_permissions}"
            )
        self._approval_queue.append(
            ApprovalPrompt(request_id, method, params, title, detail)
        )
        self._show_next_approval()
        summary = " ".join(detail.split())
        if len(summary) > 150:
            summary = summary[:147] + "…"
        self._show_desktop_notification("Codex ждёт подтверждения", summary)

    def _show_next_approval(self) -> None:
        if self._current_approval is not None or not self._approval_queue:
            return
        self._current_approval = self._approval_queue.pop(0)
        self.approval_card.set_request(
            self._current_approval.title,
            self._current_approval.detail,
        )
        self.approval_card.setVisible(True)

    def _answer_inline_approval(self, decision: str) -> None:
        if self._current_approval is None:
            return
        prompt = self._current_approval
        self._current_approval = None
        self.approval_card.setVisible(False)
        self.service.answer_approval(
            prompt.request_id,
            decision,
            prompt.method,
            prompt.params,
        )
        self._show_next_approval()

    def _user_input_requested(self, request_id: object, params: dict[str, Any]) -> None:
        self._user_input_queue.append((request_id, params))
        self._show_next_user_input()
        questions = params.get("questions", [])
        prompt = "Codex ожидает подтверждение или ответ"
        if questions and isinstance(questions[0], dict):
            prompt = str(questions[0].get("question") or prompt)
        self._show_desktop_notification("Codex ждёт ответа", prompt)

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
        QMessageBox.warning(self, "Codex", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self.tray_icon.hide()
        self.settings.save_geometry(self.saveGeometry(), self.saveState())
        self.stop_server()
        super().closeEvent(event)
