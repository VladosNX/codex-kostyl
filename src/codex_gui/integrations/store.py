from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from .models import (
    ARCHIVE_MAX_FILES,
    EXTRACTED_MAX_BYTES,
    InstalledIntegration,
    IntegrationCandidate,
    ManifestError,
    digest_bytes,
    installation_identity,
    parse_package_manifest,
)


class IntegrationStoreError(RuntimeError):
    pass


class IntegrationStore:
    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            data_root = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )
            root = Path(data_root) / "integrations"
        self.root = Path(root)

    def load_all(self) -> list[InstalledIntegration]:
        if not self.root.is_dir():
            return []
        installed: list[InstalledIntegration] = []
        for record in sorted(self.root.glob("*/installation.json")):
            if record.parent.name.startswith("."):
                continue
            try:
                payload = json.loads(record.read_text(encoding="utf-8"))
                item = self._from_record(payload)
            except (OSError, ValueError, json.JSONDecodeError, ManifestError):
                continue
            if item.installation_id == record.parent.name:
                installed.append(item)
        return installed

    def install(
        self,
        candidate: IntegrationCandidate,
        artifact_data: bytes | None = None,
        executable_override: str = "",
    ) -> InstalledIntegration:
        installation_id, profile_id = installation_identity(
            candidate.source_kind,
            candidate.source_ref,
            candidate.manifest.id,
        )
        runtime = candidate.manifest.runtime_for()
        if runtime is None:
            raise IntegrationStoreError("Интеграция не поддерживает текущую платформу")
        if runtime.artifact is not None:
            if artifact_data is None:
                raise IntegrationStoreError("Не загружен ZIP-архив адаптера")
            actual = digest_bytes(artifact_data)
            if actual != runtime.artifact.sha256.lower():
                raise IntegrationStoreError("SHA-256 архива не совпадает с manifest")
            if candidate.artifact_api_sha256 and actual != candidate.artifact_api_sha256:
                raise IntegrationStoreError("SHA-256 архива не совпадает с данными GitHub")
        elif artifact_data is not None:
            raise IntegrationStoreError("Декларативный пакет не должен содержать архив")

        item = InstalledIntegration(
            installation_id,
            profile_id,
            candidate.source_kind,
            candidate.source_ref,
            candidate.release_id,
            candidate.release_tag,
            datetime.now(timezone.utc).isoformat(),
            candidate.manifest,
            executable_override,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{installation_id}-", dir=self.root))
        destination = self.root / installation_id
        backup = self.root / f".{installation_id}.backup"
        try:
            if artifact_data is not None:
                payload_root = staging / "payload"
                payload_root.mkdir()
                safe_extract_zip(artifact_data, payload_root)
                entrypoint = payload_root / str(runtime.artifact.entrypoint)
                if not entrypoint.is_file():
                    raise IntegrationStoreError(
                        f"Entrypoint адаптера не найден: {runtime.artifact.entrypoint}"
                    )
                if os.name != "nt":
                    entrypoint.chmod(entrypoint.stat().st_mode | stat.S_IXUSR)
            self._write_record(staging / "installation.json", item)
            if backup.exists():
                shutil.rmtree(backup)
            if destination.exists():
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
            except Exception:
                if backup.exists() and not destination.exists():
                    os.replace(backup, destination)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        return item

    def set_executable_override(
        self,
        item: InstalledIntegration,
        executable: str,
    ) -> InstalledIntegration:
        updated = InstalledIntegration(
            item.installation_id,
            item.profile_id,
            item.source_kind,
            item.source_ref,
            item.release_id,
            item.release_tag,
            item.installed_at,
            item.manifest,
            executable,
        )
        record = self.root / item.installation_id / "installation.json"
        if not record.is_file():
            raise IntegrationStoreError("Запись установленной интеграции не найдена")
        temporary = record.with_suffix(".json.tmp")
        self._write_record(temporary, updated)
        os.replace(temporary, record)
        return updated

    def remove(self, item: InstalledIntegration) -> None:
        destination = self.root / item.installation_id
        try:
            destination.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise IntegrationStoreError("Некорректный путь интеграции") from exc
        if destination.is_dir():
            shutil.rmtree(destination)

    def payload_path(self, item: InstalledIntegration, relative: str) -> Path:
        path = self.root / item.installation_id / "payload" / relative
        try:
            path.resolve().relative_to((self.root / item.installation_id / "payload").resolve())
        except ValueError as exc:
            raise IntegrationStoreError("Некорректный путь внутри пакета") from exc
        return path

    @staticmethod
    def _write_record(path: Path, item: InstalledIntegration) -> None:
        path.write_text(
            json.dumps(
                {
                    "installationId": item.installation_id,
                    "profileId": item.profile_id,
                    "sourceKind": item.source_kind,
                    "sourceRef": item.source_ref,
                    "releaseId": item.release_id,
                    "releaseTag": item.release_tag,
                    "installedAt": item.installed_at,
                    "executableOverride": item.executable_override,
                    "manifest": item.manifest.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _from_record(payload: object) -> InstalledIntegration:
        if not isinstance(payload, dict):
            raise ValueError("installation.json должен быть объектом")
        manifest = parse_package_manifest(payload.get("manifest", {}))
        return InstalledIntegration(
            str(payload.get("installationId") or ""),
            str(payload.get("profileId") or ""),
            str(payload.get("sourceKind") or ""),
            str(payload.get("sourceRef") or ""),
            str(payload.get("releaseId") or ""),
            str(payload.get("releaseTag") or ""),
            str(payload.get("installedAt") or ""),
            manifest,
            str(payload.get("executableOverride") or ""),
        )


def safe_extract_zip(data: bytes, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise IntegrationStoreError(f"Некорректный ZIP-архив: {exc}") from exc
    with archive:
        members = archive.infolist()
        if len(members) > ARCHIVE_MAX_FILES:
            raise IntegrationStoreError("В архиве слишком много файлов")
        extracted_size = sum(max(0, item.file_size) for item in members)
        if extracted_size > EXTRACTED_MAX_BYTES:
            raise IntegrationStoreError("Распакованный архив слишком большой")
        destination_resolved = destination.resolve()
        for member in members:
            name = member.filename.replace("\\", "/")
            if not name or name.startswith("/"):
                raise IntegrationStoreError("Архив содержит абсолютный путь")
            parts = tuple(part for part in name.split("/") if part not in {"", "."})
            if ".." in parts:
                raise IntegrationStoreError("Архив пытается выйти из каталога установки")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise IntegrationStoreError("Символические ссылки в архиве запрещены")
            target = destination.joinpath(*parts)
            try:
                target.resolve().relative_to(destination_resolved)
            except ValueError as exc:
                raise IntegrationStoreError("Некорректный путь в архиве") from exc
            if member.is_dir() or name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
