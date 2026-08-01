from __future__ import annotations

import io
import json
import os
import stat
import zipfile

import pytest

from codex_gui.agent_settings import AgentSettingsDialog
from codex_gui.agents import AgentController, AgentProfile, AgentRegistry
from codex_gui.agents.acp import AcpDriver
from codex_gui.agents.acp import acp_driver_registration
from codex_gui.agents.codex import codex_driver_registration, codex_profile
from codex_gui.integrations import (
    AgentIntegrationManager,
    GitHubReleaseSource,
    IntegrationCandidate,
    IntegrationStore,
    IntegrationStoreError,
    ManifestError,
    current_platform_target,
    normalize_github_repository,
    parse_acp_registry,
    parse_package_manifest,
    safe_extract_zip,
)
from codex_gui.integrations.models import digest_bytes
from codex_gui.integrations.sources import HttpResponse, _response_headers


def command_manifest(**changes) -> dict:
    payload = {
        "schemaVersion": 1,
        "id": "opencode",
        "name": "OpenCode",
        "version": "1.0.0",
        "description": "OpenCode ACP integration",
        "kind": "acp-command",
        "runtime": {
            "any": {
                "command": {"system": ["opencode"]},
                "args": ["acp"],
                "env": {"OPENCODE_DISABLE_UPDATE": "1"},
            }
        },
        "installHelp": {
            "url": "https://opencode.ai/docs",
            "message": "Install OpenCode CLI",
        },
    }
    payload.update(changes)
    return payload


