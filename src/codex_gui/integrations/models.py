from __future__ import annotations

import hashlib
import json
import platform
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse


PACKAGE_MANIFEST_NAME = "codex-kostyl-agent.json"
PACKAGE_SCHEMA_VERSION = 1
MANIFEST_MAX_BYTES = 256 * 1024
ARCHIVE_MAX_BYTES = 100 * 1024 * 1024
EXTRACTED_MAX_BYTES = 250 * 1024 * 1024
ARCHIVE_MAX_FILES = 10_000

PACKAGE_KINDS = {"acp-command", "acp-adapter"}
PLATFORM_TARGETS = {
    "darwin-aarch64",
    "darwin-x86_64",
    "linux-aarch64",
    "linux-x86_64",
    "windows-aarch64",
    "windows-x86_64",
}
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXECUTABLE_PATTERN = re.compile(r"^[A-Za-z0-9_.+-]+$")
_FORBIDDEN_ENV = {"PATH", "PYTHONPATH", "COMSPEC"}
_FORBIDDEN_ENV_PREFIXES = ("LD_", "DYLD_")


class ManifestError(ValueError):
    pass


def current_platform_target(
    system: str | None = None,
    machine: str | None = None,
) -> str:
    system_name = (system or platform.system()).lower()
    machine_name = (machine or platform.machine()).lower()
    os_name = {
        "darwin": "darwin",
        "linux": "linux",
        "windows": "windows",
    }.get(system_name)
    arch = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "amd64": "x86_64",
        "x86_64": "x86_64",
    }.get(machine_name)
    if os_name is None or arch is None:
        return f"{system_name}-{machine_name}"
    return f"{os_name}-{arch}"


def _safe_relative_path(value: str, field_name: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"{field_name} должен быть относительным безопасным путём")
    return str(path)


def _env_name(value: object, field_name: str) -> str:
    name = str(value or "")
    if not _ENV_PATTERN.fullmatch(name):
        raise ManifestError(f"Некорректное имя переменной окружения в {field_name}")
    upper = name.upper()
    if upper in _FORBIDDEN_ENV or upper.startswith(_FORBIDDEN_ENV_PREFIXES):
        raise ManifestError(f"Переменная окружения {name} запрещена")
    return name


def _web_url(value: object, field_name: str) -> str:
    url = str(value or "")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ManifestError(f"{field_name} должен быть HTTP(S)-ссылкой")
    return url


def _string_list(value: object, field_name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ManifestError(f"{field_name} должен быть массивом строк")
    if any(not isinstance(item, str) or "\x00" in item for item in value):
        raise ManifestError(f"{field_name} должен содержать только строки")
    return tuple(value)


@dataclass(slots=True, frozen=True)
class InstallHelp:
    url: str = ""
    message: str = ""


@dataclass(slots=True, frozen=True)
class ExecutableRequirement:
    id: str
    commands: tuple[str, ...]
    export_as: str
    help_url: str = ""


@dataclass(slots=True, frozen=True)
class ArtifactSpec:
    asset: str
    sha256: str
    entrypoint: str


@dataclass(slots=True, frozen=True)
class RuntimeSpec:
    system_commands: tuple[str, ...] = ()
    artifact_command: str = ""
    arguments: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    artifact: ArtifactSpec | None = None
    requirements: tuple[ExecutableRequirement, ...] = ()
    configuration_required: bool = False


@dataclass(slots=True, frozen=True)
class AgentPackageManifest:
    schema_version: int
    id: str
    name: str
    version: str
    description: str
    kind: str
    runtimes: dict[str, RuntimeSpec] = field(default_factory=dict)
    homepage: str = ""
    license: str = ""
    repository: str = ""
    install_help: InstallHelp = InstallHelp()

    def runtime_for(self, target: str | None = None) -> RuntimeSpec | None:
        selected = target or current_platform_target()
        return self.runtimes.get(selected) or self.runtimes.get("any")

    def to_dict(self) -> dict[str, Any]:
        runtime: dict[str, Any] = {}
        for target, spec in self.runtimes.items():
            command: dict[str, Any]
            if spec.artifact_command:
                command = {"artifact": spec.artifact_command}
            else:
                command = {"system": list(spec.system_commands)}
            row: dict[str, Any] = {
                "command": command,
                "args": list(spec.arguments),
                "env": dict(spec.environment),
            }
            if spec.configuration_required:
                row["configurationRequired"] = True
            if spec.artifact is not None:
                row["artifact"] = {
                    "asset": spec.artifact.asset,
                    "sha256": spec.artifact.sha256,
                    "entrypoint": spec.artifact.entrypoint,
                }
            if spec.requirements:
                row["requirements"] = [
                    {
                        "id": item.id,
                        "commands": list(item.commands),
                        "exportAs": item.export_as,
                        **({"helpUrl": item.help_url} if item.help_url else {}),
                    }
                    for item in spec.requirements
                ]
            runtime[target] = row
        payload: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "kind": self.kind,
            "runtime": runtime,
        }
        if self.homepage:
            payload["homepage"] = self.homepage
        if self.license:
            payload["license"] = self.license
        if self.repository:
            payload["repository"] = self.repository
        if self.install_help.url or self.install_help.message:
            payload["installHelp"] = {
                "url": self.install_help.url,
                "message": self.install_help.message,
            }
        return payload


