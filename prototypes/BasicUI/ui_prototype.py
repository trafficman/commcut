import os
import sys
import platform
# 1. Dynamically find the project root and the bundled binaries.
# This file now lives in <project>/prototypes/BasicUI, so the project
# root is two directories above the script's own location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..'))

# 2. Point PATH at the local bin\win folder so python-mpv can load libmpv-2.dll
bin_dir = os.path.join(PROJECT_ROOT, 'bin', 'win')
os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

# 3. NOW safely import mpv
import mpv
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QPushButton, QFrame
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush

def get_dll_path():
    """Resolves an absolute, local path to the bundled libmpv-2.dll."""
    dll_path = os.path.join(PROJECT_ROOT, 'bin', 'win', 'libmpv-2.dll')
    
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

        # Interactive Timeline
        self.timeline = TimelineWidget()
        main_layout.addWidget(self.timeline)

        self.timeline.seekRequested.connect(self.on_seek_requested)

        self.timer = QTimer(self)
        self.timer.setInterval(100) # Update 10 times a second
        self.timer.timeout.connect(self.update_playhead)
        self.timer.start()
        
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
            
        import_path = os.path.join(PROJECT_ROOT, 'import', 'test.mp4')
        
        if os.path.exists(import_path):
            print(f"Playing: {import_path}")
            # Tell MPV to open and play the file
            self.player.play(import_path)
        else:
            print(f"Could not find 'test.mp4' in the import folder: {import_path}")

    def on_seek_requested(self, target_time):
        """Jumps MPV to the clicked timeline position."""
        if self.player:
            # MPV seek command ('absolute' mode)
            self.player.seek(target_time, reference='absolute')

    def update_playhead(self):
        """Pulls current time from MPV and refreshes timeline UI."""
        if self.player and self.player.duration:
            # Sync total duration if it just loaded
            if self.timeline.duration != self.player.duration:
                self.timeline.set_duration(self.player.duration)

            # Update current playback time cursor
            current = self.player.time_pos
            if current:
                self.timeline.set_position(current)

class TimelineWidget(QWidget):
    # Custom signal that emits the target timestamp when clicked/scrubbed
    seekRequested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(50)  # Give it enough vertical room for track layers
        self.duration = 0.0        # Total video duration in seconds
        self.current_time = 0.0    # Current playback position in seconds
        
        # Placeholder for your future auto-detected segments: list of (start, end) tuples
        self.segments = [] 

    def set_duration(self, duration):
        self.duration = duration
        self.update() # Trigger a redraw

    def set_position(self, current_time):
        self.current_time = current_time
        self.update() # Trigger a redraw

    def set_segments(self, segments):
        """Allows passing detected cut regions to draw them on the timeline."""
        self.segments = segments
        self.update()

    # --- 1. RENDERING (Drawing over the timeline) ---
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width = self.width()
        height = self.height()

        # Draw background track base
        painter.fillRect(0, 0, width, height, QColor(30, 30, 30))

        if self.duration <= 0:
            return

        # Example of drawing custom layers: Auto-detected segment blocks
        # We draw these *under* the playhead so they act as a background track guide
        segment_brush = QBrush(QColor(50, 90, 140, 180))
        painter.setBrush(segment_brush)
        painter.setPen(Qt.NoPen)

        for start, end in self.segments:
            x_start = int((start / self.duration) * width)
            x_end = int((end / self.duration) * width)
            # Draw a block representing an unedited segment
            painter.drawRect(x_start, 10, max(2, x_end - x_start), height - 20)

        # Draw the Playhead Position Indicator (The vertical line/cursor)
        playhead_x = int((self.current_time / self.duration) * width)
        
        painter.setPen(QPen(QColor(230, 80, 80), 2)) # Red line for playhead
        painter.drawLine(playhead_x, 0, playhead_x, height)

    # --- 2. INTERACTION (Click to jump) ---
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.duration > 0:
            click_x = event.position().x()
            width = self.width()
            
            # Calculate clicked ratio and map it to a timestamp
            ratio = max(0.0, min(1.0, click_x / width))
            target_time = ratio * self.duration
            
            # Emit the signal so the main window can tell MPV to seek
            self.seekRequested.emit(target_time)

if __name__ == "__main__":
    # Required for high-DPI scaling on modern Windows displays
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    
    app = QApplication(sys.argv)
    window = CommCutPrototype()
    window.show()
    sys.exit(app.exec())