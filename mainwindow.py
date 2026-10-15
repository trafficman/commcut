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
which is the only window visible when nobody else is up. A drag the window
accepts is answered with a highlight — a tint and a dashed border on the
central widget, `DROP_HIGHLIGHT_STYLE` — for as long as it lasts, so the
gesture has a visible state between entering and releasing.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.diagnostics import log, log_exception
from shared.environment import import_folder, resource_path, setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox

from shared.loading import LoadingDialog
from shared.session import shell
from shared.sharing import (
    ZIP_EXTENSION,
    ZipImportError,
    extract_share_zip,
    has_record_entries,
)
from shared.sources import (
    VIDEO_EXTENSIONS,
    is_video_file,
    validate_source_video,
)
from shared.ui_loader import UiLoader, adopt_title


#: The highlight a drag over the menu puts on the central widget: a
#: translucent tint behind a dashed border, the standard "drop here"
#: affordance. The rule is scoped to the central widget's own object
#: name, because a stylesheet set bare on a widget cascades to
#: everything inside it -- an unscoped rule draws the dashed border
#: around every button and label, not around the window. Literal
#: colours rather than a palette role, because the point is to stand
#: out from whichever scheme the system is running.
DROP_HIGHLIGHT_STYLE = (
    "#MainWindow {"
    "background-color: rgba(60, 110, 220, 64);"
    "border: 2px dashed rgba(60, 110, 220, 220);"
    "border-radius: 6px;"
    "}"
)


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


def zip_name_filter():
    """The file dialog's filter for share zips."""
    return f"CommCut shares (*{ZIP_EXTENSION});;All files (*)"


def choose_share_zip(parent=None):
    """Ask the user for a share zip to import. Returns its path, or None if cancelled.

    A module-level function rather than a method so a test can stand in for the
    one thing a native dialog cannot do: be answered.
    """
    dialog = QFileDialog(parent, "Choose a share to import")
    dialog.setFileMode(QFileDialog.ExistingFile)
    dialog.setAcceptMode(QFileDialog.AcceptOpen)
    dialog.setNameFilter(zip_name_filter())
    if not dialog.exec():
        return None
    chosen = dialog.selectedFiles()
    return chosen[0] if chosen else None