def json_bytes(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def adapter_archive(entrypoint: str = "bin/adapter") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo(entrypoint)
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(info, b"#!/bin/sh\n")
    return output.getvalue()


def adapter_manifest(archive: bytes, target: str | None = None, version: str = "1.0.0") -> dict:
    return {
        "schemaVersion": 1,
        "id": "foreign-agent",
        "name": "Foreign Agent",
        "version": version,
        "description": "ACP adapter for a foreign protocol",
        "kind": "acp-adapter",
        "runtime": {
            target or current_platform_target(): {
                "artifact": {
                    "asset": "adapter.zip",
                    "sha256": digest_bytes(archive),
                    "entrypoint": "bin/adapter",
                },
                "command": {"artifact": "bin/adapter"},
                "args": ["--stdio"],
                "requirements": [
                    {
                        "id": "foreign-cli",
                        "commands": ["foreign"],
                        "exportAs": "FOREIGN_CLI_BIN",
                    }
                ],
            }
        },
    }


def test_parse_declarative_manifest_and_round_trip() -> None:
    manifest = parse_package_manifest(command_manifest())

    assert manifest.kind == "acp-command"
    assert manifest.runtime_for().system_commands == ("opencode",)
    assert manifest.runtime_for().arguments == ("acp",)
    assert parse_package_manifest(manifest.to_dict()) == manifest


@pytest.mark.parametrize(
    "mutation,error",
    [
        ({"schemaVersion": 2}, "версия"),
        ({"id": "Invalid ID"}, "id"),
        ({"version": "latest"}, "version"),
        ({"homepage": "file:///tmp/agent"}, "HTTP"),
        (
            {
                "runtime": {
                    "any": {
                        "command": {"system": ["sh -c echo bad"]},
                        "args": [],
                    }
                }
            },
            "system",
        ),
        (
            {
                "runtime": {
                    "any": {
                        "command": {"system": ["agent"]},
                        "env": {"LD_PRELOAD": "/tmp/a.so"},
                    }
                }
            },
            "запрещена",
        ),
    ],
)
def test_manifest_rejects_unsafe_or_incompatible_values(mutation: dict, error: str) -> None:
    with pytest.raises(ManifestError, match=error):
        parse_package_manifest(command_manifest(**mutation))


def test_github_repository_normalization_is_restricted_to_repository_root() -> None:
    assert normalize_github_repository("VladosNX/OpenCode-Driver") == "VladosNX/OpenCode-Driver"
    assert (
        normalize_github_repository("https://github.com/VladosNX/OpenCode-Driver.git")
        == "VladosNX/OpenCode-Driver"
    )
    with pytest.raises(ValueError):
        normalize_github_repository("https://github.com/VladosNX/OpenCode-Driver/tree/main")


def test_github_source_reads_only_latest_release_manifest(qtbot) -> None:
    manifest_data = json_bytes(command_manifest())
    manifest_url = "https://github.com/example/driver/releases/download/v1/manifest.json"
    release = {
        "id": 44,
        "tag_name": "v1.0.0",
        "assets": [
            {
                "name": "codex-kostyl-agent.json",
                "size": len(manifest_data),
                "browser_download_url": manifest_url,
                "digest": f"sha256:{digest_bytes(manifest_data)}",
            }
        ],
    }

    class FakeHttp:
        def get(self, url, callback, **_kwargs) -> None:
            if url == manifest_url:
                callback(HttpResponse(manifest_data, 200, {}), "")
            else:
                callback(HttpResponse(json_bytes(release), 200, {}), "")

    source = GitHubReleaseSource(FakeHttp())  # type: ignore[arg-type]
    results = []

    source.preview(
        "https://github.com/example/driver",
        lambda value, error: results.append((value, error)),
    )

    candidate, error = results[0]
    assert error == ""
    assert candidate.source_ref == "example/driver"
    assert candidate.release_id == "44"
    assert candidate.manifest.name == "OpenCode"


def test_qt_response_headers_use_string_name_for_raw_header() -> None:
    class StringOnlyReply:
        def rawHeaderList(self):
            return [b"ETag", b"X-RateLimit-Remaining"]

        def rawHeader(self, name):
            assert isinstance(name, str)
            return {
                "ETag": b'"registry-version"',
                "X-RateLimit-Remaining": b"0",
            }[name]

    assert _response_headers(StringOnlyReply()) == {  # type: ignore[arg-type]
        "etag": '"registry-version"',
        "x-ratelimit-remaining": "0",
    }


def test_acp_registry_entry_becomes_local_command_recipe() -> None:
    raw = {
        "version": "1.0.0",
        "agents": [
            {
                "id": "opencode",
                "name": "OpenCode",
                "version": "1.18.10",
                "description": "The open source coding agent",
                "website": "https://opencode.ai",
                "distribution": {
                    "binary": {
                        current_platform_target(): {
                            "archive": "https://example.invalid/opencode.zip",
                            "cmd": "./dist/opencode",
                            "args": ["acp"],
                        }
                    }
                },
            }
        ],
    }

    candidates = parse_acp_registry(raw)

    assert len(candidates) == 1
    assert candidates[0].source_kind == "acp-registry"
    runtime = candidates[0].manifest.runtime_for()
    assert runtime.system_commands == ("opencode",)
    assert runtime.arguments == ("acp",)


def test_npx_registry_entry_requires_user_selected_executable() -> None:
    candidates = parse_acp_registry(
        {
            "version": "1.0.0",
            "agents": [
                {
                    "id": "sample",
                    "name": "Sample",
                    "version": "1.0.0",
                    "description": "Sample agent",
                    "repository": "https://github.com/example/sample",
                    "distribution": {"npx": {"package": "sample@1.0.0", "args": ["--acp"]}},
                }
            ],
        }
    )

    runtime = candidates[0].manifest.runtime_for()
    assert runtime.configuration_required is True
    assert runtime.system_commands == ()
    assert parse_package_manifest(candidates[0].manifest.to_dict()).runtime_for().configuration_required


def test_store_installs_adapter_and_restores_record(tmp_path) -> None:
    archive = adapter_archive()
    manifest = parse_package_manifest(adapter_manifest(archive))
    candidate = IntegrationCandidate(
        manifest,
        "github",
        "example/adapter",
        "42",
        "v1.0.0",
        artifact_url="https://example.invalid/adapter.zip",
    )
    store = IntegrationStore(tmp_path / "integrations")

    installed = store.install(candidate, archive)

    entrypoint = store.payload_path(installed, "bin/adapter")
    assert entrypoint.is_file()
    if os.name != "nt":
        assert os.access(entrypoint, os.X_OK)
    restored = store.load_all()
    assert restored == [installed]


def test_failed_adapter_update_keeps_previous_version(tmp_path) -> None:
    archive = adapter_archive()
    store = IntegrationStore(tmp_path / "integrations")
    first_manifest = parse_package_manifest(adapter_manifest(archive, version="1.0.0"))
    first = IntegrationCandidate(first_manifest, "github", "example/adapter", "1", "v1")
    store.install(first, archive)
    second_manifest = parse_package_manifest(adapter_manifest(archive, version="2.0.0"))
    second = IntegrationCandidate(second_manifest, "github", "example/adapter", "2", "v2")

    with pytest.raises(IntegrationStoreError, match="SHA-256"):
        store.install(second, b"not the declared archive")

    assert store.load_all()[0].manifest.version == "1.0.0"


def test_zip_extraction_rejects_traversal_and_symlink(tmp_path) -> None:
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape", b"bad")
    with pytest.raises(IntegrationStoreError, match="выйти"):
        safe_extract_zip(traversal.getvalue(), tmp_path / "traversal")

    symlink = io.BytesIO()
    with zipfile.ZipFile(symlink, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(IntegrationStoreError, match="Символические"):
        safe_extract_zip(symlink.getvalue(), tmp_path / "symlink")


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def agent_get(self, profile_id: str, key: str, default: str = "") -> str:
        return self.values.get((profile_id, key), default)

    def agent_set(self, profile_id: str, key: str, value: object) -> None:
        self.values[(profile_id, key)] = str(value)


class StubSource:
    def fetch(self, callback) -> None:
        callback([], "")

    def preview(self, _repository, callback) -> None:
        callback(None, "not implemented")

    def download_artifact(self, _candidate, callback) -> None:
        callback(None, "not implemented")


def test_manager_keeps_missing_cli_installed_and_enables_user_override(tmp_path) -> None:
    executable = tmp_path / "opencode"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    registry = AgentRegistry()
    registry.register_driver(acp_driver_registration())
    settings = MemorySettings()
    store = IntegrationStore(tmp_path / "integrations")
    stub = StubSource()
    manager = AgentIntegrationManager(
        registry,
        settings,
        store,
        stub,  # type: ignore[arg-type]
        stub,  # type: ignore[arg-type]
    )
    candidate = IntegrationCandidate(
        parse_package_manifest(
            command_manifest(
                runtime={
                    "any": {
                        "command": {"system": ["codex-kostyl-test-missing-cli"]},
                        "args": ["acp"],
                    }
                }
            )
        ),
        "github",
        "example/opencode-driver",
        "1",
        "v1.0.0",
    )
    results = []

    manager.install(candidate, lambda value, error: results.append((value, error)))

    installed = results[0][0]
    assert results[0][1] == ""
    assert manager.status(installed).code == "missing-cli"
    assert registry.profile(installed.profile_id) is not None
    assert registry.probe(installed.profile_id).available is False
    assert "Install OpenCode CLI" in registry.probe(installed.profile_id).error

    status = manager.set_executable(installed.profile_id, str(executable))

    assert status.available is True
    assert registry.profile(installed.profile_id).executable == str(executable)


def test_current_platform_uses_official_acp_target_names() -> None:
    assert current_platform_target("Linux", "amd64") == "linux-x86_64"
    assert current_platform_target("Darwin", "arm64") == "darwin-aarch64"
    assert current_platform_target("Windows", "aarch64") == "windows-aarch64"


def test_acp_profile_environment_is_passed_without_a_shell() -> None:
    profile = AgentProfile(
        "environment-test",
        "acp",
        "Environment Test",
        "agent",
        ("--acp",),
        environment=(("AGENT_TEST_VALUE", "configured"),),
    )

    driver = AcpDriver.create(profile)

    assert driver.process.program == "agent"
    assert driver.process.arguments == ["--acp"]
    assert (
        driver.process.process.processEnvironment().value("AGENT_TEST_VALUE")
        == "configured"
    )


def test_agents_settings_page_lists_built_in_and_installed_profiles(qtbot, tmp_path) -> None:
    registry = AgentRegistry()
    registry.register_driver(codex_driver_registration())
    registry.register_driver(acp_driver_registration())
    registry.add_profile(codex_profile())
    settings = MemorySettings()
    stub = StubSource()
    manager = AgentIntegrationManager(
        registry,
        settings,
        IntegrationStore(tmp_path / "integrations"),
        stub,  # type: ignore[arg-type]
        stub,  # type: ignore[arg-type]
    )
    candidate = IntegrationCandidate(
        parse_package_manifest(
            command_manifest(
                runtime={
                    "any": {
                        "command": {"system": ["codex-kostyl-test-missing-cli"]},
                        "args": ["acp"],
                    }
                }
            )
        ),
        "github",
        "example/opencode-driver",
    )
    manager.install(candidate, lambda _value, _error: None)
    controller = AgentController(registry, settings)
    dialog = AgentSettingsDialog(controller, settings, manager)  # type: ignore[arg-type]
    qtbot.addWidget(dialog)

    rows = [dialog.agent_list.item(index).text() for index in range(dialog.agent_list.count())]

    assert len(rows) == 2
    assert any("Codex" in row and "Встроенный" in row for row in rows)
    assert any("OpenCode" in row and "требует настройки" in row for row in rows)
