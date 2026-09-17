import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.environment import setup_environment

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile, QIODevice, QSaveFile
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
from shared.ui_loader import UiLoader

REQUIRED_FILE_SCHEME_PLACEHOLDER = "title"


def file_scheme_error(scheme):
    """Return a validation message for a file naming scheme, or None if valid."""
    if "{title}" not in scheme:
        return "The file naming scheme must include {title} to ensure uniqueness."
    return None


class SettingsWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings_path = os.path.join(PROJECT_ROOT, "settings.json")
        self._saved_scheme = None

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
            scheme = settings.get("file_naming_scheme", "")
            if not isinstance(scheme, str):
                raise ValueError("file_naming_scheme must be a string")
        except (OSError, ValueError) as error:
            self.ui.lineEditFileScheme.setEnabled(False)
            QMessageBox.warning(self, "Settings could not be loaded", str(error))
        else:
            self.ui.lineEditFileScheme.setText(scheme)
            self._saved_scheme = scheme

        self.ui.buttonBox.accepted.connect(self.accept_changes)
        self.ui.buttonBox.rejected.connect(self.reject_changes)

    def _read_settings(self):
        try:
            with open(self.settings_path, encoding="utf-8") as settings_file:
                settings = json.load(settings_file)
        except FileNotFoundError:
            return {}
        if not isinstance(settings, dict):
            raise ValueError("settings.json must contain a JSON object")
        return settings

    def save_file_scheme(self):
        if not self.ui.lineEditFileScheme.isEnabled():
            return True
        scheme = self.ui.lineEditFileScheme.text()
        if scheme == self._saved_scheme:
            return True
        error = file_scheme_error(scheme)
        if error is not None:
            QMessageBox.warning(self, "Invalid file naming scheme", error)
            return False
        try:
            settings = self._read_settings()
            settings["file_naming_scheme"] = scheme
            data = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            settings_file = QSaveFile(self.settings_path)
            if not settings_file.open(QIODevice.WriteOnly):
                raise OSError(settings_file.errorString())
            if settings_file.write(data) != len(data):
                error = settings_file.errorString()
                settings_file.cancelWriting()
                raise OSError(error)
            if not settings_file.commit():
                raise OSError(settings_file.errorString())
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Settings could not be saved", str(error))
            return False
        self._saved_scheme = scheme
        return True

    def accept_changes(self):
        """Save unsaved scheme edits and close on success."""
        if self.save_file_scheme():
            self.close()

    def reject_changes(self):
        """Discard unsaved scheme edits and close."""
        self.ui.lineEditFileScheme.setText(self._saved_scheme or "")
        self.close()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SettingsWindow()
    window.show()
    sys.exit(app.exec())
