from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal

from codex_gui.agents import (
    AgentAvailability,
    AgentCapabilities,
    AgentController,
    AgentDescriptor,
    AgentDriver,
    AgentManifest,
    AgentProfile,
    AgentPrompt,
    FeatureId,
    FeatureSupport,
    AgentManager,
    AgentRegistration,
    AgentRegistry,
    DriverRegistration,
)
from codex_gui.agents.acp import ACP_MODE_OPTION_ID, AcpDriver
from codex_gui.agents.codex_mapping import (
    normalize_codex_approval,
    normalize_codex_item,
    normalize_codex_thread,
)
from codex_gui.settings import AppSettings
from codex_gui.rpc import JsonRpcClient


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def agent_get(self, agent_id: str, key: str, default: str = "") -> str:
        return self.values.get(f"{agent_id}/{key}", default)

    def agent_set(self, agent_id: str, key: str, value: object) -> None:
        self.values[f"{agent_id}/{key}"] = str(value)


class FakeDriver(AgentDriver):
    def __init__(self, agent_id: str, executable: str) -> None:
        super().__init__(
            AgentDescriptor(agent_id, agent_id.title(), executable),
            AgentCapabilities(session_history=True),
        )
        self.executable = executable
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1
        self.connected = True
        self.ready.emit()

    def stop(self) -> None:
        self.stops += 1
        self.connected = False

    def set_project(self, path: str) -> None:
        self.current_project = path


def registration(agent_id: str, created: list[FakeDriver], available: bool = True) -> AgentRegistration:
    descriptor = AgentDescriptor(agent_id, agent_id.title(), agent_id)

    def factory(executable: str) -> FakeDriver:
        driver = FakeDriver(agent_id, executable)
        created.append(driver)
        return driver

    def probe(executable: str | None) -> AgentAvailability:
        if not available:
            return AgentAvailability(False, error=f"{agent_id} missing")
        return AgentAvailability(True, executable=executable or f"/{agent_id}", version="1.0.0")

    return AgentRegistration(descriptor, factory, probe)


def test_manager_owns_one_driver_and_forwards_signals(qtbot) -> None:
    registry = AgentRegistry()
    created: list[FakeDriver] = []
    registry.register(registration("one", created))
    registry.register(registration("two", created))
    manager = AgentManager(registry, MemorySettings())
    ready: list[bool] = []
    manager.ready.connect(lambda: ready.append(True))

    assert manager.activate("one") is True
    first = created[-1]
    manager.set_project("/repo")
    assert manager.connected is True
    assert ready == [True]

    assert manager.activate("two") is True
    second = created[-1]
    assert first.stops == 1
    assert second.starts == 1
    assert second.current_project == "/repo"
    assert manager.active_agent_id == "two"


def test_unavailable_agent_does_not_replace_running_driver(qtbot) -> None:
    registry = AgentRegistry()
    created: list[FakeDriver] = []
    registry.register(registration("working", created))
    registry.register(registration("missing", created, available=False))
    manager = AgentManager(registry, MemorySettings())

    assert manager.activate("working") is True
    running = manager.active_driver
    assert manager.connected is True
    assert manager.activate("missing") is False
    assert manager.active_driver is running
    assert manager.active_agent_id == "working"
    assert manager.connected is True


def test_prepare_new_session_before_agent_activation_is_silent(qtbot) -> None:
    registry = AgentRegistry()
    manager = AgentManager(registry, MemorySettings())
    errors: list[str] = []
    manager.errorOccurred.connect(errors.append)

    manager.set_project("/repo")
    manager.prepare_new_session()

    assert errors == []
    assert manager.current_project == "/repo"


def test_invalid_executable_does_not_stop_or_replace_running_driver(qtbot) -> None:
    registry = AgentRegistry()
    created: list[FakeDriver] = []
    registry.register(registration("working", created))
    manager = AgentManager(registry, MemorySettings())

    assert manager.activate("working") is True
    running = manager.active_driver
    registry._drivers["working"] = DriverRegistration(
        "working",
        lambda profile: FakeDriver("working", profile.executable),
        lambda _profile: AgentAvailability(False, error="working missing"),
    )

    assert manager.set_executable("working", "/missing") is False
    assert manager.active_driver is running
    assert running.stops == 0
    assert manager.connected is True


