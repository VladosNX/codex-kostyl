from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from PySide6.QtCore import QObject

from .agents.base import (
    AgentAction,
    AgentAvailability,
    AgentCapabilities,
    AgentConfigOption,
    AgentDescriptor,
    AgentDriver,
    AgentEvent,
    AgentManifest,
    AgentProfile,
    AgentPrompt,
    AgentRunMode,
    AgentState,
    AuthMethod,
    ConfigOptionValue,
    FeatureId,
    FeatureState,
    FeatureSupport,
    SessionSnapshot,
    SessionSummary,
)
from .agents.codex_mapping import (
    normalize_codex_approval,
    normalize_codex_item,
    normalize_codex_thread,
)
from .models import PLAN_MODE_VALUE, AccessMode, Attachment, ModelInfo, ThreadSummary
from .rpc import JsonLineProcess, JsonRpcClient

MIN_CODEX_VERSION = (0, 146, 0)
CODEX_DESCRIPTOR = AgentDescriptor(
    "codex",
    "Codex",
    "codex",
    "OpenAI Codex через локальный app-server",
)
CODEX_CAPABILITIES = AgentCapabilities(
    models=True,
    reasoning_effort=True,
    access_modes=True,
    plan_mode=True,
    authentication=True,
    rate_limits=True,
    context_usage=True,
    session_history=True,
    attachments=True,
    image_attachments=True,
    approvals=True,
    user_input=True,
    compact=True,
    review=True,
    fork=True,
)
CODEX_RUN_MODES = (
    AgentRunMode(
        AccessMode.READ_ONLY.value,
        AccessMode.READ_ONLY.title,
        "Codex не сможет изменять файлы",
        "safe",
    ),
    AgentRunMode(
        AccessMode.WORKSPACE_WRITE.value,
        AccessMode.WORKSPACE_WRITE.title,
        "Изменения разрешены только внутри проекта",
        "workspace",
    ),
    AgentRunMode(
        AccessMode.FULL_ACCESS.value,
        AccessMode.FULL_ACCESS.title,
        "Команды выполняются без дополнительных подтверждений",
        "danger",
        True,
    ),
    AgentRunMode(
        PLAN_MODE_VALUE,
        "Режим планирования",
        "Анализ без изменения файлов",
        "plan",
    ),
)
CODEX_MANIFEST = AgentManifest(
    features={
        feature.value: FeatureSupport(True)
        for feature in (
            FeatureId.CONFIG_MODEL,
            FeatureId.CONFIG_THOUGHT_LEVEL,
            FeatureId.ACCESS_MODES,
            FeatureId.RUN_PLAN,
            FeatureId.AUTHENTICATION,
            FeatureId.USAGE_QUOTA,
            FeatureId.USAGE_CONTEXT,
            FeatureId.SESSION_HISTORY,
            FeatureId.INPUT_FILES,
            FeatureId.INPUT_IMAGES,
            FeatureId.PERMISSIONS,
            FeatureId.USER_INPUT,
            FeatureId.SESSION_COMPACT,
            FeatureId.SESSION_REVIEW,
            FeatureId.SESSION_FORK,
            FeatureId.RUN_CANCEL,
        )
    },
    actions=(
        AgentAction("compact", "Сжать контекст", requires_session=True),
        AgentAction("review", "Проверить изменения", requires_session=True, argument_hint="Инструкции"),
        AgentAction("fork", "Создать копию чата", requires_session=True),
    ),
    auth_methods=(
        AuthMethod("chatgpt", "Войти через ChatGPT", "browser"),
        AuthMethod("api-key", "Войти с API-ключом", "secret"),
    ),
    implementation_name="codex",
    run_modes=CODEX_RUN_MODES,
    current_run_mode_id=AccessMode.WORKSPACE_WRITE.value,
)


