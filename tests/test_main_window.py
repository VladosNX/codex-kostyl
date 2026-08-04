from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QObject, QProcess, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QWidget

from codex_gui.agents.base import (
    AgentCapabilities,
    AgentConfigOption,
    AgentDescriptor,
    AgentManifest,
    AgentPrompt,
    AgentRunMode,
    ConfigOptionValue,
    FeatureId,
    FeatureState,
    FeatureSupport,
)
from codex_gui.main_window import MainWindow, MessageCard, NumberedChoiceMenu, QueuedCommand
from codex_gui.models import PLAN_MODE_VALUE, AccessMode, Attachment, ModelInfo, ThreadSummary


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
        self.edits: list[tuple[str, AgentPrompt]] = []

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

    def supports_message_edit(self) -> bool:
        return True

    def edit_message(self, item_id: str, prompt: AgentPrompt, callback=None) -> None:
        self.edits.append((item_id, prompt))
        self.current_thread_id = "thr_edited"
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


def test_agent_selector_is_visible_with_only_codex(qtbot) -> None:
    window, _service = make_window(qtbot)

    assert window.agent_combo.isHidden() is False
    assert window.agent_combo.count() == 1
    assert window.agent_combo.currentData() == "codex"


def test_empty_loaded_session_keeps_new_chat_starter(qtbot) -> None:
    window, _service = make_window(qtbot)

    window._render_thread({"id": "new-session", "turns": []})

    empty = window.findChild(QWidget, "emptyHint")
    assert empty is not None
    assert "Чем займёмся?" in empty.findChild(QLabel, "emptyTitle").text()


def test_unsupported_agent_features_stay_visible_but_disabled(qtbot) -> None:
    window, service = make_window(qtbot)
    service.descriptor = AgentDescriptor("minimal", "Minimal", "minimal")
    service.capabilities = AgentCapabilities(session_history=True)

    window._apply_agent_capabilities()

    assert window.settings_button.isHidden() is False
    assert window.settings_button.isEnabled() is False
    assert "не поддерживаются агентом Minimal" in window.settings_button.toolTip()
    assert window.access_combo.isHidden() is False
    assert window.access_combo.isEnabled() is False
    assert window.attach_button.isHidden() is False
    assert window.attach_button.isEnabled() is False


def test_agent_run_modes_replace_codex_access_presets(qtbot) -> None:
    settings = FakeSettings()
    window, _service = make_window(qtbot, settings)
    window._manifest_updated(
        AgentManifest(
            features={FeatureId.ACCESS_MODES.value: FeatureSupport(True)},
            run_modes=(
                AgentRunMode("build", "build", "Uses configured permissions"),
                AgentRunMode("plan", "plan", "Disallows edit tools"),
            ),
            current_run_mode_id="build",
        )
    )

    assert [window.access_combo.itemData(index) for index in range(window.access_combo.count())] == [
        "build",
        "plan",
    ]
    window.access_combo.setCurrentIndex(window.access_combo.findData("plan"))
    window._turn_state("inProgress")
    window.composer.setPlainText("Составь план")
    window._send()

    queued = window._message_queue[0]
    assert queued.run_mode_id == "plan"
    assert queued.collaboration_mode is None
    assert settings.values["run_mode"] == "plan"


def test_temporarily_disabled_feature_shows_driver_reason(qtbot) -> None:
    window, service = make_window(qtbot)
    service.descriptor = AgentDescriptor("limited", "Limited", "limited")

    def feature_state(feature: FeatureId) -> FeatureState:
        if feature is FeatureId.SESSION_COMPACT:
            return FeatureState(True, False, "Сжатие доступно после первого ответа")
        return FeatureState(False, False, "Агент не передаёт эти данные")

    service.feature_state = feature_state  # type: ignore[attr-defined]
    window._apply_agent_capabilities()
    window.composer.setPlainText("/")

    compact = window.slash_panel.list.item(0)
    assert "Сжатие доступно после первого ответа" in compact.text()
    assert compact.flags() & Qt.ItemFlag.ItemIsEnabled == Qt.ItemFlag.NoItemFlags
    assert window.weekly_limit.isHidden() is False
    assert window.weekly_limit_label.text() == "Лимит —"
    assert window.context_usage_widget.isHidden() is False
    assert window.context_usage_label.text() == "Контекст —"


