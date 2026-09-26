"""Source video picker: choose which video in the import folder to work on.

This is where the Editing Wizard starts. The main menu's **Editor** button
opens this window, the user picks one of the videos sitting in the app's
``import/`` folder, and that path is handed to the scanner as a command-line
argument. From there the journey is the one that already existed: scan
boundaries, then the editor.

It is a separate process for the same reason every other window is — see
:func:`shared.environment.launch_command`. It is launched *by* the menu and it
launches the scanner, so the chain is menu -> picker -> scanner -> editor.

The list is not the only thing that decides what can be opened.
:func:`shared.sources.resolve_import_video` re-validates the path the scanner
receives, so a hand-edited command line cannot reach a file the user was never
offered. A video that already has a ``.cmct`` sidecar has been scanned before;
it is labelled as such, because selecting it goes straight to the editor rather
than re-running the scanner.
"""

import os
import subprocess
import sys

# Make the project root importable so 'shared' resolves.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.environment import (
    launch_command, resource_path, setup_environment,
)
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from shared.diagnostics import install_excepthook, log, log_exception
from shared.sources import SourceVideo, import_folder, list_source_videos
from shared.ui_loader import UiLoader


#: Shown under the list when there is nothing to open. An empty import/ folder
#: is the expected first-run state of a packaged build, so this is not an edge
#: case — it is the first thing a tester may see.
EMPTY_MESSAGE = (
    "No videos in the import folder yet.\n\n"
    "Copy a compilation video into it and press Refresh."
)


def format_size(size_bytes):
    """A short human-readable file size for the list."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def describe(video):
    """The list entry for one video, including whether it was scanned before."""
    text = f"{video.name}    {format_size(video.size_bytes)}"
    if video.has_sidecar:
        text += "    (already scanned - opens in the editor)"
    return text


class PickerWindow(QMainWindow):
    """Lists the videos in the import folder and launches the scanner on one."""

    def __init__(self):
        super().__init__()

        ui_file = QFile(resource_path("picker", "pickerwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            raise FileNotFoundError(
                f"Could not open the picker UI file: {ui_file.fileName()}")
        loader = UiLoader()
        try:
            self.ui = loader.load(ui_file, self)
        finally:
            ui_file.close()
        self.setCentralWidget(self.ui)
        self.setWindowTitle(self.ui.windowTitle())

        # The videos currently listed, in the same order as the list items.
        # The path is kept here rather than parsed back out of the item text.
        self.videos: list[SourceVideo] = []

        self.ui.listVideos.itemSelectionChanged.connect(self._update_buttons)
        self.ui.listVideos.itemDoubleClicked.connect(self._on_activated)
        self.ui.refreshButton.clicked.connect(self.refresh)
        self.ui.openButton.clicked.connect(self._on_activated)
        self.ui.cancelButton.clicked.connect(self.close)

        self.ui.labelFolder.setText(f"Import folder: {import_folder()}")
        self.refresh()

    # --- listing ---
    def refresh(self):
        """Re-read the import folder. Safe to call while the window is open,
        which is the point: videos are added to the folder by hand."""
        self.videos = list_source_videos()
        self.ui.listVideos.clear()
        for video in self.videos:
            self.ui.listVideos.addItem(describe(video))

        if self.videos:
            self.ui.listVideos.setCurrentRow(0)
            self.ui.labelStatus.setText(
                f"{len(self.videos)} video(s) available.")
        else:
            self.ui.labelStatus.setText(
                f"{EMPTY_MESSAGE}\n\n{import_folder()}")
        self._update_buttons()

    def _selected_video(self):
        """The chosen video, or None when nothing is selected."""
        row = self.ui.listVideos.currentRow()
        if row < 0 or row >= len(self.videos):
            return None
        return self.videos[row]

    def _update_buttons(self):
        """Open is only enabled with something selected."""
        self.ui.openButton.setEnabled(self._selected_video() is not None)

    # --- launching ---
    def _on_activated(self, *_args):
        """Open the selected video, then close this window.

        The window closes rather than lingering so a tester does not stack up
        picker windows behind several scanner processes.
        """
        video = self._selected_video()
        if video is None:
            return
        try:
            command = launch_command('scanner', video.path)
            log(f"picking {video.path}: {command}")
            subprocess.Popen(command)
        except (OSError, ValueError) as error:
            # Reported, not raised: an escape here would reach Qt's event loop
            # and take the window down with a traceback.
            log_exception("could not launch the scanner", error)
            QMessageBox.warning(
                self, "Scanner could not start", str(error))
            return
        self.close()


def run(*_args):
    """Run the picker. Returns the process exit code.

    Also the entry point main.py dispatches to for '--window picker', so a
    packaged build and a source run share this one code path. Arguments are
    accepted and ignored: main.py forwards them to every window, and this one
    has nothing to open until the user picks something.
    """
    app = QApplication(sys.argv)
    install_excepthook(app)
    window = PickerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
