"""Main menu window: the entry point into the rest of the app.

Two destinations. "Editor" asks the user for a source video with a native file
dialog and opens the Segment Scanner on it — the pre-process phase of the Editing
Wizard, which detects clip boundaries and then hands off to the editor itself, so
scanner and editor are one journey rather than two menu items. A source video can
be anywhere on disk: there is no folder it has to be in, which is what freed
``import/`` to be the Library Importer's staging folder instead. "Import" opens the
Library Mesh Wizard over that folder. "Settings" opens the standalone scheme editor.

Every window in the app is in this process, and exactly one of them is on screen
at a time. This one is the shell's (:mod:`shared.session`) starting point: it is
hidden while another window is up and shown again when that window closes, so
there is never a second menu or a second taskbar entry. It never waits for or
observes the other windows; it only opens one, and closing this one ends the app.

The three things that used to be different here, and are worth not reintroducing:
children were separate processes; this window stayed open behind them; and there
was a bespoke picker window standing between this button and the scanner.
Constructing an mpv player while another top-level window is foreground was
believed to deadlock on Windows, which is what the processes were for; that hazard
was tested directly and did not reproduce (see
``experiments/mpv_foreground/``). Leaving a window permanently behind another one
is the second cost — see ``docs/architecture.md`` for why the process boundary and
the background menu both came out, and for why the picker did too.

The file dialog is a modal dialog owned by this window, which is deliberately
outside the shell's business: it tracks the one visible *window*, and the
editor's own modal dialogs are on the same footing.
"""

import os
import sys

# This file sits at the project root, so that is what goes on sys.path; the
# subdirectory scripts each step up a level to reach the same place.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.diagnostics import log
from shared.environment import resource_path, setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox

from shared.session import shell
from shared.sources import VIDEO_EXTENSIONS, validate_source_video
from shared.ui_loader import UiLoader


def video_name_filter():
    """The file dialog's filter, built from the one list of video extensions.

    Generated rather than typed so the filter and
    `shared.sources.is_video_file` cannot drift: a container added to one and
    missed in the other is invisible in the dialog and refused after the user
    picks it, which is the worse of the two failures.
    """
    patterns = " ".join(f"*{extension}" for extension in sorted(VIDEO_EXTENSIONS))
    return f"Videos ({patterns});;All files (*)"


def choose_source_video(parent=None):
    """Ask the user for a source video. Returns its path, or None if cancelled.

    A module-level function rather than a method so a test can stand in for the
    one thing a native dialog cannot do: be answered. Everything a real answer
    leads to -- validation, refusing an unusable folder, opening the scanner --
    then runs for real.

    The dialog is an instance and not `QFileDialog.getOpenFileName` for the same
    reason, and it is left **native**: the OS dialog is better than Qt's, and it
    is what remembers the last folder the user was in. That is why nothing here
    persists a start directory -- adding one would mean a new global state store
    for a courtesy the platform already provides.

    Cancelling returns None and the caller does nothing at all, which leaves the
    menu exactly where it was.
    """
    dialog = QFileDialog(parent, "Choose a source video")
    dialog.setFileMode(QFileDialog.ExistingFile)
    dialog.setAcceptMode(QFileDialog.AcceptOpen)
    dialog.setNameFilter(video_name_filter())
    if not dialog.exec():
        return None
    chosen = dialog.selectedFiles()
    return chosen[0] if chosen else None


class MainWindow(QMainWindow):
    """Landing window: the source video for the wizard, or the settings window."""

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
        self.ui.importButton.clicked.connect(self.open_import)
        self.ui.settingsButton.clicked.connect(self.open_settings)

    def open_editor(self):
        """Ask for a source video and open the scanner on it.

        The scanner is the pre-process phase of the Editing Wizard — it detects
        clip boundaries and hands off to the editor itself — so this button leads
        into scanner-then-editor rather than opening the editor directly.

        A cancelled dialog opens nothing and leaves this menu up. A path
        `validate_source_video` refuses is reported rather than opened, because
        the interesting refusals here are a folder that cannot take the `.cmct`
        and a file that is not a video, and both are far clearer before the
        scanner is built than after.
        """
        chosen = choose_source_video(self)
        if not chosen:
            return
        try:
            source = validate_source_video(chosen)
        except (ValueError, FileNotFoundError) as error:
            log(f"the chosen source was refused: {error}")
            QMessageBox.warning(self, "That video cannot be opened", str(error))
            return
        shell().open_safely('scanner', source=source)

    def open_import(self):
        """Open the Library Mesh Wizard.

        The Wizard is the first half of importing somebody else's finished clips:
        it turns the folder names in `import/` into tags. It is standalone for now —
        nothing it produces is imported yet, because the Manual Edit queue and the
        export are not built.
        """
        shell().open_safely('mesh')

    def open_settings(self):
        """Open the standalone file and folder scheme settings window."""
        shell().open_safely('settings')
