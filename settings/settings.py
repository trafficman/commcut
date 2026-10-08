import json
import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.environment import (
    import_folder,
    resource_path,
    settings_path,
    setup_environment,
)

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
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)
from shared.catalog import VocabularySync, sync_vocabulary
from shared.diagnostics import log, log_exception
from shared.exporting import (
    EXPORT_FOLDER_KEY,
    FILE_NAMING_SCHEME_KEY,
    FOLDER_ORGANIZATION_SCHEME_KEY,
    export_folder,
    export_folder_setting_error,
)
from shared.loading import LoadingDialog
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
from shared.ui_loader import UiLoader, adopt_title
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


def choose_export_folder(parent=None, start_at: str | None = None) -> str | None:
    """Ask the user for an export folder. Returns its path, or None if cancelled.

    A module-level function rather than a method so a test can stand in for the
    one thing a native dialog cannot do: be answered. The validation that follows
    an answer is not stubbed out with it — `export_folder_setting_error` runs on
    whatever comes back, chosen or typed.

    The dialog is an instance and left **native**, for the same reasons as
    `mainwindow.choose_source_video`: the OS dialog is better than Qt's, and it
    is what remembers the last folder the user was in. `start_at` is not a
    global state store — it is the value currently being edited, passed in by
    the caller — so opening in the folder already chosen costs nothing and
    saves the walk back to it.

    Cancelling returns None and the caller changes nothing at all.
    """
    dialog = QFileDialog(parent, "Choose an export folder")
    dialog.setFileMode(QFileDialog.Directory)
    dialog.setOption(QFileDialog.ShowDirsOnly, True)
    dialog.setAcceptMode(QFileDialog.AcceptOpen)
    if start_at and os.path.isdir(start_at):
        dialog.setDirectory(start_at)
    if not dialog.exec():
        return None
    chosen = dialog.selectedFiles()
    return chosen[0] if chosen else None


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

    def __init__(self, root: str, vocabulary, cancel_event=None,
                 pending_root: str | None = None):
        super().__init__()
        self.root = root
        #: The cached instance, passed in rather than resolved here: a fresh
        #: load would write a correct file and leave the cache stale, so an
        #: editor opened later in the same session would offer the old
        #: dropdowns.
        self.vocabulary = vocabulary
        #: The import folder, whose settled records count as uses so the prune
        #: does not delete a value the Tag Editor recorded a moment ago. See
        #: `shared/catalog.py:pending_record_tags`.
        self.pending_root = pending_root
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            result = sync_vocabulary(
                self.root,
                self.vocabulary,
                on_progress=lambda found, path: self.advanced.emit(found, path),
                should_cancel=self.cancel_event.is_set,
                pending_root=self.pending_root,
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
        self.settings_path = settings_path()
        self._saved_file_scheme: str | None = None
        self._saved_folder_scheme: str | None = None
        #: The export folder as last persisted. Empty means the default, which is
        #: why this is a string rather than a path: the Settings window never
        #: resolves the default, it only ever shows or omits the key.
        self._saved_export_folder: str = ""
        self._pending_warning: tuple[str, str] | None = None
        #: The vocabulary sync's own state. `_sync_close_after` is how a close
        #: requested mid-run is deferred until the thread has actually stopped --
        #: destroying a running QThread aborts the process.
        self._sync_thread: QThread | None = None
        self._sync_worker: SyncWorker | None = None
        self._sync_dialog: LoadingDialog | None = None
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
        adopt_title(self, self.ui)
        self.setCentralWidget(self.ui)

        try:
            settings = self._read_settings()
        except (OSError, ValueError) as error:
            self.ui.lineEditFileScheme.setEnabled(False)
            self.ui.lineEditFolderScheme.setEnabled(False)
            self._set_export_field_enabled(False)
            self.ui.lineEditPreview.clear()
            self.ui.lineEditFolderPreview.clear()
            self._queue_warning("Settings could not be loaded", str(error))
        else:
            self._load_scheme_fields(settings)
            self._load_export_folder_field(settings)

        self.ui.buttonBox.accepted.connect(self.accept_changes)
        self.ui.buttonBox.rejected.connect(self.reject_changes)
        self.ui.lineEditFileScheme.textChanged.connect(self._update_file_preview)
        self.ui.lineEditFolderScheme.textChanged.connect(self._update_folder_preview)
        self.ui.fileBrowseExport.clicked.connect(self.browse_for_export_folder)
        self.ui.buttonSyncVocabulary.clicked.connect(self.start_vocabulary_sync)
        self._update_file_preview()
        self._update_folder_preview()

    def browse_for_export_folder(self):
        """Put the folder the user picked in the field. Validation is the save's job.

        Nothing is checked and nothing is written here: a folder the picker
        returns still has to pass `export_folder_setting_error`, and the save
        refuses the whole set atomically if it does not. Checking in two places
        would be two answers to whether a path is acceptable.
        """
        chosen = choose_export_folder(self, self.ui.lineEditExport.text().strip())
        if chosen:
            self.ui.lineEditExport.setText(os.path.abspath(chosen))

    def _load_export_folder_field(self, settings: dict) -> None:
        """Load the export folder, which is absent-or-blank for the default.

        A value that is present but unusable is *shown* rather than hidden, so
        the user can see what is wrong with a hand-edited file and correct it
        here instead of having to open a text editor again. The save refuses it
        until then, which is the same path an invalid scheme takes.

        A value that is not a string is the exception: there is nothing to put in
        a text box, so both widgets are disabled and the stored value is left
        alone — the same treatment a malformed scheme gets. An absent key is the
        default and shows as an empty field; a JSON `null` is a *present* key
        that is not a string, and is refused rather than read as the default,
        which is the rule `export_folder` applies to the same file.
        """
        if EXPORT_FOLDER_KEY not in settings:
            value = ""
        elif not isinstance(settings[EXPORT_FOLDER_KEY], str):
            self._set_export_field_enabled(False)
            self._queue_warning(
                "Settings could not be loaded",
                f"{EXPORT_FOLDER_KEY} must be a string",
            )
            return
        else:
            value = settings[EXPORT_FOLDER_KEY]
        self.ui.lineEditExport.setText(value)
        self._saved_export_folder = value

    def _set_export_field_enabled(self, enabled: bool) -> None:
        for name in ("lineEditExport", "fileBrowseExport"):
            getattr(self.ui, name).setEnabled(enabled)

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

    def save_settings(self):
        """Validate changed fields and atomically persist every setting update."""
        file_enabled = self.ui.lineEditFileScheme.isEnabled()
        folder_enabled = self.ui.lineEditFolderScheme.isEnabled()
        export_enabled = self.ui.lineEditExport.isEnabled()
        if not file_enabled and not folder_enabled and not export_enabled:
            return True

        try:
            settings = self._read_settings()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Settings could not be saved", str(error))
            return False

        file_scheme = self.ui.lineEditFileScheme.text()
        folder_scheme = self.ui.lineEditFolderScheme.text()
        export_folder_value = self.ui.lineEditExport.text().strip()
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
        save_export = (
            export_enabled
            and export_folder_value != self._saved_export_folder
        )
        if not save_file and not save_folder and not save_export:
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
        if save_export:
            error = export_folder_setting_error(export_folder_value)
            if error is not None:
                QMessageBox.warning(self, "Invalid export folder", error)
                return False

        if save_file:
            settings[FILE_NAMING_SCHEME_KEY] = file_scheme
        if save_folder:
            settings[FOLDER_ORGANIZATION_SCHEME_KEY] = folder_scheme
        if save_export:
            # Empty means "the default", and it is stored as an absent key rather
            # than an empty string, so the file reads the same way for a hand
            # editor: nothing set is the default.
            if export_folder_value:
                settings[EXPORT_FOLDER_KEY] = os.path.abspath(export_folder_value)
            else:
                settings.pop(EXPORT_FOLDER_KEY, None)

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
        if export_enabled:
            self._saved_export_folder = export_folder_value
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
        """Save unsaved edits and close on success."""
        if self.save_settings():
            self.close()

    def _restore_fields(self) -> None:
        """Restore every editor value to its last persisted state."""
        self.ui.lineEditFileScheme.setText(self._saved_file_scheme or "")
        self.ui.lineEditFolderScheme.setText(self._saved_folder_scheme or "")
        if self.ui.lineEditExport.isEnabled():
            self.ui.lineEditExport.setText(self._saved_export_folder)

    def reject_changes(self):
        """Discard unsaved edits and close."""
        self._restore_fields()
        self.close()

    # --- the tag vocabulary sync ---

    def start_vocabulary_sync(self, root: str | None = None,
                              pending_root: str | None = None):
        """Reconcile the tag vocabulary with the clips in the export library.

        `root` defaults to the export folder and is a parameter so a test can
        point the walk at a temporary library: `export_folder()` resolves
        through the install root, and the Settings tests redirect
        `settings_path` rather than the install root.

        `pending_root` is the import folder, and the button leaves it unset so it
        resolves to `import_folder()`. It is a parameter for the same reason:
        without pointing it at a temporary folder, a test run would read the
        developer's real staged records.

        Both resolutions are guarded because this is a button handler and
        nothing above it catches: an export folder left unusable by a hand edit
        would otherwise take the exception into the event loop with nothing on
        screen. The editor and the three importer windows do not need this --
        they either already catch `ValueError` or are reported by the shell that
        tried to open them.

        The window is disabled for the duration rather than just the button,
        because the worker holds the live cached vocabulary instance and an edit
        made mid-run would silently not reach the file.
        """
        if self._sync_thread is not None:
            return None

        try:
            library = root or export_folder()
            pending = import_folder() if pending_root is None else pending_root
        except ValueError as error:
            log_exception("the vocabulary sync could not start", error)
            QMessageBox.warning(self, "Export folder is not usable", str(error))
            return None
        self._sync_cancel = threading.Event()
        self._sync_result = None
        self._sync_close_after = False

        progress = LoadingDialog("Reading the export library...", cancellable=False,
                                 modal=False, title="Tag Vocabulary Sync")
        progress.set_range(0, 0)
        progress.canceled.connect(self.request_vocabulary_sync_cancel)
        self._sync_dialog = progress

        worker = SyncWorker(library, get_vocabulary(vocabulary_path()),
                            pending_root=pending,
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
        self._sync_dialog.set_message(
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
        self._restore_fields()
        super().closeEvent(event)


def create(app=None):
    """Build the settings window. Returns it; the shell shows it.

    `app` is the process's QApplication. It is accepted for uniformity with the
    windows that need it during construction — the scanner and the editor show
    a splash while they work — and ignored here: this window constructs no
    player and runs no loop of its own.
    """
    return SettingsWindow()
