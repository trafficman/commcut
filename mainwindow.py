"""Main menu window: the entry point into the rest of the app.

Two destinations. "Editor" asks the user for a source video and opens the Segment
Scanner on it — the pre-process phase of the Editing Wizard, which detects clip
boundaries and then hands off to the editor itself, so scanner and editor are one
journey rather than two menu items. A source video can be anywhere on disk: there
is no folder it has to be in, which is what freed ``import/`` to be the Library
Importer's staging folder instead. "Import" opens the Untagged Library Mesh over
that folder. "Settings" opens the standalone scheme editor.

There are **two ways in** to the wizard, and one path out of both. The button asks
for a video with a native file dialog; a video *dropped on this window* opens the
same journey, which is the gesture somebody dragging a compilation out of a file
manager reaches for first. Both hand a path to :meth:`MainWindow.open_source`, so
the two cannot disagree about what may be opened or about where the scanner comes
from — the file dialog is an input, not the rule.

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

The drop is not that. It arrives as Qt's own drag events on this window, so it
needs no modal and no shell involvement at all — and it is confined to the menu,
which is the only window visible when nobody else is up.
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
from shared.sources import (
    VIDEO_EXTENSIONS,
    is_video_file,
    validate_source_video,
)
from shared.ui_loader import UiLoader, adopt_title


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


def dropped_source(mime_data):
    """The one path a drop should open, or None if it carries no local file.

    A drop can carry any number of files — dragging a folder's worth of rips out
    of a file manager is the obvious case — and the Editing Wizard works on one
    source at a time, so this picks **the first video** and ignores the rest. The
    log says how many videos came in, because a drop of twenty is otherwise a
    silent truncation and the person who made it is watching one window replace
    the menu.

    A drop with no video in it yields its first file anyway, and the caller
    hands *that* to `validate_source_video`, which refuses it by name. Ignoring
    it would leave somebody who dropped the wrong file with a gesture that
    visibly did nothing and no idea why; refusing it is what the file dialog
    would have done with the same file.

    URLs that are not local files — a link dragged out of a browser — are not
    paths this app can open and there is no video to compare, so they are
    dropped rather than refused.
    """
    paths = [
        url.toLocalFile()
        for url in mime_data.urls()
        if url.isLocalFile() and url.toLocalFile()
    ]
    if not paths:
        return None
    videos = [path for path in paths if is_video_file(path)]
    if len(videos) > 1:
        log(f"a drop carried {len(videos)} videos; opening the first and "
            f"ignoring the rest: {videos[0]}")
    return videos[0] if videos else paths[0]


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
        adopt_title(self, self.ui)
        self.setCentralWidget(self.ui)

        # The second way into the wizard: a video dropped on this window. Only
        # the window has to accept drops -- Qt hands a drag to the widget under
        # the cursor and, if it does not take it, up to its parent, so the
        # buttons and labels inside need nothing.
        self.setAcceptDrops(True)

        self.ui.editorButton.clicked.connect(self.open_editor)
        self.ui.importButton.clicked.connect(self.open_import)
        self.ui.settingsButton.clicked.connect(self.open_settings)

    def open_editor(self):
        """Ask for a source video and open the scanner on it.

        The scanner is the pre-process phase of the Editing Wizard — it detects
        clip boundaries and hands off to the editor itself — so this button leads
        into scanner-then-editor rather than opening the editor directly.

        A cancelled dialog opens nothing and leaves this menu up. Everything after
        the answer is shared with the drop, in `open_source`.
        """
        chosen = choose_source_video(self)
        if not chosen:
            return
        self.open_source(chosen)

    def open_source(self, path):
        """Validate a source video path and open the scanner on it.

        The one way a source video becomes a window, and both entry points — the
        file dialog and a drop — come through here. That is the point: what may be
        opened is decided once, and neither entry point can drift from the other
        or from the rule the scanner and the editor hold themselves to.

        A path `validate_source_video` refuses is reported rather than opened,
        because the interesting refusals here are a folder that cannot take the
        `.cmct` and a file that is not a video, and both are far clearer before
        the scanner is built than after. The menu stays up either way, so a
        refused drop is a second try rather than a restart.
        """
        try:
            source = validate_source_video(path)
        except (ValueError, FileNotFoundError) as error:
            log(f"the chosen source was refused: {error}")
            QMessageBox.warning(self, "That video cannot be opened", str(error))
            return
        shell().open_safely('scanner', source=source)

    def dragEnterEvent(self, event):
        """Offer to take a drag that carries a file, and only that.

        Judged on there being a local file at all, deliberately: the real check
        needs a filesystem probe of the folder for the `.cmct`, and this runs on
        every drag over the window, so the answer has to be cheap. It is also
        deliberately *not* judged on the extension, because the cursor cannot
        explain itself. A drag refused here produces no drop and therefore no
        message, so a mistyped container would be answered by nothing happening;
        accepting it here instead lets `open_source` refuse it by name, listing
        what is supported.
        """
        mime_data = event.mimeData()
        if any(url.isLocalFile() for url in mime_data.urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Open the scanner on a dropped video, exactly as the button would.

        The drop carries no modal dialog and needs nothing from the shell beyond
        the one `open_source` call, which is the whole difference from the dialog
        route and the reason it can be the quicker one.
        """
        chosen = dropped_source(event.mimeData())
        if chosen is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.open_source(chosen)

    def open_import(self):
        """Open the Untagged Library Mesh.

        **Import** opens the Library Importer's first window over `import/`: it
        turns the folder names in the untagged half of that folder into tags. It is
        also the whole importer's router — its worker already walks the folder, so a
        folder where every clip already has a record is handed on to the Tagged
        Library Mesh instead of being asked about folder names that are somebody
        install's rendered output.

        It is opened with no arguments, and there is deliberately nothing to
        choose here. `import/` is fixed beside the app because this importer
        *moves and deletes* from it: the folder it is allowed to empty stays
        inside the program root, where one mis-click cannot reach somebody's
        downloads folder. Where the library itself lives is the export folder in
        Settings, and that is configurable. See
        `shared/environment.py:import_folder` and
        [docs/importing.md](docs/importing.md).
        """
        shell().open_safely('mesh')

    def open_settings(self):
        """Open the standalone file and folder scheme settings window."""
        shell().open_safely('settings')
