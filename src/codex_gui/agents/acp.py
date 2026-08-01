from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime
import mimetypes
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from .base import (
    AgentAction,
    AgentAvailability,
    AgentConfigOption,
    AgentDriver,
    AgentEvent,
    AgentManifest,
    AgentProfile,
    AgentPrompt,
    AgentRunMode,
    AgentState,
    AuthMethod,
    ClientRequest,
    ConfigOptionValue,
    FeatureId,
    FeatureState,
    FeatureSupport,
    PermissionOption,
    SessionSnapshot,
    SessionSummary,
)
from .registry import DriverRegistration
from .. import __version__
from ..models import Attachment, ThreadSummary
from ..rpc import JsonLineProcess, JsonRpcClient

ACP_DRIVER_KIND = "acp"
ACP_MODE_OPTION_ID = "acp.session_mode"


def _base_manifest() -> AgentManifest:
    supported = {
        FeatureId.INPUT_FILES,
        FeatureId.RUN_CANCEL,
        FeatureId.PERMISSIONS,
    }
    return AgentManifest(
        features={
            feature.value: FeatureSupport(
                feature in supported,
                "Агент ещё не объявил поддержку функции" if feature not in supported else "",
            )
            for feature in FeatureId
        },
        implementation_name="acp",
        implementation_version="1",
    )


