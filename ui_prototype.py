import os
import sys
import platform
# 1. Dynamically find the absolute path to your local bin\win folder
base_dir = os.path.dirname(os.path.abspath(__file__))
bin_dir = os.path.join(base_dir, 'bin', 'win')

# 2. Inject it into the system PATH so ctypes/python-mpv can find it immediately
os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

# 3. NOW safely import mpv
import mpv
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QPushButton, QFrame
from PySide6.QtCore import Qt

def get_dll_path():
    """Resolves an absolute, local path to the bundled libmpv-2.dll."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dll_path = os.path.join(base_dir, 'bin', 'win', 'libmpv-2.dll')
    
    # Normalize path separators for Windows
    return os.path.normpath(dll_path)

class CommCutPrototype(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.setWindowTitle("CommCut - Prototype Viewer")
        self.resize(1000, 600)
        
        # Central widget and layout (equivalent to a main container with flex-column)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # 1. The Video Canvas (A standard QFrame to host MPV)
        self.video_frame = QFrame()
        self.video_frame.setStyleSheet("background-color: black;") # Black background like a video player
        # FIX: Force Qt to create a dedicated native window handle for this frame
        self.video_frame.setAttribute(Qt.WA_NativeWindow, True)
        main_layout.addWidget(self.video_frame, stretch=5) # Takes up most of the vertical space
        
        # 2. Control Panel Layout (A simple Play button for testing)
        controls_layout = QVBoxLayout()
        self.play_button = QPushButton("Load and Play 'test.mp4'")
        self.play_button.clicked.connect(self.load_and_play)
        controls_layout.addWidget(self.play_button)
        
        main_layout.addLayout(controls_layout, stretch=1)
        
        # 3. Initialize MPV Player and bind it to the QFrame window ID
        self.player = mpv.MPV(
                wid=str(int(self.video_frame.winId())),
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
            
        import_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'import', 'test.mp4')
        
        if os.path.exists(import_path):
            print(f"Playing: {import_path}")
            # Tell MPV to open and play the file
            self.player.play(import_path)
        else:
            print(f"Could not find 'test.mp4' in the import folder: {import_path}")

if __name__ == "__main__":
    # Required for high-DPI scaling on modern Windows displays
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    
    app = QApplication(sys.argv)
    window = CommCutPrototype()
    window.show()
    sys.exit(app.exec())