from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from codex_gui.models import AccessMode
from codex_gui.service import CodexService


class FakeRpc(QObject):
    initialized = Signal(dict)
    notification = Signal(str, dict)
    serverRequest = Signal(object, str, dict)
    protocolError = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.callbacks = []
        self.responses: list[tuple[object, object]] = []

    def request(self, method, params, callback=None):
        self.calls.append((method, params))
        self.callbacks.append(callback)
        return len(self.calls)

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def respond_error(self, request_id, code, message):
        self.responses.append((request_id, {"code": code, "message": message}))


def test_turn_payload_uses_selected_security_mode(qtbot, tmp_path) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_project = str(tmp_path)
    service.current_thread_id = "thr_1"
    service.current_thread_ready = True
    service.send_message("Run tests", [], "gpt-test", "high", AccessMode.WORKSPACE_WRITE)
    method, params = rpc.calls[-1]
    assert method == "turn/start"
    assert params["approvalPolicy"] == "on-request"
    assert params["sandboxPolicy"]["type"] == "workspaceWrite"
    assert params["sandboxPolicy"]["writableRoots"] == [str(tmp_path)]


def test_plan_mode_uses_collaboration_mode_and_read_only_sandbox(qtbot, tmp_path) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_project = str(tmp_path)
    service.current_thread_id = "thr_1"
    service.current_thread_ready = True

    service.send_message(
        "Prepare a plan",
        [],
        "gpt-test",
        "high",
        AccessMode.READ_ONLY,
        "plan",
    )

    method, params = rpc.calls[-1]
    assert method == "turn/start"
    assert params["sandboxPolicy"]["type"] == "readOnly"
    assert params["collaborationMode"] == {
        "mode": "plan",
        "settings": {
            "model": "gpt-test",
            "reasoning_effort": "high",
            "developer_instructions": None,
        },
    }
    assert params["model"] == "gpt-test"
    assert params["effort"] == "high"


def test_full_access_disables_command_approval_prompts(qtbot, tmp_path) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_project = str(tmp_path)
    service.current_thread_id = "thr_1"
    service.current_thread_ready = True

    service.send_message(
        "Run the command",
        [],
        "gpt-test",
        "high",
        AccessMode.FULL_ACCESS,
    )

    method, params = rpc.calls[-1]
    assert method == "turn/start"
    assert params["approvalPolicy"] == "never"
    assert params["sandboxPolicy"] == {"type": "dangerFullAccess"}


def test_new_full_access_thread_uses_never_approval_policy(qtbot, tmp_path) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_project = str(tmp_path)

    service.new_thread("gpt-test", "high", AccessMode.FULL_ACCESS)

    method, params = rpc.calls[-1]
    assert method == "thread/start"
    assert params["approvalPolicy"] == "never"