def test_generic_config_categories_keep_agent_specific_ids(qtbot) -> None:
    window, service = make_window(qtbot)
    prompts: list[AgentPrompt] = []
    service.submit_prompt = prompts.append  # type: ignore[attr-defined]
    window._set_config_options(
        (
            AgentConfigOption(
                "vendor.model.choice",
                "Model",
                "model",
                current_value="small",
                values=(
                    ConfigOptionValue("small", "Small"),
                    ConfigOptionValue("large", "Large"),
                ),
            ),
            AgentConfigOption(
                "vendor.reasoning.level",
                "Reasoning",
                "thought_level",
                current_value="low",
                values=(
                    ConfigOptionValue("low", "Low"),
                    ConfigOptionValue("high", "High"),
                ),
            ),
        )
    )
    window.model_combo.setCurrentIndex(window.model_combo.findData("large"))
    window.effort_combo.setCurrentIndex(window.effort_combo.findData("high"))
    window.composer.setPlainText("Проверь проект")

    window._send()

    assert prompts[-1].config["vendor.model.choice"] == "large"
    assert prompts[-1].config["vendor.reasoning.level"] == "high"
    assert "model" not in prompts[-1].config
    assert "thought_level" not in prompts[-1].config


def test_macos_notification_does_not_call_linux_notify_send(qtbot, monkeypatch) -> None:
    window, _service = make_window(qtbot)
    window._tray_available = False
    detached: list[tuple] = []
    monkeypatch.setattr("codex_gui.main_window.sys.platform", "darwin")
    monkeypatch.setattr(
        QProcess,
        "startDetached",
        lambda *args: detached.append(args),
    )

    window._show_desktop_notification("Title", "Message")

    assert detached == []


def test_returning_to_window_dismisses_desktop_notification(qtbot) -> None:
    window, _service = make_window(qtbot)
    dismissed: list[bool] = []
    window._dismiss_desktop_notification = lambda: dismissed.append(True)  # type: ignore[method-assign]

    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowActivate))

    assert dismissed == [True]


def test_linux_notification_is_closed_by_id(monkeypatch) -> None:
    detached: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("codex_gui.main_window.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        QProcess,
        "startDetached",
        lambda command, arguments: detached.append((command, arguments)),
    )

    MainWindow._close_linux_notification(42)

    assert detached == [
        (
            "gdbus",
            [
                "call",
                "--session",
                "--dest",
                "org.freedesktop.Notifications",
                "--object-path",
                "/org/freedesktop/Notifications",
                "--method",
                "org.freedesktop.Notifications.CloseNotification",
                "uint32 42",
            ],
        )
    ]


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


