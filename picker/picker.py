"""Source video picker: choose which video in the import folder to work on.

This is where the Editing Wizard starts. The main menu's **Editor** button
opens this window, the user picks one of the videos sitting in the app's
``import/`` folder, and that path is handed to the scanner as a constructor
argument. From there the journey is the one that already existed: scan
boundaries, then the editor.

Every window is in the same process, so the picker asks the shell
(:mod:`shared.session`) for the scanner instead of starting one. The chain is
still menu -> picker -> scanner -> editor; what changed is that it is a stack
of windows rather than a chain of processes.

The list is not the only thing that decides what can be opened.
:func:`shared.sources.resolve_import_video` re-validates the path the scanner
receives, so a hand-edited command line cannot reach a file the user was never
offered. A video that already has a ``.cmct`` sidecar has been scanned before;
it is labelled as such, because selecting it goes straight to the editor rather
than re-running the scanner.
"""

import os
import sys

# Make the project root importable so 'shared' resolves.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.environment import resource_path, setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QMainWindow

from shared.diagnostics import log
from shared.session import shell
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
        """Open the scanner on the selected video, then close this window.

        The scanner is opened through the shell rather than started as a
        process, so the chosen path is a constructor argument. This window then
        closes itself, which is not the same as the scanner closing: the shell
        removes windows by identity, so the scanner that was just pushed stays
        on top of the menu.
        """
        video = self._selected_video()
        if video is None:
            return
        log(f"picking {video.path}")
        if shell().open_safely('scanner', source=video.path) is None:
            # The scanner did not open, so this window is still the one the
            # user is looking at. Leave it up and let them pick again.
            return
        self.close()


def create(app=None):
    """Build the picker window. Returns it; the shell shows it.

    `app` is the process's QApplication. It is accepted for uniformity with the
    windows that need it during construction — the scanner and the editor show
    a splash while they work — and ignored here: this window constructs no
    player, and its buttons only ask the shell for the next window.
    """
    return PickerWindow()
