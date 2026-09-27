"""Shared mpv + ffprobe utilities used by both editor/ and scanner/.

Provides:
  - MpvBridge: the single communication channel between Qt and libmpv.
    Wraps a player.MPV instance, exposes its state as Qt signals (fired
    on the GUI thread), and provides a command surface (seek, frame step,
    keyframe nav, play/pause, file load).
  - scan_keyframes(path): ffprobe-based I-frame timestamp scanner.
  - create_mpv_player(video_frame): builds and configures an embedded
    mpv.MPV for a given QFrame, including the native-window handle mpv
    embeds into.

The `mpv` package is imported lazily inside create_mpv_player so this
module remains importable in contexts where mpv isn't needed (e.g. unit
tests, or consumers that only want scan_keyframes). Callers that
actually construct a player must load libmpv first and answer
python-mpv's own lookup for it — see
``shared.environment.mpv_import_context``, which create_mpv_player
enters for them. The ordering is the whole point of the deferral: on
macOS, python-mpv's import-time lookup searches system directories
rather than the one this app resolved, and raises rather than falling
back to a library that is already loaded.
"""

import subprocess

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from shared.diagnostics import log
from shared.environment import get_binary_path, mpv_import_context, video_output


# Keyframes closer than this (seconds) to the current position are treated as
# "the keyframe we're standing on" and skipped, so repeated presses walk
# cleanly through the list instead of re-seeking to the same spot.
_KEYFRAME_EPSILON = 0.05

# How far (seconds) the playhead may have drifted from where BoundaryPreview
# left it before its return seek is considered stale and skipped.
_PREVIEW_POSITION_EPSILON = 0.05

# How many frames past a segment boundary BoundaryPreview peeks by default, and
# how long that peek lasts before returning to the boundary.
SEGMENT_PREVIEW_FRAMES = 15
SEGMENT_PREVIEW_DWELL_MS = 450


