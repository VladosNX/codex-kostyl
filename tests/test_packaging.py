import re
import subprocess
import sys
import tomllib
from importlib.resources import files
from pathlib import Path

from codex_gui import __version__


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_desktop_entry_has_required_linux_integration_fields() -> None:
    desktop = (PROJECT_ROOT / "packaging/codex-kostyl.desktop").read_text(encoding="utf-8")
    assert desktop.startswith("[Desktop Entry]\n")
    assert "Type=Application\n" in desktop
    assert "Exec=/usr/local/bin/codex-kostyl\n" in desktop
    assert "Icon=codex-kostyl\n" in desktop
    assert "Terminal=false\n" in desktop


def test_application_icon_is_in_python_package() -> None:
    icon = files("codex_gui").joinpath("assets/codex-kostyl.svg")
    assert icon.is_file()


def test_agent_package_schema_is_in_python_package() -> None:
    schema = files("codex_gui").joinpath("assets/agent-package.schema.json")
    assert schema.is_file()


def test_application_version_has_one_packaging_source() -> None:
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", __version__)
    assert metadata["project"]["dynamic"] == ["version"]
    assert metadata["tool"]["hatch"]["version"]["path"] == (
        "src/codex_gui/_version.py"
    )


def test_version_flag_does_not_require_a_display() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "codex_gui", "--version"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == f"Codex Kostyl {__version__}"


def test_remote_installer_is_separate_and_shell_valid() -> None:
    remote_installer = PROJECT_ROOT / "install.sh"
    local_installer = PROJECT_ROOT / "scripts" / "install.sh"
    contents = remote_installer.read_text(encoding="utf-8")

    assert remote_installer.stat().st_mode & 0o111
    assert "releases/latest" in contents
    assert 'bash "$project_root/scripts/install.sh"' in contents
    subprocess.run(["bash", "-n", remote_installer], check=True)
    subprocess.run(["bash", "-n", local_installer], check=True)


def test_remote_installer_help_is_available_without_network() -> None:
    result = subprocess.run(
        ["bash", PROJECT_ROOT / "install.sh", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--version VERSION" in result.stdout
