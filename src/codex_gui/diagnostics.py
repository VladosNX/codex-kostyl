from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QStandardPaths


def configure_diagnostics() -> logging.Logger:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    log_dir = Path(location) / "logs"
    logger = logging.getLogger("codex_gui")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = RotatingFileHandler(
                log_dir / "codex-kostyl.log",
                maxBytes=512_000,
                backupCount=2,
                encoding="utf-8",
            )
        except OSError:
            # Diagnostics must never prevent the GUI from starting on a
            # read-only home directory or a broken XDG data path.
            handler = logging.NullHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger
