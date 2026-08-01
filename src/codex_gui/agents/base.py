from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, Signal


class FeatureId(str, Enum):
    SESSION_HISTORY = "session.history"
    SESSION_COMPACT = "session.compact"
    SESSION_FORK = "session.fork"
    SESSION_REVIEW = "session.review"
    INPUT_FILES = "input.files"
    INPUT_IMAGES = "input.images"
    USAGE_CONTEXT = "usage.context"
    USAGE_QUOTA = "usage.quota"
    RUN_PLAN = "run.plan"
    RUN_CANCEL = "run.cancel"
    PERMISSIONS = "permissions"
    USER_INPUT = "user_input"
    AUTHENTICATION = "authentication"
    ACCESS_MODES = "access_modes"
    CONFIG_MODEL = "config.model"
    CONFIG_THOUGHT_LEVEL = "config.thought_level"


def feature_key(value: FeatureId | str) -> str:
    return value.value if isinstance(value, FeatureId) else str(value)


@dataclass(slots=True, frozen=True)
class FeatureSupport:
    supported: bool
    reason: str = ""


@dataclass(slots=True, frozen=True)
class FeatureState:
    supported: bool
    enabled: bool
    reason: str = ""


@dataclass(slots=True, frozen=True)
class AgentAction:
    id: str
    title: str
    description: str = ""
    requires_session: bool = False
    allow_during_run: bool = False
    argument_hint: str = ""


@dataclass(slots=True, frozen=True)
class ConfigOptionValue:
    value: str
    label: str
    description: str = ""


@dataclass(slots=True, frozen=True)
class AgentConfigOption:
    id: str
    name: str
    category: str = ""
    kind: str = "select"
    current_value: str | bool = ""
    values: tuple[ConfigOptionValue, ...] = ()
    description: str = ""


@dataclass(slots=True, frozen=True)
class AuthMethod:
    id: str
    name: str
    kind: str = "agent"
    description: str = ""


@dataclass(slots=True, frozen=True)
class AgentManifest:
    features: dict[str, FeatureSupport] = field(default_factory=dict)
    actions: tuple[AgentAction, ...] = ()
    config_options: tuple[AgentConfigOption, ...] = ()
    auth_methods: tuple[AuthMethod, ...] = ()
    implementation_name: str = ""
    implementation_version: str = ""

    def support(self, feature: FeatureId | str) -> FeatureSupport:
        return self.features.get(
            feature_key(feature),
            FeatureSupport(False, "Функция не поддерживается агентом"),
        )


@dataclass(slots=True, frozen=True)
class AgentProfile:
    id: str
    driver_kind: str
    display_name: str
    executable: str
    arguments: tuple[str, ...] = ()
    description: str = ""
    built_in: bool = False
    environment: tuple[tuple[str, str], ...] = ()
    unavailable_reason: str = ""


@dataclass(slots=True, frozen=True)
class AgentDescriptor:
    """Compatibility-friendly user-facing identity of an agent profile."""

    id: str
    display_name: str
    executable_name: str
    description: str = ""


@dataclass(slots=True, frozen=True)
class AgentAvailability:
    available: bool
    executable: str = ""
    version: str = ""
    error: str = ""


@dataclass(slots=True, frozen=True)
class AgentPrompt:
    text: str
    attachments: tuple[Any, ...] = ()
    working_directory: str = ""
    config: dict[str, str | bool] = field(default_factory=dict)
    mode: str = ""
    access_mode: Any = None


@dataclass(slots=True, frozen=True)
class AgentState:
    connection_status: str = "disconnected"
    active_profile_id: str = ""
    active_session_id: str = ""
    active_run_id: str = ""
    feature_states: dict[str, FeatureState] = field(default_factory=dict)
    account_summary: dict[str, Any] | None = None
    context_usage: dict[str, Any] | None = None
    quota_usage: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class SessionSummary:
    id: str
    title: str
    cwd: str
    updated_at: int = 0
    status: str = "notLoaded"


@dataclass(slots=True, frozen=True)
class SessionSnapshot:
    id: str
    items: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AgentEvent:
    kind: str
    session_id: str
    run_id: str = ""
    item_id: str = ""
    phase: str = "update"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class PermissionOption:
    id: str
    label: str
    kind: str = ""


@dataclass(slots=True, frozen=True)
class ClientRequest:
    request_id: object
    kind: str
    title: str
    detail: str
    options: tuple[PermissionOption, ...]
    payload: dict[str, Any] = field(default_factory=dict)


# The old name remains source-compatible while the application migrates to the
# wider client-request vocabulary (permissions and structured elicitation).
PermissionRequest = ClientRequest


