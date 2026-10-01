import json
import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.environment import resource_path, setup_environment

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import (
    QFile,
    QIODevice,
    QObject,
    QSaveFile,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QVBoxLayout,
)
from shared.catalog import VocabularySync, sync_vocabulary
from shared.diagnostics import log, log_exception
from shared.exporting import (
    FILE_NAMING_SCHEME_KEY,
    FOLDER_ORGANIZATION_SCHEME_KEY,
    export_folder,
)
from shared.naming import (
    DEFAULT_FILE_NAMING_SCHEME,
    FilenameSchemeError,
    compile_filename_scheme,
    render_filename,
)
from shared.paths import (
    DEFAULT_FOLDER_SCHEME,
    FolderRenderError,
    FolderSchemeError,
    compile_folder_scheme,
    format_folder_components,
    render_folder_components,
)
from shared.ui_loader import UiLoader
from shared.vocabulary import get_vocabulary, vocabulary_path

DEFAULT_FILE_SCHEME = DEFAULT_FILE_NAMING_SCHEME
PREVIEW_ERROR_STYLE = "color: red; background-color: #ffebee;"

PREVIEW_TAGS: dict[str, str] = {
    "title": "Worlds Finale",
    "network": "Cartoon Network",
    "block": "Toonami",
    "filler_type": "Promo",
    "year": "2000",
    "time_period": "2000s",
    "show": "Batman TAS",
    "special": "Kids",
    "length": "30 Seconds",
    "information": "Remastered",
}


# ---------------------------------------------------------------------------
# Pure scheme validation helpers
# ---------------------------------------------------------------------------

def file_scheme_error(scheme: str) -> str | None:
    """Return a strict production filename-scheme error, or None if valid."""
    try:
        compile_filename_scheme(scheme)
    except FilenameSchemeError as error:
        return str(error)
    return None


def folder_scheme_error(scheme: str) -> str | None:
    """Return a production folder-resolver error, or None if valid."""
    try:
        compile_folder_scheme(scheme)
    except FolderSchemeError as error:
        return str(error)
    return None


# ---------------------------------------------------------------------------
# Vocabulary sync reporting
# ---------------------------------------------------------------------------

#: Past this many removed values the summary counts them instead of listing
#: them. A library that has drifted a long way from the file produces a list long
#: enough to be unreadable in a dialog, and the count is what answers "what
#: happened".
_REMOVED_LIST_LIMIT = 12


def format_removed(values, limit: int = _REMOVED_LIST_LIMIT) -> str:
    """One line per `(namespace, value)` pair, or a count once there are too many."""
    pairs = list(values)
    if not pairs:
        return ""
    if len(pairs) <= limit:
        return "\n".join(f"  - {namespace}: {value}"
                         for namespace, value in pairs)
    return "\n".join(
        [f"  - {namespace}: {value}" for namespace, value in pairs[:limit]]
        + [f"  ... and {len(pairs) - limit} more"]
    )


def vocabulary_sync_summary(result: VocabularySync) -> str:
    """What one sync did, as the body of the screen that reports it.

    A pure function over the result, so the screen shows the run's own
    accounting rather than a recount -- the same reasoning as the editor's
    `_export_summary`.
    """
    if result.cancelled:
        return (
            "Cancelled.\n\n"
            "Nothing was written to the tag vocabulary. A half-read library "
            "cannot say which values are unused, so nothing was removed."
        )

    lines = [f"Read {result.clips_found} clip(s) from:", result.root]

    if result.skipped_prune:
        lines.append(
            "\nNo clips were found, so nothing was added and nothing was "
            "removed. An empty library is not evidence that a tag value is "
            "unused -- it is evidence there is no library."
        )
    else:
        lines.append(
            f"\nAdded {result.values_added} value(s) to the tag dropdown lists."
        )
        if result.values_removed:
            lines.append(
                f"Removed {len(result.values_removed)} value(s) no clip uses:"
            )
            lines.append(format_removed(result.values_removed))
        else:
            lines.append("No values were removed.")
        if result.values_kept:
            # Said even when nothing was removed, and said first, because "no
            # values were removed" on its own reads as the sync having found
            # nothing to do -- when in fact the prune was prevented from acting
            # on values it had every reason to consider unused.
            lines.append(
                f"{len(result.values_kept)} value(s) no clip uses were kept "
                "anyway, because they are shipped defaults:"
            )
            lines.append(format_removed(result.values_kept))

    if result.problems:
        lines.append(
            f"\n{len(result.problems)} record(s) could not be read, so those "
            "clips contributed nothing:"
        )
        lines.extend(f"  - {problem.path}\n    {problem.message}"
                     for problem in result.problems)

    return "\n".join(lines)


