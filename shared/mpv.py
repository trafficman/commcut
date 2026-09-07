"""Shared mpv + ffprobe utilities used by both editor/ and scanner/.

Provides:
  - MpvBridge: the single communication channel between Qt and libmpv.
    Wraps a player.MPV instance, exposes its state as Qt signals (fired
    on the GUI thread), and provides a command surface (seek, frame step,
    keyframe nav, play/pause, file load).
  - scan_keyframes(path): ffprobe-based I-frame timestamp scanner.
  - create_mpv_player(video_frame): builds and configures an embedded
    mpv.MPV for a given QFrame, including the WA_NativeWindow attribute
    required for direct3d embedding.

The `mpv` package is imported lazily inside create_mpv_player so this
module remains importable in contexts where mpv isn't needed (e.g. unit
tests, or consumers that only want scan_keyframes). Callers that
actually construct a player must have already set up PATH so the mpv
DLL resolves — see shared.environment.setup_environment.
"""

import subprocess

from PySide6.QtCore import QObject, Signal, Qt


# Keyframes closer than this (seconds) to the current position are treated as
# "the keyframe we're standing on" and skipped, so repeated presses walk
# cleanly through the list instead of re-seeking to the same spot.
_KEYFRAME_EPSILON = 0.05


def scan_keyframes(path):
    """Return sorted I-frame timestamps (seconds) for a video, via ffprobe.

    Runs: ffprobe -v error -select_streams v:0 -show_entries frame=pict_type,pts_time -of csv=p=0 <path>
    and keeps only the rows whose pict_type is "I".
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "frame=pict_type,pts_time",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
    )
    keyframes = []
    for line in result.stdout.splitlines():
        try:
            pts_time, pict_type = line.split(",", 1)
        except ValueError:
            continue
        if pict_type.strip() == "I":
            try:
                keyframes.append(float(pts_time))
            except ValueError:
                continue
    return sorted(keyframes)


def create_mpv_player(video_frame):
    """Build an mpv.MPV embedded in the given QFrame.

    Sets the WA_NativeWindow attribute on the frame (required for mpv's
    direct3d renderer to embed into it), then constructs the player with
    the standard options used by both the editor and the scanner
    (osc/input disabled, keep_open, hr_seek='always'). The caller is
    responsible for wrapping the returned player in an MpvBridge.

    The mpv package is imported here (rather than at module top) so this
    module is importable in contexts that don't need mpv. Callers must
    have run shared.environment.setup_environment first so the mpv DLL
    is on PATH.
    """
    import mpv  # deferred: see module docstring

    video_frame.setAttribute(Qt.WA_NativeWindow, True)
    return mpv.MPV(
        wid=str(int(video_frame.winId())),
        vo="direct3d",
        osc=False,
        input_default_bindings=False,
        input_vo_keyboard=False,
        keep_open=True,
        hr_seek="always",
    )


class MpvBridge(QObject):
    """Single communication channel between Qt and libmpv.

    Commands flow down through the public methods; confirmed state flows
    back up as Qt signals. Widgets never read mpv state directly, so the
    UI can only ever mirror what mpv reports.
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
        player.observe_property("pause", self._on_pause)
        player.observe_property("time-pos", self._on_time_pos)
        player.observe_property("duration", self._on_duration)
        player.observe_property("eof-reached", self._on_eof)
        player.observe_property("path", self._on_path)

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
    def load_file(self, path):
        """Load a file and pause at the start (used for initial project load)."""
        self.player.play(path)
        self.player.pause = True

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
            p.seek(0, reference="absolute", precision="exact")
            p.pause = False
        else:
            p.pause = not p.pause

    def seek_exact(self, seconds):
        """Frame-exact absolute seek (no keyframe snapping)."""
        self.player.seek(seconds, reference="absolute", precision="exact")

    def step_frames(self, count=1):
        """Advance exactly one frame via exact seek.

        frame-step renders the frame's audio as it advances, and muting
        around it is racy (the audio is decoded faster than the mute
        takes effect), so we seek by one frame duration instead. Seeking
        flushes the audio buffer and stays silent.
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
        self.player.seek(target, reference="absolute", precision="exact")
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
