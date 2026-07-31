from __future__ import annotations

import json

from PySide6.QtCore import QObject, QProcess, Signal

from codex_gui.rpc import CodexProcess, JsonLineDecoder, JsonRpcClient


class FakeTransport(QObject):
    messageReceived = Signal(dict)
    protocolError = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    def send(self, payload: dict) -> None:
        self.sent.append(payload)


def test_json_line_decoder_handles_chunks_and_bad_lines() -> None:
    decoder = JsonLineDecoder()
    messages, errors = decoder.feed(b'{"id":1,"result":')
    assert messages == [] and errors == []
    messages, errors = decoder.feed(b'{}}\nnot-json\n{"method":"ready","params":{}}\n')
    assert [message.get("id") for message in messages] == [1, None]
    assert len(errors) == 1


def test_rpc_correlates_responses_and_routes_notifications(qtbot) -> None:
    transport = FakeTransport()
    rpc = JsonRpcClient(transport)  # type: ignore[arg-type]
    result_box = []
    notifications = []
    rpc.notification.connect(lambda method, params: notifications.append((method, params)))

    request_id = rpc.request("model/list", {}, lambda result, error: result_box.append((result, error)))
    transport.messageReceived.emit({"id": request_id, "result": {"data": []}})
    transport.messageReceived.emit({"method": "turn/started", "params": {"turn": {"id": "t"}}})

    assert transport.sent[0]["method"] == "model/list"
    assert result_box == [({"data": []}, None)]
    assert notifications == [("turn/started", {"turn": {"id": "t"}})]


def test_rpc_initialize_sends_initialized_notification(qtbot) -> None:
    transport = FakeTransport()
    rpc = JsonRpcClient(transport)  # type: ignore[arg-type]
    rpc.initialize()
    initialize = transport.sent[-1]
    assert initialize["params"]["capabilities"]["experimentalApi"] is True
    transport.messageReceived.emit({"id": initialize["id"], "result": {"platformOs": "linux"}})
    assert transport.sent[-1] == {"method": "initialized", "params": {}}


def test_rpc_accepts_response_when_callback_is_omitted(qtbot) -> None:
    transport = FakeTransport()
    rpc = JsonRpcClient(transport)  # type: ignore[arg-type]
    errors = []
    rpc.protocolError.connect(errors.append)
    request_id = rpc.request("thread/unsubscribe", {"threadId": "thr_1"})
    transport.messageReceived.emit({"id": request_id, "result": {}})
    assert errors == []


def test_process_stop_blocks_qprocess_signals_before_waiting(qtbot) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def state(self):
            return QProcess.ProcessState.Running

        def blockSignals(self, value):
            self.calls.append(("block", value))

        def terminate(self):
            self.calls.append("terminate")

        def waitForFinished(self, timeout):
            self.calls.append(("wait", timeout))
            return True

    transport = CodexProcess("codex")
    original_process = transport.process
    process = FakeProcess()
    transport.process = process  # type: ignore[assignment]

    transport.stop()

    assert process.calls == [("block", True), "terminate", ("wait", 1500)]
    original_process.deleteLater()
