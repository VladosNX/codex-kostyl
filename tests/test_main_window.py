from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, Qt, Signal
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

from codex_gui.main_window import MainWindow, QueuedCommand
from codex_gui.models import PLAN_MODE_VALUE, AccessMode, Attachment, ModelInfo


class FakeService(QObject):
    ready = Signal()
    modelsUpdated = Signal(object)
    accountUpdated = Signal(object)
    rateLimitsUpdated = Signal(object)
    threadsUpdated = Signal(object)
    threadLoaded = Signal(dict)
    itemUpdated = Signal(dict, bool)
    agentDelta = Signal(str, str)
    reasoningDelta = Signal(str, str)
    commandDelta = Signal(str, str)
    planDelta = Signal(str, str)
    turnPlanUpdated = Signal(dict)
    tokenUsageUpdated = Signal(dict)
    turnStateChanged = Signal(str)
    errorOccurred = Signal(str)
    loginStarted = Signal(dict)
    approvalRequested = Signal(object, str, dict)
    userInputRequested = Signal(object, dict)
    serverRequestResolved = Signal(object)
    currentThreadChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.account = None
        self.sent: list[tuple] = []
        self.actions: list[tuple[str, str]] = []
        self.approvals: list[tuple] = []
        self.current_project = "/repo"
        self.current_thread_id = ""
        self.current_turn_id = ""

    def send_message(self, *args) -> None:
        self.sent.append(args)
        self.actions.append(("message", str(args[0])))

    def compact_thread(self) -> None:
        self.actions.append(("compact", ""))
        self.turnStateChanged.emit("starting")

    def start_review(self, instructions: str = "") -> None:
        self.actions.append(("review", instructions))
        self.turnStateChanged.emit("starting")

    def fork_thread(self, callback=None) -> None:
        source = self.current_thread_id
        self.actions.append(("fork", source))
        self.current_thread_id = "thr_fork"
        self.currentThreadChanged.emit(self.current_thread_id)
        if callback:
            callback(True)

    def prepare_new_thread(self) -> None:
        self.actions.append(("new", self.current_thread_id))
        self.current_thread_id = ""
        self.currentThreadChanged.emit("")

    def interrupt(self) -> None:
        pass

    def answer_approval(self, *args) -> None:
        self.approvals.append(args)


class FakeSettings:
    def __init__(self) -> None:
        self.projects: list[str] = []
        self.values: dict[str, object] = {}
        self._access_mode = AccessMode.WORKSPACE_WRITE

    def get(self, key: str, default: str = "") -> str:
        return str(self.values.get(key, default))

    def set(self, key: str, value: object) -> None:
        self.values[key] = value

    @property
    def access_mode(self) -> AccessMode:
        return self._access_mode

    @access_mode.setter
    def access_mode(self, value: AccessMode) -> None:
        self._access_mode = value

    def restore_geometry(self) -> tuple[QByteArray, QByteArray]:
        return QByteArray(), QByteArray()

    def save_geometry(self, _geometry: QByteArray, _state: QByteArray) -> None:
        pass


def make_window(qtbot, settings: FakeSettings | None = None) -> tuple[MainWindow, FakeService]:
    service = FakeService()
    window = MainWindow(service, settings or FakeSettings(), lambda: None)  # type: ignore[arg-type]
    window._show_desktop_notification = lambda *_args: None  # type: ignore[method-assign]
    qtbot.addWidget(window)
    return window, service


def test_message_is_queued_during_active_turn_and_sent_after_completion(qtbot) -> None:
    window, service = make_window(qtbot)
    window._turn_state("inProgress")
    window.composer.setPlainText("Следующее сообщение")

    window._send()

    assert service.sent == []
    assert len(window._message_queue) == 1
    assert window.queue_panel.isHidden() is False

    window._turn_state("completed")
    qtbot.waitUntil(lambda: len(service.sent) == 1)
    assert service.sent[0][0] == "Следующее сообщение"
    assert window._message_queue == []