class AcpDriver(AgentDriver):
    """ACP v1 client over the stable local stdio transport."""

    def __init__(
        self,
        profile: AgentProfile,
        rpc: JsonRpcClient,
        process: JsonLineProcess | None = None,
        parent=None,
    ) -> None:
        super().__init__(profile, _base_manifest(), parent)
        self.rpc = rpc
        self.process = process
        self.agent_capabilities: dict[str, Any] = {}
        self.config_options: tuple[AgentConfigOption, ...] = ()
        self._protocol_config_options: tuple[AgentConfigOption, ...] = ()
        self._mode_option: AgentConfigOption | None = None
        self._pending_permissions: dict[object, list[dict[str, Any]]] = {}
        self._stream_item_ids: dict[str, str] = {}
        self._message_buffers: dict[str, str] = {}
        self._prompt_request_id: int | None = None
        self._session_generation = 0
        self._session_requests: dict[int, list[Any]] = {}

        rpc.notification.connect(self._notification)
        rpc.serverRequest.connect(self._server_request)
        rpc.protocolError.connect(self.errorOccurred)
        rpc.disconnected.connect(self._disconnected)

    @classmethod
    def create(cls, profile: AgentProfile) -> AcpDriver:
        process = JsonLineProcess(
            profile.executable,
            list(profile.arguments),
            environment=dict(profile.environment),
        )
        rpc = JsonRpcClient(process, jsonrpc_version="2.0")
        driver = cls(profile, rpc, process)
        process.started.connect(driver._initialize)
        process.stopped.connect(driver.processStopped)
        return driver

    @staticmethod
    def check_availability(profile: AgentProfile) -> AgentAvailability:
        if profile.unavailable_reason:
            return AgentAvailability(False, error=profile.unavailable_reason)
        if not profile.executable.strip():
            return AgentAvailability(
                False,
                error="Укажите путь к исполняемому файлу ACP-агента",
            )
        raw = str(Path(profile.executable).expanduser())
        resolved = shutil.which(raw) or raw
        path = Path(resolved)
        if not path.is_file():
            return AgentAvailability(
                False,
                executable=resolved,
                error=f"Исполняемый файл ACP-агента не найден: {resolved}",
            )
        if os.name != "nt" and not os.access(path, os.X_OK):
            return AgentAvailability(
                False,
                executable=resolved,
                error=f"Файл ACP-агента не является исполняемым: {resolved}",
            )
        return AgentAvailability(True, executable=str(path.resolve()))

    def start(self) -> None:
        if self.process is not None:
            self.process.start()

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()

    def _initialize(self) -> None:
        self.rpc.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                    "session": {"configOptions": {"boolean": {}}},
                },
                "clientInfo": {
                    "name": "codex_kostyl",
                    "title": "Codex Kostyl",
                    "version": __version__,
                },
            },
            self._initialized,
        )

    def _initialized(self, result: Any, error: dict[str, Any] | None) -> None:
        if error:
            self.errorOccurred.emit(str(error.get("message", "Не удалось инициализировать ACP")))
            return
        payload = result if isinstance(result, dict) else {}
        if int(payload.get("protocolVersion", 0)) != 1:
            self.errorOccurred.emit("ACP-агент не поддерживает protocolVersion 1")
            self.stop()
            return
        self.agent_capabilities = (
            payload.get("agentCapabilities", {})
            if isinstance(payload.get("agentCapabilities"), dict)
            else {}
        )
        info = payload.get("agentInfo", {})
        if not isinstance(info, dict):
            info = {}
        auth = self._parse_auth_methods(payload.get("authMethods", []))
        self.manifest = self._manifest_from_initialize(info, auth)
        self.capabilities = self.capabilities.from_manifest(self.manifest)
        self.connected = True
        self.account = (
            None
            if auth
            else {"type": "acp", "name": info.get("title") or info.get("name")}
        )
        self.manifestUpdated.emit(self.manifest)
        self.accountUpdated.emit({"account": self.account})
        self._emit_state("idle")
        if self.current_project:
            self.prepare_new_session()
        self.list_sessions()
        self.ready.emit()

    def _manifest_from_initialize(
        self,
        info: dict[str, Any],
        auth_methods: tuple[AuthMethod, ...],
    ) -> AgentManifest:
        caps = self.agent_capabilities
        prompt = caps.get("promptCapabilities", {})
        prompt = prompt if isinstance(prompt, dict) else {}
        session = caps.get("sessionCapabilities", {})
        session = session if isinstance(session, dict) else {}
        auth_caps = caps.get("auth", {})
        auth_caps = auth_caps if isinstance(auth_caps, dict) else {}
        support = {
            feature.value: FeatureSupport(False, "Агент не объявил поддержку функции")
            for feature in FeatureId
        }
        enabled = {
            FeatureId.INPUT_FILES,
            FeatureId.RUN_CANCEL,
            FeatureId.PERMISSIONS,
        }
        if prompt.get("image") is True:
            enabled.add(FeatureId.INPUT_IMAGES)
        if session.get("list") is not None:
            enabled.add(FeatureId.SESSION_HISTORY)
        if auth_methods:
            enabled.add(FeatureId.AUTHENTICATION)
        for feature in enabled:
            support[feature.value] = FeatureSupport(True)
        actions: list[AgentAction] = []
        if session.get("close") is not None:
            actions.append(AgentAction("close-session", "Закрыть сессию", requires_session=True))
        if session.get("delete") is not None:
            actions.append(AgentAction("delete-session", "Удалить сессию", requires_session=True))
        return AgentManifest(
            support,
            tuple(actions),
            (),
            auth_methods,
            str(info.get("title") or info.get("name") or "ACP agent"),
            str(info.get("version") or ""),
        )

    @staticmethod
    def _parse_auth_methods(raw: object) -> tuple[AuthMethod, ...]:
        if not isinstance(raw, list):
            return ()
        return tuple(
            AuthMethod(
                str(item.get("id", "")),
                str(item.get("name") or item.get("id") or "Войти"),
                str(item.get("type") or "agent"),
                str(item.get("description") or ""),
            )
            for item in raw
            if isinstance(item, dict) and item.get("id")
        )

    def feature_override(self, feature: str) -> FeatureState | None:
        if feature == FeatureId.USAGE_CONTEXT.value:
            support = self.manifest.support(feature)
            if not support.supported:
                return FeatureState(False, False, "Агент пока не передавал usage_update")
        return None

    def set_project(self, path: str) -> None:
        self.current_project = str(Path(path).resolve())
        self._reset_session()
        self.list_sessions()

    def list_sessions(self) -> None:
        session_caps = self.agent_capabilities.get("sessionCapabilities", {})
        if not self.connected or not isinstance(session_caps, dict) or session_caps.get("list") is None:
            self.sessionsUpdated.emit([])
            self.threadsUpdated.emit([])
            return
        self._list_page(None, [])

    def _list_page(self, cursor: str | None, collected: list[SessionSummary]) -> None:
        params: dict[str, Any] = {}
        if self.current_project:
            params["cwd"] = self.current_project
        if cursor:
            params["cursor"] = cursor

        def received(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._rpc_error(error)
                return
            payload = result if isinstance(result, dict) else {}
            rows = payload.get("sessions", [])
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                collected.append(
                    SessionSummary(
                        str(row.get("sessionId", "")),
                        str(row.get("title") or "Новый чат"),
                        str(row.get("cwd") or ""),
                        _iso_timestamp(row.get("updatedAt")),
                    )
                )
            next_cursor = payload.get("nextCursor")
            if next_cursor:
                self._list_page(str(next_cursor), collected)
                return
            self.sessionsUpdated.emit(list(collected))
            self.threadsUpdated.emit(
                [ThreadSummary(item.id, item.title, item.cwd, item.updated_at, item.status) for item in collected]
            )

        self.rpc.request("session/list", params, received)

    def load_session(self, session_id: str) -> None:
        self.current_session_id = session_id
        self.current_run_id = ""
        self._session_generation += 1
        self._stream_item_ids.clear()
        self._message_buffers.clear()
        self.currentThreadChanged.emit(session_id)
        self.sessionLoaded.emit(SessionSnapshot(session_id))
        self.threadLoaded.emit({"id": session_id, "turns": []})
        params = {"sessionId": session_id, "cwd": self.current_project, "mcpServers": []}
        session_caps = self.agent_capabilities.get("sessionCapabilities", {})
        method = "session/load" if self.agent_capabilities.get("loadSession") is True else ""
        if not method and isinstance(session_caps, dict) and session_caps.get("resume") is not None:
            method = "session/resume"
        if not method:
            self.errorOccurred.emit("ACP-агент не поддерживает загрузку сессий")
            return

        def loaded(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._rpc_error(error)
                return
            payload = result if isinstance(result, dict) else {}
            self._apply_config_options(payload.get("configOptions", []))
            self._apply_modes(payload.get("modes"))
            self._emit_state("idle")

        self.rpc.request(method, params, loaded)

    def prepare_new_session(self) -> None:
        self._reset_session()
        if self.connected and self.current_project:
            self._create_session(
                lambda ok: self._emit_state("idle") if ok else None,
                self._session_generation,
            )

    def _reset_session(self) -> None:
        self._session_generation += 1
        self.current_session_id = ""
        self.current_run_id = ""
        self.current_run_mode_id = ""
        self.config_options = ()
        self._protocol_config_options = ()
        self._mode_option = None
        self._stream_item_ids.clear()
        self._message_buffers.clear()
        self.currentThreadChanged.emit("")
        self._publish_config_options()
        self._emit_state("idle")

    def submit_prompt(self, prompt: AgentPrompt) -> None:
        if not self.connected:
            self.errorOccurred.emit("ACP-агент не подключён")
            return
        if not self.current_session_id:
            self._create_session(lambda ok: self._submit_after_session(prompt) if ok else None)
            return
        self._submit_after_session(prompt)

    def _create_session(self, callback: Any, generation: int | None = None) -> None:
        if not self.current_project:
            self.errorOccurred.emit("Сначала выберите рабочую папку")
            callback(False)
            return
        generation = self._session_generation if generation is None else generation
        callbacks = self._session_requests.get(generation)
        if callbacks is not None:
            callbacks.append(callback)
            return
        self._session_requests[generation] = [callback]
        self.rpc.request(
            "session/new",
            {"cwd": self.current_project, "mcpServers": []},
            lambda result, error: self._session_created(result, error, generation),
        )

    def _session_created(
        self,
        result: Any,
        error: dict[str, Any] | None,
        generation: int,
    ) -> None:
        callbacks = self._session_requests.pop(generation, [])
        if generation != self._session_generation:
            for callback in callbacks:
                callback(False)
            return
        if error:
            self._rpc_error(error)
            for callback in callbacks:
                callback(False)
            return
        payload = result if isinstance(result, dict) else {}
        session_id = str(payload.get("sessionId", ""))
        if not session_id:
            self.errorOccurred.emit("ACP-агент не вернул sessionId")
            for callback in callbacks:
                callback(False)
            return
        self.current_session_id = session_id
        self.currentThreadChanged.emit(session_id)
        self.sessionLoaded.emit(SessionSnapshot(session_id))
        self.threadLoaded.emit({"id": session_id, "turns": []})
        self._apply_config_options(payload.get("configOptions", []))
        self._apply_modes(payload.get("modes"))
        for callback in callbacks:
            callback(True)

    def _submit_after_session(self, prompt: AgentPrompt) -> None:
        requested_config = dict(prompt.config)
        run_mode_id = prompt.run_mode_id or prompt.mode
        protocol_mode = next(
            (option for option in self._protocol_config_options if option.category == "mode"),
            None,
        )
        if (
            run_mode_id
            and protocol_mode is not None
            and any(value.value == run_mode_id for value in protocol_mode.values)
        ):
            requested_config[protocol_mode.id] = run_mode_id
        if (
            run_mode_id
            and protocol_mode is None
            and self._mode_option is not None
            and any(value.value == run_mode_id for value in self._mode_option.values)
        ):
            requested_config[ACP_MODE_OPTION_ID] = run_mode_id
        changes = [
            (key, value)
            for key, value in requested_config.items()
            if value not in {"", None}
            and any(option.id == key and option.current_value != value for option in self.config_options)
        ]

        def apply_next() -> None:
            if not changes:
                self._start_prompt(prompt)
                return
            key, value = changes.pop(0)
            self.set_config_option(key, value, apply_next)

        apply_next()

    def _start_prompt(self, prompt: AgentPrompt) -> None:
        blocks: list[dict[str, Any]] = []
        message_id = str(uuid4())
        if prompt.text.strip():
            blocks.append({"type": "text", "text": prompt.text.strip()})
        for attachment in prompt.attachments:
            if isinstance(attachment, Attachment):
                block = self._attachment_block(attachment)
                if block is not None:
                    blocks.append(block)
        if not blocks:
            return
        user_item = {
            "id": message_id,
            "clientId": message_id,
            "kind": "user_message",
            "subtype": "acp_user_message",
            "content": [block for block in blocks if block.get("type") == "text"],
        }
        self.itemUpdated.emit(user_item, True)
        self.eventReceived.emit(
            AgentEvent("user_message", self.current_session_id, item_id=message_id, payload=user_item)
        )
        self.current_run_id = "starting"
        self.turnStateChanged.emit("starting")
        self._emit_state("starting")

        def completed(result: Any, error: dict[str, Any] | None) -> None:
            self._prompt_request_id = None
            self.current_run_id = ""
            self._stream_item_ids.clear()
            self._message_buffers.clear()
            if error:
                self.turnStateChanged.emit("failed")
                self._emit_state("failed")
                self._rpc_error(error)
                return
            payload = result if isinstance(result, dict) else {}
            stop_reason = str(payload.get("stopReason") or "end_turn")
            status = "interrupted" if stop_reason in {"cancelled", "canceled"} else "completed"
            self.turnStateChanged.emit(status)
            self._emit_state(status)
            self.list_sessions()

        request_id = self.rpc.request(
            "session/prompt",
            {"sessionId": self.current_session_id, "prompt": blocks},
            completed,
        )
        self._prompt_request_id = request_id
        self.current_run_id = str(request_id)
        self.turnStateChanged.emit("inProgress")
        self._emit_state("inProgress")

    def _attachment_block(self, attachment: Attachment) -> dict[str, Any] | None:
        if attachment.is_image and self.manifest.support(FeatureId.INPUT_IMAGES).supported:
            try:
                data = base64.b64encode(attachment.path.read_bytes()).decode("ascii")
            except OSError as exc:
                self.errorOccurred.emit(f"Не удалось прочитать вложение: {exc}")
                return None
            mime = mimetypes.guess_type(attachment.path.name)[0] or "application/octet-stream"
            return {"type": "image", "data": data, "mimeType": mime}
        return {
            "type": "resource_link",
            "name": attachment.name,
            "uri": attachment.path.resolve().as_uri(),
        }

    def cancel_run(self) -> None:
        if not self.current_session_id or not self.current_run_id:
            return
        self.rpc.notify("session/cancel", {"sessionId": self.current_session_id})
        for request_id in list(self._pending_permissions):
            self.rpc.respond(request_id, {"outcome": {"outcome": "cancelled"}})
        self._pending_permissions.clear()

    def invoke_action(self, action_id: str, arguments: str = "", callback: Any | None = None) -> None:
        if action_id == "close-session":
            self._session_action("session/close", callback)
            return
        if action_id == "delete-session":
            self._session_action("session/delete", callback)
            return
        command = next((action for action in self.manifest.actions if action.id == action_id), None)
        if command is None:
            self.errorOccurred.emit(f"ACP-агент не поддерживает действие {action_id}")
            if callback:
                callback(False)
            return
        text = f"/{action_id}" + (f" {arguments}" if arguments.strip() else "")
        self.submit_prompt(AgentPrompt(text, working_directory=self.current_project))
        if callback:
            callback(True)

    def _session_action(self, method: str, callback: Any | None) -> None:
        session_id = self.current_session_id
        self.rpc.request(
            method,
            {"sessionId": session_id},
            lambda _result, error: self._session_action_done(error, callback),
        )

    def _session_action_done(self, error: dict[str, Any] | None, callback: Any | None) -> None:
        success = error is None
        if error:
            self._rpc_error(error)
        else:
            self.prepare_new_session()
            self.list_sessions()
        if callback:
            callback(success)

    def set_config_option(self, option_id: str, value: str | bool, callback: Any | None = None) -> None:
        if not self.current_session_id:
            if callback:
                callback()
            return
        if option_id == ACP_MODE_OPTION_ID:
            self._set_mode(str(value), callback)
            return
        params: dict[str, Any] = {
            "sessionId": self.current_session_id,
            "configId": option_id,
            "value": value,
        }
        if isinstance(value, bool):
            params["type"] = "boolean"

        def changed(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._rpc_error(error)
            else:
                payload = result if isinstance(result, dict) else {}
                self._apply_config_options(payload.get("configOptions", []))
            if callback:
                callback()

        self.rpc.request("session/set_config_option", params, changed)

    def set_run_mode(self, mode_id: str) -> None:
        option = next(
            (item for item in self._protocol_config_options if item.category == "mode"),
            self._mode_option,
        )
        if option is None or not any(value.value == mode_id for value in option.values):
            self.errorOccurred.emit(f"ACP-агент не поддерживает режим {mode_id}")
            return
        self.set_config_option(option.id, mode_id)

    def _set_mode(self, mode_id: str, callback: Any | None) -> None:
        def changed(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._rpc_error(error)
            else:
                payload = result if isinstance(result, dict) else {}
                modes = payload.get("modes")
                if modes is not None:
                    self._apply_modes(modes)
                elif self._mode_option is not None:
                    self._mode_option = replace(self._mode_option, current_value=mode_id)
                    self._publish_config_options()
            if callback:
                callback()

        self.rpc.request(
            "session/set_mode",
            {"sessionId": self.current_session_id, "modeId": mode_id},
            changed,
        )

    def authenticate(self, method_id: str, _secret: str = "") -> None:
        if not any(method.id == method_id for method in self.manifest.auth_methods):
            self.errorOccurred.emit(f"Неизвестный способ входа: {method_id}")
            return

        def authenticated(_result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._rpc_error(error)
                return
            self.account = {"type": "acp", "method": method_id}
            self.accountUpdated.emit({"account": self.account})
            self.loginStarted.emit({"type": "acp", "methodId": method_id})

        self.rpc.request("authenticate", {"methodId": method_id}, authenticated)

    def logout(self) -> None:
        auth = self.agent_capabilities.get("auth", {})
        if not isinstance(auth, dict) or auth.get("logout") is None:
            self.errorOccurred.emit("ACP-агент не поддерживает выход")
            return
        self.rpc.request("logout", {}, lambda _result, error: self._logged_out(error))

    def _logged_out(self, error: dict[str, Any] | None) -> None:
        if error:
            self._rpc_error(error)
            return
        self.account = None
        self.accountUpdated.emit({"account": None})

    def respond_to_request(self, request_id: object, decision: str) -> None:
        options = self._pending_permissions.pop(request_id, [])
        if decision == "cancel":
            self.rpc.respond(request_id, {"outcome": {"outcome": "cancelled"}})
            return
        direct = next(
            (
                str(option.get("optionId") or option.get("id"))
                for option in options
                if str(option.get("optionId") or option.get("id")) == decision
            ),
            "",
        )
        preferred = {
            "accept": ("allow_once", "allow"),
            "acceptForSession": ("allow_always", "allow"),
            "decline": ("reject_once", "reject_always", "reject"),
        }.get(decision, ())
        selected = direct or next(
            (
                str(option.get("optionId") or option.get("id"))
                for kind in preferred
                for option in options
                if str(option.get("kind", "")) == kind
            ),
            "",
        )
        if not selected and options:
            selected = str(options[0].get("optionId") or options[0].get("id") or "")
        if not selected:
            self.rpc.respond(request_id, {"outcome": {"outcome": "cancelled"}})
            return
        self.rpc.respond(
            request_id,
            {"outcome": {"outcome": "selected", "optionId": selected}},
        )

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        session_id = str(params.get("sessionId", ""))
        if self.current_session_id and session_id != self.current_session_id:
            return
        update = params.get("update", {})
        if not isinstance(update, dict):
            return
        self._session_update(session_id, update)

    def _session_update(self, session_id: str, update: dict[str, Any]) -> None:
        kind = str(update.get("sessionUpdate", ""))
        if kind in {"agent_message_chunk", "agent_thought_chunk", "user_message_chunk"}:
            self._message_chunk(session_id, kind, update)
            return
        if kind in {"tool_call", "tool_call_update"}:
            item_id = str(update.get("toolCallId") or uuid4())
            item = {
                "id": item_id,
                "kind": "tool_call",
                "subtype": str(update.get("kind") or "acp_tool_call"),
                **{
                    key: value
                    for key, value in update.items()
                    if key not in {"sessionUpdate", "kind"}
                },
            }
            self.itemUpdated.emit(item, str(update.get("status", "")) in {"completed", "failed"})
            self.eventReceived.emit(AgentEvent("tool_call", session_id, self.current_run_id, item_id, payload=item))
            return
        if kind == "plan":
            entries = update.get("entries", [])
            plan = [
                {
                    "step": str(entry.get("content", "")),
                    "status": "inProgress" if entry.get("status") == "in_progress" else str(entry.get("status", "pending")),
                }
                for entry in entries
                if isinstance(entry, dict)
            ] if isinstance(entries, list) else []
            payload = {"turnId": self.current_run_id, "plan": plan}
            self.turnPlanUpdated.emit(payload)
            self.eventReceived.emit(AgentEvent("plan", session_id, self.current_run_id, payload=payload))
            return
        if kind == "usage_update":
            used = update.get("used")
            size = update.get("size")
            usage = {"last": {"totalTokens": used}, "modelContextWindow": size}
            self.tokenUsageUpdated.emit(usage)
            features = dict(self.manifest.features)
            features[FeatureId.USAGE_CONTEXT.value] = FeatureSupport(True)
            self.manifest = replace(self.manifest, features=features)
            self.manifestUpdated.emit(self.manifest)
            self.eventReceived.emit(AgentEvent("usage", session_id, self.current_run_id, payload=usage))
            return
        if kind == "config_option_update":
            self._apply_config_options(update.get("configOptions", []))
            return
        if kind == "current_mode_update":
            mode_id = str(update.get("modeId") or update.get("currentModeId") or "")
            if self._mode_option is not None and mode_id:
                self._mode_option = replace(self._mode_option, current_value=mode_id)
                self._publish_config_options()
            return
        if kind == "available_commands_update":
            self._apply_commands(update.get("availableCommands", []))
            return
        if kind == "session_info_update":
            self.list_sessions()

    def _message_chunk(self, session_id: str, kind: str, update: dict[str, Any]) -> None:
        item_id = str(update.get("messageId") or self._stream_item_ids.get(kind) or uuid4())
        self._stream_item_ids[kind] = item_id
        content = update.get("content", {})
        text = str(content.get("text", "")) if isinstance(content, dict) else ""
        if kind == "agent_message_chunk":
            self.agentDelta.emit(item_id, text)
            event_kind = "assistant_message"
        elif kind == "agent_thought_chunk":
            self.reasoningDelta.emit(item_id, text)
            event_kind = "reasoning"
        else:
            aggregate = self._message_buffers.get(item_id, "") + text
            self._message_buffers[item_id] = aggregate
            self.itemUpdated.emit(
                {
                    "id": item_id,
                    "kind": "user_message",
                    "subtype": "acp_user_message",
                    "content": [{"type": "text", "text": aggregate}],
                },
                False,
            )
            event_kind = "user_message"
        self.eventReceived.emit(
            AgentEvent(event_kind, session_id, self.current_run_id, item_id, payload={"delta": text})
        )

    def _server_request(self, request_id: object, method: str, params: dict[str, Any]) -> None:
        if method == "session/request_permission":
            raw_options = params.get("options", [])
            options = [item for item in raw_options if isinstance(item, dict)] if isinstance(raw_options, list) else []
            self._pending_permissions[request_id] = options
            tool = params.get("toolCall", {})
            tool = tool if isinstance(tool, dict) else {}
            request = ClientRequest(
                request_id,
                "permission",
                str(tool.get("title") or "Разрешение на действие"),
                _tool_detail(tool),
                tuple(
                    PermissionOption(
                        str(item.get("optionId") or item.get("id") or ""),
                        str(item.get("name") or item.get("kind") or "Выбрать"),
                        str(item.get("kind") or ""),
                    )
                    for item in options
                ),
                params,
            )
            self.clientRequestReceived.emit(request)
            self.permissionRequested.emit(request)
            return
        self.rpc.respond_error(request_id, -32601, f"Unsupported client method: {method}")

    def _apply_config_options(self, raw: object) -> None:
        self._protocol_config_options = _parse_config_options(raw)
        self._publish_config_options()

    def _apply_modes(self, raw: object) -> None:
        self._mode_option = _parse_mode_option(raw)
        self._publish_config_options()

    def _publish_config_options(self) -> None:
        protocol_mode = next(
            (option for option in self._protocol_config_options if option.category == "mode"),
            None,
        )
        fallback_mode = self._mode_option if protocol_mode is None else None
        self.config_options = self._protocol_config_options + (
            (fallback_mode,) if fallback_mode is not None else ()
        )
        mode_option = protocol_mode or fallback_mode
        run_modes = (
            tuple(
                AgentRunMode(value.value, value.label, value.description)
                for value in mode_option.values
            )
            if mode_option is not None
            else ()
        )
        self.current_run_mode_id = (
            str(mode_option.current_value) if mode_option is not None else ""
        )
        features = dict(self.manifest.features)
        categories = {option.category for option in self.config_options}
        features[FeatureId.ACCESS_MODES.value] = FeatureSupport(bool(run_modes))
        features[FeatureId.CONFIG_MODEL.value] = FeatureSupport("model" in categories)
        features[FeatureId.CONFIG_THOUGHT_LEVEL.value] = FeatureSupport("thought_level" in categories)
        features[FeatureId.RUN_PLAN.value] = FeatureSupport(
            any(
                option.category == "mode"
                and any(value.value in {"plan", "architect"} for value in option.values)
                for option in self.config_options
            )
        )
        self.manifest = replace(
            self.manifest,
            features=features,
            config_options=self.config_options,
            run_modes=run_modes,
            current_run_mode_id=self.current_run_mode_id,
        )
        self.manifestUpdated.emit(self.manifest)
        self.configOptionsUpdated.emit(self.config_options)

    def _apply_commands(self, raw: object) -> None:
        commands = raw if isinstance(raw, list) else []
        protocol_actions = tuple(
            AgentAction(
                str(item.get("name", "")).lstrip("/"),
                str(item.get("description") or item.get("name") or "Команда"),
                requires_session=True,
                argument_hint=(
                    str(item["input"].get("hint") or "")
                    if isinstance(item.get("input"), dict)
                    else ""
                ),
            )
            for item in commands
            if isinstance(item, dict) and item.get("name")
        )
        builtins = tuple(action for action in self.manifest.actions if action.id in {"close-session", "delete-session"})
        self.manifest = replace(self.manifest, actions=builtins + protocol_actions)
        self.manifestUpdated.emit(self.manifest)

    def _emit_state(self, status: str) -> None:
        self.stateUpdated.emit(
            AgentState(
                "connected" if self.connected else "disconnected",
                self.profile.id,
                self.current_session_id,
                self.current_run_id,
                current_run_mode_id=self.current_run_mode_id,
            )
        )

    def _disconnected(self) -> None:
        running = bool(self.current_run_id)
        self.connected = False
        self.current_run_id = ""
        self._pending_permissions.clear()
        if running:
            self.turnStateChanged.emit("failed")
        self._emit_state("disconnected")
        self.disconnected.emit()

    def _rpc_error(self, error: dict[str, Any]) -> None:
        self.errorOccurred.emit(str(error.get("message", "Ошибка ACP-агента")))


def acp_driver_registration() -> DriverRegistration:
    return DriverRegistration(ACP_DRIVER_KIND, AcpDriver.create, AcpDriver.check_availability)


def _parse_config_options(raw: object) -> tuple[AgentConfigOption, ...]:
    if not isinstance(raw, list):
        return ()
    parsed: list[AgentConfigOption] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        values: list[ConfigOptionValue] = []
        raw_values = item.get("options", [])
        if isinstance(raw_values, list):
            for value in raw_values:
                if not isinstance(value, dict):
                    continue
                if isinstance(value.get("options"), list):
                    for grouped in value["options"]:
                        if isinstance(grouped, dict) and grouped.get("value") is not None:
                            values.append(_config_value(grouped))
                elif value.get("value") is not None:
                    values.append(_config_value(value))
        parsed.append(
            AgentConfigOption(
                str(item["id"]),
                str(item.get("name") or item["id"]),
                str(item.get("category") or ""),
                str(item.get("type") or "select"),
                item.get("currentValue", ""),
                tuple(values),
                str(item.get("description") or ""),
            )
        )
    return tuple(parsed)


def _parse_mode_option(raw: object) -> AgentConfigOption | None:
    if not isinstance(raw, dict):
        return None
    modes = raw.get("availableModes", [])
    if not isinstance(modes, list):
        return None
    values = tuple(
        ConfigOptionValue(
            str(item.get("id", "")),
            str(item.get("name") or item.get("id") or ""),
            str(item.get("description") or ""),
        )
        for item in modes
        if isinstance(item, dict) and item.get("id")
    )
    if not values:
        return None
    return AgentConfigOption(
        ACP_MODE_OPTION_ID,
        "Режим агента",
        "mode",
        current_value=str(raw.get("currentModeId") or values[0].value),
        values=values,
    )


def _config_value(item: dict[str, Any]) -> ConfigOptionValue:
    return ConfigOptionValue(
        str(item.get("value", "")),
        str(item.get("name") or item.get("value") or ""),
        str(item.get("description") or ""),
    )


def _iso_timestamp(value: object) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def _tool_detail(tool: dict[str, Any]) -> str:
    parts = [str(tool.get("title") or "Агент хочет выполнить действие.")]
    content = tool.get("content", [])
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
    return "\n\n".join(parts)


__all__ = ["ACP_DRIVER_KIND", "AcpDriver", "acp_driver_registration"]