def dropped_source(mime_data):
    """The one path a drop should open, as `(path, source_type)`, or None if no local file.

    `source_type` is `"video"` or `"zip"`, telling the caller how to route it.

    A drop can carry any number of files -- dragging a folder's worth of rips out
    of a file manager is the obvious case -- and the Editing Wizard works on one
    source at a time, so this picks the first acceptable file of the most
    specific kind and ignores the rest. A video drop is preferred over a zip: a
    video is the editor's input and a zip is the importer's. The log says how many
    of each came in, because a drop of twenty is otherwise a silent truncation
    and the person who made it is watching one window replace the menu.

    A drop with no video or zip in it yields its first file anyway, typed as a
    video, and the caller hands that to `validate_source_video`, which refuses it
    by name. Ignoring it would leave somebody who dropped the wrong file with a
    gesture that visibly did nothing and no idea why; refusing it is what the file
    dialog would have done with the same file.

    URLs that are not local files -- a link dragged out of a browser -- are not
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
    zips = [path for path in paths if path.lower().endswith(ZIP_EXTENSION)]
    if videos:
        if len(videos) > 1:
            log(f"a drop carried {len(videos)} videos; opening the first "
                f"and ignoring the rest: {videos[0]}")
        return (videos[0], "video")
    if zips:
        if len(zips) > 1:
            log(f"a drop carried {len(zips)} zips; importing the first "
                f"and ignoring the rest: {zips[0]}")
        return (zips[0], "zip")
    return (paths[0], "video")


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
        self._drop_highlight = False

        self.ui.editorButton.clicked.connect(self.open_editor)
        self.ui.importButton.clicked.connect(self.open_import)
        self.ui.settingsButton.clicked.connect(self.open_settings)
        self.ui.libraryButton.clicked.connect(self.open_library)
        self.ui.shareButton.clicked.connect(self.open_sharing)

        self._zip_cancel = None
        self._zip_import_worker = None
        self._zip_import_thread = None
        self._zip_import_dialog = None

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

        A drag that is offered the window is also offered the highlight, which
        stays up for as long as the drag does -- `dragLeaveEvent` and
        `dropEvent` are the two ways it comes down.
        """
        mime_data = event.mimeData()
        if any(url.isLocalFile() for url in mime_data.urls()):
            self._set_drop_highlight(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        """Take the drop highlight back when the drag leaves the window.

        The other half of the pair with `dragEnterEvent`. `dropEvent`
        clears it too, because a leave is not guaranteed on every way a
        drag can end -- a release outside the window, or a drag cancelled
        outright -- and a highlight that outlives its drag is a lie about
        what the window is being offered.
        """
        self._set_drop_highlight(False)
        event.accept()

    def _set_drop_highlight(self, active):
        """Show or hide the drop highlight, and only on a change.

        Idempotent, because the enter that sets it and the drop that
        clears it can both run for the same drag (a drop is always
        preceded by an enter), and because re-applying an unchanged
        stylesheet is work the drag's every move does not need.
        """
        if self._drop_highlight == active:
            return
        self._drop_highlight = active
        self.ui.setStyleSheet(DROP_HIGHLIGHT_STYLE if active else "")

    def dropEvent(self, event):
        """Open the scanner on a dropped video, or import a dropped zip.

        The drop carries no modal dialog and needs nothing from the shell beyond
        the one `open_source` call, which is the whole difference from the dialog
        route and the reason it can be the quicker one.

        The highlight comes off first, so the window is back to its resting
        look whatever the drop then does -- open, refuse, or ignore.
        """
        self._set_drop_highlight(False)
        chosen = dropped_source(event.mimeData())
        if chosen is None:
            event.ignore()
            return
        path, source_type = chosen
        event.acceptProposedAction()
        if source_type == "zip":
            self.open_share_zip(path)
        else:
            self.open_source(path)

    def open_import(self):
        """Open the Library Import, choosing between a folder and a zip.

        **Import** opens the Library Importer's first window over `import/`:
        it turns the folder names in the untagged half of that folder into tags.
        It is also the whole importer's router -- its worker already walks the
        folder, so a folder where every clip already has a record is handed on to
        the Tagged Library Mesh instead of being asked about folder names that
        are somebody install's rendered output.

        A share zip can also be dropped on the window, or chosen here: it is
        extracted into `import/` and then handed to the same mesh, so the
        importer's auto-routing decides whether it shows the Tagged or Untagged
        half. `import/` is fixed beside the app because this importer *moves and
        deletes* from it: the folder it is allowed to empty stays inside the
        program root, where one mis-click cannot reach somebody's downloads
        folder. Where the library itself lives is the export folder in Settings,
        and that is configurable. See
        `docs/importing.md <docs/importing.md>`_ and
        `shared/environment.py:import_folder`_.
        """
        shell().open_safely('mesh')

    def open_share_zip(self, zip_path):
        """Extract a share zip into `import/`, then open the mesh on it.

        A share is a zip of finished clips with their `.cnfo` records; extracting
        it into `import/` hands it off to the mesh, which auto-routes tagged and
        untagged clips. The extraction runs on a worker thread behind a
        non-modal, cancellable `LoadingDialog`, mirroring the share-write pattern;
        teardown hangs off `QThread.finished` (invariant 14).
        """
        if not has_record_entries(zip_path):
            QMessageBox.warning(
                self, "That is not a CommCut share",
                f"{os.path.basename(zip_path)} does not contain any "
                "clip records (.cnfo). It does not look like a CommCut "
                "library share.")
            return
        self._begin_zip_import(zip_path)

    def _begin_zip_import(self, zip_path):
        """Extract `zip_path` on a worker thread behind a dialog."""
        self._zip_cancel = threading.Event()
        worker = ZipImportWorker(
            zip_path, dest_root=import_folder(),
            cancel_event=self._zip_cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_zip_import_done)
        worker.failed.connect(self._on_zip_import_failed)
        worker.cancelled.connect(self._on_zip_import_cancelled)
        worker.progressed.connect(self._on_zip_import_progressed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_zip_import_stopped)
        self._zip_import_worker = worker
        self._zip_import_thread = thread

        dialog = LoadingDialog(
            "Extracting the share…", cancellable=True,
            modal=False, title="Library Import")
        dialog.set_range(0, 0)
        dialog.canceled.connect(self._on_zip_import_cancel_requested)
        dialog.show()
        self._zip_import_dialog = dialog

        thread.start()

    def _on_zip_import_cancel_requested(self):
        if self._zip_cancel is not None:
            self._zip_cancel.set()

    def _on_zip_import_progressed(self, done, total):
        dialog = self._zip_import_dialog
        if dialog is None:
            return
        if total > 0:
            dialog.set_range(0, total)
            dialog.set_value(done)

    def _on_zip_import_done(self):
        """The zip was extracted: open the mesh over `import/`."""
        dialog, self._zip_import_dialog = self._zip_import_dialog, None
        if dialog is not None:
            dialog.close()
        self._zip_cancel = None
        shell().open_safely('mesh')

    def _on_zip_import_failed(self, message):
        """The zip could not be imported: say why."""
        dialog, self._zip_import_dialog = self._zip_import_dialog, None
        if dialog is not None:
            dialog.close()
        self._zip_cancel = None
        QMessageBox.warning(
            self, "The share could not be imported",
            f"The share could not be extracted:\n{message}")

    def _on_zip_import_cancelled(self):
        """The user gave up: say nothing was extracted."""
        dialog, self._zip_import_dialog = self._zip_import_dialog, None
        if dialog is not None:
            dialog.close()
        self._zip_cancel = None

    def _on_zip_import_stopped(self):
        """The zip thread has really stopped, so the guard can come off."""
        thread, self._zip_import_thread = self._zip_import_thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._zip_import_worker = self._zip_import_worker, None
        if worker is not None:
            worker.deleteLater()

    def open_library(self):
        """Open the Library Manager over the export library.

        The export library is where finished clips live, and a
        clip's tags are the one thing about it that can still be
        wrong: its folder and file name are a projection of them,
        so re-tagging a clip is how a clip that landed in the
        wrong place is put right. This is the third destination
        beside the wizard and the importer, and like the importer
        it is opened with no arguments — the library is the
        export folder, which Settings configures. See
        [docs/naming-and-organization.md](docs/naming-and-organization.md)
        and `library/manager.py`.
        """
        shell().open_safely('library')

    def open_sharing(self):
        """Open Library Sharing over the export library.

        A share is the export library, or a tag-filtered part
        of it, as a zip: every clip's video with its `.cnfo`
        record beside it, so a recipient drops it into their
        `import/` folder and imports it through the usual
        pipeline. Like the Library Manager it is opened with
        no arguments — the library is the export folder, which
        Settings configures, and the zip lands in the share
        folder beside the app. See
        [docs/sharing.md](docs/sharing.md) and
        `sharing/sharing.py`.
        """
        shell().open_safely('sharing')

    def open_settings(self):
        """Open the standalone file and folder scheme settings window."""
        shell().open_safely('settings')


class ZipImportWorker(QObject):
    """Extracts a share zip off the GUI thread.

    `extract_share_zip` reports progress and checks the cancel event
    before each member, so a zip the user gave up on does not have to
    finish. `done` fires on success, `cancelled` on a cancel, and
    `failed` with a reason on error; `finished` always follows, which
    is the signal the thread's teardown hangs off (invariant 14).
    """

    done = Signal()
    failed = Signal(str)
    cancelled = Signal()
    progressed = Signal(int, int)
    finished = Signal()

    def __init__(self, zip_path, dest_root=None, cancel_event=None):
        super().__init__()
        self.zip_path = zip_path
        self.dest_root = dest_root or import_folder()
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            path = extract_share_zip(
                self.zip_path,
                self.dest_root,
                on_progress=self._progress,
                should_cancel=self.cancel_event.is_set,
            )
            if path is None:
                self.cancelled.emit()
            else:
                self.done.emit()
        except ZipImportError as error:
            log_exception("importing the share zip failed", error)
            self.failed.emit(str(error))
        except Exception as error:
            log_exception("importing the share zip failed", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
        self.finished.emit()

    def _progress(self, done, total, relative_path=None):
        self.progressed.emit(done, total)