@dataclass(slots=True, frozen=True)
class AgentCapabilities:
    """Legacy boolean view used by existing widgets and third-party imports."""

    models: bool = False
    reasoning_effort: bool = False
    access_modes: bool = False
    plan_mode: bool = False
    authentication: bool = False
    rate_limits: bool = False
    context_usage: bool = False
    session_history: bool = False
    attachments: bool = False
    image_attachments: bool = False
    approvals: bool = False
    user_input: bool = False
    compact: bool = False
    review: bool = False
    fork: bool = False

    @classmethod
    def from_manifest(cls, manifest: AgentManifest) -> AgentCapabilities:
        def supported(key: FeatureId) -> bool:
            return manifest.support(key).supported

        return cls(
            models=supported(FeatureId.CONFIG_MODEL),
            reasoning_effort=supported(FeatureId.CONFIG_THOUGHT_LEVEL),
            access_modes=supported(FeatureId.ACCESS_MODES),
            plan_mode=supported(FeatureId.RUN_PLAN),
            authentication=supported(FeatureId.AUTHENTICATION),
            rate_limits=supported(FeatureId.USAGE_QUOTA),
            context_usage=supported(FeatureId.USAGE_CONTEXT),
            session_history=supported(FeatureId.SESSION_HISTORY),
            attachments=supported(FeatureId.INPUT_FILES),
            image_attachments=supported(FeatureId.INPUT_IMAGES),
            approvals=supported(FeatureId.PERMISSIONS),
            user_input=supported(FeatureId.USER_INPUT),
            compact=supported(FeatureId.SESSION_COMPACT),
            review=supported(FeatureId.SESSION_REVIEW),
            fork=supported(FeatureId.SESSION_FORK),
        )


class AgentDriver(QObject):
    """Protocol adapter boundary; concrete drivers translate wire payloads."""

    ready = Signal()
    manifestUpdated = Signal(object)
    stateUpdated = Signal(object)
    sessionsUpdated = Signal(object)
    sessionLoaded = Signal(object)
    eventReceived = Signal(object)
    clientRequestReceived = Signal(object)
    configOptionsUpdated = Signal(object)
    actionCompleted = Signal(str, object)

    # Compatibility signals consumed by the current Qt widgets. They are fed
    # from the same application-owned models as the new generic signals.
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

    def __init__(
        self,
        profile: AgentProfile | AgentDescriptor,
        manifest: AgentManifest | AgentCapabilities,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if isinstance(profile, AgentDescriptor):
            profile = AgentProfile(
                profile.id,
                profile.id,
                profile.display_name,
                profile.executable_name,
                description=profile.description,
            )
        if isinstance(manifest, AgentCapabilities):
            manifest = _manifest_from_legacy(manifest)
        self.profile = profile
        self.descriptor = AgentDescriptor(
            profile.id,
            profile.display_name,
            profile.executable,
            profile.description,
        )
        self.manifest = manifest
        self.capabilities = AgentCapabilities.from_manifest(manifest)
        self.current_project = ""
        self.current_session_id = ""
        self.current_run_id = ""
        self.connected = False
        self.account: dict[str, Any] | None = None

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

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def restart(self) -> None:
        self.stop()
        self.start()

    def feature_override(self, _feature: str) -> FeatureState | None:
        return None


def _manifest_from_legacy(capabilities: AgentCapabilities) -> AgentManifest:
    mapping = {
        FeatureId.CONFIG_MODEL: capabilities.models,
        FeatureId.CONFIG_THOUGHT_LEVEL: capabilities.reasoning_effort,
        FeatureId.ACCESS_MODES: capabilities.access_modes,
        FeatureId.RUN_PLAN: capabilities.plan_mode,
        FeatureId.AUTHENTICATION: capabilities.authentication,
        FeatureId.USAGE_QUOTA: capabilities.rate_limits,
        FeatureId.USAGE_CONTEXT: capabilities.context_usage,
        FeatureId.SESSION_HISTORY: capabilities.session_history,
        FeatureId.INPUT_FILES: capabilities.attachments,
        FeatureId.INPUT_IMAGES: capabilities.image_attachments,
        FeatureId.PERMISSIONS: capabilities.approvals,
        FeatureId.USER_INPUT: capabilities.user_input,
        FeatureId.SESSION_COMPACT: capabilities.compact,
        FeatureId.SESSION_REVIEW: capabilities.review,
        FeatureId.SESSION_FORK: capabilities.fork,
        FeatureId.RUN_CANCEL: True,
    }
    return AgentManifest(
        features={feature.value: FeatureSupport(value) for feature, value in mapping.items()}
    )
