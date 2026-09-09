import os
import subprocess
import sys

# Make the project root importable so 'shared' resolves.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.environment import setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from shared.ffmpeg import clip_to_temp
from shared.mpv import MpvBridge, create_mpv_player, scan_keyframes
from shared.segments import sidecar_path
from shared.ui_loader import UiLoader
from marker_timeline import MarkerTimelineWidget

from PySide6.QtWidgets import QMainWindow, QApplication, QStyle, QSplashScreen
from PySide6.QtCore import Qt, QFile
from PySide6.QtGui import QPixmap, QColor

# Seconds of test footage the scanner works on (stream-copied to temp/).
CLIP_DURATION = 120


def _clear_temp_clips():
    """Remove stale .mp4 preview clips from temp/ so each scanner run starts
    fresh and clips don't accumulate across runs.
    """
    temp_dir = os.path.join(PROJECT_ROOT, "temp")
    if not os.path.isdir(temp_dir):
        return
    for name in os.listdir(temp_dir):
        if name.lower().endswith(".mp4"):
            try:
                os.remove(os.path.join(temp_dir, name))
            except OSError:
                pass


def _editor_to_launch(source_path):
    """Return the editor script path to launch if a .cmct sidecar already
    exists for the source, else None.

    The scanner must never overwrite an existing .cmct, so when one is
    present we hand off to the Video Editor instead of running the scanner.
    """
    if os.path.exists(sidecar_path(source_path)):
        return os.path.join(PROJECT_ROOT, "editor", "editor.py")
    return None


class ScannerWindow(QMainWindow):
    """The Segment Scanner window.

    Loads the test video and hooks up the transport controls (play/pause,
    frame ±, keyframe ±) plus the two marker timelines. The keyframe list is
    populated by scan_keyframes in __main__ before the window is constructed,
    then pushed onto the bridge via set_keyframes.

    Place Boundary, Undo, and Test Scan are wired: Place Boundary stamps the
    playhead into the User Marked timeline; Undo removes the last user-placed
    boundary; Test Scan runs blackdetect and stamps midpoint markers into the
    Scanner Preview timeline. Remaining: Finished (full-source scan).
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

        # Track the playhead position so Place Boundary can stamp it.
        self.current_position = 0.0
        self.bridge.positionChanged.connect(self._on_position_changed)

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

        # Wire the Place Boundary button
        self.ui.boundaryButton.clicked.connect(self.on_place_boundary)

        # Wire the Undo button (removes the last user-placed boundary)
        self.ui.undoButton.clicked.connect(self.on_undo)

        # Wire the Test Scan button
        self.ui.scanButton.clicked.connect(self.on_test_scan)

        # Load the clipped test video
        self.clip_path = clip_to_temp(
            os.path.join(PROJECT_ROOT, "import", "test.mp4"),
            CLIP_DURATION,
            output_dir=os.path.join(PROJECT_ROOT, "temp"),
        )
        self.bridge.load_file(self.clip_path)

    def _on_position_changed(self, position):
        self.current_position = position

    def on_place_boundary(self):
        """Stamp the current playhead as a boundary in the User Marked timeline.

        Boundaries live only in memory (the marker timeline's list); nothing
        is written to a .cmct file.
        """
        self.ui.timelineWidget2.add_marker(self.current_position)

    def on_undo(self):
        """Remove the last user-placed boundary from the User Marked timeline.

        Assumes the user worked left-to-right, so the last (rightmost)
        marker in the list is the most recent placement. No-op if the
        timeline has no user markers.
        """
        self.ui.timelineWidget2.pop_marker()

    def on_test_scan(self):
        """Run ffmpeg's ``blackdetect`` on the 2-min preview and stamp one
        midpoint boundary per black run into the upper **Scanner Preview**
        timeline (timelineWidget1).

        Slider mapping:
          - Minimum Black Frames -> ``d = frames / fps`` (minimum run length)
          - Black Levels          -> ``pix_th = level / 100``
        Each black run with a real (non-N/A) end yields a single midpoint
        marker at ``(black_start + black_end) / 2``. Runs open at EOF
        (black_end:N/A) are skipped. Previous markers are cleared first so
        re-scanning reflects the current slider settings.
        """
        self.ui.timelineWidget1.set_markers([])

        fps = self.bridge.video_fps
        if not fps:
            return

        frames = self.ui.horizontalSlider.value()
        level = self.ui.horizontalSlider_2.value()
        min_sec = frames / fps if frames > 0 else 0.0
        pix_th = level / 100.0

        cmd = [
            "ffmpeg", "-y", "-v", "info",
            "-i", self.clip_path,
            "-vf", f"blackdetect=d={min_sec:.3f}:pix_th={pix_th:.4f}",
            "-an", "-f", "null", "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self._stamp_blackdetect_mids(proc.stderr)

    def _stamp_blackdetect_mids(self, stderr):
        """Parse blackdetect stderr and stamp midpoint markers (timelineWidget1)."""
        mids = []
        for line in stderr.splitlines():
            if "[blackdetect" not in line or "black_start:" not in line:
                continue
            kv = {}
            for tok in line.split():
                if ":" not in tok:
                    continue
                key, value = tok.split(":", 1)
                kv[key] = value
            end = kv.get("black_end")
            if end in (None, "N/A"):
                continue
            try:
                t1 = float(kv["black_start"])
                t2 = float(end)
            except (KeyError, ValueError):
                continue
            mids.append((t1 + t2) / 2.0)
        mids.sort()
        self.ui.timelineWidget1.set_markers(mids)

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
    source_path = os.path.join(PROJECT_ROOT, "import", "test.mp4")

    # Never overwrite an existing .cmct: if one already exists for the
    # source, skip the scanner and open the Video Editor instead.
    editor_path = _editor_to_launch(source_path)
    if editor_path is not None:
        subprocess.Popen([sys.executable, editor_path])
        sys.exit(0)

    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)

    # Start fresh: drop any leftover preview clips from prior runs.
    _clear_temp_clips()

    media_path = clip_to_temp(
        source_path,
        CLIP_DURATION,
        output_dir=os.path.join(PROJECT_ROOT, "temp"),
    )

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