def scan_keyframes(path):
    """Return sorted I-frame timestamps (seconds) for a video, via ffprobe.

    Runs: ffprobe -v error -select_streams v:0 -show_entries frame=pict_type,pts_time -of csv=p=0 <path>
    and keeps only the rows whose pict_type is "I".
    """
    result = subprocess.run(
        [
            get_binary_path("ffprobe"), "-v", "error",
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

    Forces a native window handle on the frame and hands it to mpv as `wid`.
    On Windows that is a child HWND, which is what mpv's direct3d driver
    embeds into; on macOS it is an NSView* and on Linux an X11/Wayland window,
    and mpv's generic GPU output embeds into that. The attribute is needed on
    every platform, not just Windows -- `winId()` is what produces the handle
    in the first place.

    The `vo` driver comes from shared.environment.video_output() rather than
    being hardcoded: 'direct3d' is a Windows path, and the other platforms use
    mpv's generic GPU output.

    libmpv is loaded, and python-mpv's own import-time lookup for it is
    answered from the same path, before `import mpv` runs. That ordering is why
    the import is deferred at all: python-mpv resolves libmpv for itself the
    moment it is imported, and on macOS that lookup cannot see a source install
    -- so a source install cannot start without this. It is wrapped here
    because the alternative is a bare ctypes OSError telling the user to read
    the `ctypes.util.find_library` documentation.

    A `winId()` of 0 means the frame never got a native handle, and passing
    that to mpv produces a window with no video in it -- which reads as a
    broken build rather than a bug, so it is refused instead.
    """
    with mpv_import_context():
        import mpv  # deferred: see module docstring

    video_frame.setAttribute(Qt.WA_NativeWindow, True)
    handle = int(video_frame.winId())
    if not handle:
        raise RuntimeError(
            "Qt did not give the video frame a native window handle, so mpv "
            "has nothing to embed into. This is a bug in commcut, not a "
            "missing video file."
        )

    return mpv.MPV(
        wid=str(handle),
        vo=video_output(),
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
            log("No media loaded.")
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

    @property
    def position(self):
        """Current playhead position in seconds, or None if not known yet."""
        return self.player.time_pos

    @property
    def paused(self):
        """Whether mpv is paused, or None while the state is unknown."""
        value = self.player.pause
        return None if value is None else bool(value)

    @property
    def duration(self):
        """Loaded media duration in seconds, or None if not known yet."""
        return self.player.duration

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

    @property
    def video_fps(self):
        """Container frame rate of the loaded file, or None if unavailable.

        Lets callers convert frame-count slider values into durations for
        the blackdetect filter's ``d`` parameter without touching mpv state
        directly (preserving the bridge pattern: widgets don't read mpv).
        """
        fps = self.player.container_fps
        if fps is None:
            return None
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            return None
        return fps if fps > 0 else None

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


class BoundaryPreview(QObject):
    """Briefly show the clip just past a segment boundary, then come back.

    Segments are a transition-point model, so a boundary is exactly where one
    clip ends and the next begins — which in practice is a black frame. That
    is the right resting place for the playhead (End Seg and Start Seg cut at
    `player.time_pos`), but it is a useless thing to *look* at, so stepping
    through segments with the Active arrow keys lands on nothing but black.

    ``flash(boundary)`` therefore seeks to the boundary, seeks a few frames
    past it so the clip is briefly visible, and schedules a return to the
    exact boundary. Two exact seeks cost milliseconds, so the peek itself does
    not slow anything down; the only deliberate delay is the dwell.

    A pending peek is abandoned the moment the playhead is used for anything
    else (a user seek, a frame step, a cut) — see ``cancel``. The return also
    skips itself if the playhead has since moved somewhere else on its own, so
    a pending timer can never yank the playhead out from under an action.

    ``frames`` is the off switch: 0 (or None) turns the peek off and leaves
    every navigation seeking straight to the boundary. ``dwell_ms`` is how
    long the peek lasts. Both are plain public attributes, so a future
    settings value only has to assign them.
    """

    def __init__(self, bridge, frames=SEGMENT_PREVIEW_FRAMES,
                 dwell_ms=SEGMENT_PREVIEW_DWELL_MS, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.frames = frames
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(dwell_ms)
        self._timer.timeout.connect(self._return_to_boundary)
        self._boundary = None
        self._peeked_to = None

    @property
    def pending(self):
        """True while a peek is showing and its return is still scheduled."""
        return self._boundary is not None

    def cancel(self):
        """Abandon a pending peek, leaving the playhead where it is."""
        self._timer.stop()
        self._boundary = None
        self._peeked_to = None

    def flash(self, boundary):
        """Seek to `boundary`, peek a few frames past it, then return.

        A no-op beyond the single seek to `boundary` when the peek is turned
        off, when the frame rate is not known yet, or when mpv is playing (a
        seek out and back would read as a stutter mid-playback).
        """
        self.cancel()
        self.bridge.seek_exact(boundary)

        offset = self._offset()
        if offset <= 0.0 or self.bridge.paused is not True:
            return

        target = boundary + offset
        duration = self.bridge.duration
        if duration is not None and target > duration:
            target = duration
        self.bridge.seek_exact(target)
        self._boundary = boundary
        self._peeked_to = target
        self._timer.start()

    def _offset(self):
        """The peek distance in seconds, or 0.0 when it cannot be computed."""
        if not self.frames:
            return 0.0
        fps = self.bridge.video_fps
        if not fps:
            return 0.0
        return self.frames / fps

    def _return_to_boundary(self):
        """Seek back to the boundary unless the playhead has moved on."""
        self._timer.stop()
        boundary, peeked_to = self._boundary, self._peeked_to
        self._boundary = self._peeked_to = None
        if boundary is None:
            return

        current = self.bridge.position
        if current is not None and peeked_to is not None:
            if abs(current - peeked_to) > _PREVIEW_POSITION_EPSILON:
                # Something else owns the playhead now; leave it alone.
                return
        self.bridge.seek_exact(boundary)
