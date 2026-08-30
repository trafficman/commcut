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

from timeline import TimelineWidget, Segment

# 4. Qt libs
from PySide6.QtWidgets import QMainWindow, QApplication, QStyle
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import Qt, QFile, QObject, Signal, Slot

def get_dll_path():
    """Resolves an absolute, local path to the bundled libmpv-2.dll."""
    dll_path = os.path.join(PROJECT_ROOT, 'bin', 'win', 'libmpv-2.dll')
    
    # Normalize path separators for Windows
    return os.path.normpath(dll_path)


# Keyframes closer than this (seconds) to the current position are treated as
# "the keyframe we're standing on" and skipped, so repeated presses walk cleanly
# through the list instead of re-seeking to the same spot.
_KEYFRAME_EPSILON = 0.05


class MpvBridge(QObject):
    """Single communication channel between Qt and libmpv.

    Commands flow down through the public methods; confirmed state flows back
    up as Qt signals. Widgets never read mpv state directly, so the UI can
    only ever mirror what mpv reports.
    """

    pauseChanged = Signal(bool)
    positionChanged = Signal(float)
    durationChanged = Signal(float)
    fileLoaded = Signal(str)
    playbackEnded = Signal()

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.keyframes = []

        # Observers fire on mpv's worker thread. Emitting Qt signals is
        # thread-safe and delivers the payload on the GUI thread.
        player.observe_property('pause', self._on_pause)
        player.observe_property('time-pos', self._on_time_pos)
        player.observe_property('duration', self._on_duration)
        player.observe_property('eof-reached', self._on_eof)
        player.observe_property('path', self._on_path)

    # --- state callbacks (mpv worker thread) ---
    def _on_pause(self, name, value):
        if value is not None:
            self.pauseChanged.emit(bool(value))

    def _on_time_pos(self, name, value):
        if value is not None:
            self.positionChanged.emit(float(value))

    def _on_duration(self, name, value):
        if value is not None:
            self.durationChanged.emit(float(value))

    def _on_eof(self, name, value):
        if value:
            self.playbackEnded.emit()

    def _on_path(self, name, value):
        if value:
            self.fileLoaded.emit(str(value))

    # --- command surface (call from the GUI thread) ---
    def load_and_play(self, path):
        self.player.play(path)
        self.player.pause = False

    def toggle_play(self):
        p = self.player
        if p.idle_active:
            print("No media loaded.")
            return
        if p.eof_reached:
            # keep-open froze us on the final frame: restart from the top
            p.seek(0, reference='absolute', precision='exact')
            p.pause = False
        else:
            p.pause = not p.pause

    def seek_exact(self, seconds):
        """Frame-exact absolute seek (no keyframe snapping)."""
        self.player.seek(seconds, reference='absolute', precision='exact')

    def step_frames(self, count=1):
        """Advance exactly one frame via exact seek.

        frame-step renders the frame's audio as it advances, and muting around
        it is racy (the audio is decoded faster than the mute takes effect), so
        we seek by one frame duration instead. Seeking flushes the audio buffer
        and stays silent.
        """
        fps = self.player.container_fps
        pos = self.player.time_pos
        if not fps or pos is None:
            # fps/position not yet known (file still loading); nothing to step
            return
        target = pos + count / fps
        if target < 0.0:
            target = 0.0
        dur = self.player.duration
        if dur is not None and target > dur:
            target = dur
        self.player.seek(target, reference='absolute', precision='exact')
        self.player.pause = True

    # --- keyframe navigation ---
    def set_keyframes(self, times):
        """Replace the keyframe list with a sorted list of times (seconds)."""
        self.keyframes = sorted(times)

    def next_keyframe(self):
        """Seek to the first keyframe after the current position."""
        pos = self.player.time_pos
        if pos is None:
            return
        for t in self.keyframes:
            if t > pos + _KEYFRAME_EPSILON:
                self.seek_exact(t)
                return

    def prev_keyframe(self):
        """Seek to the last keyframe before the current position."""
        pos = self.player.time_pos
        if pos is None:
            return
        for t in reversed(self.keyframes):
            if t < pos - _KEYFRAME_EPSILON:
                self.seek_exact(t)
                return
class UiLoader(QUiLoader):
    """QUiLoader that can construct our custom promoted widgets.

    QUiLoader.createWidget is called for every widget in the .ui file. When it
    encounters our promoted class name we build the real widget; everything
    else falls through to the base implementation.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._custom_widgets = {}

    def register_widget(self, cls):
        self._custom_widgets[cls.__name__] = cls

    def createWidget(self, className, parent=None, name=""):
        if className in self._custom_widgets:
            widget = self._custom_widgets[className](parent)
            widget.setObjectName(name)
            return widget
        return super().createWidget(className, parent, name)


class MediaPlayer(QMainWindow):
    def __init__(self):
        super().__init__()

        # Define UI file
        ui_file = QFile(os.path.join(SCRIPT_DIR, "mainwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            print(f"Failed to open UI File")
            sys.exit(-1)
        
        # Load the UI file created in Qt Designer
        loader = UiLoader()
        loader.register_widget(TimelineWidget)
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
        self.media_path = os.path.join(PROJECT_ROOT, 'import', 'test.mp4')
        self.player = mpv.MPV(
                wid=str(int(video_frame.winId())),
                vo='direct3d',
                osc=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
                keep_open=True,
                hr_seek='always'
            )

        # Start paused so mpv's state and the button agree before any load.
        self.player.pause = True
        self.bridge = MpvBridge(self.player, parent=self)

        play_button = self.ui.playPause
        play_button.clicked.connect(self.on_transport_clicked)
        self.ui.forwardFrame.clicked.connect(lambda: self.bridge.step_frames(1))
        self.ui.backwardFrame.clicked.connect(lambda: self.bridge.step_frames(-1))
        self.ui.forwardKeyFrame.clicked.connect(self.bridge.next_keyframe)
        self.ui.backwardKeyFrame.clicked.connect(self.bridge.prev_keyframe)
        self.bridge.pauseChanged.connect(self.on_pause_changed)
        self.bridge.fileLoaded.connect(lambda p: print(f"Loaded: {p}"))
        self.bridge.playbackEnded.connect(lambda: print("Reached end of file."))

        self.bridge.positionChanged.connect(self.ui.timelineWidget.set_position)
        self.bridge.durationChanged.connect(self.ui.timelineWidget.set_duration)
        self.ui.timelineWidget.seekRequested.connect(self.bridge.seek_exact)

        self._sync_button(paused=True)

        # Placeholder
        self.bridge.set_keyframes([5.0, 12.0, 20.0, 35.0])

    def on_transport_clicked(self):
        """First press loads test.mp4; later presses toggle play/pause."""
        if not os.path.exists(self.media_path):
            print(f"Could not find 'test.mp4' in the import folder: {self.media_path}")
            return

        if self.player.idle_active:
            self.bridge.load_and_play(self.media_path)
        else:
            # toggle_play also handles restart-at-end-of-file internally
            self.bridge.toggle_play()

    @Slot(bool)
    def on_pause_changed(self, paused):
        self._sync_button(paused)

    def _sync_button(self, paused):
        """Mirror mpv's pause state onto the button label/icon."""
        btn = self.ui.playPause
        style = btn.style()
        if paused:
            btn.setText("Play")
            btn.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        else:
            btn.setText("Pause")
            btn.setIcon(style.standardIcon(QStyle.SP_MediaPause))

if __name__ == "__main__":
    # Required for high-DPI scaling on modern Windows displays
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    window = MediaPlayer()
    window.resize(1024, 768)
    window.show()
    sys.exit(app.exec())