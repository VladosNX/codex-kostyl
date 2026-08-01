from __future__ import annotations

import sys
from importlib.resources import files

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .agents import AgentController, AgentRegistry
from .agents.acp import acp_driver_registration
from .agents.codex import (
    CodexDriver,
    codex_driver_registration,
    codex_profile,
)
from .main_window import MainWindow
from .diagnostics import configure_diagnostics
from .settings import AppSettings

STYLE = """
QWidget { background: #111315; color: #e8e8e6; font-size: 13px; selection-background-color: #4b5650; selection-color: #fff; }
QLabel { background: transparent; }
QMainWindow, #chatPanel, #timeline, #timelineColumn, #conversationScroll, #conversationScroll > QWidget > QWidget { background: #111315; }
QSplitter::handle { background: #2a2d2d; }

#sidebar { background: #181a1b; border-right: 1px solid #292c2c; min-width: 238px; max-width: 330px; }
#brandMark { color: #f1f2ee; font-size: 19px; }
#brandName { color: #f2f2ef; font-size: 14px; font-weight: 700; letter-spacing: 2px; }
#sectionLabel { color: #969c97; font-size: 11px; font-weight: 700; padding: 10px 8px 1px 8px; }
#threadSearch { background: #202323; color: #e7e9e6; border: 1px solid #353a38; border-radius: 8px; padding: 7px 9px; }
#threadSearch:focus { border-color: #69736d; }
#projectCombo { background: transparent; border: 0; border-radius: 7px; padding: 7px 8px; font-weight: 600; }
#projectCombo:hover, #projectCombo:on { background: #242727; }
#projectCombo::drop-down, #optionCombo::drop-down, #accessCombo::drop-down { border: 0; width: 18px; }
#projectBubble { background: #252827; border: 1px solid #363b38; border-radius: 12px; }
#projectBubbleIcon { color: #8f9892; font-size: 14px; }
#projectBubbleButton { background: transparent; border: 0; border-radius: 10px; color: #aeb4af; padding: 0; font-size: 15px; }
#projectBubbleButton:hover { background: #363b38; color: #fff; }
#iconButton, #attachButton { background: transparent; border: 0; border-radius: 7px; color: #a9ada8; font-size: 18px; padding: 0; }
#iconButton:hover, #attachButton:hover { background: #2a2d2d; color: #fff; }
#newChatButton { background: #ededeb; color: #171918; border: 0; border-radius: 8px; padding: 9px 12px; text-align: left; font-weight: 650; }
#newChatButton:hover { background: #fff; }
#newChatButton:pressed { background: #d7d8d4; }
#newChatButton:disabled { background: #2a2c2c; color: #6d716e; }
#threadList { background: transparent; border: 0; outline: 0; padding: 0; }
#threadList::item { color: #b8bbb7; border: 0; border-radius: 7px; padding: 7px 9px; }
#threadList::item:hover { background: #222525; color: #eeeeeb; }
#threadList::item:selected { background: #2a2d2c; color: #fff; }
#threadList:disabled { color: #666a66; }
#accountButton { background: transparent; color: #c0c3be; border: 0; border-top: 1px solid #292c2c; border-radius: 0; padding: 13px 6px 5px 6px; text-align: left; }
#accountButton:hover { color: #fff; }

#topbar { background: #111315; border-bottom: 1px solid #252828; }
#chatTitle { color: #f3f3f0; font-size: 14px; font-weight: 650; }
#chatContext { color: #969c97; font-size: 11px; }
#sidebarToggle, #settingsButton { background: transparent; border: 0; border-radius: 7px; padding: 4px; }
#sidebarToggle:hover, #settingsButton:hover { background: #292d2b; }
#readyStatus { color: #86a992; background: #18231d; border: 1px solid #283c30; border-radius: 10px; padding: 4px 9px; font-size: 11px; }
#readyStatus[active="true"] { color: #d3b778; background: #282318; border-color: #493d25; }
#contextUsage { background: transparent; }
#contextUsageLabel { color: #a1a7a2; font-size: 11px; }
#contextUsageBar { background: #303432; border: 0; border-radius: 2px; }
#contextUsageBar::chunk { background: #789b83; border-radius: 2px; }
#contextUsageBar[level="warning"]::chunk { background: #b69a5c; }
#contextUsageBar[level="danger"]::chunk { background: #b96767; }

#composerShell { background: #111315; }
#composerArea { background: transparent; }
#composerPanel { background: #1b1e1f; border: 1px solid #3a3e3d; border-radius: 14px; }
#composerPanel[editingQueue="true"] { border: 1px solid #6c5b36; background: #1d1d1a; }
#composer { background: transparent; color: #eeeeeb; border: 0; padding: 7px 5px; font-size: 14px; }
#composer:disabled { color: #747874; }
#composerHint { color: #8d938e; font-size: 11px; padding-top: 3px; }
#noticeBanner { background: #1b2420; border: 1px solid #365043; border-radius: 9px; }
#noticeBanner[level="warning"] { background: #292319; border-color: #675735; }
#noticeBanner[level="error"] { background: #2b1d1d; border-color: #69403d; }
#noticeLabel { color: #c9d5ce; font-size: 12px; }
#queueEditBanner { background: #292419; border: 1px solid #5f5132; border-radius: 9px; }
#queueEditIcon { color: #d8bc79; font-size: 16px; }
#queueEditTitle { color: #e3c989; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
#queueEditDetail { color: #b9b09b; font-size: 11px; }
#queueEditCancel { background: transparent; color: #c6bead; border: 1px solid #554e3e; padding: 5px 9px; }
#queueEditCancel:hover { background: #363023; color: #fff5df; }
#slashCommandPanel { background: #1b1e1d; border: 1px solid #3a403c; border-radius: 11px; }
#slashCommandList { background: transparent; border: 0; outline: 0; }
#slashCommandList::item { color: #aeb5b0; border: 0; border-radius: 7px; padding: 5px 9px; }
#slashCommandList::item:selected { background: #303632; color: #f2f3ef; }
#slashCommandList::item:disabled { color: #666b67; background: transparent; }
#optionCombo { background: transparent; color: #9ca19c; border: 0; border-radius: 6px; padding: 5px 7px; min-height: 20px; font-size: 11px; }
#optionCombo:hover, #optionCombo:on { background: #292c2c; color: #e8e8e5; }
#accessCombo { color: #d4ddd7; background: #202a24; border: 1px solid #385044; border-radius: 8px; padding: 5px 8px; min-height: 20px; font-size: 11px; }
#accessCombo[mode="safe"] { color: #b8d8c2; background: #18251e; border-color: #31503c; }
#accessCombo[mode="workspace"] { color: #d4ddd7; background: #202a24; border-color: #385044; }
#accessCombo[mode="plan"] { color: #d2c9eb; background: #251f32; border-color: #51436d; }
#accessCombo[mode="danger"] { color: #efb8b2; background: #301e1e; border-color: #70413d; }
#accessCombo[nextTurn="true"] { border-style: dashed; }
#weeklyLimit { background: transparent; }
#weeklyLimitLabel { color: #a1a6a1; font-size: 11px; }
#weeklyLimitProgress { background: #303432; border: 0; border-radius: 2px; }
#weeklyLimitProgress::chunk { background: #789b83; border-radius: 2px; }
#weeklyLimitProgress[level="warning"]::chunk { background: #b69a5c; }
#weeklyLimitProgress[level="danger"]::chunk { background: #b96767; }
#weeklyLimitProgress[level="unavailable"]::chunk { background: #4b504d; }
#sendButton { background: #eeeeeb; color: #171918; border: 0; border-radius: 17px; padding: 0; font-size: 19px; font-weight: 700; }
#sendButton:hover { background: #fff; }
#sendButton:pressed { background: #cfd1cc; }
#stopButton { background: #e8e8e5; color: #1b1d1c; border: 0; border-radius: 17px; padding: 0; font-size: 12px; }
#stopButton:hover { background: #fff; }

#approvalCard { background: #211f19; border: 1px solid #554a31; border-radius: 12px; }
#approvalIcon { background: #d2b36d; color: #1c1a16; border-radius: 12px; font-weight: 800; }
#approvalTitle { color: #f0e8d5; font-size: 13px; font-weight: 650; }
#approvalDetail { color: #c4bca9; font-size: 12px; padding-left: 31px; }
#approvalPrimaryButton { background: #e8e2d4; color: #211f1a; border: 0; font-weight: 650; }
#approvalPrimaryButton:hover { background: #fffaf0; }
#approvalSecondaryButton { background: transparent; color: #c6bead; border: 1px solid #554e3e; }
#approvalSecondaryButton:hover { background: #302c23; color: #f1eadc; }
#approvalDangerButton { background: transparent; color: #c98b83; border: 1px solid #63433e; }
#approvalDangerButton:hover { background: #3a2523; color: #efaaa0; }

#userCard { background: #252827; border: 1px solid #343837; border-radius: 13px; }
#agentCard { background: transparent; border: 0; }
#userCard QTextBrowser, #agentCard QTextBrowser { background: transparent; border: 0; padding: 0; }
#messageActionButton { background: transparent; border: 0; color: #747a76; padding: 0; font-size: 15px; }
#messageActionButton:hover { color: #e5e7e3; background: #303432; border-radius: 5px; }
#messageActionButton:focus { border: 1px solid #778079; }
#scrollDownButton { background: #292d2b; color: #e6e9e5; border: 1px solid #464d48; border-radius: 19px; padding: 0; font-size: 20px; font-weight: 650; }
#scrollDownButton:hover { background: #383e3a; border-color: #68716b; }
#thinkingIndicator { background: transparent; border: 0; }
#thinkingLabel { color: #8d9891; font-size: 12px; font-style: italic; padding: 4px; }
#turnDuration { color: #929b94; font-size: 11px; padding: 1px 5px 5px 5px; }
#turnDuration[result="failed"] { color: #b77b76; }
#turnDuration[result="stopped"] { color: #a18e68; }
#activityGroup { background: #171919; border: 1px solid #292d2b; border-radius: 9px; }
#activityGroupTitle { background: transparent; border: 0; color: #959c96; font-size: 10px; font-weight: 700; letter-spacing: 1px; padding: 10px 13px 8px 13px; text-align: left; }
#activityGroupTitle:hover { color: #d7dad6; background: #1e2120; }
#activityGroupItems { background: transparent; }
#activityCard { background: transparent; border: 0; border-top: 1px solid #292d2b; border-radius: 0; }
#activityCard QTextBrowser { background: #111313; border: 0; border-radius: 6px; padding: 6px; }
#activityToggle { background: transparent; border: 0; color: #9da29d; padding: 3px; }
#activityToggle:hover { color: #e5e7e3; }
#executionPlanCard { background: #181c1a; border: 1px solid #303c35; border-radius: 10px; }
#executionPlanTitle { color: #9eafa4; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
#executionPlanExplanation { color: #aeb6b0; font-size: 11px; }
#executionPlanStep { color: #9da49f; font-size: 12px; padding: 2px 0; }
#executionPlanStep[status="inProgress"] { color: #e1d09c; font-weight: 650; }
#executionPlanStep[status="completed"] { color: #819c89; }
#emptyHint { background: transparent; border: 0; }
#emptyGlyph { color: #8e9d94; font-size: 30px; }
#emptyTitle { color: #e9e9e6; font-size: 23px; font-weight: 600; }
#emptyDescription { color: #a0a5a0; font-size: 13px; }
#emptyStarterTitle { color: #929993; font-size: 10px; font-weight: 700; letter-spacing: 1px; padding-top: 8px; }
#starterButton { background: #1b1e1d; color: #d1d5d1; border: 1px solid #303532; padding: 8px 12px; text-align: left; }
#starterButton:hover { background: #252a27; border-color: #47504a; color: #fff; }
#historyNotice { color: #8b918c; background: #181b1a; border: 1px solid #2c302e; border-radius: 8px; padding: 8px 12px; }
#queuePanel { background: #1b1e1d; border: 1px solid #303532; border-radius: 10px; }
#queueTitle { color: #a8aea9; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
#queueActionButton { background: transparent; border: 0; color: #9ca19d; padding: 3px 7px; font-size: 10px; }
#queueActionButton:hover { color: #fff; background: #303432; }
#queueItem { background: #202422; border: 1px solid #303633; border-radius: 8px; }
#queueItem[editing="true"] { background: #29261e; border-color: #665735; }
#queueItemIndex { color: #aeb6b0; background: #303632; border-radius: 11px; font-size: 10px; font-weight: 700; }
#queueItemText { color: #d0d5d1; font-size: 12px; }
#queueItemAction { background: transparent; border: 0; border-radius: 6px; padding: 4px; }
#queueItemAction:hover { background: #343a36; }
#questionCard, #planConfirmationCard { background: #1d221f; border: 1px solid #3c5143; border-radius: 12px; }
#questionTitle, #planConfirmationTitle { color: #e8eee9; font-size: 13px; font-weight: 650; }
#questionPrompt, #planConfirmationDetail { color: #b3bbb5; font-size: 12px; }
#questionError { color: #d98f87; font-size: 11px; }
#questionOptions, #questionAnswer { background: #252a27; border: 1px solid #3b443e; border-radius: 7px; padding: 7px; }

QLineEdit, QDialog QTextEdit { background: #202323; border: 1px solid #3a3e3d; border-radius: 8px; padding: 8px; }
QLineEdit:focus, QDialog QTextEdit:focus { border-color: #747b76; }
QPushButton { background: #303433; border: 1px solid #414644; border-radius: 7px; padding: 7px 12px; }
QPushButton:hover { background: #3a3f3d; border-color: #555b58; }
QPushButton:disabled { color: #6e726e; background: #242726; border-color: #303331; }
QComboBox QAbstractItemView, QMenu { background: #242727; color: #e8e8e5; border: 1px solid #414543; border-radius: 8px; padding: 4px; outline: 0; selection-background-color: #3a3f3c; selection-color: #fff; }
#requestSettingsMenu { min-width: 210px; }
QMenu::item { padding: 7px 24px 7px 10px; border-radius: 5px; }
QMenu::item:selected { background: #3a3f3c; }
QMenu::separator { background: #3a3d3c; height: 1px; margin: 5px; }
QToolTip { background: #2a2d2c; color: #f1f1ee; border: 1px solid #494e4b; padding: 5px; }
QPushButton:focus, QToolButton:focus, QComboBox:focus { border: 1px solid #78827b; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #383c3a; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4b504d; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { height: 0; background: transparent; }
QStatusBar { background: #151717; color: #989f99; border-top: 1px solid #252828; font-size: 11px; }
QStatusBar::item { border: 0; }
"""


