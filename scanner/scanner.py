import os
import sys

# Make the project root importable so 'shared' resolves.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.environment import setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from shared.mpv import MpvBridge, create_mpv_player, scan_keyframes
from shared.ui_loader import UiLoader
from marker_timeline import MarkerTimelineWidget

from PySide6.QtWidgets import QMainWindow, QApplication, QStyle, QSplashScreen
from PySide6.QtCore import Qt, QFile
from PySide6.QtGui import QPixmap, QColor


class ScannerWindow(QMainWindow):
    """The Segment Scanner window.

    Loads the test video and hooks up the transport controls (play/pause,
    frame ±, keyframe ±) plus the two marker timelines. The keyframe list is
    populated by scan_keyframes in __main__ before the window is constructed,
    then pushed onto the bridge via set_keyframes.

    Future work: Place Boundary / Undo, Test Scan, Finished, detector sliders.
    """

    def __init__(self):
        super().__init__()

        # Load the .ui file
        ui_file = QFile(os.path.join(SCRIPT_DIR, "scannerwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            print(f"Failed to open UI File")
            sys.exit(-1)

        loader = UiLoader()
        loader.register_widget(MarkerTimelineWidget)
        self.ui = loader.load(ui_file, self)
        self.setCentralWidget(self.ui)

        # Create the embedded mpv player in the video frame
        video_frame = self.ui.videoContainer
        self.player = create_mpv_player(video_frame)
        self.bridge = MpvBridge(self.player, parent=self)

        # Wire the play/pause button
        self.ui.playPause.clicked.connect(self.on_play_pause)
        self.bridge.pauseChanged.connect(self.on_pause_changed)

        # Wire frame and keyframe skip buttons
        self.ui.forwardFrame.clicked.connect(lambda: self.bridge.step_frames(1))
        self.ui.backwardFrame.clicked.connect(lambda: self.bridge.step_frames(-1))
        self.ui.forwardKeyFrame.clicked.connect(self.bridge.next_keyframe)
        self.ui.backwardKeyFrame.clicked.connect(self.bridge.prev_keyframe)

        # Wire both marker timelines to the bridge (playhead + duration + seek)
        for timeline in (self.ui.timelineWidget1, self.ui.timelineWidget2):
            self.bridge.positionChanged.connect(timeline.set_position)
            self.bridge.durationChanged.connect(timeline.set_duration)
            timeline.seekRequested.connect(self.bridge.seek_exact)

        # Slider value labels: live updates as the user drags the slider.
        # "Minimum Black Frames" is the count (0-40); "Black Levels" is a
        # percentage (0-100).
        self.ui.valueMinimumBlackFrames.setText(
            f"{self.ui.horizontalSlider.value()} frames"
        )
        self.ui.horizontalSlider.valueChanged.connect(
            lambda v: self.ui.valueMinimumBlackFrames.setText(f"{v} frames")
        )
        self.ui.valueBlackLevels.setText(f"{self.ui.horizontalSlider_2.value()}%")
        self.ui.horizontalSlider_2.valueChanged.connect(
            lambda v: self.ui.valueBlackLevels.setText(f"{v}%")
        )

        self._sync_button(paused=True)

        # Load the test video
        self.bridge.load_file(os.path.join(PROJECT_ROOT, "import", "test.mp4"))

    def on_play_pause(self):
        self.bridge.toggle_play()

    def on_pause_changed(self, paused):
        self._sync_button(paused)

    def _sync_button(self, paused):
        """Mirror mpv's pause state onto the play/pause button label/icon."""
        btn = self.ui.playPause
        style = btn.style()
        if paused:
            btn.setText("Play")
            btn.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        else:
            btn.setText("Pause")
            btn.setIcon(style.standardIcon(QStyle.SP_MediaPause))


if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)

    media_path = os.path.join(PROJECT_ROOT, "import", "test.mp4")

    # Splash while ffprobe scans keyframes. The scan runs synchronously
    # on the GUI thread (it's typically fast); the splash gives the user
    # something to look at. The splash is closed BEFORE the mpv player is
    # constructed — constructing mpv (direct3d renderer) while another
    # top-level window is the active foreground can deadlock on Windows.
    pixmap = QPixmap(480, 270)
    pixmap.fill(QColor(30, 30, 30))
    splash = QSplashScreen(pixmap)
    splash.show()
    splash.showMessage(
        f"Loading {os.path.basename(media_path)}…",
        Qt.AlignCenter | Qt.AlignBottom,
        QColor(200, 200, 200),
    )
    app.processEvents()

    keyframes = scan_keyframes(media_path)

    splash.close()
    window = ScannerWindow()
    window.bridge.set_keyframes(keyframes)
    window.resize(1024, 768)
    window.show()

    sys.exit(app.exec())