def test_approval_response_shape(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.answer_approval(7, "acceptForSession")
    assert rpc.responses == [(7, {"decision": "acceptForSession"})]


def test_permission_approval_uses_permission_response_schema(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    params = {
        "permissions": {
            "network": {"enabled": True},
            "fileSystem": {"read": ["/repo"], "write": None},
            "unexpected": {"ignored": True},
        }
    }

    service.answer_approval(
        8,
        "acceptForSession",
        "item/permissions/requestApproval",
        params,
    )
    service.answer_approval(
        9,
        "decline",
        "item/permissions/requestApproval",
        params,
    )

    assert rpc.responses == [
        (
            8,
            {
                "permissions": {
                    "network": {"enabled": True},
                    "fileSystem": {"read": ["/repo"], "write": None},
                },
                "scope": "session",
            },
        ),
        (9, {"permissions": {}, "scope": "turn"}),
    ]


def test_opening_saved_thread_resumes_before_reading(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_project = "/repo"
    service.open_thread("thr_saved")
    assert rpc.calls == [("thread/resume", {"threadId": "thr_saved", "cwd": "/repo"})]


def test_thread_list_is_global_and_paginated(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.connected = True
    received = []
    service.threadsUpdated.connect(received.append)

    service.list_threads()
    method, params = rpc.calls[0]
    assert method == "thread/list"
    assert "cwd" not in params
    assert "sourceKinds" not in params
    rpc.callbacks[0]({"data": [{"id": "a", "cwd": "/one"}], "nextCursor": "page-2"}, None)

    assert rpc.calls[1][1]["cursor"] == "page-2"
    rpc.callbacks[1]({"data": [{"id": "b", "cwd": "/two"}], "nextCursor": None}, None)
    assert [thread.id for thread in received[0]] == ["a", "b"]


def test_rate_limits_refresh_after_completed_turn(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.connected = True
    service.account = {"type": "chatgpt"}
    service.current_thread_id = "thr_1"
    service.current_turn_id = "turn_1"
    received = []
    service.rateLimitsUpdated.connect(received.append)

    rpc.notification.emit(
        "turn/completed",
        {"threadId": "thr_1", "turn": {"id": "turn_1", "status": "completed"}},
    )
    assert rpc.calls[-1] == ("account/rateLimits/read", {})

    rpc.callbacks[-1](
        {
            "rateLimits": {
                "secondary": {"usedPercent": 25, "windowDurationMins": 10080}
            }
        },
        None,
    )
    assert received[-1]["rateLimits"]["secondary"]["usedPercent"] == 25


def test_server_request_resolution_is_forwarded_to_the_ui(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    resolved = []
    service.serverRequestResolved.connect(resolved.append)

    rpc.notification.emit("serverRequest/resolved", {"requestId": 17})

    assert resolved == [17]


def test_context_usage_and_turn_plan_notifications_are_forwarded(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_thread_id = "thread_1"
    service.current_turn_id = "turn_1"
    usage_updates = []
    plan_updates = []
    service.tokenUsageUpdated.connect(usage_updates.append)
    service.turnPlanUpdated.connect(plan_updates.append)

    usage = {
        "last": {"totalTokens": 10},
        "total": {"totalTokens": 20},
        "modelContextWindow": 100,
    }
    plan = {
        "threadId": "thread_1",
        "turnId": "turn_1",
        "explanation": None,
        "plan": [{"step": "Test", "status": "inProgress"}],
    }
    rpc.notification.emit(
        "thread/tokenUsage/updated",
        {"threadId": "thread_1", "turnId": "turn_1", "tokenUsage": usage},
    )
    rpc.notification.emit("turn/plan/updated", plan)

    assert usage_updates == [usage]
    assert plan_updates == [plan]


def test_notifications_from_another_thread_are_ignored(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_thread_id = "current"
    service.current_turn_id = "turn_current"
    deltas = []
    states = []
    service.agentDelta.connect(lambda item_id, delta: deltas.append((item_id, delta)))
    service.turnStateChanged.connect(states.append)

    rpc.notification.emit(
        "item/agentMessage/delta",
        {
            "threadId": "other",
            "turnId": "turn_other",
            "itemId": "message",
            "delta": "wrong chat",
        },
    )
    rpc.notification.emit(
        "turn/completed",
        {
            "threadId": "other",
            "turn": {"id": "turn_other", "status": "completed"},
        },
    )

    assert deltas == []
    assert states == []
    assert service.current_turn_id == "turn_current"


def test_stale_thread_read_does_not_replace_current_chat(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    loaded = []
    service.threadLoaded.connect(loaded.append)

    service.open_thread("thread_a")
    rpc.callbacks[0]({}, None)
    assert rpc.calls[1][0] == "thread/read"
    service.open_thread("thread_b")
    rpc.callbacks[1]({"thread": {"id": "thread_a", "turns": []}}, None)

    assert loaded == []
    assert service.current_thread_id == "thread_b"


def test_missing_new_thread_id_fails_instead_of_retrying_forever(qtbot, tmp_path) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_project = str(tmp_path)
    states = []
    errors = []
    service.turnStateChanged.connect(states.append)
    service.errorOccurred.connect(errors.append)

    service.send_message("hello", [], "gpt-test", "high", AccessMode.WORKSPACE_WRITE)
    assert states == ["starting"]
    assert len(rpc.calls) == 1
    assert rpc.calls[0][0] == "thread/start"
    rpc.callbacks[0]({"thread": {}}, None)

    assert states[-1] == "failed"
    assert "идентификатор" in errors[-1]
    assert len(rpc.calls) == 1


def test_disconnect_fails_a_turn_that_is_still_starting(qtbot, tmp_path) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_project = str(tmp_path)
    states = []
    service.turnStateChanged.connect(states.append)

    service.send_message("hello", [], "gpt-test", "high", AccessMode.WORKSPACE_WRITE)
    service._disconnected()

    assert states == ["starting", "failed"]
    assert service._turn_start_pending is False


def test_compact_review_and_custom_review_rpc_parameters(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_thread_id = "thr_1"
    service.current_thread_ready = True

    service.compact_thread()
    service.start_review()
    service.start_review("Проверь обработку ошибок")

    assert rpc.calls[0] == (
        "thread/compact/start",
        {"threadId": "thr_1"},
    )
    assert rpc.calls[1] == (
        "review/start",
        {
            "threadId": "thr_1",
            "delivery": "inline",
            "target": {"type": "uncommittedChanges"},
        },
    )
    assert rpc.calls[2] == (
        "review/start",
        {
            "threadId": "thr_1",
            "delivery": "inline",
            "target": {
                "type": "custom",
                "instructions": "Проверь обработку ошибок",
            },
        },
    )


def test_fork_switches_after_loading_copy_and_refreshes_history(qtbot) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.connected = True
    service.current_project = "/repo"
    service.current_thread_id = "thr_source"
    loaded = []
    switched = []
    service.threadLoaded.connect(loaded.append)

    service.fork_thread(switched.append)
    assert rpc.calls[0] == (
        "thread/fork",
        {"threadId": "thr_source", "ephemeral": False},
    )
    rpc.callbacks[0]({"thread": {"id": "thr_copy"}}, None)

    assert service.current_thread_id == "thr_copy"
    assert rpc.calls[1] == (
        "thread/read",
        {"threadId": "thr_copy", "includeTurns": True},
    )
    copied = {
        "id": "thr_copy",
        "turns": [{"items": [{"id": "old", "type": "agentMessage", "text": "copy"}]}],
    }
    rpc.callbacks[1]({"thread": copied}, None)

    assert loaded == [
        {
            "id": "thr_copy",
            "turns": [
                {
                    "items": [
                        {
                            "id": "old",
                            "kind": "assistant_message",
                            "subtype": "agentMessage",
                            "text": "copy",
                        }
                    ]
                }
            ],
        }
    ]
    assert switched == [True]
    assert rpc.calls[2][0] == "thread/list"
    assert all(method not in {"thread/archive", "thread/delete"} for method, _params in rpc.calls)


def test_collaboration_mode_can_transition_from_plan_to_default(qtbot, tmp_path) -> None:
    rpc = FakeRpc()
    service = CodexService(rpc)  # type: ignore[arg-type]
    service.current_project = str(tmp_path)
    service.current_thread_id = "thr_1"
    service.current_thread_ready = True

    service.send_message(
        "Составь план",
        [],
        "gpt-test",
        "high",
        AccessMode.READ_ONLY,
        "plan",
    )
    rpc.callbacks[-1]({"turn": {"id": "plan_turn"}}, None)
    rpc.notification.emit(
        "turn/completed",
        {"turn": {"id": "plan_turn", "status": "completed"}},
    )
    service.send_message(
        "Теперь реализуй",
        [],
        "gpt-test",
        "high",
        AccessMode.WORKSPACE_WRITE,
    )

    turn_calls = [params for method, params in rpc.calls if method == "turn/start"]
    assert turn_calls[0]["collaborationMode"]["mode"] == "plan"
    assert turn_calls[1]["collaborationMode"] == {
        "mode": "default",
        "settings": {
            "model": "gpt-test",
            "reasoning_effort": "high",
            "developer_instructions": None,
        },
    }
