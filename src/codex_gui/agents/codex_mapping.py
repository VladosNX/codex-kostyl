from __future__ import annotations

from typing import Any

from .base import PermissionOption, PermissionRequest
from ..models import TimelineItemKind

_ITEM_KINDS = {
    "userMessage": TimelineItemKind.USER_MESSAGE.value,
    "agentMessage": TimelineItemKind.ASSISTANT_MESSAGE.value,
    "plan": TimelineItemKind.PLAN.value,
    "reasoning": TimelineItemKind.REASONING.value,
    "commandExecution": TimelineItemKind.COMMAND.value,
    "fileChange": TimelineItemKind.FILE_CHANGE.value,
    "mcpToolCall": TimelineItemKind.TOOL_CALL.value,
    "dynamicToolCall": TimelineItemKind.TOOL_CALL.value,
    "webSearch": TimelineItemKind.TOOL_CALL.value,
    "collabToolCall": TimelineItemKind.TOOL_CALL.value,
    "contextCompaction": TimelineItemKind.SYSTEM_ACTIVITY.value,
    "enteredReviewMode": TimelineItemKind.SYSTEM_ACTIVITY.value,
    "exitedReviewMode": TimelineItemKind.SYSTEM_ACTIVITY.value,
}


def normalize_codex_item(item: dict[str, Any]) -> dict[str, Any]:
    """Translate a Codex item into the application-owned timeline schema."""
    normalized = dict(item)
    vendor_kind = str(normalized.pop("type", "unknown"))
    normalized["kind"] = _ITEM_KINDS.get(vendor_kind, TimelineItemKind.SYSTEM_ACTIVITY.value)
    # Retain a semantic subtype for generic activity/tool renderers without
    # making the UI inspect a Codex protocol method or notification name.
    normalized["subtype"] = vendor_kind
    return normalized


def normalize_codex_thread(thread: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(thread)
    turns: list[dict[str, Any]] = []
    for raw_turn in thread.get("turns", []):
        if not isinstance(raw_turn, dict):
            continue
        turn = dict(raw_turn)
        turn["items"] = [
            normalize_codex_item(item)
            for item in raw_turn.get("items", [])
            if isinstance(item, dict)
        ]
        turns.append(turn)
    normalized["turns"] = turns
    return normalized


def normalize_codex_approval(
    request_id: object,
    method: str,
    params: dict[str, Any],
    project: str,
) -> PermissionRequest:
    reason = str(params.get("reason") or "Причина не указана.").strip()
    if "commandExecution" in method:
        kind = "command"
        title = "Выполнение команды"
        command = params.get("command") or "Команда не указана"
        if isinstance(command, list):
            command = " ".join(map(str, command))
        detail = f"Команда:\n{command}\n\nРабочая папка: {project}\n\nЗачем это нужно:\n{reason}"
    elif "fileChange" in method:
        kind = "file_change"
        title = "Изменение файлов"
        paths = params.get("paths") or params.get("files") or []
        affected = (
            "\n".join(f"• {path}" for path in paths)
            if isinstance(paths, list)
            else str(paths)
        )
        scope = affected or f"Внутри проекта: {project}"
        detail = f"Агент запрашивает изменение файлов.\n\nОбласть:\n{scope}\n\nЗачем это нужно:\n{reason}"
    else:
        kind = "permissions"
        title = "Дополнительные разрешения"
        detail = (
            "Агент запрашивает доступ за пределами обычного режима.\n\n"
            f"Запрошено:\n{_permission_summary(params.get('permissions', {}))}"
            f"\n\nЗачем это нужно:\n{reason}"
        )
    return PermissionRequest(
        request_id,
        kind,
        title,
        detail,
        (
            PermissionOption("decline", "Запретить"),
            PermissionOption("acceptForSession", "Разрешить для сессии"),
            PermissionOption("accept", "Разрешить один раз"),
            PermissionOption("cancel", "Остановить выполнение"),
        ),
    )


def _permission_summary(permissions: object) -> str:
    if not isinstance(permissions, dict) or not permissions:
        return "Область дополнительных разрешений не указана."
    labels = {
        "network": "Доступ к сети",
        "fileSystem": "Доступ к файловой системе",
        "filesystem": "Доступ к файловой системе",
        "writableRoots": "Дополнительные папки для записи",
    }
    lines: list[str] = []
    for key, value in permissions.items():
        label = labels.get(str(key), str(key))
        if isinstance(value, dict):
            enabled = value.get("enabled")
            detail = (
                "разрешён"
                if enabled is True
                else "запрещён"
                if enabled is False
                else "запрошен"
            )
            paths = value.get("writableRoots") or value.get("paths")
            if isinstance(paths, list) and paths:
                detail += ": " + ", ".join(map(str, paths))
        elif isinstance(value, list):
            detail = ", ".join(map(str, value))
        elif isinstance(value, bool):
            detail = "разрешён" if value else "запрещён"
        else:
            detail = str(value)
        lines.append(f"• {label}: {detail}")
    return "\n".join(lines)