def test_codex_wire_payloads_are_normalized_before_the_ui() -> None:
    item = normalize_codex_item(
        {"id": "answer", "type": "agentMessage", "text": "done"}
    )
    assert item == {
        "id": "answer",
        "kind": "assistant_message",
        "subtype": "agentMessage",
        "text": "done",
    }
    thread = normalize_codex_thread(
        {"id": "thread", "turns": [{"items": [{"id": "cmd", "type": "commandExecution"}]}]}
    )
    assert thread["turns"][0]["items"][0]["kind"] == "command"

    approval = normalize_codex_approval(
        7,
        "item/permissions/requestApproval",
        {"reason": "API", "permissions": {"network": {"enabled": True}}},
        "/repo",
    )
    assert approval.kind == "permissions"
    assert "Доступ к сети: разрешён" in approval.detail
    assert [option.id for option in approval.options] == [
        "decline",
        "acceptForSession",
        "accept",
        "cancel",
    ]


def test_legacy_settings_migrate_to_codex_namespace(monkeypatch, tmp_path) -> None:
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    store.setValue("model", "legacy-model")
    store.setValue("effort", "high")
    monkeypatch.setattr("codex_gui.settings.QSettings", lambda *_args: store)

    settings = AppSettings()

    assert settings.agent_get("codex", "model") == "legacy-model"
    assert settings.agent_get("codex", "effort") == "high"
    settings.agent_set("other", "model", "other-model")
    assert settings.agent_get("codex", "model") == "legacy-model"
    assert settings.agent_get("other", "model") == "other-model"


def test_controller_uses_composition_and_exposes_feature_reasons(qtbot) -> None:
    class FeatureDriver(FakeDriver):
        def __init__(self, executable: str) -> None:
            super().__init__("feature", executable)
            features = {
                FeatureId.SESSION_COMPACT.value: FeatureSupport(True),
                FeatureId.SESSION_HISTORY.value: FeatureSupport(False),
            }
            self.manifest = AgentManifest(features=features)
            self.capabilities = AgentCapabilities.from_manifest(self.manifest)

    registry = AgentRegistry()
    registry.register_driver(
        DriverRegistration(
            "feature-driver",
            lambda profile: FeatureDriver(profile.executable),
            lambda profile: AgentAvailability(True, executable=profile.executable),
        )
    )
    registry.add_profile(
        AgentProfile("feature", "feature-driver", "Feature", "/feature")
    )
    controller = AgentController(registry, MemorySettings())

    assert not isinstance(controller, AgentDriver)
    assert controller.activate("feature") is True
    compact = controller.feature_state(FeatureId.SESSION_COMPACT)
    history = controller.feature_state(FeatureId.SESSION_HISTORY)
    assert compact.supported is True
    assert compact.enabled is False
    assert "откройте" in compact.reason
    assert history.supported is False
    assert history.enabled is False

    controller.active_driver.current_session_id = "session-1"
    controller.active_driver.currentThreadChanged.emit("session-1")
    assert controller.feature_state(FeatureId.SESSION_COMPACT).enabled is True


class FakeAcpTransport(QObject):
    messageReceived = Signal(dict)
    protocolError = Signal(str)
    stopped = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    def send(self, payload: dict) -> None:
        self.sent.append(payload)


def make_acp_driver() -> tuple[AcpDriver, FakeAcpTransport]:
    transport = FakeAcpTransport()
    rpc = JsonRpcClient(transport, jsonrpc_version="2.0")  # type: ignore[arg-type]
    profile = AgentProfile("acp-test", "acp", "ACP Test", "/agent")
    return AcpDriver(profile, rpc), transport


def initialize_acp(driver: AcpDriver, transport: FakeAcpTransport) -> None:
    driver._initialize()
    request = transport.sent[-1]
    transport.messageReceived.emit(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"image": True},
                    "sessionCapabilities": {"list": {}, "resume": {}},
                },
                "agentInfo": {"name": "fake", "title": "Fake ACP", "version": "1.2"},
                "authMethods": [{"id": "login", "name": "Agent login"}],
            },
        }
    )


