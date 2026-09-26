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
from shared.exporting import missing_required_tags

# Qt libs
from PySide6.QtWidgets import (
    QMainWindow, QApplication, QStyle, QSplashScreen, QMessageBox
)
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

# The base record fields every exported clip needs, in form order, mapped to
# the label the user sees. These are enforced on the front end as well as in
# shared.exporting: a keep segment cannot be staged or saved without them.
# Segments marked ignored are exempt — they are excluded from export, so the
# backend's required-tag rule does not apply to them either.
_REQUIRED_TAG_FIELDS = {
    "title": "Title",
    "network": "Network",
    "filler_type": "Type",
    "time_period": "Time Period",
}

# Inline style for a required field that still needs a value.
_REQUIRED_FIELD_STYLE = "QLineEdit { border: 1px solid #c0392b; }"


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
        self.ui.exportButton.clicked.connect(self.on_export)
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

        # Cache the authored stylesheets so the required-field outline can be
        # toggled without clobbering anything the .ui file set.
        self._required_base_styles = {
            key: getattr(self.ui, _TAG_FIELDS[key]).styleSheet()
            for key in _REQUIRED_TAG_FIELDS
        }

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
        self._refresh_required_fields()
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
        if self.segment_model.start_segment(
            self.current_index, position, self._inherited_tags()
        ):
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
        """Sync each lock button against the pinned lock values.

        A lock is engaged when its key is in self.tag_locks *and* the field
        currently holds the pinned value. The pinned value is deliberately
        *not* updated by field edits: a lock is a value that propagates to
        later segments, so a field that stops matching it is a segment
        deliberately deviating from the lock, and the toggle says so.
        """
        for key, attr in _LOCK_BUTTONS.items():
            btn = getattr(self.ui, attr)
            locked = self.tag_locks.get(key)
            current = getattr(self.ui, _TAG_FIELDS[key]).text()
            desired = locked is not None and locked == current
            btn.blockSignals(True)
            btn.setChecked(desired)
            btn.blockSignals(False)

    def on_toggle_lock(self, key, checked):
        """Pin (or release) the current field value as a lock, then re-render.

        Checking a toggle pins whatever the field currently holds. An already
        disengaged toggle that matches its pin is re-engaged by restoring the
        text rather than by clicking it, so a click here always means "pin
        this value" or "release the pin".
        """
        if checked:
            self.tag_locks[key] = getattr(self.ui, _TAG_FIELDS[key]).text()
        else:
            self.tag_locks.pop(key, None)
        self._refresh_lock_buttons()

    def on_tag_edited(self, key, text):
        """Update the in-memory model when a tag field is edited, and mark dirty."""
        self.segment_model.segments[self.current_index]["tags"][key] = text
        self.dirty = True
        self._update_stage_button()
        # Re-derive the toggles on every keystroke so this field's lock
        # switches off the moment the text stops matching its pinned value,
        # and back on as soon as it matches again.
        self._refresh_lock_buttons()
        # Likewise keep the required-field outline in step with the text.
        self._refresh_required_fields()

    def _inherited_tags(self):
        """Tag values a newly created segment should start with.

        Locked values only. Unlocked tags -- Title included -- deliberately
        do not carry over, so the new segment, the form, and the export-time
        lock materialization in shared.exporting all agree.
        """
        return {key: value for key, value in self.tag_locks.items() if value}

    def _missing_required_labels(self):
        """Display names of the required tags the active segment still lacks.

        Read from the form, so the check reflects what the user is looking at
        rather than what happens to be in the model. Ignored segments are
        exempt, matching the export preflight, which skips them entirely.
        """
        segment = self.segment_model.segments[self.current_index]
        if segment["ignored"]:
            return []
        missing = set(missing_required_tags(self._read_tags_from_form()))
        return [
            label for key, label in _REQUIRED_TAG_FIELDS.items()
            if key in missing
        ]

    def _refresh_required_fields(self):
        """Outline the required fields the active segment is still missing.

        Recomputed on every keystroke, ignore toggle, and segment change, so
        the outline always states what Stage will demand right now.
        """
        missing = set(self._missing_required_labels())
        for key, label in _REQUIRED_TAG_FIELDS.items():
            line_edit = getattr(self.ui, _TAG_FIELDS[key])
            line_edit.setStyleSheet(
                _REQUIRED_FIELD_STYLE if label in missing
                else self._required_base_styles[key]
            )

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
        self._refresh_required_fields()

    def on_end_segment(self):
        """Split the active segment at the current playhead position."""
        position = self.player.time_pos
        if position is None:
            return
        if self.segment_model.end_segment(
            self.current_index, position, self._inherited_tags()
        ):
            self.dirty = True
            self._update_stage_button()
            self._refresh_timeline()

    def on_stage(self):
        """Lock in the active segment, advance to the next.

        A keep segment missing any of the four base record fields is refused
        before anything is written, so the .cmct sidecar never receives a
        staged record that export would later reject.
        """
        missing = self._missing_required_labels()
        if missing:
            self._refresh_required_fields()
            QMessageBox.warning(
                self,
                "Missing required tags",
                "This segment needs all four required tags before it can be "
                "staged:\n\n- " + "\n- ".join(missing) +
                "\n\nMark the segment as skipped (Skip) if it should not be "
                "exported.",
            )
            return
        self.segment_model.segments[self.current_index]["tags"] = self._read_tags_from_form()
        self.segment_model.save(sidecar_path(self.media_path))
        self.dirty = False
        self._update_stage_button()
        if self.current_index + 1 >= self.segment_model.segment_count():
            print("Editing complete.")
            return
        self.current_index += 1
        self._snap_playhead_to_active_start()
        self._refresh_timeline()

    def on_export(self):
        """Persist edits, plan named destinations, then transcode every keep clip."""
        self.ui.exportButton.setEnabled(False)
        try:
            from shared.exporting import (
                load_export_schemes,
                model_with_tag_locks,
                validate_segment_model,
            )
            from shared.ffmpeg import export_named_model

            # Check the form before anything is persisted, so an incomplete
            # record is never written to the .cmct sidecar. validate_segment_model
            # only covers structure; the required-tag rule is enforced here and
            # again by the export preflight.
            missing = self._missing_required_labels()
            if missing:
                raise ValueError(
                    "The active segment needs all four required tags before it "
                    "can be saved:\n- " + "\n- ".join(missing)
                )
            if 0 <= self.current_index < self.segment_model.segment_count():
                self.segment_model.segments[self.current_index]["tags"] = (
                    self._read_tags_from_form()
                )
            validate_segment_model(self.segment_model)
            self.segment_model.save(sidecar_path(self.media_path))
            self.dirty = False
            self._update_stage_button()

            export_model = model_with_tag_locks(
                self.segment_model,
                self.tag_locks,
            )
            schemes = load_export_schemes(
                os.path.join(PROJECT_ROOT, "settings.json")
            )
            out_dir = os.path.join(PROJECT_ROOT, "export")
            _, result = export_named_model(
                self.media_path,
                export_model,
                schemes,
                out_dir,
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Export could not start", str(error))
            return
        finally:
            self.ui.exportButton.setEnabled(True)

        print(
            f"Exported {result.succeeded} clip(s) to {out_dir}; "
            f"{result.failed} failed"
        )
        if result.failures:
            details = "\n\n".join(
                f"Segment {failure.segment_index + 1} — {failure.destination}\n"
                f"{failure.message}"
                for failure in result.failures[:3]
            )
            if len(result.failures) > 3:
                details += f"\n\n…and {len(result.failures) - 3} more failure(s)."
            QMessageBox.warning(
                self,
                "Export completed with errors",
                f"{result.failed} clip(s) failed:\n\n{details}",
            )

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