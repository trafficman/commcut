import os
import sys

# Make the project root importable so 'shared' resolves. This must happen
# before importing anything from shared.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.environment import setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from shared.mpv import MpvBridge, create_mpv_player, scan_keyframes
from shared.timeline import TimelineWidget, Segment
from shared.segments import sidecar_path, probe_duration, SegmentModel

# Qt libs
from PySide6.QtWidgets import QMainWindow, QApplication, QStyle, QSplashScreen
from shared.ui_loader import UiLoader
from PySide6.QtCore import Qt, QFile, QObject, Signal, Slot, QThread
from PySide6.QtGui import QPixmap, QColor


# scan_keyframes and _KEYFRAME_EPSILON live in shared.mpv now.


class PreScanWorker(QObject):
    """Runs per-file pre-work off the GUI thread, for the splash screen.

    Emits finished(list) with the keyframe timestamps once scanning is done.
    Extend run() with more scan stages (scene detection, waveform, ...) as the
    project grows — emit a progress message between each stage.
    """
    finished = Signal(list)

    def __init__(self, path):
        super().__init__()
        self.path = path

    @Slot()
    def run(self):
        keyframes = scan_keyframes(self.path)
        self.finished.emit(keyframes)


# Tag key → attribute name on self.ui for the corresponding QLineEdit.
_TAG_FIELDS = {
    "title":       "lineEditTitle",
    "network":     "lineEditNetwork",
    "block":       "lineEditBlock",
    "filler_type": "lineEditType",
    "year":        "lineEditYear",
    "time_period": "lineEditTimePeriod",
    "show":        "lineEditShow",
    "special":     "lineEditSpecial",
    "length":      "lineEditLength",
    "information": "lineEditInfo",
}

# Tag key → attribute name on self.ui for the corresponding lock toggle button.
# Title is intentionally excluded — Title must be unique per segment.
_LOCK_BUTTONS = {
    "filler_type": "lockType",
    "network":     "lockNetwork",
    "year":        "lockYear",
    "time_period": "lockTimePeriod",
    "block":       "lockBlock",
    "show":        "lockShow",
    "special":     "lockSpecial",
    "length":      "lockLength",
    "information": "lockInfo",
}


class MediaPlayer(QMainWindow):
    def __init__(self):
        super().__init__()

        # Define UI file
        ui_file = QFile(os.path.join(SCRIPT_DIR, "editorwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            print(f"Failed to open UI File")
            sys.exit(-1)
        
        # Load the UI file created in Qt Designer
        loader = UiLoader()
        loader.register_widget(TimelineWidget)
        self.ui = loader.load(ui_file, self)
        self.setCentralWidget(self.ui)

        video_frame = self.ui.videoContainer

        # Initialize MPV Player and bind it to the QFrame window ID. The
        # create_mpv_player helper sets the WA_NativeWindow attribute (required
        # for mpv's direct3d renderer to embed into the frame) and applies the
        # standard mpv options used by both the editor and the scanner.
        # Keep the MPV instance in its own attribute; do NOT overwrite
        # self.ui.videoContainer or you lose the widget reference.
        self.media_path = os.path.join(PROJECT_ROOT, 'import', 'test.mp4')
        self.player = create_mpv_player(video_frame)

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
        self.bridge.fileLoaded.connect(self.on_file_loaded)
        self.bridge.playbackEnded.connect(lambda: print("Reached end of file."))

        self.bridge.positionChanged.connect(self.ui.timelineWidget.set_position)
        self.bridge.durationChanged.connect(self.ui.timelineWidget.set_duration)
        self.ui.timelineWidget.seekRequested.connect(self.bridge.seek_exact)

        self._sync_button(paused=True)

        # --- editing state ---
        self.segment_model = SegmentModel.load(sidecar_path(self.media_path))
        self.current_index = 0
        self.zoom_active = True
        self.tag_locks = {}  # tag key → locked value, carried across unedited segments
        self.ui.clipEnd.clicked.connect(self.on_end_segment)
        self.ui.stageButton.clicked.connect(self.on_stage)
        self.ui.toggleZoom.toggled.connect(self.on_toggle_zoom)
        self.ui.toggleZoom.setChecked(self.zoom_active)
        self.ui.activeLeft.clicked.connect(lambda: self._move_active(-1))
        self.ui.activeRight.clicked.connect(lambda: self._move_active(1))
        self.ui.mergeNext.clicked.connect(self.on_merge_next)
        self.ui.clipIgnore.toggled.connect(self.on_toggle_ignore)
        self.ui.clipStart.clicked.connect(self.on_start_segment)
        for key, attr in _LOCK_BUTTONS.items():
            getattr(self.ui, attr).toggled.connect(
                lambda checked, k=key: self.on_toggle_lock(k, checked))
        for key, attr in _TAG_FIELDS.items():
            getattr(self.ui, attr).textChanged.connect(
                lambda text, k=key: self.on_tag_edited(k, text))

        # Stage button doubles as the "unsaved changes" indicator: checkable +
        # enabled when dirty, unchecked + disabled when clean. The click action
        # still fires `clicked` so on_stage runs normally.
        self.dirty = False
        self.ui.stageButton.setCheckable(True)
        self.ui.undoButton.setCheckable(True)
        self.ui.undoButton.clicked.connect(self.on_undo)
        self._update_stage_button()

        self._refresh_timeline()

        self.bridge.load_file(self.media_path)

    def on_toggle_ignore(self, checked):
        """Toggle the active segment's ignored flag."""
        self.segment_model.segments[self.current_index]["ignored"] = checked
        self.dirty = True
        self._update_stage_button()
        self.ui.timelineWidget.update()

    def on_toggle_zoom(self, checked):
        """Toggle between zoom-to-active-segment and zoom-fit-whole-video."""
        self.zoom_active = checked
        if checked:
            self.ui.timelineWidget.zoom_to_segment(self.current_index)
        else:
            self.ui.timelineWidget.zoom_fit()

    def _move_active(self, delta):
        """Move the active segment index by delta, clamped to valid range."""
        new_index = self.current_index + delta
        if 0 <= new_index < self.segment_model.segment_count():
            self.current_index = new_index
            self._snap_playhead_to_active_start()
            self._refresh_timeline()

    def on_merge_next(self):
        """Merge the active segment into the next one."""
        if self.segment_model.merge_next(self.current_index):
            self.dirty = True
            self._update_stage_button()
            self._refresh_timeline()

    def on_start_segment(self):
        """Split the active segment at the playhead, activating the right part."""
        position = self.player.time_pos
        if position is None:
            return
        if self.segment_model.start_segment(self.current_index, position):
            self.current_index += 1
            self.dirty = True
            self._update_stage_button()
            self._snap_playhead_to_active_start()
            self._refresh_timeline()

    def _read_tags_from_form(self):
        """Snapshot the current form values into a tags dict."""
        return {key: getattr(self.ui, attr).text() for key, attr in _TAG_FIELDS.items()}

    def _write_tags_to_form(self, tags):
        """Populate the form from a tags dict.

        On a still-unedited segment, locked tags are pre-filled from
        self.tag_locks (overriding any stored empty value), since locks are
        the only source of input for unedited segments. Signals are blocked
        during setText so the dirty flag isn't set by the programmatic write.
        """
        unedited = not self._is_segment_edited(self.current_index)
        for key, attr in _TAG_FIELDS.items():
            line_edit = getattr(self.ui, attr)
            if unedited and key in self.tag_locks:
                value = self.tag_locks[key]
            else:
                value = tags.get(key, "")
            line_edit.blockSignals(True)
            line_edit.setText(value)
            line_edit.blockSignals(False)

    def _is_segment_edited(self, index):
        """True if the segment has any non-empty tag value."""
        return any(self.segment_model.segments[index]["tags"].values())

    def _refresh_lock_buttons(self):
        """Set lock button checked states to match the computed values.

        On an unedited segment, a lock is checked iff its key is in
        self.tag_locks. On an edited segment, all locks are disengaged.
        """
        unedited = not self._is_segment_edited(self.current_index)
        for key, attr in _LOCK_BUTTONS.items():
            btn = getattr(self.ui, attr)
            desired = unedited and key in self.tag_locks
            btn.blockSignals(True)
            btn.setChecked(desired)
            btn.blockSignals(False)

    def on_toggle_lock(self, key, checked):
        """Record (or clear) a locked tag value, then re-render lock states."""
        if checked:
            form_attr = _TAG_FIELDS[key]
            self.tag_locks[key] = getattr(self.ui, form_attr).text()
        else:
            self.tag_locks.pop(key, None)
        self._refresh_lock_buttons()

    def on_tag_edited(self, key, text):
        """Update the in-memory model when a tag field is edited, and mark dirty."""
        self.segment_model.segments[self.current_index]["tags"][key] = text
        self.dirty = True
        self._update_stage_button()

    def _update_stage_button(self):
        """Reflect the dirty state on the Stage and Undo buttons.

        Both buttons are checkable and enabled only when there are unstaged
        changes. Signals are blocked so programmatic toggling doesn't
        re-trigger any handlers.
        """
        for btn in (self.ui.stageButton, self.ui.undoButton):
            btn.blockSignals(True)
            btn.setChecked(self.dirty)
            btn.setEnabled(self.dirty)
            btn.blockSignals(False)

    def on_undo(self):
        """Revert the in-memory model to the last-staged .cmct state."""
        self.segment_model = SegmentModel.load(sidecar_path(self.media_path))
        if self.current_index >= self.segment_model.segment_count():
            self.current_index = max(0, self.segment_model.segment_count() - 1)
        self.dirty = False
        self._update_stage_button()
        self._snap_playhead_to_active_start()
        self._refresh_timeline()

    def _snap_playhead_to_active_start(self):
        """Seek mpv to the start of the current active segment."""
        if self.current_index < self.segment_model.segment_count():
            self.bridge.seek_exact(self.segment_model.start(self.current_index))

    def _refresh_timeline(self):
        """Push the current segment model + active index onto the timeline."""
        segments = [Segment(self.segment_model.start(i), self.segment_model.end(i),
                            self.segment_model.segments[i]["ignored"])
                    for i in range(self.segment_model.segment_count())]
        self.ui.timelineWidget.set_duration(self.segment_model.duration)
        self.ui.timelineWidget.set_segments(segments)
        self.ui.timelineWidget.set_active_index(self.current_index)
        if self.zoom_active:
            self.ui.timelineWidget.zoom_to_segment(self.current_index)
        else:
            self.ui.timelineWidget.zoom_fit()
        self.ui.clipIgnore.blockSignals(True)
        self.ui.clipIgnore.setChecked(self.segment_model.segments[self.current_index]["ignored"])
        self.ui.clipIgnore.blockSignals(False)
        self._write_tags_to_form(self.segment_model.segments[self.current_index]["tags"])
        self._refresh_lock_buttons()

    def on_end_segment(self):
        """Split the active segment at the current playhead position."""
        position = self.player.time_pos
        if position is None:
            return
        if self.segment_model.end_segment(self.current_index, position):
            self.dirty = True
            self._update_stage_button()
            self._refresh_timeline()

    def on_stage(self):
        """Lock in the active segment, advance to the next."""
        self.segment_model.segments[self.current_index]["tags"] = self._read_tags_from_form()
        self.segment_model.save(sidecar_path(self.media_path))
        self.dirty = False
        self._update_stage_button()
        self.current_index += 1
        if self.current_index >= self.segment_model.segment_count():
            print("Editing complete.")
            return
        self._snap_playhead_to_active_start()
        self._refresh_timeline()

    def on_file_loaded(self, path):
        """Called when mpv finishes loading a file: snap to the active segment start."""
        print(f"Loaded: {path}")
        self._snap_playhead_to_active_start()

    def on_transport_clicked(self):
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

    pixmap = QPixmap(480, 270)
    pixmap.fill(QColor(30, 30, 30))
    splash = QSplashScreen(pixmap)
    splash.show()

    media_path = os.path.join(PROJECT_ROOT, 'import', 'test.mp4')
    splash.showMessage(f"Now loading {os.path.basename(media_path)}…",
                       Qt.AlignCenter | Qt.AlignBottom, QColor(200, 200, 200))
    app.processEvents()

    keyframes = scan_keyframes(media_path)

    sidecar = sidecar_path(media_path)
    if not os.path.exists(sidecar):
        duration = probe_duration(media_path) or 0.0
        SegmentModel.placeholder(os.path.basename(media_path), duration).save(sidecar)

    splash.close()
    window = MediaPlayer()
    window.bridge.set_keyframes(keyframes)
    window.segment_model = SegmentModel.load(sidecar_path(media_path))
    window.resize(1024, 768)
    window.show()

    sys.exit(app.exec())