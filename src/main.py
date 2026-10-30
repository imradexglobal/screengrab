"""Screen Monitor Application - Main entry point.

A professional Windows desktop application that continuously monitors the
screen, detects numbers displayed anywhere on the screen or within user-
selected screen regions, and saves valid, unique numeric readings into a
local SQLite database file named screen_monitor.db.
"""

import logging
import os
import sys
from pathlib import Path

# Ensure the repository root is in sys.path so 'src' can be imported
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from PySide6.QtWidgets import QApplication
from src.ui.main_window import MainWindow

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("ScreenMonitor")


def main():
    """Entry point for the Screen Monitor application."""
    logger.info("Launching Screen Monitor Application...")
    app = QApplication(sys.argv)
    app.setApplicationName("Screen Monitor")
    app.setApplicationVersion("2.0.0")
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    logger.info("Screen Monitor GUI window displayed.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