class CodexDriver(AgentDriver):
    """Native driver for the Codex app-server protocol."""

    def __init__(
        self,
        rpc: JsonRpcClient,
        parent: QObject | None = None,
        process: JsonLineProcess | None = None,
    ) -> None:
        super().__init__(CODEX_DESCRIPTOR, CODEX_MANIFEST, parent)
        self.rpc = rpc
        self.process = process
        self.current_thread_ready = False
        self._turn_start_pending = False
        self._resuming: set[str] = set()
        self._after_resume: dict[str, list[Callable[[], None]]] = {}
        self._thread_list_generation = 0
        self._permission_context: dict[object, tuple[str, dict[str, Any]]] = {}
        self.models: list[ModelInfo] = []
        self.account: dict[str, Any] | None = None
        self.connected = False

        rpc.initialized.connect(self._bootstrap)
        rpc.notification.connect(self._notification)
        rpc.serverRequest.connect(self._server_request)
        rpc.protocolError.connect(self.errorOccurred)
        disconnected = getattr(rpc, "disconnected", None)
        if disconnected is not None:
            disconnected.connect(self._disconnected)

    @classmethod
    def create(cls, executable: str | AgentProfile = "codex") -> "CodexDriver":
        profile = executable if isinstance(executable, AgentProfile) else None
        program = profile.executable if profile is not None else executable
        arguments = list(profile.arguments) if profile and profile.arguments else ["app-server", "--stdio"]
        process = JsonLineProcess(program, arguments)
        rpc = JsonRpcClient(process)
        driver = cls(rpc, process=process)
        if profile is not None:
            driver.profile = profile
            driver.descriptor = AgentDescriptor(
                profile.id,
                profile.display_name,
                profile.executable,
                profile.description,
            )
        process.started.connect(rpc.initialize)
        process.stopped.connect(driver.processStopped)
        return driver

    @staticmethod
    def check_availability(executable: str | AgentProfile | None = None) -> AgentAvailability:
        if isinstance(executable, AgentProfile):
            executable = executable.executable
        path = str(Path(executable).expanduser()) if executable else shutil.which("codex")
        if not path:
            return AgentAvailability(
                False,
                error=(
                    "Codex CLI не найден. Установите Codex или укажите путь "
                    "к исполняемому файлу в меню агента."
                ),
            )
        resolved = shutil.which(path) or path
        try:
            completed = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return AgentAvailability(False, executable=resolved, error=f"Не удалось проверить Codex CLI: {exc}")
        output = completed.stdout.strip() or completed.stderr.strip()
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
        if not match:
            return AgentAvailability(
                False,
                executable=resolved,
                error=f"Не удалось определить версию Codex: {output}",
            )
        version = tuple(map(int, match.groups()))
        version_text = ".".join(map(str, version))
        if version < MIN_CODEX_VERSION:
            required = ".".join(map(str, MIN_CODEX_VERSION))
            return AgentAvailability(
                False,
                executable=resolved,
                version=version_text,
                error=f"Нужен Codex CLI {required} или новее; установлен {version_text}.",
            )
        return AgentAvailability(True, executable=resolved, version=version_text)

    def start(self) -> None:
        if self.process is not None:
            self.process.start()

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()

    def _bootstrap(self, _result: dict[str, Any]) -> None:
        self.connected = True
        self.current_turn_id = ""
        self._turn_start_pending = False
        self.turnStateChanged.emit("idle")
        self._emit_generic_state("idle")
        self.refresh_account()
        self.refresh_models()
        if self.current_thread_id:
            self.open_thread(self.current_thread_id)
        else:
            self.list_threads()
        self.ready.emit()

    def feature_override(self, feature: str) -> FeatureState | None:
        if feature == FeatureId.USAGE_QUOTA.value:
            if not self.connected:
                return FeatureState(True, False, "Codex не подключён")
            if not isinstance(self.account, dict) or self.account.get("type") != "chatgpt":
                return FeatureState(
                    True,
                    False,
                    "Недельный лимит доступен только для аккаунта ChatGPT",
                )
        return None

    def _emit_generic_state(self, status: str) -> None:
        self.stateUpdated.emit(
            AgentState(
                connection_status="connected" if self.connected else "disconnected",
                active_profile_id=self.profile.id,
                active_session_id=self.current_session_id,
                active_run_id=self.current_run_id,
                current_run_mode_id=self.current_run_mode_id,
            )
        )

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
        if not isinstance(rows, list):
            rows = []
        self.models = [
            ModelInfo.from_wire(row)
            for row in rows
            if isinstance(row, dict) and not row.get("hidden", False)
        ]
        model_values = tuple(
            ConfigOptionValue(model.id, model.display_name)
            for model in self.models
            if model.id
        )
        config_options: list[AgentConfigOption] = []
        if model_values:
            default_model = next((model.id for model in self.models if model.is_default), model_values[0].value)
            config_options.append(
                AgentConfigOption(
                    "model",
                    "Модель",
                    category="model",
                    current_value=default_model,
                    values=model_values,
                )
            )
        efforts = sorted({effort for model in self.models for effort in model.efforts})
        if efforts:
            config_options.append(
                AgentConfigOption(
                    "thought_level",
                    "Уровень рассуждений",
                    category="thought_level",
                    current_value=efforts[0],
                    values=tuple(ConfigOptionValue(value, value) for value in efforts),
                )
            )
        self.manifest = replace(self.manifest, config_options=tuple(config_options))
        self.manifestUpdated.emit(self.manifest)
        self.configOptionsUpdated.emit(self.manifest.config_options)
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
        self._fetch_threads_page(self._thread_list_generation, None, [], set())

    def _fetch_threads_page(
        self,
        generation: int,
        cursor: str | None,
        collected: list[ThreadSummary],
        seen_cursors: set[str],
    ) -> None:
        if cursor:
            if cursor in seen_cursors:
                self.errorOccurred.emit("Codex вернул повторяющийся курсор списка чатов")
                return
            seen_cursors.add(cursor)
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
            if not isinstance(rows, list):
                rows = []
            collected.extend(
                ThreadSummary.from_wire(row) for row in rows if isinstance(row, dict)
            )
            next_cursor = payload.get("nextCursor")
            if next_cursor:
                self._fetch_threads_page(
                    generation,
                    str(next_cursor),
                    collected,
                    seen_cursors,
                )
            else:
                self.sessionsUpdated.emit(
                    [
                        SessionSummary(
                            item.id,
                            item.title,
                            item.cwd,
                            item.updated_at,
                            item.status,
                        )
                        for item in collected
                    ]
                )
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
        self._resume_thread(lambda: self._read_thread(thread_id))

    def _resume_thread(self, after_resume: Callable[[], None] | None = None) -> None:
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
            if self.current_thread_id != thread_id:
                return
            if error:
                if self._turn_start_pending:
                    self._turn_start_pending = False
                    self.turnStateChanged.emit("failed")
                self._emit_rpc_error(error)
                return
            self.current_thread_ready = True
            for callback in callbacks:
                callback()

        params = {"threadId": thread_id}
        if self.current_project:
            params["cwd"] = self.current_project
        self.rpc.request("thread/resume", params, resumed)

    def _read_thread(self, thread_id: str) -> None:
        self.rpc.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
            lambda result, error: self._thread_read(thread_id, result, error),
        )

    def _thread_read(
        self,
        thread_id: str,
        result: Any,
        error: dict[str, Any] | None,
    ) -> None:
        if self.current_thread_id != thread_id:
            return
        if error:
            self._emit_rpc_error(error)
            return
        thread = result.get("thread", {}) if isinstance(result, dict) else {}
        if not isinstance(thread, dict):
            thread = {}
        result_thread_id = str(thread.get("id", ""))
        if result_thread_id and result_thread_id != thread_id:
            self.errorOccurred.emit("Codex вернул данные другого чата")
            return
        normalized = normalize_codex_thread(thread)
        self.sessionLoaded.emit(SessionSnapshot(thread_id, raw=normalized))
        self.threadLoaded.emit(normalized)

    def new_thread(
        self,
        model: str,
        effort: str | None,
        access_mode: AccessMode,
        after_created: Callable[[], None] | None = None,
        collaboration_mode: str | None = None,
    ) -> None:
        if not self.current_project:
            if after_created:
                self._turn_start_pending = False
                self.turnStateChanged.emit("failed")
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
        project = self.current_project

        def done(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                if after_created:
                    self._turn_start_pending = False
                    self.turnStateChanged.emit("failed")
                self._emit_rpc_error(error)
                return
            thread = result.get("thread", {}) if isinstance(result, dict) else {}
            if not isinstance(thread, dict):
                thread = {}
            thread_id = str(thread.get("id", ""))
            if not thread_id:
                if after_created:
                    self._turn_start_pending = False
                    self.turnStateChanged.emit("failed")
                self.errorOccurred.emit("Codex не вернул идентификатор нового чата")
                return
            if self.current_project != project or self.current_thread_id:
                self.rpc.request("thread/unsubscribe", {"threadId": thread_id})
                if after_created:
                    self._turn_start_pending = False
                    self.turnStateChanged.emit("failed")
                return
            self.current_thread_id = thread_id
            self.current_thread_ready = True
            self.currentThreadChanged.emit(self.current_thread_id)
            normalized = normalize_codex_thread(thread)
            self.sessionLoaded.emit(SessionSnapshot(thread_id, raw=normalized))
            self.threadLoaded.emit(normalized)
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
            self._turn_start_pending = True
            self.turnStateChanged.emit("starting")
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
            self._turn_start_pending = True
            self.turnStateChanged.emit("starting")
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
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        params["collaborationMode"] = {
            "mode": PLAN_MODE_VALUE if collaboration_mode == PLAN_MODE_VALUE else "default",
            "settings": {
                "model": model,
                "reasoning_effort": effort,
                "developer_instructions": None,
            },
        }
        self._turn_start_pending = True
        self.turnStateChanged.emit("starting")

        def started(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._turn_start_pending = False
                self.turnStateChanged.emit("failed")
                self._emit_rpc_error(error)
                return
            turn = result.get("turn", {}) if isinstance(result, dict) else {}
            turn_id = str(turn.get("id", ""))
            if turn_id and self._turn_start_pending:
                self.current_turn_id = turn_id
                self._turn_start_pending = False

        self.rpc.request("turn/start", params, started)

    # Generic driver contract -------------------------------------------------
    def list_sessions(self) -> None:
        self.list_threads()

    def load_session(self, session_id: str) -> None:
        self.open_thread(session_id)

    def prepare_new_session(self) -> None:
        self.prepare_new_thread()

    def submit_prompt(self, prompt: AgentPrompt) -> None:
        mode_id = prompt.run_mode_id or prompt.mode
        collaboration_mode = prompt.mode or None
        if mode_id == PLAN_MODE_VALUE:
            access_mode = AccessMode.READ_ONLY
            collaboration_mode = PLAN_MODE_VALUE
        else:
            try:
                access_mode = AccessMode(mode_id)
            except ValueError:
                access_mode = prompt.access_mode
                if not isinstance(access_mode, AccessMode):
                    access_mode = AccessMode.WORKSPACE_WRITE
        self.send_message(
            prompt.text,
            list(prompt.attachments),
            str(prompt.config.get("model", "")),
            str(prompt.config.get("thought_level") or "") or None,
            access_mode,
            collaboration_mode,
        )

    def set_run_mode(self, mode_id: str) -> None:
        if not any(mode.id == mode_id for mode in self.manifest.run_modes):
            self.errorOccurred.emit(f"Codex не поддерживает режим {mode_id}")
            return
        self.current_run_mode_id = mode_id
        self.manifest = replace(self.manifest, current_run_mode_id=mode_id)
        self.manifestUpdated.emit(self.manifest)

    def cancel_run(self) -> None:
        self.interrupt()

    def invoke_action(
        self,
        action_id: str,
        arguments: str = "",
        callback: Any | None = None,
    ) -> None:
        if action_id == "compact":
            self.compact_thread()
        elif action_id == "review":
            self.start_review(arguments)
        elif action_id == "fork":
            self.fork_thread(callback)
        else:
            self.errorOccurred.emit(f"Неизвестное действие Codex: {action_id}")
            if callback:
                callback(False)

    def respond_to_request(self, request_id: object, decision: str) -> None:
        self.resolve_permission(request_id, decision)

    def authenticate(self, method_id: str, secret: str = "") -> None:
        if method_id == "chatgpt":
            self.login_chatgpt()
        elif method_id == "api-key":
            self.login_api_key(secret)
        else:
            self.errorOccurred.emit(f"Неизвестный способ входа Codex: {method_id}")

    def compact_thread(self) -> None:
        """Compact the current thread and wait for its turn notifications."""
        if not self.current_thread_id:
            self.errorOccurred.emit("Сначала создайте чат")
            return
        self._start_action_turn(
            "thread/compact/start",
            {"threadId": self.current_thread_id},
        )

    def start_review(self, instructions: str = "") -> None:
        """Start an inline review for uncommitted changes or custom instructions."""
        if not self.current_thread_id:
            self.errorOccurred.emit("Сначала создайте чат")
            return
        target: dict[str, Any]
        if instructions.strip():
            target = {"type": "custom", "instructions": instructions.strip()}
        else:
            target = {"type": "uncommittedChanges"}
        self._start_action_turn(
            "review/start",
            {
                "threadId": self.current_thread_id,
                "delivery": "inline",
                "target": target,
            },
        )

    def _start_action_turn(self, method: str, params: dict[str, Any]) -> None:
        self._turn_start_pending = True
        self.turnStateChanged.emit("starting")

        def started(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._turn_start_pending = False
                self.turnStateChanged.emit("failed")
                self._emit_rpc_error(error)
                return
            turn = result.get("turn", {}) if isinstance(result, dict) else {}
            turn_id = str(turn.get("id", ""))
            if turn_id and self._turn_start_pending:
                self.current_turn_id = turn_id
                self._turn_start_pending = False

        self.rpc.request(method, params, started)

    def fork_thread(self, after_switched: Any | None = None) -> None:
        """Create a persistent fork, switch to it, and load its copied history."""
        source_thread_id = self.current_thread_id
        if not source_thread_id:
            self.errorOccurred.emit("Сначала создайте чат")
            if after_switched:
                after_switched(False)
            return

        def forked(result: Any, error: dict[str, Any] | None) -> None:
            if error:
                self._emit_rpc_error(error)
                if after_switched:
                    after_switched(False)
                return
            thread = result.get("thread", {}) if isinstance(result, dict) else {}
            thread_id = str(thread.get("id", ""))
            if not thread_id:
                self.errorOccurred.emit("Codex не вернул идентификатор копии чата")
                if after_switched:
                    after_switched(False)
                return
            self.current_thread_id = thread_id
            self.current_turn_id = ""
            self.current_thread_ready = True
            self.currentThreadChanged.emit(thread_id)

            def loaded(read_result: Any, read_error: dict[str, Any] | None) -> None:
                if read_error:
                    self._emit_rpc_error(read_error)
                    if after_switched:
                        after_switched(False)
                    return
                loaded_thread = (
                    read_result.get("thread", {})
                    if isinstance(read_result, dict)
                    else {}
                )
                normalized = normalize_codex_thread(loaded_thread)
                self.sessionLoaded.emit(
                    SessionSnapshot(self.current_session_id, raw=normalized)
                )
                self.threadLoaded.emit(normalized)
                self.list_threads()
                if after_switched:
                    after_switched(True)

            self.rpc.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
                loaded,
            )

        self.rpc.request(
            "thread/fork",
            {"threadId": source_thread_id, "ephemeral": False},
            forked,
        )

    def interrupt(self) -> None:
        if not self.current_thread_id or not self.current_turn_id:
            return
        self.rpc.request(
            "turn/interrupt",
            {"threadId": self.current_thread_id, "turnId": self.current_turn_id},
            lambda _result, error: self._emit_rpc_error(error) if error else None,
        )

    def answer_approval(
        self,
        request_id: object,
        decision: str,
        method: str = "",
        params: dict[str, Any] | None = None,
    ) -> None:
        if method == "item/permissions/requestApproval":
            if decision == "cancel":
                self.cancel_server_request(request_id)
                return
            requested = (params or {}).get("permissions", {})
            permissions: dict[str, Any] = {}
            if decision in {"accept", "acceptForSession"} and isinstance(
                requested, dict
            ):
                for key in ("network", "fileSystem"):
                    value = requested.get(key)
                    if isinstance(value, dict):
                        permissions[key] = value
            self.rpc.respond(
                request_id,
                {
                    "permissions": permissions,
                    "scope": "session" if decision == "acceptForSession" else "turn",
                },
            )
            return
        self.rpc.respond(request_id, {"decision": decision})

    def resolve_permission(self, request_id: object, decision: str) -> None:
        method, params = self._permission_context.pop(request_id, ("", {}))
        self.answer_approval(request_id, decision, method, params)

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
            if not self._is_current_thread_event(params):
                return
            usage = params.get("tokenUsage", {})
            if isinstance(usage, dict):
                self.tokenUsageUpdated.emit(usage)
                self.eventReceived.emit(
                    AgentEvent(
                        "usage",
                        self.current_session_id,
                        self.current_run_id,
                        payload=usage,
                    )
                )
            return
        if method == "turn/plan/updated":
            if self._is_current_turn_event(params):
                self.turnPlanUpdated.emit(params)
            return
        if method == "turn/started":
            if not self._is_current_thread_event(params):
                return
            turn = params.get("turn", {})
            if not isinstance(turn, dict):
                return
            self.current_turn_id = str(turn.get("id", ""))
            self._turn_start_pending = False
            self.turnStateChanged.emit("inProgress")
            self._emit_generic_state("inProgress")
            return
        if method == "turn/completed":
            if not self._is_current_turn_event(params):
                return
            turn = params.get("turn", {})
            if not isinstance(turn, dict):
                return
            status = str(turn.get("status", "completed"))
            self.current_turn_id = ""
            self._turn_start_pending = False
            self.turnStateChanged.emit(status)
            self._emit_generic_state(status)
            self.list_threads()
            self.refresh_rate_limits()
            if status == "failed":
                error = turn.get("error") or {}
                message = (
                    error.get("message", "Ход завершился ошибкой")
                    if isinstance(error, dict)
                    else error
                )
                self.errorOccurred.emit(str(message))
            return
        if method in {"item/started", "item/completed"}:
            if not self._is_current_turn_event(params):
                return
            item = params.get("item", {})
            if isinstance(item, dict):
                normalized = normalize_codex_item(item)
                self.itemUpdated.emit(
                    normalized,
                    method.endswith("completed"),
                )
                self.eventReceived.emit(
                    AgentEvent(
                        str(normalized.get("kind", "system_activity")),
                        self.current_session_id,
                        self.current_run_id,
                        str(normalized.get("id", "")),
                        "completed" if method.endswith("completed") else "started",
                        normalized,
                    )
                )
            return
        if method == "item/agentMessage/delta":
            if not self._is_current_turn_event(params):
                return
            self.agentDelta.emit(str(params.get("itemId", "")), str(params.get("delta", "")))
            self.eventReceived.emit(
                AgentEvent(
                    "assistant_message",
                    self.current_session_id,
                    self.current_run_id,
                    str(params.get("itemId", "")),
                    payload={"delta": str(params.get("delta", ""))},
                )
            )
            return
        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            if not self._is_current_turn_event(params):
                return
            self.reasoningDelta.emit(str(params.get("itemId", "")), str(params.get("delta", "")))
            self.eventReceived.emit(
                AgentEvent(
                    "reasoning",
                    self.current_session_id,
                    self.current_run_id,
                    str(params.get("itemId", "")),
                    payload={"delta": str(params.get("delta", ""))},
                )
            )
            return
        if method == "item/commandExecution/outputDelta":
            if not self._is_current_turn_event(params):
                return
            self.commandDelta.emit(str(params.get("itemId", "")), str(params.get("delta", "")))
            self.eventReceived.emit(
                AgentEvent(
                    "command",
                    self.current_session_id,
                    self.current_run_id,
                    str(params.get("itemId", "")),
                    payload={"delta": str(params.get("delta", ""))},
                )
            )
            return
        if method == "item/plan/delta":
            if not self._is_current_turn_event(params):
                return
            self.planDelta.emit(str(params.get("itemId", "")), str(params.get("delta", "")))
            self.eventReceived.emit(
                AgentEvent(
                    "plan",
                    self.current_session_id,
                    self.current_run_id,
                    str(params.get("itemId", "")),
                    payload={"delta": str(params.get("delta", ""))},
                )
            )
            return
        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            self._permission_context.pop(request_id, None)
            self.serverRequestResolved.emit(request_id)
            return
        if method == "error":
            error = params.get("error", params)
            message = error.get("message", "Ошибка Codex") if isinstance(error, dict) else error
            self.errorOccurred.emit(str(message))

    def _server_request(self, request_id: object, method: str, params: dict[str, Any]) -> None:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            self._permission_context[request_id] = (method, params)
            request = normalize_codex_approval(
                request_id,
                method,
                params,
                self.current_project or "не выбрана",
            )
            self.clientRequestReceived.emit(request)
            self.permissionRequested.emit(request)
            self.approvalRequested.emit(request_id, method, params)
            return
        if method == "item/tool/requestUserInput":
            self.userInputRequested.emit(request_id, params)
            return
        self.rpc.respond_error(request_id, -32601, f"Unsupported client method: {method}")

    def _emit_rpc_error(self, error: dict[str, Any] | None) -> None:
        if error:
            self.errorOccurred.emit(str(error.get("message", "Ошибка Codex")))

    def _is_current_thread_event(self, params: dict[str, Any]) -> bool:
        thread_id = str(params.get("threadId", ""))
        return bool(self.current_thread_id) and (
            not thread_id or thread_id == self.current_thread_id
        )

    def _is_current_turn_event(self, params: dict[str, Any]) -> bool:
        if not self._is_current_thread_event(params):
            return False
        turn_id = str(params.get("turnId", ""))
        if not turn_id:
            turn = params.get("turn")
            if isinstance(turn, dict):
                turn_id = str(turn.get("id", ""))
        return not self.current_turn_id or not turn_id or turn_id == self.current_turn_id

    def _disconnected(self) -> None:
        was_running = bool(self.current_turn_id) or self._turn_start_pending
        self.connected = False
        self.current_thread_ready = False
        self.current_turn_id = ""
        self._turn_start_pending = False
        self._resuming.clear()
        self._after_resume.clear()
        self._permission_context.clear()
        if was_running:
            self.turnStateChanged.emit("failed")
        self._emit_generic_state("disconnected")
        self.disconnected.emit()


# Backward-compatible import for tests and third-party source-tree users.
CodexService = CodexDriver
