from PySide6.QtWidgets import QApplication, QLabel

from codex_gui.main_window import (
    MAX_ACTIVITY_CONTENT_CHARS,
    ActivityCard,
    ActivityGroupCard,
    Composer,
    ExecutionPlanCard,
    InlineUserInputCard,
    MessageCard,
    ThinkingIndicator,
    context_usage,
    format_duration,
    recent_thread_items,
    stream_render_interval,
)


def test_composer_grows_with_content_and_shrinks_when_cleared(qtbot) -> None:
    composer = Composer()
    qtbot.addWidget(composer)
    composer.resize(500, Composer.MIN_HEIGHT)
    composer.show()
    assert composer.height() == Composer.MIN_HEIGHT

    composer.setPlainText("\n".join(f"line {number}" for number in range(12)))
    qtbot.waitUntil(lambda: composer.height() > Composer.MIN_HEIGHT)
    assert composer.height() <= Composer.MAX_HEIGHT

    composer.clear()
    qtbot.waitUntil(lambda: composer.height() == Composer.MIN_HEIGHT)


def test_message_streaming_is_rendered_in_batches(qtbot) -> None:
    card = MessageCard("agent", "start")
    qtbot.addWidget(card)
    rendered: list[str] = []
    original_render = card.renderer.render
    card.renderer.render = lambda text: (rendered.append(text), original_render(text))[1]

    for _ in range(100):
        card.append("x")

    assert rendered == []
    qtbot.waitUntil(lambda: len(rendered) == 1)
    assert rendered[0] == "start" + ("x" * 100)


def test_message_card_has_no_author_label_and_copies_full_text(qtbot) -> None:
    card = MessageCard("user", "Полный текст")
    qtbot.addWidget(card)

    assert not any(label.text() in {"ВЫ", "CODEX"} for label in card.findChildren(QLabel))
    assert card.copy_button.icon().isNull() is False
    assert card.copy_button.accessibleName() == "Скопировать сообщение"
    assert card.edit_button is not None and card.edit_button.icon().isNull() is False
    assert card.edit_button.accessibleName() == "Редактировать сообщение"
    card.copy_button.click()
    assert QApplication.clipboard().text() == "Полный текст"


def test_message_card_requests_editing_in_composer(qtbot) -> None:
    card = MessageCard("user", "Исправь этот текст")
    qtbot.addWidget(card)

    with qtbot.waitSignal(card.editRequested) as signal:
        assert card.edit_button is not None
        card.edit_button.click()

    assert signal.args == ["Исправь этот текст"]


def test_agent_message_cannot_be_edited(qtbot) -> None:
    card = MessageCard("agent", "Готовый ответ")
    qtbot.addWidget(card)

    assert card.edit_button is None


def test_activity_group_is_collapsed_by_default(qtbot) -> None:
    group = ActivityGroupCard()
    qtbot.addWidget(group)
    group.add_activity(ActivityCard("Терминал", "output"))

    assert group.header.isChecked() is False
    assert group.items_container.isHidden() is True

    group.header.setChecked(True)
    assert group.items_container.isHidden() is False


def test_thinking_indicator_animates(qtbot) -> None:
    indicator = ThinkingIndicator()
    qtbot.addWidget(indicator)
    initial = indicator.label.text()

    indicator.start()
    qtbot.waitUntil(lambda: indicator.label.text() != initial, timeout=1200)
    indicator.set_activity("ИИ выполняет команду")
    assert "ИИ выполняет команду" in indicator.label.text()
    indicator.stop()


def test_inline_user_input_returns_selected_option(qtbot) -> None:
    card = InlineUserInputCard()
    qtbot.addWidget(card)
    card.set_request(
        {
            "questions": [
                {
                    "id": "approval",
                    "header": "План готов",
                    "question": "Начать реализацию?",
                    "options": [
                        {"label": "Да", "description": "Начать работу"},
                        {"label": "Нет", "description": "Оставить план"},
                    ],
                }
            ]
        }
    )

    with qtbot.waitSignal(card.submitted) as signal:
        card.submit_button.click()

    assert signal.args == [{"approval": ["Да"]}]


def test_duration_is_formatted_for_short_and_long_turns() -> None:
    assert format_duration(850) == "0,8 сек"
    assert format_duration(12_400) == "12 сек"
    assert format_duration(65_000) == "1 мин 5 сек"
    assert format_duration(3_665_000) == "1 ч 1 мин 5 сек"


def test_context_usage_uses_codex_window_information() -> None:
    usage = {
        "last": {"totalTokens": 32_000},
        "total": {"totalTokens": 80_000},
        "modelContextWindow": 128_000,
    }
    assert context_usage(usage) == (25, 32_000, 128_000)
    assert context_usage({"last": {"totalTokens": 1_000}}) is None


def test_execution_plan_card_updates_step_states(qtbot) -> None:
    card = ExecutionPlanCard()
    qtbot.addWidget(card)
    card.set_plan(
        "План выполнения",
        [
            {"step": "Проверить код", "status": "completed"},
            {"step": "Исправить ошибку", "status": "inProgress"},
        ],
    )

    steps = [
        label
        for label in card.findChildren(QLabel)
        if label.objectName() == "executionPlanStep"
    ]
    assert [label.text() for label in steps] == [
        "✓  Проверить код",
        "◉  Исправить ошибку",
    ]


def test_activity_streaming_is_batched_and_bounded(qtbot) -> None:
    card = ActivityCard("Терминал")
    qtbot.addWidget(card)
    for _ in range(20):
        card.append("x" * 10_000)

    assert len(card.content) == MAX_ACTIVITY_CONTENT_CHARS
    assert card._content_truncated is True
    qtbot.waitUntil(lambda: "Показан только конец" in card.body.toPlainText())


def test_recent_thread_items_bounds_widget_work() -> None:
    thread = {
        "turns": [
            {"items": [{"id": f"{turn}-{item}"} for item in range(4)]}
            for turn in range(6)
        ]
    }
    items, omitted_turns, omitted_items = recent_thread_items(thread, max_turns=3, max_items=5)
    assert [item["id"] for item in items] == ["4-3", "5-0", "5-1", "5-2", "5-3"]
    assert omitted_turns == 3
    assert omitted_items == 7


def test_large_streams_are_rendered_less_frequently() -> None:
    assert stream_render_interval(1_000) == 60
    assert stream_render_interval(50_000) == 120
    assert stream_render_interval(150_000) == 250
