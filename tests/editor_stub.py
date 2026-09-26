"""Shared widget-backed stub for exercising the real editor methods.

The editor window itself needs libmpv and a real video, so tests bind the
real ``MediaPlayer`` methods onto this stub, which supplies only the ``ui``
namespace and the segment model. That keeps the code under test the shipped
code rather than a re-implementation.
"""

import os

from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from editor.editor import MediaPlayer, _LOCK_BUTTONS, _REQUIRED_TAG_FIELDS, _TAG_FIELDS
from shared.segments import SegmentModel


def ensure_qapp():
    """Create the offscreen QApplication the widget-backed tests need."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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
    _move_active = MediaPlayer._move_active
    _snap_playhead_to_active_start = lambda self: None

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
        self._refresh_timeline = self._refresh_form_and_locks
        # Mirrors the self._refresh_timeline() call at the end of
        # MediaPlayer.__init__, so the initial form/lock/outline state matches.
        self._refresh_timeline()

    def _refresh_form_and_locks(self):
        """Mirrors MediaPlayer._refresh_timeline minus the timeline widget."""
        self._write_tags_to_form(
            self.segment_model.segments[self.current_index]["tags"])
        self._refresh_lock_buttons()
        self._refresh_required_fields()

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
        self._refresh_form_and_locks()

    def set_ignored(self, value):
        self.ui.clipIgnore.setChecked(value)
