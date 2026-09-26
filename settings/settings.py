import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.environment import setup_environment

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile, QIODevice, QSaveFile, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
from shared.exporting import (
    FILE_NAMING_SCHEME_KEY,
    FOLDER_ORGANIZATION_SCHEME_KEY,
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
# Settings window
# ---------------------------------------------------------------------------

class SettingsWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings_path = os.path.join(PROJECT_ROOT, "settings.json")
        self._saved_file_scheme: str | None = None
        self._saved_folder_scheme: str | None = None
        self._pending_warning: tuple[str, str] | None = None

        ui_file = QFile(os.path.join(SCRIPT_DIR, "settingswindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            raise OSError(ui_file.errorString())
        try:
            self.ui = UiLoader().load(ui_file, self)
        finally:
            ui_file.close()
        if self.ui is None:
            raise RuntimeError("Failed to load settings UI")
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
        self._update_file_preview()
        self._update_folder_preview()

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
            with open(self.settings_path, encoding="utf-8") as settings_file:
                settings = json.load(settings_file)
        except FileNotFoundError:
            return {}
        except RecursionError as error:
            raise ValueError("settings.json nesting is too deep") from error
        if not isinstance(settings, dict):
            raise ValueError("settings.json must contain a JSON object")
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
            QMessageBox.warning(self, "Settings could not be saved", str(error))
            return False

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

    def closeEvent(self, event):
        """Treat window-manager close like Cancel when the window is reused."""
        self._restore_schemes()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SettingsWindow()
    window.show()
    sys.exit(app.exec())
