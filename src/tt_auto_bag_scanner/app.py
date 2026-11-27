"""Desktop application entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from tt_auto_bag_scanner.application.app_controller import AppController
from tt_auto_bag_scanner.ui.main_window import MainWindow


def main() -> int:
    """Start the operator UI; construction remains side-effect free on import."""
    application = QApplication(sys.argv)
    application.setApplicationName("TT Auto Bag Scanner")
    application.setOrganizationName("TT Auto Bag Scanner")

    window = MainWindow()
    controller = AppController(window)
    application.aboutToQuit.connect(controller.close)
    window.show()
    return int(application.exec())