def test_acp_initialize_and_session_list_transcript(qtbot) -> None:
    driver, transport = make_acp_driver()
    manifests = []
    sessions = []
    driver.manifestUpdated.connect(manifests.append)
    driver.sessionsUpdated.connect(sessions.append)

    initialize_acp(driver, transport)

    initialize = transport.sent[0]
    assert initialize["jsonrpc"] == "2.0"
    assert initialize["method"] == "initialize"
    assert driver.connected is True
    assert manifests[-1].support(FeatureId.SESSION_HISTORY).supported is True
    assert manifests[-1].support(FeatureId.INPUT_IMAGES).supported is True
    listing = transport.sent[-1]
    assert listing["method"] == "session/list"
    transport.messageReceived.emit(
        {
            "jsonrpc": "2.0",
            "id": listing["id"],
            "result": {
                "sessions": [
                    {
                        "sessionId": "s1",
                        "cwd": "/repo",
                        "title": "Первый чат",
                        "updatedAt": "2026-08-01T10:00:00Z",
                    }
                ]
            },
        }
    )
    assert sessions[-1][0].id == "s1"
    assert sessions[-1][0].title == "Первый чат"


def test_acp_prompt_stream_and_permission_transcript(qtbot) -> None:
    driver, transport = make_acp_driver()
    initialize_acp(driver, transport)
    driver.current_project = "/repo"
    driver.current_session_id = "s1"
    deltas = []
    items = []
    states = []
    requests = []
    driver.agentDelta.connect(lambda item_id, delta: deltas.append((item_id, delta)))
    driver.itemUpdated.connect(lambda item, _finished: items.append(item))
    driver.turnStateChanged.connect(states.append)
    driver.clientRequestReceived.connect(requests.append)

    driver.submit_prompt(AgentPrompt("Привет", working_directory="/repo"))
    prompt = transport.sent[-1]
    assert prompt["method"] == "session/prompt"
    assert prompt["params"] == {
        "sessionId": "s1",
        "prompt": [{"type": "text", "text": "Привет"}],
    }
    transport.messageReceived.emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "answer",
                    "content": {"type": "text", "text": "Ответ"},
                },
            },
        }
    )
    assert deltas == [("answer", "Ответ")]

    transport.messageReceived.emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s1",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tool-1",
                    "title": "Запустить тесты",
                    "kind": "execute",
                    "status": "in_progress",
                },
            },
        }
    )
    assert items[-1]["kind"] == "tool_call"
    assert items[-1]["subtype"] == "execute"

    transport.messageReceived.emit(
        {
            "jsonrpc": "2.0",
            "id": 77,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": {"title": "Запустить тесты"},
                "options": [
                    {"optionId": "yes", "name": "Разрешить", "kind": "allow_once"},
                    {"optionId": "no", "name": "Запретить", "kind": "reject_once"},
                ],
            },
        }
    )
    assert requests[-1].options[0].id == "yes"
    driver.respond_to_request(77, "yes")
    assert transport.sent[-1]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "yes"}
    }

    transport.messageReceived.emit(
        {
            "jsonrpc": "2.0",
            "id": prompt["id"],
            "result": {"stopReason": "end_turn"},
        }
    )
    assert states[-1] == "completed"