class SyncWorker(QObject):
    """Runs one vocabulary sync off the GUI thread.

    The walk reads every `.cnfo` under the export root, which is a network share
    or a USB stick as often as it is a local folder and is slow enough there to
    be felt. `shared/catalog.py` holds no Qt types and takes the progress and
    cancel callbacks, so this worker is the same thin shape as the editor's
    `ExportWorker`: plain data in, signals out, never a widget.

    `finished` is emitted exactly once on every path, including a failed one, so
    the window has one place that tears the run down. The broad
    `except Exception` is deliberate -- an unhandled exception in a `QThread`
    slot reaches PySide6's abort path and `install_excepthook` does not stop it.
    """

    #: clips read so far, and the record now being read
    advanced = Signal(int, str)
    finished = Signal(object)

    def __init__(self, root: str, vocabulary, cancel_event=None):
        super().__init__()
        self.root = root
        #: The cached instance, passed in rather than resolved here: a fresh
        #: load would write a correct file and leave the cache stale, so an
        #: editor opened later in the same session would offer the old
        #: dropdowns.
        self.vocabulary = vocabulary
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            result = sync_vocabulary(
                self.root,
                self.vocabulary,
                on_progress=lambda found, path: self.advanced.emit(found, path),
                should_cancel=self.cancel_event.is_set,
            )
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception(f"the tag vocabulary sync over {self.root} failed", error)
            result = error
        self.finished.emit(result)


class VocabularySyncDialog(QDialog):
    """The screen that ends a sync, built in code rather than from a `.ui`.

    Transient and modal with no layout worth designing -- the same reasoning as
    the editor's `ExportSummaryDialog`: no `resource_path`, nothing to add to the
    packaged payload. It reports rather than asks, so the only button is Close.
    """

    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tag Vocabulary Sync")
        self._error = isinstance(result, Exception)

        if self._error:
            body = (
                "The tag vocabulary could not be synced.\n\n"
                f"{type(result).__name__}: {result}\n\n"
                "Nothing was written."
            )
        else:
            body = vocabulary_sync_summary(result)

        layout = QVBoxLayout(self)
        heading = QLabel(
            "The sync did not finish" if self._error else "Tag vocabulary synced",
            self,
        )
        heading.setStyleSheet("font-weight: bold;")
        details = QPlainTextEdit(body, self)
        details.setReadOnly(True)
        details.setMinimumSize(560, 320)
        layout.addWidget(heading)
        layout.addWidget(details)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def summary_text(self) -> str:
        """The body on screen, for a test to assert on."""
        return self.findChild(QPlainTextEdit).toPlainText()


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------

class SettingsWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings_path = os.path.join(PROJECT_ROOT, "settings.json")
        self._saved_file_scheme: str | None = None
        self._saved_folder_scheme: str | None = None
        self._pending_warning: tuple[str, str] | None = None
        #: The vocabulary sync's own state. `_sync_close_after` is how a close
        #: requested mid-run is deferred until the thread has actually stopped --
        #: destroying a running QThread aborts the process.
        self._sync_thread: QThread | None = None
        self._sync_worker: SyncWorker | None = None
        self._sync_dialog: QProgressDialog | None = None
        self._sync_cancel = threading.Event()
        self._sync_result = None
        self._sync_close_after = False

        ui_file = QFile(resource_path("settings", "settingswindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            raise FileNotFoundError(
                f"Could not open the settings UI file: {ui_file.fileName()}")
        try:
            self.ui = UiLoader().load(ui_file, self)
        finally:
            ui_file.close()
        if self.ui is None:
            raise RuntimeError(
                f"Failed to load the settings UI: {ui_file.fileName()}")
        self.setCentralWidget(self.ui)
        self.setWindowTitle(self.ui.windowTitle())

        try:
            settings = self._read_settings()
        except (OSError, ValueError) as error:
            self.ui.lineEditFileScheme.setEnabled(False)
            self.ui.lineEditFolderScheme.setEnabled(False)
            self.ui.lineEditPreview.clear()
            self.ui.lineEditFolderPreview.clear()
            self._queue_warning("Settings could not be loaded", str(error))
        else:
            self._load_scheme_fields(settings)

        self.ui.buttonBox.accepted.connect(self.accept_changes)
        self.ui.buttonBox.rejected.connect(self.reject_changes)
        self.ui.lineEditFileScheme.textChanged.connect(self._update_file_preview)
        self.ui.lineEditFolderScheme.textChanged.connect(self._update_folder_preview)
        self.ui.buttonSyncVocabulary.clicked.connect(self.start_vocabulary_sync)
        self._update_file_preview()
        self._update_folder_preview()
        self._lock_folder_choices()

    def _lock_folder_choices(self) -> None:
        """Keep the import/export folder rows visibly unwired.

        The folders are fixed for this alpha — import/ and export/ beside the
        executable, chosen by the source picker rather than by hand — so these
        four widgets are shown but disabled, labelled "coming soon", rather
        than removed. Disabling them here as well as in the .ui means a
        re-enabled widget in the .ui cannot quietly imply they work.
        """
        for name in ("lineEditImport", "lineEditExport",
                     "fileBrowseImport", "fileBrowseExport"):
            getattr(self.ui, name).setEnabled(False)
        for name in ("labelImport", "labelExport"):
            label = getattr(self.ui, name)
            label.setText(f"{label.text().split(' (')[0]} (coming soon)")

    def _load_scheme_fields(self, settings: dict) -> None:
        """Load both schemes independently so one bad value cannot block the other."""
        errors: list[str] = []

        try:
            file_scheme = settings.get(FILE_NAMING_SCHEME_KEY, DEFAULT_FILE_SCHEME)
            if not isinstance(file_scheme, str):
                raise ValueError(f"{FILE_NAMING_SCHEME_KEY} must be a string")
        except ValueError as error:
            self.ui.lineEditFileScheme.setEnabled(False)
            errors.append(str(error))
        else:
            self.ui.lineEditFileScheme.setText(file_scheme)
            self._saved_file_scheme = file_scheme

        try:
            folder_scheme = settings.get(
                FOLDER_ORGANIZATION_SCHEME_KEY,
                DEFAULT_FOLDER_SCHEME,
            )
            if not isinstance(folder_scheme, str):
                raise ValueError(f"{FOLDER_ORGANIZATION_SCHEME_KEY} must be a string")
            scheme_error = folder_scheme_error(folder_scheme)
            if scheme_error is not None:
                raise ValueError(scheme_error)
        except ValueError as error:
            self.ui.lineEditFolderScheme.setEnabled(False)
            self.ui.lineEditFolderPreview.clear()
            errors.append(str(error))
        else:
            self.ui.lineEditFolderScheme.setText(folder_scheme)
            self._saved_folder_scheme = folder_scheme

        if errors:
            self._queue_warning(
                "Settings could not be loaded",
                "\n".join(errors),
            )

    def _queue_warning(self, title: str, message: str) -> None:
        """Defer load warnings until the caller has entered Qt's event loop."""
        self._pending_warning = (title, message)
        QTimer.singleShot(0, self._show_pending_warning)

    def _show_pending_warning(self) -> None:
        """Display at most one queued constructor-time warning."""
        if self._pending_warning is None:
            return
        title, message = self._pending_warning
        self._pending_warning = None
        QMessageBox.warning(self, title, message)

    def _read_settings(self):
        try:
            # utf-8-sig, not utf-8: this file is user data that people edit and
            # that other tools write, and a plain utf-8 read raises
            # JSONDecodeError on a leading byte-order mark. PowerShell 5.1's
            # Set-Content/Out-File, Notepad, and anything else that defaults
            # to "UTF-8 with BOM" all produce one. utf-8-sig reads both that
            # and BOM-less UTF-8 transparently. _write_settings emits no BOM,
            # so the two agree.
            with open(self.settings_path, encoding="utf-8-sig") as settings_file:
                settings = json.load(settings_file)
        except FileNotFoundError:
            # Expected on a fresh install: both schemes fall back to their
            # defaults and the file appears on first save.
            log(f"no settings file yet at {self.settings_path}; "
                f"using the default schemes")
            return {}
        except RecursionError as error:
            raise ValueError("settings.json nesting is too deep") from error
        except (OSError, ValueError) as error:
            # Logged as well as shown. A dialog is a one-time notification the
            # user can dismiss without reading the path, and settings.json
            # holds their naming schemes, so a failed read has to leave a
            # record. The original ValueError messages are preserved verbatim
            # because the tests and the dialog both quote them.
            log_exception(f"could not read {self.settings_path}", error)
            raise
        if not isinstance(settings, dict):
            error = ValueError("settings.json must contain a JSON object")
            log_exception(f"could not read {self.settings_path}", error)
            raise error
        log(f"read {self.settings_path}")
        return settings

    @staticmethod
    def _write_settings(settings_path: str, settings: dict) -> None:
        """Atomically write all settings so one scheme cannot save without the other."""
        try:
            data = (
                json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError(f"Settings data could not be encoded: {error}") from error
        settings_file = QSaveFile(settings_path)
        if not settings_file.open(QIODevice.WriteOnly):
            raise OSError(settings_file.errorString())
        if settings_file.write(data) != len(data):
            error = settings_file.errorString()
            settings_file.cancelWriting()
            raise OSError(error)
        if not settings_file.commit():
            raise OSError(settings_file.errorString())

    def save_schemes(self):
        """Validate changed fields and atomically persist every scheme update."""
        file_enabled = self.ui.lineEditFileScheme.isEnabled()
        folder_enabled = self.ui.lineEditFolderScheme.isEnabled()
        if not file_enabled and not folder_enabled:
            return True

        try:
            settings = self._read_settings()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Settings could not be saved", str(error))
            return False

        file_scheme = self.ui.lineEditFileScheme.text()
        folder_scheme = self.ui.lineEditFolderScheme.text()
        save_file = (
            file_enabled
            and (
                file_scheme != self._saved_file_scheme
                or FILE_NAMING_SCHEME_KEY not in settings
            )
        )
        save_folder = (
            folder_enabled
            and (
                folder_scheme != self._saved_folder_scheme
                or FOLDER_ORGANIZATION_SCHEME_KEY not in settings
            )
        )
        if not save_file and not save_folder:
            return True

        # Unchanged legacy values are preserved even if stricter validation added
        # later would now reject them.  Only user-edited fields block the save.
        if save_file:
            error = file_scheme_error(file_scheme)
            if error is not None:
                QMessageBox.warning(self, "Invalid file naming scheme", error)
                return False
        if save_folder:
            error = folder_scheme_error(folder_scheme)
            if error is not None:
                QMessageBox.warning(self, "Invalid folder organization scheme", error)
                return False

        if save_file:
            settings[FILE_NAMING_SCHEME_KEY] = file_scheme
        if save_folder:
            settings[FOLDER_ORGANIZATION_SCHEME_KEY] = folder_scheme

        try:
            self._write_settings(self.settings_path, settings)
        except (OSError, ValueError) as error:
            # The likely cause is that the install folder is not writable --
            # QSaveFile needs write access to the *directory*, not just the
            # file, so an app dropped somewhere read-only fails here even when
            # settings.json itself is writable. Log the path so that is
            # diagnosable after the dialog is dismissed.
            log_exception(
                f"could not save to {self.settings_path}", error)
            QMessageBox.warning(self, "Settings could not be saved", str(error))
            return False

        log(f"saved {self.settings_path}")
        if file_enabled:
            self._saved_file_scheme = file_scheme
        if folder_enabled:
            self._saved_folder_scheme = folder_scheme
        return True

    def _set_preview(self, preview, text: str, *, is_error: bool = False) -> None:
        """Display either a rendered value or a consistently styled error."""
        preview.setText(text)
        preview.setStyleSheet(PREVIEW_ERROR_STYLE if is_error else "")

    def _update_file_preview(self) -> None:
        """Live-render the naming scheme against placeholder tags."""
        scheme = self.ui.lineEditFileScheme.text()
        preview = self.ui.lineEditPreview
        if not self.ui.lineEditFileScheme.isEnabled() or not scheme:
            self._set_preview(preview, "")
            return
        error = file_scheme_error(scheme)
        if error:
            self._set_preview(preview, error, is_error=True)
            return
        try:
            result = render_filename(scheme, PREVIEW_TAGS)
        except Exception as exc:
            self._set_preview(preview, f"Error: {exc}", is_error=True)
        else:
            self._set_preview(preview, result)

    def _update_folder_preview(self) -> None:
        """Render the folder preview through the same resolver used by export."""
        scheme = self.ui.lineEditFolderScheme.text()
        preview = self.ui.lineEditFolderPreview
        if not self.ui.lineEditFolderScheme.isEnabled() or not scheme:
            self._set_preview(preview, "")
            return
        try:
            compiled_scheme = compile_folder_scheme(scheme)
            components = render_folder_components(compiled_scheme, PREVIEW_TAGS)
            result = format_folder_components(components)
        except (FolderSchemeError, FolderRenderError) as error:
            self._set_preview(preview, str(error), is_error=True)
        else:
            self._set_preview(preview, result)

    def accept_changes(self):
        """Save unsaved scheme edits and close on success."""
        if self.save_schemes():
            self.close()

    def _restore_schemes(self) -> None:
        """Restore both editor values to their last persisted state."""
        self.ui.lineEditFileScheme.setText(self._saved_file_scheme or "")
        self.ui.lineEditFolderScheme.setText(self._saved_folder_scheme or "")

    def reject_changes(self):
        """Discard unsaved scheme edits and close."""
        self._restore_schemes()
        self.close()

    # --- the tag vocabulary sync ---

    def start_vocabulary_sync(self, root: str | None = None):
        """Reconcile the tag vocabulary with the clips in the export library.

        `root` defaults to the export folder and is a parameter so a test can
        point the walk at a temporary library: `export_folder()` resolves
        through the install root, and the Settings tests redirect
        `PROJECT_ROOT` rather than the install root.

        The window is disabled for the duration rather than just the button,
        because the worker holds the live cached vocabulary instance and an edit
        made mid-run would silently not reach the file.
        """
        if self._sync_thread is not None:
            return None

        library = root or export_folder()
        self._sync_cancel = threading.Event()
        self._sync_result = None
        self._sync_close_after = False

        progress = QProgressDialog("Reading the export library...", None, 0, 0, self)
        progress.setWindowTitle("Tag Vocabulary Sync")
        # Parentless: setEnabled(False) below cascades to child widgets, and a
        # disabled dialog's Cancel button does nothing -- `self._sync_dialog`
        # keeps it alive instead. Deliberately not application modal either, so
        # this window's own close button still reaches closeEvent and can ask
        # about cancelling. Both are the export dialog's reasoning, for the same
        # reason.
        progress.setParent(None)
        progress.setModal(False)
        progress.setMinimumDuration(0)
        # An indeterminate dialog closes itself on reaching its maximum, which
        # would read as a cancel of a sync that had nothing to cancel.
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(self.request_vocabulary_sync_cancel)
        self._sync_dialog = progress

        worker = SyncWorker(library, get_vocabulary(vocabulary_path()),
                            cancel_event=self._sync_cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.advanced.connect(self._on_sync_advanced)
        worker.finished.connect(self._on_sync_finished)
        thread.finished.connect(self._on_sync_stopped)

        self._sync_worker = worker
        self._sync_thread = thread

        self.setEnabled(False)
        # Shown before the thread starts, so the dialog is on screen before the
        # walk begins rather than racing it.
        progress.show()
        thread.start()
        log(f"syncing the tag vocabulary from {library}")
        return worker

    def _on_sync_advanced(self, clips_found: int, relative_path: str):
        """Progress arrives with no total -- `os.walk` cannot know one -- so the
        dialog counts clips and names the record being read."""
        if self._sync_dialog is None:
            return
        self._sync_dialog.setLabelText(
            f"Read {clips_found} clip(s)\n{relative_path}")

    def request_vocabulary_sync_cancel(self):
        """Ask the walk to stop. Nothing is written when it does -- a half-read
        library cannot say which values are unused."""
        self._sync_cancel.set()

    def _on_sync_finished(self, result):
        """Stash the outcome and ask the thread to stop. One code path for a
        completed walk, a cancelled one, and a failed one."""
        self._sync_result = result
        if isinstance(result, Exception):
            log(f"the tag vocabulary sync failed: {result}")
        else:
            log(
                f"tag vocabulary synced from {result.root}: "
                f"{result.clips_found} clip(s), "
                f"{result.values_added} added, "
                f"{len(result.values_removed)} removed"
            )
        if self._sync_thread is not None:
            self._sync_thread.quit()

    def _on_sync_stopped(self):
        """Tear the run down and report it. Reached only from `thread.finished`,
        which is why there is no `wait()` anywhere in this flow: the thread has
        already stopped by the time the window lets go of it."""
        if self._sync_dialog is not None:
            self._sync_dialog.deleteLater()
        thread = self._sync_thread
        if thread is not None:
            thread.deleteLater()
        if self._sync_worker is not None:
            self._sync_worker.deleteLater()
        self._sync_dialog = None
        self._sync_thread = None
        self._sync_worker = None
        self._sync_cancel = threading.Event()
        self.setEnabled(True)

        if self._sync_close_after:
            self._sync_close_after = False
            self._sync_result = None
            self.close()
            return

        result = self._sync_result
        self._sync_result = None
        dialog = VocabularySyncDialog(result, self)
        dialog.exec()
        dialog.deleteLater()

    def closeEvent(self, event):
        """Treat window-manager close like Cancel when the window is reused, and
        refuse it outright while a sync is running."""
        if self._sync_thread is not None:
            if self._sync_close_after:
                event.ignore()
                return
            answer = QMessageBox.question(
                self,
                "A vocabulary sync is running",
                "The export library is being read.\n\n"
                "Cancel the sync and close?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            # Cancel first, then close from _on_sync_finished. Destroying a
            # QThread that is still running aborts the process, so there is no
            # path that lets this window go first.
            self._sync_cancel.set()
            self._sync_close_after = True
            event.ignore()
            return
        self._restore_schemes()
        super().closeEvent(event)


def create(app=None):
    """Build the settings window. Returns it; the shell shows it.

    `app` is the process's QApplication. It is accepted for uniformity with the
    windows that need it during construction — the scanner and the editor show
    a splash while they work — and ignored here: this window constructs no
    player and runs no loop of its own.
    """
    return SettingsWindow()
