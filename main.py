"""Application entry point.

Opens the main menu. Every other window is launched from there as its own
process, so this script's only job is to show the menu and run the event loop.
"""

import os
import sys

# main.py sits at the project root, which is what the shared package needs on
# sys.path before it can be imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.environment import setup_environment

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mainwindow import MainWindow


if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