def test_acp_uses_advertised_config_ids_and_session_modes(qtbot) -> None:
    driver, transport = make_acp_driver()
    initialize_acp(driver, transport)
    driver.current_project = "/repo"
    driver.current_session_id = "s1"
    driver._apply_config_options(
        [
            {
                "id": "vendor.model.choice",
                "name": "Model",
                "category": "model",
                "currentValue": "small",
                "options": [
                    {"value": "small", "name": "Small"},
                    {"value": "large", "name": "Large"},
                ],
            }
        ]
    )
    driver._apply_modes(
        {
            "currentModeId": "code",
            "availableModes": [
                {"id": "code", "name": "Code"},
                {"id": "plan", "name": "Plan"},
            ],
        }
    )

    assert [option.id for option in driver.config_options] == [
        "vendor.model.choice",
        ACP_MODE_OPTION_ID,
    ]
    assert driver.manifest.support(FeatureId.RUN_PLAN).supported is True

    driver.submit_prompt(
        AgentPrompt(
            "Сделай план",
            config={"vendor.model.choice": "large"},
            mode="plan",
        )
    )
    config_request = transport.sent[-1]
    assert config_request["method"] == "session/set_config_option"
    assert config_request["params"]["configId"] == "vendor.model.choice"
    transport.messageReceived.emit(
        {
            "jsonrpc": "2.0",
            "id": config_request["id"],
            "result": {
                "configOptions": [
                    {
                        "id": "vendor.model.choice",
                        "name": "Model",
                        "category": "model",
                        "currentValue": "large",
                        "options": [
                            {"value": "small", "name": "Small"},
                            {"value": "large", "name": "Large"},
                        ],
                    }
                ]
            },
        }
    )
    mode_request = transport.sent[-1]
    assert mode_request["method"] == "session/set_mode"
    assert mode_request["params"]["modeId"] == "plan"


def test_acp_prefers_config_mode_and_exposes_agent_run_modes(qtbot) -> None:
    driver, transport = make_acp_driver()
    initialize_acp(driver, transport)
    driver.current_project = "/repo"
    driver.current_session_id = "s1"
    driver._apply_config_options(
        [
            {
                "id": "mode",
                "name": "Session Mode",
                "category": "mode",
                "currentValue": "build",
                "options": [
                    {
                        "value": "build",
                        "name": "build",
                        "description": "Uses configured permissions",
                    },
                    {
                        "value": "plan",
                        "name": "plan",
                        "description": "Disallows edit tools",
                    },
                ],
            }
        ]
    )
    driver._apply_modes(
        {
            "currentModeId": "legacy",
            "availableModes": [{"id": "legacy", "name": "Legacy"}],
        }
    )

    assert [option.id for option in driver.config_options] == ["mode"]
    assert [mode.id for mode in driver.manifest.run_modes] == ["build", "plan"]
    assert driver.manifest.current_run_mode_id == "build"
    assert driver.manifest.support(FeatureId.ACCESS_MODES).supported is True

    driver.set_run_mode("plan")

    request = transport.sent[-1]
    assert request["method"] == "session/set_config_option"
    assert request["params"] == {
        "sessionId": "s1",
        "configId": "mode",
        "value": "plan",
    }


def test_acp_prepares_session_to_discover_modes_before_first_prompt(qtbot) -> None:
    driver, transport = make_acp_driver()
    initialize_acp(driver, transport)
    driver.set_project("/repo")

    driver.prepare_new_session()

    request = transport.sent[-1]
    assert request["method"] == "session/new"
    transport.messageReceived.emit(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "sessionId": "new-session",
                "configOptions": [
                    {
                        "id": "mode",
                        "name": "Session Mode",
                        "category": "mode",
                        "currentValue": "build",
                        "options": [
                            {"value": "build", "name": "build"},
                            {"value": "plan", "name": "plan"},
                        ],
                    }
                ],
            },
        }
    )

    assert driver.current_session_id == "new-session"
    assert [mode.id for mode in driver.manifest.run_modes] == ["build", "plan"]
    before = len([item for item in transport.sent if item.get("method") == "session/new"])
    driver.submit_prompt(AgentPrompt("hello", run_mode_id="plan"))
    after = len([item for item in transport.sent if item.get("method") == "session/new"])
    assert after == before
    assert transport.sent[-1]["method"] == "session/set_config_option"


def test_custom_agent_profiles_round_trip_through_settings(monkeypatch, tmp_path) -> None:
    store = QSettings(str(tmp_path / "profiles.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr("codex_gui.settings.QSettings", lambda *_args: store)
    settings = AppSettings()
    profile = AgentProfile(
        "goose",
        "acp",
        "Goose",
        "/usr/bin/goose",
        ("acp",),
        "ACP agent",
    )

    settings.save_agent_profile(profile)
    restored = settings.agent_profiles
    assert restored == [profile]
    settings.remove_agent_profile("goose")
    assert settings.agent_profiles == []