def test_completed_plan_shows_confirmation_and_can_start_implementation(qtbot) -> None:
    window, service = make_window(qtbot)
    window.access_combo.setCurrentIndex(window.access_combo.findData(PLAN_MODE_VALUE))
    window._active_collaboration_mode = PLAN_MODE_VALUE
    window._turn_state("inProgress")
    window.access_combo.setCurrentIndex(
        window.access_combo.findData(AccessMode.WORKSPACE_WRITE.value)
    )
    window._upsert_item(
        {"id": "plan_1", "type": "plan", "text": "1. Проверить\n2. Исправить"},
        True,
    )

    window._turn_state("completed")

    assert window.plan_confirmation_card.isHidden() is False
    window.plan_confirmation_card.implement_button.click()
    assert service.sent[-1][0] == "Реализуй утверждённый план."
    assert service.sent[-1][4] is AccessMode.WORKSPACE_WRITE
    assert service.sent[-1][5] is None
    assert window.access_combo.currentData() == AccessMode.WORKSPACE_WRITE.value


def test_turn_settings_remain_editable_and_are_captured_for_next_message(qtbot) -> None:
    window, service = make_window(qtbot)
    window._set_models(
        [
            ModelInfo("model-a", "Model A", ["low"], "low"),
            ModelInfo("model-b", "Model B", ["medium", "high"], "medium"),
        ]
    )
    window._turn_state("inProgress")

    assert window.model_combo.isEnabled() is True
    assert window.effort_combo.isEnabled() is True
    assert window.access_combo.isEnabled() is True

    window.model_combo.setCurrentIndex(window.model_combo.findData("model-b"))
    window.effort_combo.setCurrentIndex(window.effort_combo.findData("high"))
    window.access_combo.setCurrentIndex(window.access_combo.findData(PLAN_MODE_VALUE))
    window.composer.setPlainText("Следующий запрос")
    window._send()

    assert service.sent == []
    queued = window._message_queue[0]
    assert queued.model == "model-b"
    assert queued.effort == "high"
    assert queued.access_mode is AccessMode.READ_ONLY
    assert queued.collaboration_mode == PLAN_MODE_VALUE


def test_unknown_saved_effort_falls_back_to_model_default(qtbot) -> None:
    settings = FakeSettings()
    settings.values["effort"] = "unsupported"
    window, _service = make_window(qtbot, settings)

    window._set_models(
        [ModelInfo("model-a", "Model A", ["low", "high"], "high")]
    )

    assert window.effort_combo.currentData() == "high"
    assert settings.values["effort"] == "high"


