from importlib.resources import files
from pathlib import Path


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
