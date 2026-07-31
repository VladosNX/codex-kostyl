from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .models import PLAN_MODE_VALUE, AccessMode, Attachment, ModelInfo, ThreadSummary
from .rpc import JsonRpcClient


class CodexService(QObject):
    ready = Signal()
    accountUpdated = Signal(object)
    rateLimitsUpdated = Signal(object)
    modelsUpdated = Signal(object)
    threadsUpdated = Signal(object)
    threadLoaded = Signal(dict)
    currentThreadChanged = Signal(str)
    itemUpdated = Signal(dict, bool)
    agentDelta = Signal(str, str)
    reasoningDelta = Signal(str, str)
    commandDelta = Signal(str, str)
    planDelta = Signal(str, str)
    turnPlanUpdated = Signal(dict)
    tokenUsageUpdated = Signal(dict)
    turnStateChanged = Signal(str)
    approvalRequested = Signal(object, str, dict)
    userInputRequested = Signal(object, dict)
    serverRequestResolved = Signal(object)
    errorOccurred = Signal(str)
    loginStarted = Signal(dict)

    def __init__(self, rpc: JsonRpcClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.rpc = rpc
        self.current_project = ""
        self.current_thread_id = ""
        self.current_turn_id = ""
        self.current_thread_ready = False
        self._resuming: set[str] = set()
        self._after_resume: dict[str, list[Any]] = {}
        self._thread_list_generation = 0
        self.models: list[ModelInfo] = []
        self.account: dict[str, Any] | None = None
        self.connected = False
        self._thread_defaults: dict[str, Any] = {}

        rpc.initialized.connect(self._bootstrap)
        rpc.notification.connect(self._notification)
        rpc.serverRequest.connect(self._server_request)
        rpc.protocolError.connect(self.errorOccurred)

    def _bootstrap(self, _result: dict[str, Any]) -> None:
        self.connected = True
        self.current_turn_id = ""
        self.turnStateChanged.emit("idle")
        self.refresh_account()
        self.refresh_models()
        if self.current_thread_id:
            self.open_thread(self.current_thread_id)
        else:
            self.list_threads()
        self.ready.emit()

    def refresh_account(self) -> None:
        self.rpc.request("account/read", {"refreshToken": False}, self._account_response)

    def _account_response(self, result: Any, error: dict[str, Any] | None) -> None:
        if error:
            self._emit_rpc_error(error)
            return
        payload = result if isinstance(result, dict) else {}
        self.account = payload.get("account")
        self.accountUpdated.emit(payload)
        if isinstance(self.account, dict) and self.account.get("type") == "chatgpt":
            self.refresh_rate_limits()
        else:
            self.rateLimitsUpdated.emit({})

    def refresh_rate_limits(self) -> None:
        if not self.connected or not isinstance(self.account, dict) or self.account.get("type") != "chatgpt":
            self.rateLimitsUpdated.emit({})
            return
        self.rpc.request("account/rateLimits/read", {}, self._rate_limits_response)

    def _rate_limits_response(self, result: Any, error: dict[str, Any] | None) -> None:
        if error:
            self.rateLimitsUpdated.emit({})
            return
        self.rateLimitsUpdated.emit(result if isinstance(result, dict) else {})

    def login_chatgpt(self) -> None:
        self.rpc.request(
            "account/login/start",
            {"type": "chatgpt", "appBrand": "codex"},
            self._login_response,
        )

    def login_api_key(self, api_key: str) -> None:
        self.rpc.request(
            "account/login/start",
            {"type": "apiKey", "apiKey": api_key},
            self._login_response,
        )

    def _login_response(self, result: Any, error: dict[str, Any] | None) -> None:
        if error:
            self._emit_rpc_error(error)
            return
        if isinstance(result, dict):
            self.loginStarted.emit(result)
            if result.get("type") == "apiKey":
                self.refresh_account()

    def logout(self) -> None:
        self.rpc.request("account/logout", {}, lambda result, error: self.refresh_account())

    def refresh_models(self) -> None:
        self.rpc.request("model/list", {"limit": 100}, self._models_response)

    def _models_response(self, result: Any, error: dict[str, Any] | None) -> None:
        if error:
            self._emit_rpc_error(error)
            return
        rows = result.get("data", []) if isinstance(result, dict) else []
        self.models = [ModelInfo.from_wire(row) for row in rows if not row.get("hidden", False)]
        self.modelsUpdated.emit(self.models)

    def set_project(self, path: str) -> None:
        self._unsubscribe_current()
        self.current_project = str(Path(path).resolve())
        self.current_thread_id = ""
        self.current_turn_id = ""
        self.current_thread_ready = False
        self.list_threads()

    def list_threads(self) -> None:
        if not self.connected:
            return
        self._thread_list_generation += 1
        self._fetch_threads_page(self._thread_list_generation, None, [])

    def _fetch_threads_page(
        self,
        generation: int,
        cursor: str | None,
        collected: list[ThreadSummary],
    ) -> None:
        params: dict[str, Any] = {
            "limit": 100,
            "sortKey": "recency_at",
            "sortDirection": "desc",
        }
        if cursor:
            params["cursor"] = cursor

        def page_received(result: Any, error: dict[str, Any] | None) -> None:
            if generation != self._thread_list_generation:
                return
            if error:
                self._emit_rpc_error(error)
                return
            payload = result if isinstance(result, dict) else {}
            rows = payload.get("data", [])
            collected.extend(ThreadSummary.from_wire(row) for row in rows)
            next_cursor = payload.get("nextCursor")
            if next_cursor:
                self._fetch_threads_page(generation, str(next_cursor), collected)
            else:
                self.threadsUpdated.emit(collected)

        self.rpc.request(
            "thread/list",
            params,
            page_received,
        )

    def open_thread(self, thread_id: str) -> None:
        if self.current_thread_id and self.current_thread_id != thread_id:
            self._unsubscribe_current()
        self.current_thread_id = thread_id
        self.current_thread_ready = False
        self.currentThreadChanged.emit(thread_id)
        self._resume_thread(lambda: self._read_current_thread())

    def _resume_thread(self, after_resume: Any | None = None) -> None:
        if not self.current_thread_id:
            return
        thread_id = self.current_thread_id
        if after_resume:
            self._after_resume.setdefault(thread_id, []).append(after_resume)
        if thread_id in self._resuming:
            return
        self._resuming.add(thread_id)

        def resumed(_result: Any, error: dict[str, Any] | None) -> None:
            self._resuming.discard(thread_id)
            callbacks = self._after_resume.pop(thread_id, [])
            if error:
                self._emit_rpc_error(error)
                return
            if self.current_thread_id != thread_id:
                return
            self.current_thread_ready = True
            for callback in callbacks:
                callback()

        params = {"threadId": thread_id}
        if self.current_project:
            params["cwd"] = self.current_project
        self.rpc.request("thread/resume", params, resumed)

    def _read_current_thread(self) -> None:
        self.rpc.request(
            "thread/read",
            {"threadId": self.current_thread_id, "includeTurns": True},
            self._thread_read,
        )

    def _thread_read(self, result: Any, error: dict[str, Any] | None) -> None:
        if error:
            self._emit_rpc_error(error)
            return
        thread = result.get("thread", {}) if isinstance(result, dict) else {}
        self.threadLoaded.emit(thread)

    def new_thread(
        self,
        model: str,
        effort: str | None,
        access_mode: AccessMode,
        after_created: Any | None = None,
        collaboration_mode: str | None = None,
    ) -> None:
        if not self.current_project:
            self.errorOccurred.emit("Сначала выберите рабочую папку")
            return
        params: dict[str, Any] = {
            "cwd": self.current_project,
            "approvalPolicy": access_mode.approval_policy,
            "ephemeral": False,
            "serviceName": "codex_kostyl",
        }
        if model:
            params["model"] = model
        self._thread_defaults = {
            "model": model,
            "effort": effort,
            "access": access_mode,
            "collaboration_mode": collaboration_mode,
        }

        def done(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._emit_rpc_error(error)
                return
            thread = result.get("thread", {}) if isinstance(result, dict) else {}
            self.current_thread_id = str(thread.get("id", ""))
            self.current_thread_ready = True
            self.currentThreadChanged.emit(self.current_thread_id)
            self.threadLoaded.emit(thread)
            self.list_threads()
            if after_created:
                after_created()

        self.rpc.request("thread/start", params, done)

    def prepare_new_thread(self) -> None:
        self._unsubscribe_current()
        self.current_thread_id = ""
        self.current_turn_id = ""
        self.current_thread_ready = False
        self.currentThreadChanged.emit("")

    def _unsubscribe_current(self) -> None:
        thread_id = self.current_thread_id
        if not self.connected or not thread_id or self.current_turn_id:
            return
        self.rpc.request("thread/unsubscribe", {"threadId": thread_id})

    def send_message(
        self,
        text: str,
        attachments: list[Attachment],
        model: str,
        effort: str | None,
        access_mode: AccessMode,
        collaboration_mode: str | None = None,
    ) -> None:
        if not self.current_thread_id:
            self.new_thread(
                model,
                effort,
                access_mode,
                lambda: self.send_message(
                    text,
                    attachments,
                    model,
                    effort,
                    access_mode,
                    collaboration_mode,
                ),
                collaboration_mode,
            )
            return
        if not self.current_thread_ready:
            self._resume_thread(
                lambda: self.send_message(
                    text,
                    attachments,
                    model,
                    effort,
                    access_mode,
                    collaboration_mode,
                )
            )
            return
        inputs: list[dict[str, Any]] = []
        if text.strip():
            inputs.append({"type": "text", "text": text.strip()})
        inputs.extend(item.as_user_input() for item in attachments)
        if not inputs:
            return
        params: dict[str, Any] = {
            "threadId": self.current_thread_id,
            "input": inputs,
            "cwd": self.current_project,
            "approvalPolicy": access_mode.approval_policy,
            "sandboxPolicy": access_mode.sandbox_policy(self.current_project),
        }
        if collaboration_mode == PLAN_MODE_VALUE:
            params["collaborationMode"] = {
                "mode": PLAN_MODE_VALUE,
                "settings": {
                    "model": model,
                    "reasoning_effort": effort,
                    "developer_instructions": None,
                },
            }
        else:
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
        self.turnStateChanged.emit("starting")

        def started(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self.turnStateChanged.emit("failed")
                self._emit_rpc_error(error)
                return
            turn = result.get("turn", {}) if isinstance(result, dict) else {}
            self.current_turn_id = str(turn.get("id", ""))

        self.rpc.request("turn/start", params, started)

    def interrupt(self) -> None:
        if not self.current_thread_id or not self.current_turn_id:
            return
        self.rpc.request(
            "turn/interrupt",
            {"threadId": self.current_thread_id, "turnId": self.current_turn_id},
            lambda _result, error: self._emit_rpc_error(error) if error else None,
        )

    def answer_approval(self, request_id: object, decision: str) -> None:
        self.rpc.respond(request_id, {"decision": decision})

    def answer_user_input(self, request_id: object, answers: dict[str, list[str]]) -> None:
        payload = {key: {"answers": values} for key, values in answers.items()}
        self.rpc.respond(request_id, {"answers": payload})

    def cancel_server_request(self, request_id: object) -> None:
        self.rpc.respond_error(request_id, -32800, "Canceled by user")

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "account/updated":
            self.refresh_account()
            return
        if method == "account/rateLimits/updated":
            # The push payload may contain only the bucket that changed. Read the
            # complete snapshot so the weekly bucket is not accidentally cleared.
            self.refresh_rate_limits()
            return
        if method == "account/login/completed":
            if params.get("success", True):
                self.refresh_account()
            else:
                self.errorOccurred.emit(str(params.get("error", "Не удалось войти")))
            return
        if method == "thread/tokenUsage/updated":
            thread_id = str(params.get("threadId", ""))
            if not self.current_thread_id or thread_id == self.current_thread_id:
                usage = params.get("tokenUsage", {})
                if isinstance(usage, dict):
                    self.tokenUsageUpdated.emit(usage)
            return
        if method == "turn/plan/updated":
            turn_id = str(params.get("turnId", ""))
            if not self.current_turn_id or turn_id == self.current_turn_id:
                self.turnPlanUpdated.emit(params)
            return
        if method == "turn/started":
            turn = params.get("turn", {})
            self.current_turn_id = str(turn.get("id", ""))
            self.turnStateChanged.emit("inProgress")
            return
        if method == "turn/completed":
            turn = params.get("turn", {})
            status = str(turn.get("status", "completed"))
            self.current_turn_id = ""
            self.turnStateChanged.emit(status)
            self.list_threads()
            self.refresh_rate_limits()
            if status == "failed":
                error = turn.get("error") or {}
                self.errorOccurred.emit(str(error.get("message", "Ход завершился ошибкой")))
            return
        if method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            if isinstance(item, dict):
                self.itemUpdated.emit(item, method.endswith("completed"))
            return
        if method == "item/agentMessage/delta":
            self.agentDelta.emit(str(params.get("itemId", "")), str(params.get("delta", "")))
            return
        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            self.reasoningDelta.emit(str(params.get("itemId", "")), str(params.get("delta", "")))
            return
        if method == "item/commandExecution/outputDelta":
            self.commandDelta.emit(str(params.get("itemId", "")), str(params.get("delta", "")))
            return
        if method == "item/plan/delta":
            self.planDelta.emit(str(params.get("itemId", "")), str(params.get("delta", "")))
            return
        if method == "serverRequest/resolved":
            self.serverRequestResolved.emit(params.get("requestId"))
            return
        if method == "error":
            error = params.get("error", params)
            self.errorOccurred.emit(str(error.get("message", "Ошибка Codex")))

    def _server_request(self, request_id: object, method: str, params: dict[str, Any]) -> None:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            self.approvalRequested.emit(request_id, method, params)
            return
        if method == "item/tool/requestUserInput":
            self.userInputRequested.emit(request_id, params)
            return
        self.rpc.respond_error(request_id, -32601, f"Unsupported client method: {method}")

    def _emit_rpc_error(self, error: dict[str, Any] | None) -> None:
        if error:
            self.errorOccurred.emit(str(error.get("message", "Ошибка Codex")))
