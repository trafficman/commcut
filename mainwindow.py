"""Main menu window: the entry point into the rest of the app.

Two destinations for now. "Editor" launches the Segment Scanner, which is the
pre-process phase of the Editing Wizard: it detects clip boundaries in a source
video and then hands off to the editor, so the two are one journey rather than
two menu items. "Settings" opens the standalone scheme editor.

Children are launched as separate processes, matching the scanner's own handoff
to the editor. That keeps this window alive in the background -- it does not
wait for or observe them -- and it means each window owns its own mpv instance
and Qt event loop, which matters on Windows where constructing an mpv player
while another top-level window is foreground can deadlock.
"""

import os
import subprocess
import sys

# This file sits at the project root, so that is what goes on sys.path; the
# subdirectory scripts each step up a level to reach the same place.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.environment import setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QMainWindow, QMessageBox

from shared.ui_loader import UiLoader


def _launch(relative_path):
    """Start a project script as its own process and return the Popen handle.

    The child is deliberately not waited on: this window stays open and
    usable while the launched window works.
    """
    script = os.path.join(PROJECT_ROOT, relative_path)
    if not os.path.exists(script):
        raise FileNotFoundError(f"Could not find {relative_path} at {script}")
    return subprocess.Popen([sys.executable, script])


class MainWindow(QMainWindow):
    """Landing window that opens the scanner or the settings window."""

    def __init__(self):
        super().__init__()

        ui_file = QFile(os.path.join(SCRIPT_DIR, "mainwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            print("Failed to open UI File")
            sys.exit(-1)
        try:
            self.ui = UiLoader().load(ui_file, self)
        finally:
            ui_file.close()
        if self.ui is None:
            raise RuntimeError("Failed to load main window UI")
        self.setCentralWidget(self.ui)
        self.setWindowTitle(self.ui.windowTitle())

        self.ui.editorButton.clicked.connect(self.open_editor)
        self.ui.settingsButton.clicked.connect(self.open_settings)

    def _open(self, relative_path, title):
        """Launch a child window, reporting any failure instead of crashing."""
        try:
            _launch(relative_path)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, f"{title} could not start", str(error))

    def open_editor(self):
        """Open the segment scanner, which leads into the video editor."""
        self._open(os.path.join("scanner", "scanner.py"), "Editor")

    def open_settings(self):
        """Open the standalone file and folder scheme settings window."""
        self._open(os.path.join("settings", "settings.py"), "Settings")
