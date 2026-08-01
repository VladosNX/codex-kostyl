from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from . import __version__

RpcCallback = Callable[[Any | None, dict[str, Any] | None], None]


class JsonLineDecoder:
    """Incrementally decode UTF-8 JSON objects separated by newlines."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[list[dict[str, Any]], list[str]]:
        self._buffer.extend(chunk)
        messages: list[dict[str, Any]] = []
        errors: list[str] = []
        while b"\n" in self._buffer:
            raw, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            self._decode_line(raw, messages, errors)
        return messages, errors

    def finish(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Decode a final non-newline-terminated message at transport EOF."""
        messages: list[dict[str, Any]] = []
        errors: list[str] = []
        raw = bytes(self._buffer)
        self._buffer.clear()
        self._decode_line(raw, messages, errors)
        return messages, errors

    @staticmethod
    def _decode_line(
        raw: bytes,
        messages: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        if not raw.strip():
            return
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("JSON-RPC message is not an object")
            messages.append(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"Некорректное сообщение app-server: {exc}")


class JsonLineProcess(QObject):
    """Cross-platform NDJSON subprocess transport.

    QProcess keeps argument quoting platform-correct; drivers provide the
    executable and argument vector without invoking a shell.
    """

    messageReceived = Signal(dict)
    stderrReceived = Signal(str)
    protocolError = Signal(str)
    started = Signal()
    stopped = Signal(int, str)

    def __init__(
        self,
        program: str,
        arguments: list[str] | None = None,
        parent: QObject | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.program = program
        self.arguments = list(arguments or [])
        self.environment = dict(environment or {})
        self.process = QProcess(self)
        if self.environment:
            process_environment = QProcessEnvironment.systemEnvironment()
            for key, value in self.environment.items():
                process_environment.insert(key, value)
            self.process.setProcessEnvironment(process_environment)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.started.connect(self.started)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self._decoder = JsonLineDecoder()

    def start(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self._decoder = JsonLineDecoder()
        self.process.blockSignals(False)
        self.process.setProgram(self.program)
        self.process.setArguments(self.arguments)
        self.process.start()

    def stop(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        # PySide can invoke Python slots re-entrantly from waitForFinished().
        # During application teardown that caused a reproducible SIGSEGV in the
        # QProcess.finished -> Python signal bridge. No lifecycle notification is
        # needed after the main window has committed to closing, so block the
        # process signals before the synchronous shutdown.
        self.process.blockSignals(True)
        self.process.terminate()
        if not self.process.waitForFinished(1500):
            self.process.kill()
            self.process.waitForFinished(500)

    def send(self, payload: dict[str, Any]) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            raise RuntimeError("Процесс агента не запущен")
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self.process.write(wire)

    def _read_stdout(self) -> None:
        messages, errors = self._decoder.feed(bytes(self.process.readAllStandardOutput()))
        for error in errors:
            self.protocolError.emit(error)
        for message in messages:
            self.messageReceived.emit(message)

    def _read_stderr(self) -> None:
        text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        if text:
            self.stderrReceived.emit(text.rstrip())

    def _finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        # QProcess normally emits readyRead first, but explicitly drain both
        # channels so a short final diagnostic or JSON response is not lost.
        self._read_stdout()
        self._read_stderr()
        messages, errors = self._decoder.finish()
        for error in errors:
            self.protocolError.emit(error)
        for message in messages:
            self.messageReceived.emit(message)
        self.stopped.emit(exit_code, exit_status.name)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        self.protocolError.emit(
            f"Ошибка запуска процесса агента: {error.name}: {self.process.errorString()}"
        )


class CodexProcess(JsonLineProcess):
    """Compatibility wrapper for the original public transport name."""

    def __init__(self, codex_path: str = "codex", parent: QObject | None = None) -> None:
        super().__init__(codex_path, ["app-server", "--stdio"], parent=parent)


class JsonRpcClient(QObject):
    notification = Signal(str, dict)
    serverRequest = Signal(object, str, dict)
    protocolError = Signal(str)
    initialized = Signal(dict)
    disconnected = Signal()

    def __init__(
        self,
        transport: JsonLineProcess,
        parent: QObject | None = None,
        jsonrpc_version: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.transport = transport
        self.jsonrpc_version = jsonrpc_version
        self._next_id = 1
        self._pending: dict[int, RpcCallback] = {}
        transport.messageReceived.connect(self._on_message)
        transport.protocolError.connect(self.protocolError)
        stopped = getattr(transport, "stopped", None)
        if stopped is not None:
            stopped.connect(self._transport_stopped)

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_kostyl",
                    "title": "Codex Kostyl",
                    "version": __version__,
                },
                "capabilities": {"experimentalApi": True},
            },
            self._initialized,
        )

    def request(self, method: str, params: dict[str, Any], callback: RpcCallback | None = None) -> int:
        request_id = self._next_id
        self._next_id += 1
        self._pending[request_id] = callback or (lambda _result, _error: None)
        try:
            payload = {"method": method, "id": request_id, "params": params}
            if self.jsonrpc_version:
                payload["jsonrpc"] = self.jsonrpc_version
            self.transport.send(payload)
        except Exception:
            self._pending.pop(request_id, None)
            raise
        return request_id

    def notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {"method": method, "params": params}
        if self.jsonrpc_version:
            payload["jsonrpc"] = self.jsonrpc_version
        self.transport.send(payload)

    def respond(self, request_id: object, result: Any) -> None:
        payload = {"id": request_id, "result": result}
        if self.jsonrpc_version:
            payload["jsonrpc"] = self.jsonrpc_version
        self.transport.send(payload)

    def respond_error(self, request_id: object, code: int, message: str) -> None:
        payload = {"id": request_id, "error": {"code": code, "message": message}}
        if self.jsonrpc_version:
            payload["jsonrpc"] = self.jsonrpc_version
        self.transport.send(payload)

    def _initialized(self, result: Any | None, error: dict[str, Any] | None) -> None:
        if error:
            self.protocolError.emit(error.get("message", "Не удалось инициализировать app-server"))
            return
        self.notify("initialized", {})
        self.initialized.emit(result if isinstance(result, dict) else {})

    def _on_message(self, message: dict[str, Any]) -> None:
        raw_params = message.get("params", {})
        params = raw_params if isinstance(raw_params, dict) else {}
        if "method" in message and not isinstance(raw_params, dict):
            self.protocolError.emit("Некорректные параметры JSON-RPC сообщения")
        if "method" in message and "id" in message:
            self.serverRequest.emit(message["id"], str(message["method"]), params)
            return
        if "method" in message:
            self.notification.emit(str(message["method"]), params)
            return
        request_id = message.get("id")
        try:
            callback = self._pending.pop(request_id, None)
        except TypeError:
            callback = None
        if callback is None:
            self.protocolError.emit(f"Ответ на неизвестный запрос: {request_id}")
            return
        raw_error = message.get("error")
        error = raw_error if isinstance(raw_error, dict) else None
        if raw_error is not None and error is None:
            error = {"message": str(raw_error)}
        try:
            callback(message.get("result"), error)
        except Exception as exc:
            self.protocolError.emit(f"Ошибка обработки ответа app-server: {exc}")

    def _transport_stopped(self, _exit_code: int, _status: str) -> None:
        # Responses from the old process can never arrive after a restart.
        self._pending.clear()
        self.disconnected.emit()
