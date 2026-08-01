from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from PySide6.QtCore import QObject, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .models import (
    ARCHIVE_MAX_BYTES,
    MANIFEST_MAX_BYTES,
    PACKAGE_MANIFEST_NAME,
    AgentPackageManifest,
    InstallHelp,
    IntegrationCandidate,
    ManifestError,
    RuntimeSpec,
    digest_bytes,
    parse_package_manifest,
)


SourceCallback = Callable[[Any | None, str], None]
_REPOSITORY_PATTERN = re.compile(
    r"^(?:https://github\.com/)?(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


@dataclass(slots=True, frozen=True)
class HttpResponse:
    data: bytes
    status: int
    headers: dict[str, str]


class HttpClient(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        self._replies: set[QNetworkReply] = set()

    def get(
        self,
        url: str,
        callback: SourceCallback,
        *,
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        request.setRawHeader(b"User-Agent", b"Codex-Kostyl")
        for key, value in (headers or {}).items():
            request.setRawHeader(key.encode("ascii"), value.encode("utf-8"))
        reply = self.manager.get(request)
        self._replies.add(reply)
        buffer = bytearray()
        too_large = [False]

        def read_available() -> None:
            if too_large[0]:
                reply.readAll()
                return
            buffer.extend(bytes(reply.readAll()))
            if len(buffer) > max_bytes:
                too_large[0] = True
                reply.abort()

        reply.readyRead.connect(read_available)

        def finished() -> None:
            self._replies.discard(reply)
            status = int(
                reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            )
            length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
            error = reply.error()
            read_available()
            data = bytes(buffer)
            response_headers = _response_headers(reply)
            message = ""
            if too_large[0] or (length is not None and int(length) > max_bytes):
                message = "Ответ сервера превышает допустимый размер"
            elif len(data) > max_bytes:
                message = "Ответ сервера превышает допустимый размер"
            elif error != QNetworkReply.NetworkError.NoError and status != 304:
                if status == 403 and response_headers.get("x-ratelimit-remaining") == "0":
                    message = "GitHub API rate limit исчерпан; попробуйте позже"
                else:
                    message = reply.errorString()
            elif status >= 400 and status != 304:
                message = f"Сервер вернул HTTP {status}"
            reply.deleteLater()
            if message:
                callback(None, message)
            else:
                callback(HttpResponse(data, status, response_headers), "")

        reply.finished.connect(finished)


def normalize_github_repository(value: str) -> str:
    normalized = value.strip().split("#", 1)[0].split("?", 1)[0]
    match = _REPOSITORY_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError("Укажите ссылку вида https://github.com/owner/repository")
    return f"{match.group('owner')}/{match.group('repo')}"


def _response_headers(reply: QNetworkReply) -> dict[str, str]:
    """Normalize Qt headers across PySide versions.

    rawHeaderList() exposes QByteArray values, while some PySide6 builds bind
    rawHeader() to a str-only signature. Converting the name before the second
    call works with both forms of the API.
    """
    headers: dict[str, str] = {}
    for raw_name in reply.rawHeaderList():
        name = bytes(raw_name).decode("latin-1")
        headers[name.lower()] = bytes(reply.rawHeader(name)).decode(
            "utf-8", errors="replace"
        )
    return headers


class GitHubReleaseSource(QObject):
    def __init__(self, http: HttpClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.http = http

    def preview(self, repository: str, callback: SourceCallback) -> None:
        try:
            source_ref = normalize_github_repository(repository)
        except ValueError as exc:
            callback(None, str(exc))
            return
        owner, repo = source_ref.split("/", 1)
        api_url = (
            "https://api.github.com/repos/"
            f"{quote(owner, safe='')}/{quote(repo, safe='')}/releases/latest"
        )

        def release_loaded(value: Any | None, error: str) -> None:
            if error or not isinstance(value, HttpResponse):
                callback(None, error or "Не удалось получить GitHub Release")
                return
            try:
                release = json.loads(value.data)
                manifest_asset = _release_asset(release, PACKAGE_MANIFEST_NAME)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                callback(None, str(exc))
                return
            if int(manifest_asset.get("size") or 0) > MANIFEST_MAX_BYTES:
                callback(None, "Manifest в GitHub Release слишком большой")
                return
            manifest_url = str(manifest_asset.get("browser_download_url") or "")
            if not manifest_url:
                callback(None, "GitHub Release не содержит ссылку на manifest")
                return

            def manifest_loaded(raw: Any | None, manifest_error: str) -> None:
                if manifest_error or not isinstance(raw, HttpResponse):
                    callback(None, manifest_error or "Не удалось загрузить manifest")
                    return
                try:
                    expected_manifest_digest = _asset_digest(manifest_asset)
                    actual_manifest_digest = digest_bytes(raw.data)
                    if expected_manifest_digest and expected_manifest_digest != actual_manifest_digest:
                        raise ValueError("SHA-256 manifest не совпадает с данными GitHub")
                    manifest = parse_package_manifest(raw.data)
                    runtime = manifest.runtime_for()
                    if runtime is None:
                        raise ValueError("Пакет не поддерживает текущую платформу")
                    artifact_url = ""
                    artifact_size = 0
                    artifact_api_digest = ""
                    if runtime.artifact is not None:
                        artifact_asset = _release_asset(release, runtime.artifact.asset)
                        artifact_url = str(artifact_asset.get("browser_download_url") or "")
                        artifact_size = int(artifact_asset.get("size") or 0)
                        artifact_api_digest = _asset_digest(artifact_asset)
                        if artifact_size > ARCHIVE_MAX_BYTES:
                            raise ValueError("ZIP-архив адаптера слишком большой")
                        if (
                            artifact_api_digest
                            and artifact_api_digest != runtime.artifact.sha256.lower()
                        ):
                            raise ValueError("SHA-256 ZIP не совпадает между manifest и GitHub")
                    candidate = IntegrationCandidate(
                        manifest,
                        "github",
                        source_ref,
                        str(release.get("id") or ""),
                        str(release.get("tag_name") or ""),
                        actual_manifest_digest,
                        artifact_url,
                        artifact_size,
                        artifact_api_digest,
                    )
                except (ManifestError, ValueError, TypeError) as exc:
                    callback(None, str(exc))
                    return
                callback(candidate, "")

            self.http.get(
                manifest_url,
                manifest_loaded,
                max_bytes=MANIFEST_MAX_BYTES,
            )

        self.http.get(
            api_url,
            release_loaded,
            max_bytes=2 * 1024 * 1024,
            headers={"Accept": "application/vnd.github+json"},
        )

    def download_artifact(self, candidate: IntegrationCandidate, callback: SourceCallback) -> None:
        if not candidate.artifact_url:
            callback(b"", "")
            return

        def loaded(value: Any | None, error: str) -> None:
            if error or not isinstance(value, HttpResponse):
                callback(None, error or "Не удалось загрузить архив адаптера")
                return
            callback(value.data, "")

        self.http.get(
            candidate.artifact_url,
            loaded,
            max_bytes=ARCHIVE_MAX_BYTES,
        )


class AcpRegistrySource(QObject):
    URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"

    def __init__(
        self,
        http: HttpClient,
        cache_root: str | Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.http = http
        self.cache_root = Path(cache_root)

    def fetch(self, callback: SourceCallback) -> None:
        cached, etag = self._read_cache()
        headers = {"If-None-Match": etag} if etag else {}

        def loaded(value: Any | None, error: str) -> None:
            if error:
                if cached is not None:
                    self._parse_and_return(cached, callback)
                else:
                    callback(None, error)
                return
            if not isinstance(value, HttpResponse):
                callback(None, "Не удалось загрузить ACP Registry")
                return
            if value.status == 304 and cached is not None:
                self._parse_and_return(cached, callback)
                return
            self._write_cache(value.data, value.headers.get("etag", ""))
            self._parse_and_return(value.data, callback)

        self.http.get(self.URL, loaded, max_bytes=4 * 1024 * 1024, headers=headers)

    @staticmethod
    def _parse_and_return(data: bytes, callback: SourceCallback) -> None:
        try:
            candidates = parse_acp_registry(data)
        except (ValueError, json.JSONDecodeError) as exc:
            callback(None, f"Некорректный ACP Registry: {exc}")
            return
        callback(candidates, "")

    def _read_cache(self) -> tuple[bytes | None, str]:
        registry_path = self.cache_root / "registry.json"
        etag_path = self.cache_root / "registry.etag"
        try:
            return (
                registry_path.read_bytes(),
                etag_path.read_text(encoding="utf-8") if etag_path.is_file() else "",
            )
        except OSError:
            return None, ""

    def _write_cache(self, data: bytes, etag: str) -> None:
        try:
            self.cache_root.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_root / "registry.json.tmp"
            temporary.write_bytes(data)
            temporary.replace(self.cache_root / "registry.json")
            if etag:
                (self.cache_root / "registry.etag").write_text(etag, encoding="utf-8")
        except OSError:
            pass


def parse_acp_registry(raw: bytes | str | dict[str, Any]) -> list[IntegrationCandidate]:
    if isinstance(raw, bytes):
        payload = json.loads(raw.decode("utf-8"))
    elif isinstance(raw, str):
        payload = json.loads(raw)
    else:
        payload = raw
    if not isinstance(payload, dict) or not isinstance(payload.get("agents"), list):
        raise ValueError("registry.json не содержит массив agents")
    registry_version = str(payload.get("version") or "")
    candidates: list[IntegrationCandidate] = []
    for row in payload["agents"]:
        if not isinstance(row, dict):
            continue
        try:
            manifest = _manifest_from_registry_entry(row)
        except (TypeError, ValueError):
            continue
        candidates.append(
            IntegrationCandidate(
                manifest,
                "acp-registry",
                manifest.id,
                registry_version,
                manifest.version,
            )
        )
    return candidates


def _manifest_from_registry_entry(row: dict[str, Any]) -> AgentPackageManifest:
    package_id = str(row.get("id") or "")
    name = str(row.get("name") or "")
    version = str(row.get("version") or "")
    description = str(row.get("description") or "")
    if not package_id or not name or not version or not description:
        raise ValueError("Неполная запись ACP Registry")
    distribution = row.get("distribution")
    if not isinstance(distribution, dict):
        raise ValueError("Нет distribution")
    runtimes: dict[str, RuntimeSpec] = {}
    binary = distribution.get("binary")
    if isinstance(binary, dict):
        for target, target_row in binary.items():
            if not isinstance(target_row, dict):
                continue
            command = str(target_row.get("cmd") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if not command:
                continue
            args = tuple(str(value) for value in target_row.get("args", []) if isinstance(value, str))
            env = _registry_environment(target_row.get("env", {}))
            runtimes[str(target)] = RuntimeSpec((command,), arguments=args, environment=env)
    runner_name = ""
    package_name = ""
    runner_row: dict[str, Any] = {}
    if not runtimes:
        for candidate_runner in ("npx", "uvx"):
            value = distribution.get(candidate_runner)
            if isinstance(value, dict):
                runner_name = candidate_runner
                runner_row = value
                package_name = str(value.get("package") or "")
                break
        args = tuple(
            str(value) for value in runner_row.get("args", []) if isinstance(value, str)
        )
        runtimes["any"] = RuntimeSpec(
            (),
            arguments=args,
            environment=_registry_environment(runner_row.get("env", {})),
            configuration_required=True,
        )
    help_url = str(row.get("website") or row.get("repository") or "")
    message = "Установите агент и укажите путь к его ACP-executable."
    if runner_name and package_name:
        message = (
            f"Пакет каталога: {runner_name} {package_name}. "
            "Приложение не устанавливает его автоматически; укажите установленный executable."
        )
    return AgentPackageManifest(
        1,
        package_id,
        name,
        version,
        description,
        "acp-command",
        runtimes,
        str(row.get("website") or ""),
        str(row.get("license") or ""),
        str(row.get("repository") or ""),
        InstallHelp(help_url, message),
    )


def _registry_environment(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    result: list[tuple[str, str]] = []
    for key, item in value.items():
        name = str(key)
        upper = name.upper()
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or upper in {"PATH", "PYTHONPATH", "COMSPEC"}
            or upper.startswith(("LD_", "DYLD_"))
            or not isinstance(item, str)
        ):
            continue
        result.append((name, item))
    return tuple(result)


def _release_asset(release: object, name: str) -> dict[str, Any]:
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise ValueError("Некорректный ответ GitHub Releases API")
    matches = [
        item
        for item in release["assets"]
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise ValueError(f"Release должен содержать ровно один asset {name}")
    return matches[0]


def _asset_digest(asset: dict[str, Any]) -> str:
    raw = str(asset.get("digest") or "")
    if raw.startswith("sha256:") and len(raw) == 71:
        return raw[7:].lower()
    return ""
