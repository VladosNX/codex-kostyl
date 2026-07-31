from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from codex_gui.models import AccessMode, Attachment
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
    assert "effort" not in params


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
    received = []
    service.rateLimitsUpdated.connect(received.append)

    rpc.notification.emit(
        "turn/completed",
        {"turn": {"id": "turn_1", "status": "completed"}},
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