@dataclass(slots=True, frozen=True)
class IntegrationCandidate:
    manifest: AgentPackageManifest
    source_kind: str
    source_ref: str
    release_id: str = ""
    release_tag: str = ""
    manifest_sha256: str = ""
    artifact_url: str = ""
    artifact_size: int = 0
    artifact_api_sha256: str = ""


@dataclass(slots=True, frozen=True)
class InstalledIntegration:
    installation_id: str
    profile_id: str
    source_kind: str
    source_ref: str
    release_id: str
    release_tag: str
    installed_at: str
    manifest: AgentPackageManifest
    executable_override: str = ""


@dataclass(slots=True, frozen=True)
class IntegrationStatus:
    code: str
    available: bool
    message: str
    executable: str = ""


def parse_package_manifest(raw: bytes | str | dict[str, Any]) -> AgentPackageManifest:
    if isinstance(raw, bytes):
        if len(raw) > MANIFEST_MAX_BYTES:
            raise ManifestError("Manifest слишком большой")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"Некорректный JSON manifest: {exc}") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MANIFEST_MAX_BYTES:
            raise ManifestError("Manifest слишком большой")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"Некорректный JSON manifest: {exc}") from exc
    else:
        payload = raw
    if not isinstance(payload, dict):
        raise ManifestError("Manifest должен быть JSON-объектом")

    schema_version = payload.get("schemaVersion")
    if schema_version != PACKAGE_SCHEMA_VERSION:
        raise ManifestError("Неподдерживаемая версия manifest; обновите приложение")
    package_id = str(payload.get("id") or "")
    if not _ID_PATTERN.fullmatch(package_id):
        raise ManifestError("id должен состоять из строчных букв, цифр и дефисов")
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip()
    version = str(payload.get("version") or "")
    kind = str(payload.get("kind") or "")
    if not name or not description:
        raise ManifestError("name и description обязательны")
    if not _VERSION_PATTERN.fullmatch(version):
        raise ManifestError("version должен быть семантической версией")
    if kind not in PACKAGE_KINDS:
        raise ManifestError("kind должен быть acp-command или acp-adapter")

    runtime_rows = payload.get("runtime")
    if not isinstance(runtime_rows, dict) or not runtime_rows:
        raise ManifestError("runtime должен содержать хотя бы одну платформу")
    runtimes: dict[str, RuntimeSpec] = {}
    for target, row in runtime_rows.items():
        if target not in PLATFORM_TARGETS | {"any"}:
            raise ManifestError(f"Неизвестная платформа: {target}")
        if target == "any" and kind != "acp-command":
            raise ManifestError("ACP-адаптер должен объявлять конкретные платформы")
        runtimes[target] = _parse_runtime(row, kind, str(target))

    help_row = payload.get("installHelp", {})
    if not isinstance(help_row, dict):
        raise ManifestError("installHelp должен быть объектом")
    return AgentPackageManifest(
        schema_version,
        package_id,
        name,
        version,
        description,
        kind,
        runtimes,
        _web_url(payload.get("homepage"), "homepage"),
        str(payload.get("license") or ""),
        _web_url(payload.get("repository"), "repository"),
        InstallHelp(
            _web_url(help_row.get("url"), "installHelp.url"),
            str(help_row.get("message") or ""),
        ),
    )


