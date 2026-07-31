from __future__ import annotations

from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

from codex_gui.main_window import MainWindow
from codex_gui.models import PLAN_MODE_VALUE, AccessMode, ModelInfo


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

    def __init__(self) -> None:
        super().__init__()
        self.account = None
        self.sent: list[tuple] = []

    def send_message(self, *args) -> None:
        self.sent.append(args)

    def interrupt(self) -> None:
        pass


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
