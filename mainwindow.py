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

"Launched as separate processes" is a property of the architecture, not of the
build: in a packaged app there are no .py files on disk, so launch_command()
re-executes the application binary with a --window flag. See
shared/environment.py.
"""

import os
import subprocess
import sys

# This file sits at the project root, so that is what goes on sys.path; the
# subdirectory scripts each step up a level to reach the same place.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.environment import (
    launch_command, resource_path, setup_environment,
)
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QMainWindow, QMessageBox

from shared.diagnostics import log, log_exception
from shared.ui_loader import UiLoader


def _launch(window_name):
    """Start a child window as its own process and return the Popen handle.

    The child is deliberately not waited on: this window stays open and
    usable while the launched window works.
    """
    command = launch_command(window_name)
    log(f"launching {window_name}: {command}")
    return subprocess.Popen(command)


class MainWindow(QMainWindow):
    """Landing window that opens the scanner or the settings window."""

    def __init__(self):
        super().__init__()

        ui_file = QFile(resource_path("mainwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            # Raised rather than printed: in a windowed packaged build a print
            # goes nowhere, and sys.exit(-1) from a constructor produces a
            # traceback with no explanation. Both used to be the failure mode.
            raise FileNotFoundError(
                f"Could not open the main window UI file: "
                f"{ui_file.fileName()}")
        try:
            self.ui = UiLoader().load(ui_file, self)
        finally:
            ui_file.close()
        if self.ui is None:
            raise RuntimeError(
                f"Failed to load the main window UI: {ui_file.fileName()}")
        self.setCentralWidget(self.ui)
        self.setWindowTitle(self.ui.windowTitle())

        self.ui.editorButton.clicked.connect(self.open_editor)
        self.ui.settingsButton.clicked.connect(self.open_settings)

    def _open(self, window_name, title):
        """Launch a child window, reporting any failure instead of crashing."""
        try:
            _launch(window_name)
        except (OSError, ValueError) as error:
            log_exception(f"could not launch the {title} window", error)
            QMessageBox.warning(self, f"{title} could not start", str(error))

    def open_editor(self):
        """Open the segment scanner, which leads into the video editor."""
        self._open('scanner', "Editor")

    def open_settings(self):
        """Open the standalone file and folder scheme settings window."""
        self._open('settings', "Settings")