def test_completed_turn_leaves_duration_in_timeline(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._turn_state("starting")
    qtbot.wait(20)

    window._turn_state("completed")

    labels = [
        label
        for label in window.timeline.findChildren(QLabel)
        if label.objectName() == "turnDuration"
    ]
    assert len(labels) == 1
    assert labels[0].text().startswith("Готово за ")


def test_permission_prompt_shows_scope_and_preserves_response_context(qtbot) -> None:
    window, service = make_window(qtbot)
    params = {
        "reason": "Нужен доступ к API",
        "permissions": {"network": {"enabled": True}},
    }

    window._approval_requested(
        42,
        "item/permissions/requestApproval",
        params,
    )
    assert "enabled" in window.approval_card.detail.text()
    window._answer_inline_approval("acceptForSession")

    assert service.approvals == [
        (42, "acceptForSession", "item/permissions/requestApproval", params)
    ]


def test_context_usage_and_updated_plan_are_rendered(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._set_context_usage(
        {
            "last": {"totalTokens": 96_000},
            "total": {"totalTokens": 150_000},
            "modelContextWindow": 128_000,
        }
    )
    window._turn_plan_updated(
        {
            "turnId": "turn_1",
            "explanation": "Обновляю план",
            "plan": [{"step": "Запустить тесты", "status": "inProgress"}],
        }
    )

    assert window.context_usage_label.text() == "Контекст 75%"
    assert window.context_usage_bar.value() == 75
    assert window.context_usage_widget.isHidden() is False
    assert window._execution_plan_cards["turn_1"].explanation.text() == "Обновляю план"


def test_scroll_down_button_is_centered_and_scrolls_to_bottom(qtbot) -> None:
    window, _service = make_window(qtbot)
    window.resize(1000, 700)
    window.show()
    qtbot.wait(20)
    bar = window.scroll.verticalScrollBar()
    bar.setRange(0, 500)
    bar.setValue(0)
    window._update_scroll_down_button()

    assert window.scroll_down_button.isHidden() is False
    window._position_scroll_down_button()
    assert abs(
        window.scroll_down_button.geometry().center().x()
        - window.scroll.viewport().rect().center().x()
    ) <= 1

    window.scroll_down_button.click()
    assert bar.value() == bar.maximum()


def test_saved_full_access_does_not_warn_during_startup(qtbot, monkeypatch) -> None:
    settings = FakeSettings()
    settings.values["run_mode"] = AccessMode.FULL_ACCESS.value
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: warnings.append(True),
    )

    window, _service = make_window(qtbot, settings)

    assert window.access_combo.currentData() == AccessMode.FULL_ACCESS.value
    assert warnings == []
    assert window.context_usage_widget.parentWidget() is window.weekly_limit.parentWidget()


def test_project_selector_is_in_prompt_bubble_not_sidebar(qtbot) -> None:
    window, _service = make_window(qtbot)
    sidebar = window.findChild(QWidget, "sidebar")
    composer_panel = window.findChild(QWidget, "composerPanel")

    assert window.project_bubble.isAncestorOf(window.project_combo)
    assert sidebar is not None
    assert sidebar.isAncestorOf(window.project_combo) is False
    assert composer_panel is not None
    assert composer_panel.isAncestorOf(window.project_bubble) is False
    composer_area_layout = composer_panel.parentWidget().layout()
    assert composer_area_layout.indexOf(window.project_bubble) < composer_area_layout.indexOf(window.slash_panel)
    assert composer_area_layout.indexOf(window.slash_panel) < composer_area_layout.indexOf(composer_panel)


def test_slash_panel_appears_filters_and_shows_unavailable_reason(qtbot) -> None:
    window, _service = make_window(qtbot)

    window.composer.setPlainText("/")
    assert window.slash_panel.isHidden() is False
    assert window.slash_panel.list.count() == 6
    compact = window.slash_panel.list.item(0)
    assert compact.text().startswith("/compact\n")
    assert "Сначала создайте текущий чат" in compact.text()
    assert compact.flags() & Qt.ItemFlag.ItemIsEnabled == Qt.ItemFlag.NoItemFlags

    window.composer.setPlainText("/re")
    assert window.slash_panel.list.count() == 1
    assert window.slash_panel.list.item(0).data(Qt.ItemDataRole.UserRole) == "review"

    window.composer.setPlainText("/review инструкция")
    assert window.slash_panel.isHidden() is True


def test_slash_panel_keyboard_tab_enter_escape_and_click(qtbot) -> None:
    window, service = make_window(qtbot)
    service.current_thread_id = "thr_source"
    window.show()

    window.composer.setPlainText("/")
    assert window.slash_panel.selected_command() == "compact"
    qtbot.keyClick(window.composer, Qt.Key.Key_Down)
    assert window.slash_panel.selected_command() == "review"
    qtbot.keyClick(window.composer, Qt.Key.Key_Up)
    assert window.slash_panel.selected_command() == "compact"

    window.composer.setPlainText("/rev")
    qtbot.keyClick(window.composer, Qt.Key.Key_Tab)
    assert window.composer.toPlainText() == "/review "
    assert window.slash_panel.isHidden() is True

    window.composer.setPlainText("/co")
    qtbot.keyClick(window.composer, Qt.Key.Key_Return)
    assert service.actions[-1] == ("compact", "")
    assert window.composer.toPlainText() == ""
    window._turn_state("completed")

    window.composer.setPlainText("/")
    qtbot.keyClick(window.composer, Qt.Key.Key_Escape)
    assert window.slash_panel.isHidden() is True

    window.composer.setPlainText("/fo")
    item = window.slash_panel.list.item(0)
    rect = window.slash_panel.list.visualItemRect(item)
    qtbot.mouseClick(
        window.slash_panel.list.viewport(),
        Qt.MouseButton.LeftButton,
        pos=rect.center(),
    )
    assert service.actions[-1] == ("fork", "thr_source")
    assert service.current_thread_id == "thr_fork"


def test_help_clears_command_and_shows_full_list(qtbot) -> None:
    window, _service = make_window(qtbot)
    window.composer.setPlainText("/help")

    window._send()

    assert window.composer.toPlainText() == ""
    assert window.slash_panel.isHidden() is False
    assert window.slash_panel.list.count() == 6


def test_unknown_slash_and_absolute_path_are_regular_prompts(qtbot) -> None:
    window, service = make_window(qtbot)
    window.composer.setPlainText("/unknown")
    assert window.slash_panel.isHidden() is True
    qtbot.keyClick(window.composer, Qt.Key.Key_Return)
    assert "\n" in window.composer.toPlainText()

    window.composer.setPlainText("/unknown argument")
    window._send()
    window.composer.setPlainText("/home/user/project")
    window._send()

    assert [call[0] for call in service.sent[-2:]] == [
        "/unknown argument",
        "/home/user/project",
    ]


def test_plan_without_text_switches_immediately_and_preserves_attachments(qtbot, tmp_path) -> None:
    window, _service = make_window(qtbot)
    attachment_path = tmp_path / "notes.txt"
    attachment_path.write_text("notes")
    window.attachments.append(Attachment(Path(attachment_path), False))
    window._render_attachments()
    window._turn_state("inProgress")
    window.composer.setPlainText("/plan")

    window._send()

    assert window.access_combo.currentData() == PLAN_MODE_VALUE
    assert window.composer.toPlainText() == ""
    assert [item.path for item in window.attachments] == [attachment_path]
    assert window._message_queue == []


def test_plan_with_prompt_supports_attachments_and_queues_while_active(qtbot, tmp_path) -> None:
    window, service = make_window(qtbot)
    attachment_path = tmp_path / "plan.md"
    attachment_path.write_text("context")
    window.attachments.append(Attachment(Path(attachment_path), False))
    window._render_attachments()
    window._turn_state("inProgress")
    window.composer.setPlainText("/plan Подготовь миграцию")

    window._send()

    queued = window._message_queue[0]
    assert not isinstance(queued, QueuedCommand)
    assert queued.text == "Подготовь миграцию"
    assert queued.collaboration_mode == PLAN_MODE_VALUE
    assert queued.queue_syntax == "/plan Подготовь миграцию"
    assert queued.attachments[0].path == attachment_path
    assert window.attachments == []
    assert service.sent == []


def test_action_command_preserves_selected_attachments(qtbot, tmp_path) -> None:
    window, service = make_window(qtbot)
    service.current_thread_id = "thr_1"
    attachment_path = tmp_path / "keep.txt"
    attachment_path.write_text("keep")
    attachment = Attachment(Path(attachment_path), False)
    window.attachments.append(attachment)
    window._render_attachments()
    window.composer.setPlainText("/review проверь API")

    window._send()

    assert service.actions[-1] == ("review", "проверь API")
    assert window.attachments == [attachment]


def test_send_without_project_preserves_draft_and_attachments(qtbot, tmp_path) -> None:
    window, service = make_window(qtbot)
    service.current_project = ""
    attachment_path = tmp_path / "draft.txt"
    attachment_path.write_text("draft")
    attachment = Attachment(attachment_path, False)
    window.attachments.append(attachment)
    window._render_attachments()
    window.composer.setPlainText("Не потеряй этот текст")
    window._add_project = lambda: None  # type: ignore[method-assign]

    window._send()

    assert window.composer.toPlainText() == "Не потеряй этот текст"
    assert window.attachments == [attachment]
    assert service.sent == []


def test_mixed_message_and_command_queue_is_strict_fifo(qtbot) -> None:
    window, service = make_window(qtbot)
    service.current_thread_id = "thr_1"
    window._turn_state("inProgress")
    for text in ("первое", "/compact", "второе"):
        window.composer.setPlainText(text)
        window._send()

    assert isinstance(window._message_queue[1], QueuedCommand)
    assert "/compact" in window.queue_items_layout.itemAt(1).widget().text()
    window._turn_state("completed")
    qtbot.waitUntil(lambda: service.actions == [("message", "первое")])
    window._turn_state("inProgress")
    window._turn_state("completed")
    qtbot.waitUntil(lambda: service.actions[-1] == ("compact", ""))
    assert window._turn_active is True
    window._turn_state("completed")
    qtbot.waitUntil(lambda: service.actions[-1] == ("message", "второе"))

    assert service.actions == [
        ("message", "первое"),
        ("compact", ""),
        ("message", "второе"),
    ]


def test_queue_stops_after_command_error_and_keeps_remaining_items(qtbot) -> None:
    window, service = make_window(qtbot)
    service.current_thread_id = "thr_1"
    window._turn_state("inProgress")
    for text in ("/compact", "после ошибки"):
        window.composer.setPlainText(text)
        window._send()

    window._turn_state("completed")
    qtbot.waitUntil(lambda: service.actions == [("compact", "")])
    window._turn_state("failed")
    qtbot.wait(10)

    assert window._queue_paused is True
    assert len(window._message_queue) == 1
    assert service.sent == []
    assert "ПРИОСТАНОВЛЕНА" in window.queue_label.text()

    window._resume_queue()
    assert service.sent[-1][0] == "после ошибки"


def test_queue_keeps_message_when_its_attachment_disappears(qtbot, tmp_path) -> None:
    window, service = make_window(qtbot)
    service.current_thread_id = "thr_1"
    attachment_path = tmp_path / "queued.txt"
    attachment_path.write_text("queued")
    window._turn_state("inProgress")
    window.attachments.append(Attachment(attachment_path, False))
    window.composer.setPlainText("Сообщение с файлом")
    window._send()
    attachment_path.unlink()
    window._show_error = lambda _message: None  # type: ignore[method-assign]

    window._turn_state("completed")
    qtbot.wait(20)

    assert len(window._message_queue) == 1
    assert window._queue_paused is True
    assert service.sent == []


def test_fork_blocks_navigation_until_callback(qtbot) -> None:
    window, service = make_window(qtbot)
    service.current_thread_id = "thr_1"
    service.fork_thread = lambda _callback=None: None  # type: ignore[method-assign]
    window.composer.setPlainText("/fork")

    window._send()

    assert window._queue_action_pending is True
    assert window.new_chat_button.isEnabled() is False
    assert window.thread_list.isEnabled() is False
    assert window.project_combo.isEnabled() is False


def test_new_command_uses_current_project_and_keeps_fifo_queue(qtbot) -> None:
    window, service = make_window(qtbot)
    service.current_thread_id = "thr_1"
    window._turn_state("inProgress")
    for text in ("/new", "сообщение нового чата"):
        window.composer.setPlainText(text)
        window._send()

    window._turn_state("completed")
    qtbot.waitUntil(lambda: len(service.actions) >= 1)
    assert service.actions[0] == ("new", "thr_1")
    qtbot.waitUntil(lambda: service.sent[-1][0] == "сообщение нового чата")
    assert service.current_project == "/repo"


def test_compaction_and_review_events_render_as_action_cards(qtbot) -> None:
    window, _service = make_window(qtbot)
    events = (
        ("contextCompaction", "Контекст сжат"),
        ("enteredReviewMode", "Режим ревью запущен"),
        ("exitedReviewMode", "Режим ревью завершён"),
    )
    for index, (kind, _title) in enumerate(events):
        window._upsert_item({"id": f"event_{index}", "type": kind}, True)

    assert [window.cards[f"event_{index}"].toggle.text() for index in range(3)] == [
        title for _kind, title in events
    ]
