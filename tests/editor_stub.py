"""Shared widget-backed stub for exercising the real editor methods.

The editor window itself needs libmpv and a real video, so tests bind the
real ``MediaPlayer`` methods onto this stub, which supplies only the ``ui``
namespace and the segment model. That keeps the code under test the shipped
code rather than a re-implementation.
"""

import os

from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from editor.editor import MediaPlayer, _LOCK_BUTTONS, _REQUIRED_TAG_FIELDS, _TAG_FIELDS
from shared.mpv import BoundaryPreview
from shared.segments import SegmentModel
from shared.timeline import TimelineWidget


def ensure_qapp():
    """Create the offscreen QApplication the widget-backed tests need."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class FakeBridge:
    """The MpvBridge surface the editor uses, without libmpv or a video.

    Records every seek and frame/keyframe step so tests can assert the exact
    playhead sequence — which is what the boundary peek is made of — instead
    of only where the playhead happened to end up.
    """

    def __init__(self, duration=120.0, fps=23.976, paused=True):
        self.seeks = []
        self.frame_steps = []
        self.keyframe_steps = []
        self.play_toggles = 0
        self.position = 0.0
        self.duration = duration
        self.video_fps = fps
        self.paused = paused

    def seek_exact(self, seconds):
        self.seeks.append(seconds)
        self.position = seconds

    def step_frames(self, count=1):
        self.frame_steps.append(count)

    def next_keyframe(self):
        self.keyframe_steps.append(1)

    def prev_keyframe(self):
        self.keyframe_steps.append(-1)

    def toggle_play(self):
        self.paused = not self.paused
        self.play_toggles += 1

    def take_seeks(self):
        """Return the recorded seeks and clear the log."""
        seeks, self.seeks = self.seeks, []
        return seeks


class EditorStub:
    """Minimal MediaPlayer surface covering the tag and segment code paths."""

    _is_segment_edited = MediaPlayer._is_segment_edited
    _write_tags_to_form = MediaPlayer._write_tags_to_form
    _refresh_lock_buttons = MediaPlayer._refresh_lock_buttons
    _refresh_required_fields = MediaPlayer._refresh_required_fields
    _read_tags_from_form = MediaPlayer._read_tags_from_form
    _update_stage_button = MediaPlayer._update_stage_button
    _inherited_tags = MediaPlayer._inherited_tags
    _missing_required_labels = MediaPlayer._missing_required_labels
    on_toggle_lock = MediaPlayer.on_toggle_lock
    on_tag_edited = MediaPlayer.on_tag_edited
    on_toggle_ignore = MediaPlayer.on_toggle_ignore
    on_end_segment = MediaPlayer.on_end_segment
    on_start_segment = MediaPlayer.on_start_segment
    on_stage = MediaPlayer.on_stage
    on_toggle_zoom = MediaPlayer.on_toggle_zoom
    _refresh_timeline = MediaPlayer._refresh_timeline
    _move_active = MediaPlayer._move_active
    _activate = MediaPlayer._activate
    _snap_playhead_to_active_start = MediaPlayer._snap_playhead_to_active_start
    on_seek_requested = MediaPlayer.on_seek_requested
    on_step_frames = MediaPlayer.on_step_frames
    on_step_keyframe = MediaPlayer.on_step_keyframe
    on_transport_clicked = MediaPlayer.on_transport_clicked
    on_file_loaded = MediaPlayer.on_file_loaded

    def __init__(self, segments, duration=120.0, media_path=None):
        ensure_qapp()
        self.ui = type("Ui", (), {})()
        for attr in _TAG_FIELDS.values():
            setattr(self.ui, attr, QLineEdit())
        for attr in _LOCK_BUTTONS.values():
            button = QPushButton()
            button.setCheckable(True)  # matches checkable=true in editorwindow.ui
            setattr(self.ui, attr, button)
        for attr in ("stageButton", "undoButton", "clipIgnore"):
            setattr(self.ui, attr, QPushButton())
        # The Skip toggle is checkable in editorwindow.ui; without that,
        # setChecked would be a no-op and the ignored flag would never stick.
        self.ui.clipIgnore.setCheckable(True)
        for key, attr in _LOCK_BUTTONS.items():
            getattr(self.ui, attr).toggled.connect(
                lambda checked, k=key: self.on_toggle_lock(k, checked))
        for key, attr in _TAG_FIELDS.items():
            getattr(self.ui, attr).textChanged.connect(
                lambda text, k=key: self.on_tag_edited(k, text))
        for attr in ("clipEnd", "clipStart"):
            setattr(self.ui, attr, QPushButton())
        self.ui.clipIgnore.toggled.connect(self.on_toggle_ignore)
        # The real timeline widget plus the zoom toggle, so the shipped zoom
        # code (on_toggle_zoom / _refresh_timeline) is what gets exercised.
        # Shown because Qt delivers resizeEvent to a visible widget only, and
        # a hidden one would silently skip the zoom re-apply under test.
        self.ui.timelineWidget = TimelineWidget()
        self.ui.timelineWidget.resize(800, 60)
        self.ui.timelineWidget.show()
        self.ui.toggleZoom = QPushButton()
        self.ui.toggleZoom.setCheckable(True)
        self.ui.toggleZoom.toggled.connect(self.on_toggle_zoom)
        self.ui.toggleZoom.setChecked(True)  # matches MediaPlayer.__init__
        # Mirrors MediaPlayer.__init__: cache the authored stylesheets so the
        # required-field outline can be toggled without clobbering them.
        self._required_base_styles = {
            key: getattr(self.ui, _TAG_FIELDS[key]).styleSheet()
            for key in _REQUIRED_TAG_FIELDS
        }

        self.segment_model = SegmentModel("test.mp4", duration, segments)
        self.current_index = 0
        self.tag_locks = {}
        self.dirty = False
        self.media_path = media_path or "test.mp4"
        self.player = type("Player", (), {"time_pos": 10.0})()
        # Mirrors MediaPlayer.__init__: the playhead's only channel is the
        # bridge, and the boundary peek rides on it.
        self.bridge = FakeBridge(duration=duration)
        self.boundary_preview = BoundaryPreview(self.bridge)
        # Mirrors the self._refresh_timeline() call at the end of
        # MediaPlayer.__init__, so the initial form/lock/outline/zoom state
        # matches.
        self._refresh_timeline()

    # --- helpers ---

    def set_tag(self, key, value):
        getattr(self.ui, _TAG_FIELDS[key]).setText(value)

    def get_tag(self, key):
        return getattr(self.ui, _TAG_FIELDS[key]).text()

    def fill_required(self, **overrides):
        """Populate the four base record fields with usable defaults."""
        defaults = {
            "title": "Some Title",
            "network": "Cartoon Network",
            "filler_type": "Promo",
            "time_period": "Morning",
        }
        defaults.update(overrides)
        for key, value in defaults.items():
            self.set_tag(key, value)

    def click_lock(self, key):
        getattr(self.ui, _LOCK_BUTTONS[key]).click()

    def checked_locks(self):
        return sorted(
            key for key, attr in _LOCK_BUTTONS.items()
            if getattr(self.ui, attr).isChecked()
        )

    def outlined_required_fields(self):
        """Tag keys of required fields currently showing the warning style."""
        return sorted(
            key for key in _REQUIRED_TAG_FIELDS
            if getattr(self.ui, _TAG_FIELDS[key]).styleSheet()
        )

    def form(self):
        return {k: getattr(self.ui, v).text() for k, v in _TAG_FIELDS.items()
                if getattr(self.ui, v).text()}

    def go_to(self, index):
        self.current_index = index
        self._refresh_timeline()

    def navigate_to(self, index):
        """Step to `index` the way the Active ←/→ buttons do."""
        self._move_active(index - self.current_index)

    def set_ignored(self, value):
        self.ui.clipIgnore.setChecked(value)

    def set_playhead(self, position):
        self.player.time_pos = position
        self.bridge.position = position

    def timeline(self):
        return self.ui.timelineWidget

    def zoom_fit_toggle(self, checked):
        """Click the zoom toggle to its given state."""
        self.ui.toggleZoom.setChecked(checked)

    def resize_timeline(self, width):
        """Resize the timeline widget, as a window resize would."""
        self.ui.timelineWidget.resize(width, self.ui.timelineWidget.height())

    def finish_preview(self):
        """Let the peek's dwell elapse and run its return seek."""
        self.boundary_preview._return_to_boundary()

    def preview_pending(self):
        return self.boundary_preview.pending
