"""Installable ACP integrations and isolated adapter packages."""

from .manager import AgentIntegrationManager
from .models import (
    AgentPackageManifest,
    InstalledIntegration,
    IntegrationCandidate,
    IntegrationStatus,
    ManifestError,
    RuntimeSpec,
    current_platform_target,
    parse_package_manifest,
)
from .sources import (
    AcpRegistrySource,
    GitHubReleaseSource,
    HttpClient,
    normalize_github_repository,
    parse_acp_registry,
)
from .store import IntegrationStore, IntegrationStoreError, safe_extract_zip

__all__ = [
    "AcpRegistrySource",
    "AgentIntegrationManager",
    "AgentPackageManifest",
    "GitHubReleaseSource",
    "HttpClient",
    "InstalledIntegration",
    "IntegrationCandidate",
    "IntegrationStatus",
    "IntegrationStore",
    "IntegrationStoreError",
    "ManifestError",
    "RuntimeSpec",
    "current_platform_target",
    "normalize_github_repository",
    "parse_acp_registry",
    "parse_package_manifest",
    "safe_extract_zip",
]
