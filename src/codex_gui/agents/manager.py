from __future__ import annotations

from dataclasses import replace
from typing import Any

from PySide6.QtCore import QObject, Signal

from .base import (
    AgentAvailability,
    AgentCapabilities,
    AgentDescriptor,
    AgentManifest,
    AgentProfile,
    AgentPrompt,
    AgentState,
    FeatureId,
    FeatureState,
    feature_key,
)
from .registry import AgentRegistry


class AgentController(QObject):
    """Application controller that owns one driver without pretending to be one."""

    ready = Signal()
    manifestUpdated = Signal(object)
    stateUpdated = Signal(object)
    featureStatesChanged = Signal(object)
    profilesChanged = Signal(object)
    agentsChanged = Signal(object)  # compatibility alias for descriptors
    activeAgentChanged = Signal(str)
    availabilityChanged = Signal(object)

    sessionsUpdated = Signal(object)
    sessionLoaded = Signal(object)
    eventReceived = Signal(object)
    clientRequestReceived = Signal(object)
    configOptionsUpdated = Signal(object)
    actionCompleted = Signal(str, object)

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
    permissionRequested = Signal(object)
    userInputRequested = Signal(object, dict)
    serverRequestResolved = Signal(object)
    errorOccurred = Signal(str)
    loginStarted = Signal(dict)
    disconnected = Signal()
    processStopped = Signal(int, str)

    _FORWARDED_SIGNALS = (
        "ready",
        "sessionsUpdated",
        "sessionLoaded",
        "eventReceived",
        "clientRequestReceived",
        "configOptionsUpdated",
        "actionCompleted",
        "accountUpdated",
        "rateLimitsUpdated",
        "modelsUpdated",
        "threadsUpdated",
        "threadLoaded",
        "currentThreadChanged",
        "itemUpdated",
        "agentDelta",
        "reasoningDelta",
        "commandDelta",
        "planDelta",
        "turnPlanUpdated",
        "tokenUsageUpdated",
        "turnStateChanged",
        "approvalRequested",
        "permissionRequested",
        "userInputRequested",
        "serverRequestResolved",
        "errorOccurred",
        "loginStarted",
        "disconnected",
        "processStopped",
    )

    def __init__(self, registry: AgentRegistry, settings: Any, parent=None) -> None:
        super().__init__(parent)
        self.registry = registry
        self.settings = settings
        self.active_driver: Any | None = None
        self.active_profile_id = ""
        self.active_agent_id = ""  # old public name
        self.profile: AgentProfile | None = None
        self.descriptor = AgentDescriptor("", "", "")
        self.manifest = AgentManifest()
        self.capabilities = AgentCapabilities()
        self.availability = AgentAvailability(False, error="Агент не выбран")
        self.current_project = ""
        self.current_session_id = ""
        self.current_run_id = ""
        self.connected = False
        self.account: dict[str, Any] | None = None
        self.state = AgentState()
        self._generation = 0
        self._connections: list[tuple[Any, Any]] = []

    @property
    def current_thread_id(self) -> str:
        return self.current_session_id

    @current_thread_id.setter
    def current_thread_id(self, value: str) -> None:
        self.current_session_id = value

    @property
    def current_turn_id(self) -> str:
        return self.current_run_id

    @current_turn_id.setter
    def current_turn_id(self, value: str) -> None:
        self.current_run_id = value

    @property
    def available_profiles(self) -> list[AgentProfile]:
        return self.registry.profiles()

    @property
    def available_agents(self) -> list[AgentDescriptor]:
        return self.registry.descriptors()

    def activate(self, profile_id: str) -> bool:
        if self.active_profile_id == profile_id and self.active_driver is not None:
            if not self.active_driver.connected:
                self.active_driver.start()
            return True
        profile = self.registry.profile(profile_id)
        if profile is None:
            self.errorOccurred.emit(f"Неизвестный агент: {profile_id}")
            return False
        executable = profile.executable
        getter = getattr(self.settings, "agent_get", None)
        if callable(getter):
            executable = str(getter(profile_id, "executable", executable)) or executable
        availability = self.registry.probe(profile_id, executable)
        if not availability.available:
            if self.active_driver is None:
                self.availability = availability
                self.availabilityChanged.emit(availability)
                self.connected = False
                self._publish_state()
            self.errorOccurred.emit(availability.error)
            return False

        previous_project = self.current_project
        self._release_active_driver()
        driver = self.registry.create(profile_id, availability.executable)
        resolved_profile = replace(profile, executable=availability.executable)
        driver.profile = resolved_profile
        driver.descriptor = AgentDescriptor(
            resolved_profile.id,
            resolved_profile.display_name,
            resolved_profile.executable,
            resolved_profile.description,
        )
        self.active_driver = driver
        self.active_profile_id = profile_id
        self.active_agent_id = profile_id
        self.profile = resolved_profile
        self.descriptor = driver.descriptor
        self.manifest = driver.manifest
        self.capabilities = AgentCapabilities.from_manifest(self.manifest)
        self.availability = availability
        self.current_project = previous_project
        if previous_project:
            driver.current_project = previous_project
        self._connect_driver(driver)
        self.availabilityChanged.emit(availability)
        self.activeAgentChanged.emit(profile_id)
        self.profilesChanged.emit(self.available_profiles)
        self.agentsChanged.emit(self.available_agents)
        self.manifestUpdated.emit(self.manifest)
        self._publish_state()
        driver.start()
        return True

    def _connect_driver(self, driver: Any) -> None:
        self._generation += 1
        generation = self._generation

        for name in self._FORWARDED_SIGNALS:
            source = getattr(driver, name)
            target = getattr(self, name)

            def forward(*args: Any, _target=target, _generation=generation) -> None:
                if _generation == self._generation:
                    self._sync_state()
                    _target.emit(*args)

            source.connect(forward)
            self._connections.append((source, forward))

        def manifest_changed(manifest: AgentManifest) -> None:
            if generation != self._generation:
                return
            self.manifest = manifest
            self.capabilities = AgentCapabilities.from_manifest(manifest)
            self.manifestUpdated.emit(manifest)
            self._publish_state()

        driver.manifestUpdated.connect(manifest_changed)
        self._connections.append((driver.manifestUpdated, manifest_changed))

        def context_changed(payload: object) -> None:
            if generation != self._generation:
                return
            self.state = replace(
                self.state,
                context_usage=payload if isinstance(payload, dict) else None,
            )
            self._publish_state()

        def quota_changed(payload: object) -> None:
            if generation != self._generation:
                return
            self.state = replace(
                self.state,
                quota_usage=payload if isinstance(payload, dict) else None,
            )
            self._publish_state()

        driver.tokenUsageUpdated.connect(context_changed)
        driver.rateLimitsUpdated.connect(quota_changed)
        self._connections.extend(
            (
                (driver.tokenUsageUpdated, context_changed),
                (driver.rateLimitsUpdated, quota_changed),
            )
        )

    def _disconnect_driver(self) -> None:
        self._generation += 1
        for source, target in self._connections:
            try:
                source.disconnect(target)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()

    def _release_active_driver(self) -> None:
        if self.active_driver is None:
            return
        driver = self.active_driver
        self._disconnect_driver()
        driver.stop()
        driver.deleteLater()
        self.active_driver = None
        self.connected = False
        self.current_session_id = ""
        self.current_run_id = ""
        self.account = None
        self._publish_state()

    def _sync_state(self) -> None:
        driver = self.active_driver
        if driver is None:
            return
        self.connected = bool(driver.connected)
        self.current_project = str(driver.current_project)
        self.current_session_id = str(driver.current_session_id)
        self.current_run_id = str(driver.current_run_id)
        self.account = driver.account
        self._publish_state()

    def _publish_state(self) -> None:
        feature_states = self._compute_feature_states()
        status = "connected" if self.connected else "disconnected"
        self.state = AgentState(
            connection_status=status,
            active_profile_id=self.active_profile_id,
            active_session_id=self.current_session_id,
            active_run_id=self.current_run_id,
            feature_states=feature_states,
            account_summary=self.account,
            context_usage=self.state.context_usage,
            quota_usage=self.state.quota_usage,
        )
        self.stateUpdated.emit(self.state)
        self.featureStatesChanged.emit(feature_states)

    def _compute_feature_states(self) -> dict[str, FeatureState]:
        keys = {item.value for item in FeatureId} | set(self.manifest.features)
        states: dict[str, FeatureState] = {}
        name = self.descriptor.display_name or "Агент"
        for key in keys:
            support = self.manifest.support(key)
            if not support.supported:
                states[key] = FeatureState(
                    False,
                    False,
                    support.reason or f"Функция не поддерживается агентом {name}",
                )
                continue
            enabled = self.connected
            reason = "" if enabled else f"{name} не подключён"
            if key in {
                FeatureId.SESSION_COMPACT.value,
                FeatureId.SESSION_FORK.value,
                FeatureId.SESSION_REVIEW.value,
            } and not self.current_session_id:
                enabled = False
                reason = "Сначала откройте или создайте чат"
            if key == FeatureId.RUN_CANCEL.value and not self.current_run_id:
                enabled = False
                reason = "Нет активного выполнения"
            if key in {
                FeatureId.SESSION_COMPACT.value,
                FeatureId.SESSION_FORK.value,
                FeatureId.SESSION_REVIEW.value,
            } and self.current_run_id:
                enabled = False
                reason = "Недоступно во время активного выполнения"
            override = (
                self.active_driver.feature_override(key)
                if self.active_driver is not None
                else None
            )
            states[key] = override or FeatureState(True, enabled, reason)
        return states

    def feature_state(self, feature: FeatureId | str) -> FeatureState:
        key = feature_key(feature)
        return self.state.feature_states.get(
            key,
            FeatureState(False, False, "Функция не поддерживается агентом"),
        )

    def action_state(self, action_id: str) -> FeatureState:
        feature = {
            "compact": FeatureId.SESSION_COMPACT,
            "review": FeatureId.SESSION_REVIEW,
            "fork": FeatureId.SESSION_FORK,
        }.get(action_id)
        if feature is not None:
            return self.feature_state(feature)
        action = next((item for item in self.manifest.actions if item.id == action_id), None)
        if action is None:
            return FeatureState(False, False, "Действие не поддерживается агентом")
        if not self.connected:
            return FeatureState(True, False, "Агент не подключён")
        if action.requires_session and not self.current_session_id:
            return FeatureState(True, False, "Сначала откройте или создайте чат")
        if self.current_run_id and not action.allow_during_run:
            return FeatureState(True, False, "Недоступно во время активного выполнения")
        return FeatureState(True, True)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        driver = self.active_driver
        if driver is None:
            self.errorOccurred.emit("Сначала выберите доступного агента")
            return None
        callback = getattr(driver, method, None)
        if not callable(callback):
            self.errorOccurred.emit(f"Агент не поддерживает операцию {method}")
            return None
        result = callback(*args, **kwargs)
        self._sync_state()
        return result

    def start(self) -> None:
        if self.active_driver is not None:
            self.active_driver.start()

    def stop(self) -> None:
        if self.active_driver is not None:
            self.active_driver.stop()

    def restart(self) -> None:
        if self.active_driver is not None:
            self.active_driver.restart()

    def set_executable(self, profile_id: str, executable: str) -> bool:
        profile = self.registry.profile(profile_id)
        if profile is None:
            return False
        availability = self.registry.probe(profile_id, executable)
        if not availability.available:
            self.errorOccurred.emit(availability.error)
            return False
        updated = replace(profile, executable=availability.executable)
        self.registry.replace_profile(updated)
        setter = getattr(self.settings, "agent_set", None)
        if callable(setter):
            setter(profile_id, "executable", availability.executable)
        if self.active_profile_id == profile_id:
            self._release_active_driver()
            self.active_profile_id = ""
            self.active_agent_id = ""
        return self.activate(profile_id)

    def add_profile(self, profile: AgentProfile) -> None:
        self.registry.add_profile(profile)
        saver = getattr(self.settings, "save_agent_profile", None)
        if callable(saver):
            saver(profile)
        self.profilesChanged.emit(self.available_profiles)
        self.agentsChanged.emit(self.available_agents)

    def remove_profile(self, profile_id: str) -> None:
        profile = self.registry.profile(profile_id)
        if profile is None:
            return
        if profile.built_in:
            raise ValueError("Встроенный профиль нельзя удалить")
        self.registry.remove_profile(profile_id)
        if profile_id == self.active_profile_id:
            self._release_active_driver()
            self.active_profile_id = ""
            self.active_agent_id = ""
        remover = getattr(self.settings, "remove_agent_profile", None)
        if callable(remover):
            remover(profile_id)
        self.profilesChanged.emit(self.available_profiles)
        self.agentsChanged.emit(self.available_agents)

    def set_project(self, path: str) -> None:
        self.current_project = path
        if self.active_driver is not None:
            self._call("set_project", path)

    def list_sessions(self) -> None:
        method = "list_sessions" if callable(getattr(self.active_driver, "list_sessions", None)) else "list_threads"
        self._call(method)

    def open_session(self, session_id: str) -> None:
        method = "load_session" if callable(getattr(self.active_driver, "load_session", None)) else "open_thread"
        self._call(method, session_id)

    def prepare_new_session(self) -> None:
        method = "prepare_new_session" if callable(getattr(self.active_driver, "prepare_new_session", None)) else "prepare_new_thread"
        self._call(method)

    def submit_prompt(self, prompt: AgentPrompt) -> None:
        if callable(getattr(self.active_driver, "submit_prompt", None)):
            self._call("submit_prompt", prompt)
            return
        self._call(
            "send_message",
            prompt.text,
            list(prompt.attachments),
            str(prompt.config.get("model", "")),
            prompt.config.get("thought_level") or None,
            prompt.access_mode,
            prompt.mode or None,
        )

    def set_config_option(self, option_id: str, value: str | bool) -> None:
        if callable(getattr(self.active_driver, "set_config_option", None)):
            self._call("set_config_option", option_id, value)
            return
        setter = getattr(self.settings, "agent_set", None)
        if callable(setter):
            setter(self.active_profile_id, option_id, value)

    def cancel_run(self) -> None:
        method = "cancel_run" if callable(getattr(self.active_driver, "cancel_run", None)) else "interrupt"
        self._call(method)

    def invoke_action(
        self,
        action_id: str,
        arguments: str = "",
        callback: Any | None = None,
    ) -> None:
        state = self.action_state(action_id)
        if not state.enabled:
            self.errorOccurred.emit(state.reason)
            if callback:
                callback(False)
            return
        if callable(getattr(self.active_driver, "invoke_action", None)):
            self._call("invoke_action", action_id, arguments, callback)
            return
        legacy = {
            "compact": ("compact_thread", ()),
            "review": ("start_review", (arguments,)),
            "fork": ("fork_thread", (callback,)),
        }.get(action_id)
        if legacy is None:
            self.errorOccurred.emit(f"Неизвестное действие: {action_id}")
            return
        self._call(legacy[0], *legacy[1])

    def respond_to_request(self, request_id: object, decision: str) -> None:
        method = "respond_to_request" if callable(getattr(self.active_driver, "respond_to_request", None)) else "resolve_permission"
        self._call(method, request_id, decision)

    def authenticate(self, method_id: str, secret: str = "") -> None:
        if callable(getattr(self.active_driver, "authenticate", None)):
            self._call("authenticate", method_id, secret)
            return
        if method_id == "chatgpt":
            self._call("login_chatgpt")
        elif method_id == "api-key":
            self._call("login_api_key", secret)

    # Compatibility surface while MainWindow and external imports migrate.
    def list_threads(self) -> None:
        self.list_sessions()

    def open_thread(self, thread_id: str) -> None:
        self.open_session(thread_id)

    def prepare_new_thread(self) -> None:
        self.prepare_new_session()

    def send_message(self, text: str, attachments: list[Any], model: str, effort: str | None, access_mode: Any, collaboration_mode: str | None = None) -> None:
        self.submit_prompt(
            AgentPrompt(
                text=text,
                attachments=tuple(attachments),
                working_directory=self.current_project,
                config={"model": model, "thought_level": effort or ""},
                mode=collaboration_mode or "",
                access_mode=access_mode,
            )
        )

    def interrupt(self) -> None:
        self.cancel_run()

    def compact_thread(self) -> None:
        self.invoke_action("compact")

    def start_review(self, instructions: str = "") -> None:
        self.invoke_action("review", instructions)

    def fork_thread(self, callback: Any | None = None) -> None:
        self.invoke_action("fork", callback=callback)

    def answer_approval(self, *args: Any, **kwargs: Any) -> None:
        self._call("answer_approval", *args, **kwargs)

    def resolve_permission(self, request_id: object, decision: str) -> None:
        self.respond_to_request(request_id, decision)

    def answer_user_input(self, *args: Any, **kwargs: Any) -> None:
        self._call("answer_user_input", *args, **kwargs)

    def cancel_server_request(self, request_id: object) -> None:
        self._call("cancel_server_request", request_id)

    def refresh_account(self) -> None:
        self._call("refresh_account")

    def login_chatgpt(self) -> None:
        self.authenticate("chatgpt")

    def login_api_key(self, api_key: str) -> None:
        self.authenticate("api-key", api_key)

    def logout(self) -> None:
        self._call("logout")


# Source-compatible name; unlike the old implementation it no longer inherits
# AgentDriver and therefore keeps orchestration separate from protocol adapters.
AgentManager = AgentController
