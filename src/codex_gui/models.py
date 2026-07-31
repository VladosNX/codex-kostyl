from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from pathlib import Path
from typing import Any

WEEKLY_WINDOW_MINUTES = 7 * 24 * 60
PLAN_MODE_VALUE = "plan"


class AccessMode(str, Enum):
    READ_ONLY = "readOnly"
    WORKSPACE_WRITE = "workspaceWrite"
    FULL_ACCESS = "dangerFullAccess"

    @property
    def title(self) -> str:
        return {
            self.READ_ONLY: "Только чтение",
            self.WORKSPACE_WRITE: "Рабочая папка",
            self.FULL_ACCESS: "Полный доступ",
        }[self]

    def sandbox_policy(self, cwd: str) -> dict[str, Any]:
        if self is AccessMode.WORKSPACE_WRITE:
            return {
                "type": self.value,
                "writableRoots": [cwd],
                "networkAccess": False,
            }
        if self is AccessMode.READ_ONLY:
            return {"type": self.value, "networkAccess": False}
        return {"type": self.value}

    @property
    def approval_policy(self) -> str:
        return "never" if self is AccessMode.FULL_ACCESS else "on-request"


@dataclass(slots=True)
class Attachment:
    path: Path
    is_image: bool

    @property
    def name(self) -> str:
        return self.path.name

    def as_user_input(self) -> dict[str, str]:
        if self.is_image:
            return {"type": "localImage", "path": str(self.path)}
        return {"type": "mention", "name": self.name, "path": str(self.path)}


@dataclass(slots=True)
class ModelInfo:
    id: str
    display_name: str
    efforts: list[str] = field(default_factory=list)
    default_effort: str | None = None
    modalities: set[str] = field(default_factory=lambda: {"text"})
    is_default: bool = False

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "ModelInfo":
        efforts = [
            str(item.get("reasoningEffort", item.get("effort", "")))
            for item in data.get("supportedReasoningEfforts", [])
        ]
        efforts = [value for value in efforts if value]
        return cls(
            id=str(data.get("model") or data.get("id") or ""),
            display_name=str(data.get("displayName") or data.get("model") or data.get("id") or ""),
            efforts=efforts,
            default_effort=data.get("defaultReasoningEffort"),
            modalities=set(data.get("inputModalities", ["text", "image"])),
            is_default=bool(data.get("isDefault")),
        )


@dataclass(slots=True)
class ThreadSummary:
    id: str
    title: str
    cwd: str
    updated_at: int = 0
    status: str = "notLoaded"

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "ThreadSummary":
        title = data.get("name") or data.get("preview") or "Новый чат"
        title = " ".join(str(title).strip().split()) or "Новый чат"
        if len(title) > 72:
            title = title[:69] + "…"
        raw_status = data.get("status", "notLoaded")
        if isinstance(raw_status, dict):
            raw_status = raw_status.get("type", "active")
        return cls(
            id=str(data.get("id", "")),
            title=title,
            cwd=str(data.get("cwd") or ""),
            updated_at=int(data.get("updatedAt") or data.get("createdAt") or 0),
            status=str(raw_status),
        )


@dataclass(slots=True, frozen=True)
class RateLimitWindow:
    used_percent: float
    window_duration_mins: int
    resets_at: int | None = None

    @property
    def remaining_percent(self) -> int:
        return round(max(0.0, min(100.0, 100.0 - self.used_percent)))


def weekly_limit_from_payload(payload: dict[str, Any]) -> RateLimitWindow | None:
    """Return the weekly ChatGPT quota window from an app-server payload."""
    buckets: list[dict[str, Any]] = []
    by_limit_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        buckets.extend(value for value in by_limit_id.values() if isinstance(value, dict))
    fallback = payload.get("rateLimits")
    if isinstance(fallback, dict):
        buckets.append(fallback)

    candidates: list[RateLimitWindow] = []
    for bucket in buckets:
        for key in ("primary", "secondary"):
            raw = bucket.get(key)
            if not isinstance(raw, dict):
                continue
            try:
                used = float(raw["usedPercent"])
                duration = int(raw["windowDurationMins"])
                resets = int(raw["resetsAt"]) if raw.get("resetsAt") is not None else None
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(used):
                continue
            candidates.append(RateLimitWindow(used, duration, resets))

    # Allow a small server-side rounding difference while avoiding accidentally
    # presenting a five-hour bucket as the weekly subscription limit.
    weekly = [
        item
        for item in candidates
        if abs(item.window_duration_mins - WEEKLY_WINDOW_MINUTES) <= 60
    ]
    return min(weekly, key=lambda item: abs(item.window_duration_mins - WEEKLY_WINDOW_MINUTES), default=None)


@dataclass(slots=True)
class TimelineItem:
    id: str
    kind: str
    data: dict[str, Any]
    complete: bool = False