def codex_preflight() -> tuple[str | None, str | None]:
    """Compatibility wrapper around the Codex driver's availability probe."""
    availability = CodexDriver.check_availability()
    return (
        availability.executable if availability.available else None,
        None if availability.available else availability.error,
    )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Codex Kostyl")
    app.setApplicationDisplayName("Codex Kostyl")
    app.setDesktopFileName("codex-kostyl")
    app.setOrganizationName("CodexKostyl")
    app.setWindowIcon(QIcon(str(files("codex_gui").joinpath("assets/codex-kostyl.svg"))))
    app.setStyle("Fusion")
    font = QFont()
    font.setFamilies(["Inter", "Noto Sans", "DejaVu Sans"])
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(STYLE)
    logger = configure_diagnostics()

    settings = AppSettings()
    registry = AgentRegistry()
    registry.register_driver(codex_driver_registration())
    registry.register_driver(acp_driver_registration())
    registry.add_profile(codex_profile())
    for profile in settings.agent_profiles:
        try:
            registry.add_profile(profile)
        except ValueError as exc:
            logger.warning("Ignored invalid agent profile %s: %s", profile.id, exc)
    service = AgentController(registry, settings)
    window = MainWindow(service, settings, service.stop)
    service.errorOccurred.connect(
        lambda message: logger.error("Agent/protocol error: %s", message)
    )

    def server_stopped(code: int, status: str) -> None:
        name = service.descriptor.display_name or "Агент"
        logger.warning("agent stopped: id=%s code=%s status=%s", service.active_agent_id, code, status)
        if window._closing:
            return
        window.statusBar().showMessage(f"{name} остановлен ({code}, {status})")
        answer = QMessageBox.question(
            window,
            f"{name} остановлен",
            f"Перезапустить {name}? Сохранённые чаты не будут потеряны.",
            QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Close,
            QMessageBox.StandardButton.Retry,
        )
        if answer == QMessageBox.StandardButton.Retry:
            service.restart()
        else:
            window.close()

    service.processStopped.connect(server_stopped)
    window.show()
    selected_profile = settings.selected_agent_id
    if registry.profile(selected_profile) is None:
        selected_profile = "codex"
        settings.selected_agent_id = selected_profile
    service.activate(selected_profile)
    return app.exec()
