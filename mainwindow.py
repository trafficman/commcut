"""Main menu window: the entry point into the rest of the app.

Three destinations for now. "Editor" opens the source picker, which lists the
videos in the app's import/ folder; the one chosen is handed to the Segment
Scanner, the pre-process phase of the Editing Wizard, which detects clip
boundaries and then hands off to the editor itself — so picker, scanner, and
editor are one journey rather than three menu items. "Settings" opens the
standalone scheme editor.

Every window in the app is in this process, and exactly one of them is on screen
at a time. This one is the shell's (:mod:`shared.session`) starting point: it is
hidden while another window is up and shown again when that window closes, so
there is never a second menu or a second taskbar entry. It never waits for or
observes the other windows; it only opens one, and closing this one ends the app.

The two things that used to be different here, and are worth not reintroducing:
children were separate processes, and this window stayed open behind them.
Constructing an mpv player while another top-level window is foreground was
believed to deadlock on Windows, which is what the processes were for; that
hazard was tested directly and did not reproduce (see
``experiments/mpv_foreground/``). Leaving a window permanently behind another one
is the second cost — see ``docs/architecture.md`` for why the process boundary and
the background menu both came out.
"""

import os
import sys

# This file sits at the project root, so that is what goes on sys.path; the
# subdirectory scripts each step up a level to reach the same place.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.environment import resource_path, setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QMainWindow

from shared.session import shell
from shared.ui_loader import UiLoader


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

    def open_editor(self):
        """Open the source picker, which leads into the scanner and editor."""
        shell().open_safely('picker')

    def open_settings(self):
        """Open the standalone file and folder scheme settings window."""
        shell().open_safely('settings')
