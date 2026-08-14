import os
import sys
import platform
# 1. Dynamically find the project root and the bundled binaries.
# This file now lives in <project>/prototypes/SegmentDector, so the project
# root is two directories above the script's own location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..'))

# 2. Point PATH at the local bin\win folder so python-mpv can load libmpv-2.dll
bin_dir = os.path.join(PROJECT_ROOT, 'bin', 'win')
os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

# 3. NOW safely import mpv
import mpv

# 4. Qt libs
from PySide6.QtWidgets import QMainWindow, QApplication
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import Qt, QFile

def get_dll_path():
    """Resolves an absolute, local path to the bundled libmpv-2.dll."""
    dll_path = os.path.join(PROJECT_ROOT, 'bin', 'win', 'libmpv-2.dll')
    
    # Normalize path separators for Windows
    return os.path.normpath(dll_path)

class MediaPlayer(QMainWindow):
    def __init__(self):
        super().__init__()

        # Define UI file
        ui_file = QFile(os.path.join(SCRIPT_DIR, "mainwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            print(f"Failed to open UI File")
            sys.exit(-1)
        
        # Load the UI file created in Qt Designer
        loader = QUiLoader()
        self.ui = loader.load(ui_file, self)
        self.setCentralWidget(self.ui)
        
        # Example: Accessing widgets you placed in Designer
        # self.ui.playButton.clicked.connect(self.handle_play)

        video_frame = self.ui.videoContainer

        # FIX: Force Qt to create a dedicated native window handle for the
        # frame. Without this the QFrame has no stable native HWND, so MPV's
        # direct3d renderer cannot embed into it (it opens its own window or
        # draws nothing). This is the key difference from the working prototype.
        video_frame.setAttribute(Qt.WA_NativeWindow, True)

        # Initialize MPV Player and bind it to the QFrame window ID.
        # Keep the MPV instance in its own attribute; do NOT overwrite
        # self.ui.videoContainer or you lose the widget reference.
        self.player = mpv.MPV(
                wid=str(int(video_frame.winId())),
                vo='direct3d',
                osc=True,
                input_default_bindings=True,
                input_vo_keyboard=True
            )

    def load_and_play(self):
        """Loads test.mp4 from the import folder and starts playback."""
        if not self.player:
            print("MPV player is not initialized.")
            return

        import_path = os.path.join(PROJECT_ROOT, 'import', 'test.mp4')

        if os.path.exists(import_path):
            print(f"Playing: {import_path}")
            # Tell MPV to open and play the file
            self.player.play(import_path)
        else:
            print(f"Could not find 'test.mp4' in the import folder: {import_path}")

    def showEvent(self, event):
        """Auto-load the test clip once the window is first shown."""
        super().showEvent(event)
        if not getattr(self, "_loaded", False):
            self._loaded = True
            self.load_and_play()

if __name__ == "__main__":
    # Required for high-DPI scaling on modern Windows displays
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    window = MediaPlayer()
    window.resize(1024, 768)
    window.show()
    sys.exit(app.exec())