def _parse_runtime(row: object, kind: str, target: str) -> RuntimeSpec:
    if not isinstance(row, dict):
        raise ManifestError(f"runtime.{target} должен быть объектом")
    command = row.get("command")
    if not isinstance(command, dict):
        raise ManifestError(f"runtime.{target}.command обязателен")
    has_system = "system" in command
    has_artifact = "artifact" in command
    if has_system == has_artifact:
        raise ManifestError("command должен содержать ровно одно из system или artifact")

    system_commands: tuple[str, ...] = ()
    artifact_command = ""
    if has_system:
        configuration_required = row.get("configurationRequired") is True
        system_commands = _string_list(
            command.get("system"),
            f"runtime.{target}.command.system",
            allow_empty=configuration_required,
        )
        for executable in system_commands:
            if not _EXECUTABLE_PATTERN.fullmatch(executable):
                raise ManifestError("system содержит только имена executable без путей")
    else:
        configuration_required = False
        artifact_command = _safe_relative_path(
            str(command.get("artifact") or ""), f"runtime.{target}.command.artifact"
        )

    arguments = _string_list(row.get("args", []), f"runtime.{target}.args")
    env_row = row.get("env", {})
    if not isinstance(env_row, dict):
        raise ManifestError(f"runtime.{target}.env должен быть объектом")
    environment: list[tuple[str, str]] = []
    for key, value in env_row.items():
        name = _env_name(key, f"runtime.{target}.env")
        if not isinstance(value, str) or "\x00" in value:
            raise ManifestError("Значения env должны быть строками")
        environment.append((name, value))

    artifact = None
    artifact_row = row.get("artifact")
    if artifact_row is not None:
        if kind != "acp-adapter" or not isinstance(artifact_row, dict):
            raise ManifestError("artifact разрешён только для acp-adapter")
        asset = str(artifact_row.get("asset") or "")
        digest = str(artifact_row.get("sha256") or "").lower()
        entrypoint = _safe_relative_path(
            str(artifact_row.get("entrypoint") or ""),
            f"runtime.{target}.artifact.entrypoint",
        )
        if not asset.endswith(".zip") or "/" in asset or "\\" in asset:
            raise ManifestError("asset адаптера должен быть именем ZIP-файла")
        if not _SHA256_PATTERN.fullmatch(digest):
            raise ManifestError("Для ZIP-адаптера обязателен SHA-256")
        if artifact_command != entrypoint:
            raise ManifestError("command.artifact должен совпадать с artifact.entrypoint")
        artifact = ArtifactSpec(asset, digest, entrypoint)
    elif kind == "acp-adapter":
        raise ManifestError("Для acp-adapter обязателен artifact")

    requirements_row = row.get("requirements", [])
    if not isinstance(requirements_row, list):
        raise ManifestError("requirements должен быть массивом")
    requirements: list[ExecutableRequirement] = []
    seen_exports: set[str] = set()
    for requirement in requirements_row:
        if not isinstance(requirement, dict):
            raise ManifestError("Элемент requirements должен быть объектом")
        requirement_id = str(requirement.get("id") or "")
        if not _ID_PATTERN.fullmatch(requirement_id):
            raise ManifestError("Некорректный requirements.id")
        commands = _string_list(
            requirement.get("commands"), "requirements.commands", allow_empty=False
        )
        if any(not _EXECUTABLE_PATTERN.fullmatch(value) for value in commands):
            raise ManifestError("requirements.commands содержит только имена executable")
        export_as = _env_name(requirement.get("exportAs"), "requirements.exportAs")
        if export_as in seen_exports:
            raise ManifestError("requirements.exportAs должен быть уникальным")
        seen_exports.add(export_as)
        requirements.append(
            ExecutableRequirement(
                requirement_id,
                commands,
                export_as,
                _web_url(requirement.get("helpUrl"), "requirements.helpUrl"),
            )
        )
    return RuntimeSpec(
        system_commands,
        artifact_command,
        arguments,
        tuple(environment),
        artifact,
        tuple(requirements),
        configuration_required,
    )


def installation_identity(source_kind: str, source_ref: str, package_id: str) -> tuple[str, str]:
    material = f"{source_kind}\n{source_ref.lower()}\n{package_id}".encode("utf-8")
    installation_id = hashlib.sha256(material).hexdigest()[:24]
    return installation_id, f"integration-{installation_id}"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