def test_thinking_label_tracks_current_agent_activity(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._turn_state("inProgress")
    assert window._thinking_indicator is not None

    window._reasoning_delta("reasoning", "Проверяю")
    assert "ИИ анализирует" in window._thinking_indicator.label.text()

    window._command_delta("command", "pytest")
    assert "ИИ выполняет команду" in window._thinking_indicator.label.text()

    window._agent_delta("answer", "Готово")
    assert "ИИ пишет ответ" in window._thinking_indicator.label.text()


def test_acp_tool_call_uses_title_when_kind_is_missing(qtbot) -> None:
    window, _service = make_window(qtbot)

    window._upsert_item(
        {
            "id": "tool-without-kind",
            "kind": "tool_call",
            "subtype": "acp_tool_call",
            "title": "Read README.md",
        },
        False,
    )

    assert window.cards["tool-without-kind"].toggle.text() == "Read README.md"


def test_acp_tool_call_keeps_kind_when_title_is_also_present(qtbot) -> None:
    window, _service = make_window(qtbot)

    window._upsert_item(
        {
            "id": "tool-with-kind",
            "kind": "tool_call",
            "subtype": "execute",
            "title": "Run pytest",
        },
        False,
    )

    assert window.cards["tool-with-kind"].toggle.text() == "execute"


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
    assert "Доступ к сети: разрешён" in window.approval_card.detail.text()
    assert "Нужен доступ к API" in window.approval_card.detail.text()
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


def test_request_settings_use_nested_model_and_effort_menus(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._set_models(
        [
            ModelInfo("model-a", "Model A", ["low"], "low"),
            ModelInfo("model-b", "Model B", ["medium", "high"], "medium"),
        ]
    )

    assert window.access_combo.parentWidget().objectName() == "composerPanel"
    assert window.context_usage_widget.parentWidget().objectName() == "composerPanel"
    assert window.weekly_limit.parentWidget().objectName() == "composerPanel"

    menu = window._build_request_settings_menu()
    model_menu = menu.actions()[0].menu()
    effort_menu = menu.actions()[1].menu()

    assert model_menu is not None and model_menu.title() == "1   Model A"
    assert effort_menu is not None and effort_menu.title() == "2   Low effort"
    model_menu.actions()[1].trigger()
    assert window.model_combo.currentData() == "model-b"

    menu = window._build_request_settings_menu()
    effort_menu = menu.actions()[1].menu()
    assert effort_menu is not None and effort_menu.title() == "2   Medium effort"
    effort_menu.actions()[1].trigger()

    assert window.effort_combo.currentData() == "high"


def test_access_mode_has_semantic_style_and_active_turn_notice(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._turn_state("inProgress")
    window.access_combo.setCurrentIndex(window.access_combo.findData(PLAN_MODE_VALUE))

    assert window.access_combo.property("mode") == "plan"
    assert window.access_shortcut_label.property("accessTone") == "plan"
    assert "#d2c9eb" in window.access_shortcut_label.styleSheet()
    assert window.access_combo.property("nextTurn") is True
    assert "следующему сообщению" in window.notice_label.text()
    assert window.notice_banner.isHidden() is False


def test_thread_search_filters_title_folder_and_path(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._set_threads(
        [
            ThreadSummary("one", "Исправить интерфейс", "/repo/frontend"),
            ThreadSummary("two", "Обновить документацию", "/repo/docs"),
        ]
    )

    window.thread_search.setText("docs")

    assert window.thread_list.item(0).isHidden() is True
    assert window.thread_list.item(1).isHidden() is False


def test_sidebar_can_be_collapsed_with_header_control(qtbot) -> None:
    window, _service = make_window(qtbot)
    window.show()
    qtbot.wait(10)

    window.sidebar_toggle.click()
    assert window.sidebar_panel.isHidden() is True

    window.sidebar_toggle.click()
    assert window.sidebar_panel.isHidden() is False


def test_application_shortcuts_are_registered(qtbot) -> None:
    window, _service = make_window(qtbot)

    assert window.new_chat_shortcut.key().toString() == "Ctrl+K"
    assert window.access_mode_shortcut.key().toString() == "Ctrl+M"
    assert window.attach_file_shortcut.key().toString() == "Ctrl+O"
    assert window.request_settings_shortcut.key().toString() == "Ctrl+I"
    assert window.latest_activity_shortcut.key().toString() == "Ctrl+T"
    assert window.stop_shortcut.key().toString() == "Esc"
    inline_shortcuts = {
        label.text()
        for label in window.findChildren(QLabel, "inlineShortcutLabel")
    }
    assert inline_shortcuts == {"Ctrl+K", "Ctrl+M"}
    assert window.sidebar_toggle.text() == "Ctrl+B"
    assert window.settings_button.text() == "Ctrl+I"
    assert "Ctrl+O" not in inline_shortcuts
    assert "Enter" not in inline_shortcuts
    assert "Esc" not in inline_shortcuts


def test_new_chat_and_attach_shortcuts_call_safe_actions(qtbot) -> None:
    window, service = make_window(qtbot)
    attachments_opened: list[bool] = []
    window._choose_attachments = lambda: attachments_opened.append(True)  # type: ignore[method-assign]

    window.new_chat_shortcut.activated.emit()
    window.attach_file_shortcut.activated.emit()

    assert service.actions[-1][0] == "new"
    assert attachments_opened == [True]


def test_access_mode_menu_supports_number_selection(qtbot) -> None:
    window, _service = make_window(qtbot)
    menu = window._build_access_mode_menu()
    menu.show()

    qtbot.keyClick(menu, Qt.Key.Key_1)

    assert window.access_combo.currentData() == AccessMode.READ_ONLY.value


def test_access_combo_click_uses_always_numbered_menu(qtbot, monkeypatch) -> None:
    window, _service = make_window(qtbot)
    visible_actions: list[list[str]] = []

    monkeypatch.setattr(
        NumberedChoiceMenu,
        "exec",
        lambda menu, *_args: visible_actions.append(
            [action.text() for action in menu.actions()]
        ),
    )

    window.access_combo.showPopup()

    assert visible_actions
    assert all(
        text.startswith(f"{index}   ")
        for index, text in enumerate(visible_actions[0], start=1)
    )


def test_request_settings_menu_supports_number_selection(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._set_models(
        [
            ModelInfo("model-a", "Model A", ["low"], "low"),
            ModelInfo("model-b", "Model B", ["medium", "high"], "medium"),
        ]
    )

    menu = window._build_request_settings_menu()
    model_menu = menu.actions()[0].menu()
    assert isinstance(model_menu, NumberedChoiceMenu)
    model_menu.show()
    qtbot.keyClick(model_menu, Qt.Key.Key_2)
    assert window.model_combo.currentData() == "model-b"

    menu = window._build_request_settings_menu()
    effort_menu = menu.actions()[1].menu()
    assert isinstance(effort_menu, NumberedChoiceMenu)
    effort_menu.show()
    qtbot.keyClick(effort_menu, Qt.Key.Key_2)
    assert window.effort_combo.currentData() == "high"

    menu = window._build_request_settings_menu()
    model_menu = menu.actions()[0].menu()
    assert isinstance(model_menu, NumberedChoiceMenu)
    menu.show()
    qtbot.keyClick(menu, Qt.Key.Key_1)
    assert model_menu.isVisible() is True
    qtbot.keyClick(model_menu, Qt.Key.Key_1)
    assert menu.isVisible() is False


def test_ctrl_i_opens_request_settings_from_composer(qtbot, monkeypatch) -> None:
    window, service = make_window(qtbot)
    opened: list[bool] = []
    monkeypatch.setattr(
        NumberedChoiceMenu,
        "exec",
        lambda _menu, *_args: opened.append(True),
    )
    window.show()
    window.composer.setFocus()

    qtbot.keyClick(
        window.composer,
        Qt.Key.Key_I,
        modifier=Qt.KeyboardModifier.ControlModifier,
    )

    assert opened == [True]
    assert service.sent == []


def test_ctrl_t_toggles_latest_activity_group(qtbot) -> None:
    window, _service = make_window(qtbot)

    first = window._activity("first", "Первая команда", "first output")
    group = window._latest_activity_group
    assert group is not None
    assert group.header.isChecked() is False
    assert group.items_container.isHidden() is True
    assert first.toggle.isChecked() is False

    second = window._activity("second", "Вторая команда", "second output")
    assert first.toggle.isChecked() is False
    assert second.toggle.isChecked() is False

    window.show()
    window.composer.setFocus()
    qtbot.keyClick(
        window.composer,
        Qt.Key.Key_T,
        modifier=Qt.KeyboardModifier.ControlModifier,
    )
    assert group.header.isChecked() is True
    assert group.items_container.isHidden() is False
    assert second.toggle.isChecked() is True
    assert second.body.isHidden() is False
    qtbot.keyClick(
        window.composer,
        Qt.Key.Key_T,
        modifier=Qt.KeyboardModifier.ControlModifier,
    )
    assert group.header.isChecked() is False
    assert group.items_container.isHidden() is True
    assert second.toggle.isChecked() is False
    assert second.body.isHidden() is True


def test_open_activity_group_follows_newest_action(qtbot) -> None:
    window, _service = make_window(qtbot)
    first = window._activity("first", "Первая команда", "first output")

    window._toggle_latest_activity()
    second = window._activity("second", "Вторая команда", "second output")
    group = window._latest_activity_group

    assert group is not None and group.header.isChecked() is True
    assert first.toggle.isChecked() is False
    assert second.toggle.isChecked() is True


def test_ctrl_t_hint_is_only_visible_on_latest_activity_group(qtbot) -> None:
    window, _service = make_window(qtbot)

    window._activity("first", "Первая команда", "first output")
    first_group = window._latest_activity_group
    assert first_group is not None
    assert first_group.header.shortcut_label.isHidden() is False

    window._agent_delta("answer", "Готово")
    window._activity("second", "Вторая команда", "second output")
    second_group = window._latest_activity_group

    assert second_group is not None and second_group is not first_group
    assert first_group.header.shortcut_label.isHidden() is True
    assert second_group.header.shortcut_label.isHidden() is False


def test_ctrl_m_opens_access_menu_from_composer(qtbot, monkeypatch) -> None:
    window, service = make_window(qtbot)
    opened: list[bool] = []
    monkeypatch.setattr(
        NumberedChoiceMenu,
        "exec",
        lambda _menu, *_args: opened.append(True),
    )
    window.show()
    window.composer.setFocus()

    qtbot.keyClick(
        window.composer,
        Qt.Key.Key_M,
        modifier=Qt.KeyboardModifier.ControlModifier,
    )

    assert opened == [True]
    assert service.sent == []


def test_escape_stop_requires_confirmation(qtbot, monkeypatch) -> None:
    window, service = make_window(qtbot)
    interrupted: list[bool] = []
    service.interrupt = lambda: interrupted.append(True)  # type: ignore[method-assign]
    window._turn_state("inProgress")
    assert window.stop_shortcut.isEnabled() is True

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No,
    )
    window.stop_shortcut.activated.emit()
    assert interrupted == []

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    window.stop_shortcut.activated.emit()
    assert interrupted == [True]


def test_sidebar_auto_hides_on_narrow_window_but_respects_manual_choice(qtbot) -> None:
    window, _service = make_window(qtbot)
    window.show()
    window.resize(1200, 700)
    qtbot.wait(10)
    assert window.sidebar_panel.isHidden() is False

    window.resize(900, 700)
    qtbot.wait(10)
    assert window.sidebar_panel.isHidden() is True
    assert window._sidebar_auto_hidden is True

    window.resize(1200, 700)
    qtbot.wait(10)
    assert window.sidebar_panel.isHidden() is False

    window.sidebar_toggle.click()
    window.resize(900, 700)
    window.resize(1200, 700)
    qtbot.wait(10)
    assert window.sidebar_panel.isHidden() is True


def test_empty_state_starter_moves_prompt_to_composer(qtbot) -> None:
    window, _service = make_window(qtbot)
    buttons = [
        button
        for button in window.timeline.findChildren(QPushButton)
        if button.objectName() == "starterButton"
    ]

    assert len(buttons) == 3
    buttons[0].click()
    assert window.composer.toPlainText().startswith("Изучи проект")


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


def test_slash_panel_uses_prefix_before_cursor_and_preserves_existing_prompt(qtbot) -> None:
    window, service = make_window(qtbot)
    existing_prompt = "Подготовь план миграции базы"
    window.composer.setPlainText(existing_prompt)
    cursor = window.composer.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    window.composer.setTextCursor(cursor)

    qtbot.keyClicks(window.composer, "/pl")

    assert window.slash_panel.isHidden() is False
    assert window.slash_panel.selected_command() == "plan"

    qtbot.keyClick(window.composer, Qt.Key.Key_Tab)

    assert window.composer.toPlainText() == f"/plan {existing_prompt}"
    assert window.composer.textCursor().position() == len("/plan ")
    assert window.slash_panel.isHidden() is True

    window.composer.setPlainText(existing_prompt)
    cursor = window.composer.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    window.composer.setTextCursor(cursor)
    qtbot.keyClicks(window.composer, "/pl")
    qtbot.keyClick(window.composer, Qt.Key.Key_Return)

    assert service.sent[-1][0] == existing_prompt
    assert service.sent[-1][5] == PLAN_MODE_VALUE


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
    qtbot.keyClick(
        window.composer,
        Qt.Key.Key_Return,
        modifier=Qt.KeyboardModifier.ShiftModifier,
    )
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
    command_row = window.queue_items_layout.itemAt(1).widget()
    command_text = command_row.findChild(QLabel, "queueItemText")
    assert command_text is not None and "/compact" in command_text.text()
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


def test_queued_message_has_explicit_edit_and_remove_actions(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._turn_state("inProgress")
    window.composer.setPlainText("Исходный текст")
    window._send()

    row = window.queue_items_layout.itemAt(0).widget()
    actions = row.findChildren(QWidget, "queueItemAction")
    qtbot.mouseClick(row, Qt.MouseButton.LeftButton)

    assert len(actions) == 2
    assert len(window._message_queue) == 1
    assert window._update_queued_message(0, "Отредактированный текст") is True
    assert len(window._message_queue) == 1
    assert window._message_queue[0].text == "Отредактированный текст"


def test_queue_edit_uses_composer_and_restores_existing_draft(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._turn_state("inProgress")
    window.composer.setPlainText("Сообщение из очереди")
    window._send()
    window.composer.setPlainText("Новый черновик пользователя")

    window._edit_queued_message(0)

    assert window.composer.toPlainText() == "Сообщение из очереди"
    assert window.queue_edit_banner.isHidden() is False
    assert window.send_button.accessibleName() == "Сохранить изменения сообщения в очереди"
    assert window.attach_button.isEnabled() is False

    window.composer.setPlainText("Изменённое сообщение")
    window._send()

    assert window._message_queue[0].text == "Изменённое сообщение"
    assert window.composer.toPlainText() == "Новый черновик пользователя"
    assert window.queue_edit_banner.isHidden() is True
    assert window.attach_button.isEnabled() is True


def test_queue_waits_while_message_is_being_edited(qtbot) -> None:
    window, service = make_window(qtbot)
    window._turn_state("inProgress")
    window.composer.setPlainText("Не отправляй до завершения редактирования")
    window._send()
    window._edit_queued_message(0)

    window._turn_state("completed")
    qtbot.wait(10)
    assert service.sent == []

    window._cancel_queue_edit()
    qtbot.waitUntil(lambda: len(service.sent) == 1)
    assert service.sent[0][0] == "Не отправляй до завершения редактирования"


def test_queue_does_not_show_redundant_added_notice(qtbot) -> None:
    window, _service = make_window(qtbot)
    window._turn_state("inProgress")
    window.composer.setPlainText("Следующее сообщение")

    window._send()

    assert window.queue_panel.isHidden() is False
    assert window.notice_banner.isHidden() is True


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


def test_user_message_edit_creates_real_agent_branch(qtbot) -> None:
    window, service = make_window(qtbot)
    window._upsert_item(
        {
            "id": "msg_1",
            "kind": "user_message",
            "content": [{"type": "text", "text": "Исходный текст"}],
        },
        True,
    )

    card = window.cards["msg_1"]
    assert isinstance(card, MessageCard)
    assert card.edit_button is not None
    card.edit_button.click()

    assert window.message_edit_banner.isHidden() is False
    assert window.send_button.accessibleName() == "Сохранить изменения сообщения"
    assert window.composer.toPlainText() == "Исходный текст"

    window.composer.setPlainText("Изменённый текст")
    window._send()

    assert len(service.edits) == 1
    item_id, prompt = service.edits[0]
    assert item_id == "msg_1"
    assert prompt.text == "Изменённый текст"
    assert service.current_thread_id == "thr_edited"
    assert service.sent == []
    assert window.message_edit_banner.isHidden() is True
    assert window.composer.toPlainText() == ""
    assert window.send_button.isEnabled() is True


def test_unsupported_agent_does_not_copy_message_into_composer(qtbot) -> None:
    window, service = make_window(qtbot)
    service.supports_message_edit = lambda: False  # type: ignore[method-assign]
    window.composer.setPlainText("Черновик")
    window._upsert_item(
        {
            "id": "msg_1",
            "kind": "user_message",
            "content": [{"type": "text", "text": "Исходный текст"}],
        },
        True,
    )

    card = window.cards["msg_1"]
    assert isinstance(card, MessageCard)
    assert card.edit_button is not None
    card.edit_button.click()

    assert window.composer.toPlainText() == "Черновик"
    assert window.message_edit_banner.isHidden() is True
    assert service.edits == []


def test_new_request_restores_a_paused_queue_after_interrupt(qtbot) -> None:
    window, service = make_window(qtbot)
    service.current_thread_id = "thr_1"
    window._turn_state("inProgress")
    window.composer.setPlainText("Отложенное сообщение")
    window._send()

    window._turn_state("interrupted")
    qtbot.wait(10)
    assert window._queue_paused is True

    window.composer.setPlainText("Новый запрос после остановки")
    window._send()

    assert window._queue_paused is False
    assert service.sent[-1][0] == "Новый запрос после остановки"

    window._turn_state("inProgress")
    window._turn_state("completed")
    qtbot.waitUntil(lambda: len(service.sent) == 2)
    assert service.sent[1][0] == "Отложенное сообщение"


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
