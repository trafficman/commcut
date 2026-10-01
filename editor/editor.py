import os
import sys
import threading
from dataclasses import dataclass

# Make the project root importable so 'shared' resolves. This must happen
# before importing anything from shared.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.environment import resource_path, setup_environment
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from shared.diagnostics import log, log_exception
from shared.mpv import (
    BoundaryPreview, MpvBridge, SEGMENT_PREVIEW_DWELL_MS,
    SEGMENT_PREVIEW_FRAMES, create_mpv_player, scan_keyframes,
)
from shared.timeline import TimelineWidget, Segment, ZOOM_FIT, ZOOM_SEGMENT
from shared.segments import (
    sidecar_path, probe_duration,
    SegmentModel,
    END_BOUNDARY_BLOCKED, END_BOUNDARY_NO_CHANGE,
)
from shared.sources import require_source_video
from shared.exporting import (
    load_export_schemes,
    missing_required_tags,
    model_with_tag_locks,
    validate_segment_model,
)
from shared.ffmpeg import (
    execute_export_plan,
    ExportClipFailure,
    ExportExecutionResult,
    plan_export,
)

# Qt libs
from PySide6.QtWidgets import (
    QMainWindow, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QProgressDialog, QPushButton, QStyle, QSplashScreen,
    QVBoxLayout, QComboBox, QCompleter,
)
from shared.ui_loader import UiLoader
from shared.vocabulary import get_vocabulary, record_use, vocabulary_path
from PySide6.QtCore import Qt, QFile, QObject, QUrl, Signal, Slot, QThread
from PySide6.QtGui import QDesktopServices, QPixmap, QColor


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


@dataclass(frozen=True)
class ExportOutcome:
    """How a named export ended, in the one shape the window handles.

    `error` covers the failures that stop a run before or outside the batch --
    an unreadable settings file, a refused preflight, a missing source. A batch
    that ran and then had per-clip problems is a `result` with failures, not an
    error. `out_dir` is carried rather than recomputed by the window, so the log
    line states the directory the batch actually used.

    `skipped` is how many planned clips the run left alone because an earlier
    run in this same session had already written them. It is the planner's
    count, not a window-side guess, and it stays 0 on the paths where no plan
    exists.
    """
    out_dir: str = ""
    result: ExportExecutionResult | None = None
    cancelled: bool = False
    error: str | None = None
    skipped: int = 0


@dataclass(frozen=True)
class ExportSummary:
    """What a finished run accomplished, in the shape the summary screen shows.

    Built from an `ExportOutcome` by `_export_summary` and nothing else, so the
    numbers on screen are the batch's own rather than a recount. A clean run
    always has at least one written clip: `plan_export` refuses an empty batch,
    so "nothing was written and nothing failed" is not an outcome that can
    reach this screen.
    """
    written: int
    failed: int
    skipped: int
    out_dir: str
    failures: tuple[ExportClipFailure, ...]


def _export_summary(outcome):
    """Describe a finished, non-cancelled run. Pure: no Qt, no filesystem."""
    result = outcome.result
    return ExportSummary(
        written=result.succeeded,
        failed=result.failed,
        skipped=outcome.skipped,
        out_dir=outcome.out_dir,
        failures=result.failures,
    )


#: What the user chose on the summary screen. `menu` closes the editor, whose
#: process exit hands the still-open main menu back to the foreground.
ACTION_MENU = 'menu'
ACTION_KEEP_EDITING = 'keep_editing'
ACTION_EXPORT_REST = 'export_rest'


def _describe_failures(failures):
    """One block per failed clip: which segment, which file, and why."""
    return "\n\n".join(
        f"Segment {failure.segment_index + 1} — {failure.destination}\n"
        f"{failure.message}"
        for failure in failures
    )


class ExportSummaryDialog(QDialog):
    """What the run accomplished, and what to do next.

    Shown over the editor once a batch has actually run, whether it wrote every
    clip or only some. It is built in code rather than loaded from a `.ui` file
    because it is a transient modal with no layout worth designing: no resource
    path, nothing to add to the packaged `.ui` payload, and the export path
    already builds its own widget here (the progress dialog).

    The editor stays disabled behind this for as long as it is up, so the
    buttons are the only live controls on screen and the answer is deliberate.
    `on_open_folder` is the window's own folder-opening action rather than
    something this class does itself, which keeps the desktop out of the dialog.
    """

    def __init__(self, summary, on_open_folder=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export complete")
        self._action = None
        self._on_open_folder = on_open_folder

        layout = QVBoxLayout(self)
        layout.addWidget(self._headline(summary))
        if summary.skipped:
            layout.addWidget(QLabel(
                f"{summary.skipped} clip(s) were already written and left alone."))
        layout.addWidget(self._destination_label(summary.out_dir))
        if summary.failures:
            layout.addWidget(QLabel(
                f"{summary.failed} clip(s) failed and were not written:"))
            layout.addWidget(self._failures_box(summary.failures))
        layout.addLayout(self._button_row(summary))

    def _headline(self, summary):
        if summary.failed:
            text = (f"Exported {summary.written} clip(s). "
                    f"{summary.failed} failed.")
        else:
            text = f"Exported {summary.written} clip(s)."
        label = QLabel(text)
        label.setWordWrap(True)
        return label

    def _destination_label(self, out_dir):
        """The destination, selectable so it can be copied out of the dialog."""
        label = QLabel(out_dir)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    def _failures_box(self, failures):
        box = QPlainTextEdit(_describe_failures(failures))
        box.setReadOnly(True)
        box.setMinimumSize(420, 120)
        return box

    def _button_row(self, summary):
        row = QHBoxLayout()
        if self._on_open_folder is not None:
            open_folder = QPushButton("Open export folder")
            open_folder.clicked.connect(
                lambda: self._on_open_folder(summary.out_dir))
            row.addWidget(open_folder)
        row.addStretch(1)

        keep_editing = QPushButton("Keep editing")
        keep_editing.clicked.connect(lambda: self._choose(ACTION_KEEP_EDITING))
        row.addWidget(keep_editing)

        # Only worth offering when this run wrote something: with nothing
        # written there is nothing to skip, and a plain re-export of the same
        # batch would simply fail the same way.
        if summary.failed and summary.written:
            export_rest = QPushButton("Export the rest")
            export_rest.clicked.connect(
                lambda: self._choose(ACTION_EXPORT_REST))
            row.addWidget(export_rest)

        back = QPushButton("Back to main menu")
        back.setDefault(True)
        back.clicked.connect(lambda: self._choose(ACTION_MENU))
        row.addWidget(back)
        return row

    def _choose(self, action):
        self._action = action
        self.accept()

    def chosen_action(self):
        """The button pressed, or keep-editing if the dialog was dismissed.

        Escape and the window close button land here too, and staying in the
        editor is the safe reading of an unasked question.
        """
        return self._action or ACTION_KEEP_EDITING


class ExportWorker(QObject):
    """Runs one named export off the GUI thread.

    Planning and transcoding both happen here, because planning walks the whole
    export tree twice and is as slow as the batch looks. The worker holds only
    plain data -- a model snapshot, the two schemes, a destination root -- and
    never a widget, so nothing it does can re-enter the editor.

    `finished` is emitted exactly once on every path, including a refused
    preflight, so the window has one place that tears the export down. The
    broad `except Exception` is deliberate: an unhandled exception in a QThread
    slot reaches PySide6's abort path, and install_excepthook does not stop it.
    """
    #: total clips, and how many of them this run is skipping
    planned = Signal(int, int)
    #: clips already finished, total, relative path of the clip now running
    advanced = Signal(int, int, str)
    finished = Signal(object)

    def __init__(self, source_path, model, schemes, out_dir,
                 skip_destinations=(), cancel_event=None):
        super().__init__()
        self.source_path = source_path
        self.model = model
        self.schemes = schemes
        self.out_dir = out_dir
        self.skip_destinations = tuple(skip_destinations)
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            plan = plan_export(
                self.model,
                self.schemes,
                self.out_dir,
                skip_destinations=self.skip_destinations,
            )
        except Exception as error:
            log_exception("export planning failed", error)
            self.finished.emit(ExportOutcome(out_dir=self.out_dir, error=str(error)))
            return

        if plan.skipped:
            log(f"export skipping {len(plan.skipped)} already-written clip(s)")
        self.planned.emit(len(plan.clips), len(plan.skipped))

        try:
            result = execute_export_plan(
                self.source_path,
                plan,
                on_progress=self.advanced.emit,
                should_cancel=self.cancel_event.is_set,
            )
        except Exception as error:
            log_exception("export execution failed", error)
            self.finished.emit(ExportOutcome(out_dir=self.out_dir, error=str(error)))
            return

        self.finished.emit(ExportOutcome(
            out_dir=self.out_dir,
            result=result,
            cancelled=result.cancelled,
            skipped=len(plan.skipped),
        ))


# Tag key → attribute name on self.ui for the tag fields that suggest from the
# vocabulary. Title is deliberately absent, for the same reason it is absent from
# _LOCK_BUTTONS below: it is unique per clip, so there is nothing worth carrying
# forward to the next segment and nothing worth suggesting back — a list of every
# title ever typed is a list with one use each. It stays a plain QLineEdit.
_SUGGESTED_TAG_FIELDS = {
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

# Every tag field on the form, in form order. Title is the odd one out, so the
# split is expressed once above rather than as two parallel lists to keep in
# step: a new tag goes in _SUGGESTED_TAG_FIELDS and lands here automatically.
_TAG_FIELDS = {
    "title": "lineEditTitle",
    **_SUGGESTED_TAG_FIELDS,
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

# The border on a required field that still needs a value. Built into a
# selector from the field's own class at use time, so changing a field's widget
# class cannot leave this selecting nothing -- which would remove the warning
# silently, since a non-matching QSS rule is not an error.
_REQUIRED_FIELD_BORDER = "1px solid #c0392b"

# Shown in a dropdown that has nothing in it yet, so an empty list reads as an
# instruction rather than as a broken control.
_EMPTY_VOCABULARY_HINT = "Populate this list by staging tags"


def _field_text(field):
    """The text a tag field currently holds.

    One accessor for one reason: the tag fields are two different widget types,
    and they disagree about both their accessors (`QLineEdit.text()`,
    `QComboBox.currentText()`) and their change signals. Every reader goes
    through here rather than each call site choosing one, so a widget type
    changing breaks one function instead of the ones nobody looked at. The
    editable combo's `currentText()` is the line edit's text when nothing in the
    list matches, which is what the form means -- a typed value, not a selection.
    """
    if isinstance(field, QComboBox):
        return field.currentText()
    return field.text()


def _field_change_signal(field):
    """The signal a tag field emits when the user changes its text.

    `QLineEdit` has no `editTextChanged` and `QComboBox` has no `textChanged`,
    so the choice lives here. Either way it fires on typing *and*, for the combo,
    on picking from the popup -- and the blockSignals pair around
    `_write_tags_to_form` keeps the programmatic write out of it.
    """
    if isinstance(field, QComboBox):
        return field.editTextChanged
    return field.textChanged


def _set_field_text(field, value):
    """Write a tag field's text, whichever of the two widget types it is.

    The counterpart to `_field_text`, for the same reason: `setEditText` exists
    only on the combo, and setting one field with the other's setter is a
    `AttributeError` at runtime rather than a mistake a reader can see.
    """
    if isinstance(field, QComboBox):
        field.setEditText(value)
    else:
        field.setText(value)


class MediaPlayer(QMainWindow):
    def __init__(self, media_path):
        super().__init__()

        # Define UI file

        ui_file = QFile(resource_path("editor", "editorwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            # Raised rather than printed and exited: in a windowed packaged
            # build a print goes nowhere.
            raise FileNotFoundError(
                f"Could not open the editor UI file: {ui_file.fileName()}")
        
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
        # The compilation video this run works on. It arrives from the picker
        # (or the scanner's handoff) and is already validated, so the editor
        # never guesses at a filename: the .cmct sidecar read below and every
        # export path derive from this one value.
        self.media_path = media_path
        self.player = create_mpv_player(video_frame)

        # Start paused so mpv's state and the button agree before any load.
        self.player.pause = True
        self.bridge = MpvBridge(self.player, parent=self)

        play_button = self.ui.playPause
        play_button.clicked.connect(self.on_transport_clicked)
        self.ui.forwardFrame.clicked.connect(lambda: self.on_step_frames(1))
        self.ui.backwardFrame.clicked.connect(lambda: self.on_step_frames(-1))
        self.ui.forwardKeyFrame.clicked.connect(lambda: self.on_step_keyframe(1))
        self.ui.backwardKeyFrame.clicked.connect(lambda: self.on_step_keyframe(-1))
        self.bridge.pauseChanged.connect(self.on_pause_changed)
        self.bridge.fileLoaded.connect(self.on_file_loaded)
        self.bridge.playbackEnded.connect(lambda: print("Reached end of file."))

        self.bridge.positionChanged.connect(self.ui.timelineWidget.set_position)
        self.bridge.durationChanged.connect(self.ui.timelineWidget.set_duration)
        self.ui.timelineWidget.seekRequested.connect(self.on_seek_requested)

        # Brief boundary peek on every active-segment change: the boundary
        # itself is a black frame, so stepping segments would otherwise only
        # ever show black. `frames`/`dwell_ms` are public attributes, so a
        # future settings value turns the peek off by setting frames to 0
        # (or None) and tunes the delay without touching this call site.
        self.boundary_preview = BoundaryPreview(
            self.bridge,
            frames=SEGMENT_PREVIEW_FRAMES,
            dwell_ms=SEGMENT_PREVIEW_DWELL_MS,
            parent=self,
        )

        self._sync_button(paused=True)

        # --- editing state ---
        self.segment_model = SegmentModel.load(sidecar_path(self.media_path))
        self.current_index = 0
        self.tag_locks = {}  # tag key → locked value, carried across unedited segments

        # --- export state ---
        # All four stay non-None only while a batch is running, and all four
        # exist so the running QThread is never garbage collected underneath
        # itself. _resume_skips is the destinations this session's own cancelled
        # run committed: the only thing that may be offered as a skip, since a
        # skip found by looking at the filesystem would silently stop a
        # re-tagged clip from ever being written again.
        self._export_thread = None
        self._export_worker = None
        self._export_dialog = None
        self._export_cancel = None
        self._resume_skips = frozenset()
        self._close_after_export = False
        self._last_export_outcome = None

        self.ui.clipEnd.clicked.connect(self.on_end_segment)
        self.ui.stageButton.clicked.connect(self.on_stage)
        self.ui.exportButton.clicked.connect(self.on_export)
        self.ui.toggleZoom.toggled.connect(self.on_toggle_zoom)
        # Open zoomed to the active segment. The toggle is the single source of
        # truth for the zoom mode from here on, so the widget can never end up
        # showing a different view than the button claims.
        self.ui.toggleZoom.setChecked(True)
        self.ui.activeLeft.clicked.connect(lambda: self._move_active(-1))
        self.ui.activeRight.clicked.connect(lambda: self._move_active(1))
        self.ui.mergeNext.clicked.connect(self.on_merge_next)
        self.ui.clipIgnore.toggled.connect(self.on_toggle_ignore)
        self.ui.clipStart.clicked.connect(self.on_start_segment)
        for key, attr in _LOCK_BUTTONS.items():
            getattr(self.ui, attr).toggled.connect(
                lambda checked, k=key: self.on_toggle_lock(k, checked))
        for key, attr in _TAG_FIELDS.items():
            _field_change_signal(getattr(self.ui, attr)).connect(
                lambda text, k=key: self.on_tag_edited(k, text))

        # Cache the authored stylesheets so the required-field outline can be
        # toggled without clobbering anything the .ui file set.
        self._required_base_styles = {
            key: getattr(self.ui, _TAG_FIELDS[key]).styleSheet()
            for key in _REQUIRED_TAG_FIELDS
        }
        self._init_tag_vocabulary()

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
        """Toggle between zoom-to-active-segment and zoom-fit-whole-video.

        The mode is handed to the timeline widget, which remembers it and
        re-applies it on resize, so the view survives a window resize.
        """
        self.ui.timelineWidget.set_zoom_mode(
            ZOOM_SEGMENT if checked else ZOOM_FIT)

    def _move_active(self, delta):
        """Move the active segment index by delta, clamped to valid range."""
        new_index = self.current_index + delta
        if 0 <= new_index < self.segment_model.segment_count():
            self._activate(new_index)

    def _activate(self, index, preview=True):
        """Make `index` the active segment and move the playhead to its start.

        Every path that *steps through* segments goes through here, so the
        boundary peek (and the playhead snap that follows it) cannot be
        applied to some transitions and forgotten on others: Active ←/→,
        Start Seg, and Stage. preview=False is for reverting the model (Undo),
        which is not the user looking at clips; the initial load snaps
        directly via _snap_playhead_to_active_start.
        """
        self.current_index = index
        self._snap_playhead_to_active_start(preview=preview)
        self._refresh_timeline()

    def on_seek_requested(self, position):
        """Timeline scrub: a user seek owns the playhead, so drop any peek."""
        self.boundary_preview.cancel()
        self.bridge.seek_exact(position)

    def on_step_frames(self, count):
        """Step by frames, abandoning any pending boundary peek."""
        self.boundary_preview.cancel()
        self.bridge.step_frames(count)

    def on_step_keyframe(self, direction):
        """Step to the next/previous keyframe, abandoning any pending peek."""
        self.boundary_preview.cancel()
        if direction >= 0:
            self.bridge.next_keyframe()
        else:
            self.bridge.prev_keyframe()

    def on_merge_next(self):
        """Merge the active segment into the next one."""
        if self.segment_model.merge_next(self.current_index):
            self.dirty = True
            self._update_stage_button()
            self._refresh_timeline()

    def on_start_segment(self):
        """Split the active segment at the playhead, activating the right part."""
        # The playhead is about to be read as a cut point, so it must not be
        # yanked back by a pending peek halfway through the action.
        self.boundary_preview.cancel()
        position = self.player.time_pos
        if position is None:
            return
        if self.segment_model.start_segment(
            self.current_index, position, self._inherited_tags()
        ):
            self.dirty = True
            self._update_stage_button()
            self._activate(self.current_index + 1)

    def _read_tags_from_form(self):
        """Snapshot the current form values into a tags dict."""
        return {key: _field_text(getattr(self.ui, attr)) for key, attr in _TAG_FIELDS.items()}

    def _configure_tag_combo(self, combo):
        """Make one tag field an editable combo that suggests from the vocabulary.

        The combo owns its own item list and refuses to grow one from typing:
        the default insert policy turns every half-remembered value into a
        permanent entry, which would quietly duplicate what the vocabulary file
        is for and leave the user scrolling through their own typos.

        The completer is what makes this more than a list to scroll: it narrows
        as you type, case-insensitively and by substring, so `toon` finds
        `Toonami`. `UnfilteredPopupCompletion` is the part that matters for
        correctness -- the default inline mode rewrites what you typed to match
        a completion, so pressing Enter commits `Toonami Kids` when you wrote
        `Toonami`.

        The popup is opened by calling the completer on each edit rather than by
        `QComboBox.setCompleterPopupVisible(True)`, which PySide6 does not
        expose. Signals are already blocked around every programmatic write, so
        this fires for typing and for picking, and not for the form being
        repopulated.
        """
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        combo.setCompleter(combo.completer())
        completer = combo.completer()
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        combo.editTextChanged.connect(
            lambda text: completer.complete() if text and combo.count() else None
        )

    def _init_tag_vocabulary(self, path=None):
        """Bind the tag dropdowns to a vocabulary file.

        `path` exists for the widget tests, which point it at a temp folder so a
        suite run cannot write to the real install root. Production leaves it
        alone and gets the file beside the executable.

        The most-recently-used order is per editor, not per process: a session
        is one source video, and opening the next one should not inherit the
        last one's habits at the top of every list.

        Configuring the combo lives here rather than in a separate call so there
        is one place that makes a tag field a dropdown at all -- a field that is
        populated but not configured is a closed list, which looks fine right up
        until someone tries to type.
        """
        for attr in _SUGGESTED_TAG_FIELDS.values():
            self._configure_tag_combo(getattr(self.ui, attr))
        self._vocabulary_path = path or vocabulary_path()
        self._vocabulary = get_vocabulary(self._vocabulary_path)
        self._recent_tag_values = {key: [] for key in _SUGGESTED_TAG_FIELDS}
        self._refresh_tag_combos()

    def _ordered_tag_values(self, namespace):
        """The values to offer for a namespace: most recently used first.

        "Recently" means this editing session -- one source video -- which is
        why the order lives on the editor rather than in the vocabulary file.
        The remainder is alphabetical so the list is deterministic and a value
        the user has not touched this session is still findable.
        """
        recent = [
            value for value in self._recent_tag_values.get(namespace, [])
            if value
        ]
        available = self._vocabulary.values(namespace)
        ordered = list(dict.fromkeys(recent))
        ordered.extend(
            value for value in available if value not in set(ordered)
        )
        return ordered

    def _refresh_tag_combos(self):
        """Repopulate every dropdown from the vocabulary, most recent first.

        `QComboBox.clear()` empties the line edit as well as the item list and
        emits change signals, so each field's text is saved and restored around
        the refill. Without that, a Stage would erase what the user is in the
        middle of typing into every *other* field.

        This is called when a stage adds values, never on segment navigation:
        repopulating while someone is mid-entry would eat the entry.
        """
        vocabulary = self._vocabulary
        for key, attr in _SUGGESTED_TAG_FIELDS.items():
            combo = getattr(self.ui, attr)
            values = self._ordered_tag_values(key)
            if not values and combo.count() == 1 and combo.itemText(0) == (
                _EMPTY_VOCABULARY_HINT
            ):
                continue
            # Saved and restored through the same accessor the form is read with, or a
            # refresh would hand back a different value than _read_tags_from_form
            # reports.
            typed = _field_text(combo)
            combo.blockSignals(True)
            combo.clear()
            for value in values:
                combo.addItem(value)
            if not values:
                combo.addItem(_EMPTY_VOCABULARY_HINT)
                # Disabled so it cannot be chosen as a tag by clicking it; the
                # line edit is still free text, so a typed value is unaffected.
                item = combo.model().item(0)
                if item is not None:
                    item.setEnabled(False)
            combo.setEditText(typed)
            combo.blockSignals(False)

    def _note_recent_tags(self, tags):
        """Move the values just committed to the front of their dropdowns.

        Takes only the suggestable tags -- see `_commit_form_tags_to_model`,
        which filters once and passes the result here and to `record_use`.

        Returns whether any ordering changed. Using a value already in the
        vocabulary changes no file, but it does change what the dropdown should
        lead with -- so the refresh cannot hang off the vocabulary's own
        "changed" answer, or re-using a value would leave the list showing
        whatever the last *new* value put there.
        """
        moved = False
        for key, value in tags.items():
            value = value.strip()
            if not value:
                continue
            recent = self._recent_tag_values.setdefault(key, [])
            if recent[:1] == [value]:
                continue
            if value in recent:
                recent.remove(value)
            recent.insert(0, value)
            moved = True
        return moved

    def _commit_form_tags_to_model(self):
        """Write the form's tags into the model and note them as used.

        The one place form-typed values become model tags. The boundary
        operations deliberately pass the lock values rather than the form, so
        these two call sites -- Stage and export preparation -- are the complete
        definition of "a tag was used". That matters because the vocabulary is
        fed here rather than from a keystroke: a form read mid-entry holds
        `Cartoon N`, which is not a tag anybody means.

        Both run after their own required-tag check, so a stage refused for a
        missing required field records nothing.

        The whole record goes to the model; only the *suggestable* tags go to
        the vocabulary, because the file exists to populate dropdowns and title
        has no dropdown. Filtering once here rather than in `record_use` keeps
        `shared/vocabulary.py` a plain store that does not need to know which of
        the tags are worth remembering.
        """
        tags = self._read_tags_from_form()
        self.segment_model.segments[self.current_index]["tags"] = tags
        suggested = {
            key: value for key, value in tags.items()
            if key in _SUGGESTED_TAG_FIELDS
        }
        reordered = self._note_recent_tags(suggested)
        if record_use(suggested, self._vocabulary_path) or reordered:
            self._refresh_tag_combos()

    def _write_tags_to_form(self, tags):
        """Populate the form from a tags dict.

        On a still-unedited segment, locked tags are pre-filled from
        self.tag_locks (overriding any stored empty value), since locks are
        the only source of input for unedited segments. Signals are blocked
        around the write so the dirty flag isn't set by it.
        """
        unedited = not self._is_segment_edited(self.current_index)
        for key, attr in _TAG_FIELDS.items():
            field = getattr(self.ui, attr)
            if unedited and key in self.tag_locks:
                value = self.tag_locks[key]
            else:
                value = tags.get(key, "")
            field.blockSignals(True)
            _set_field_text(field, value)
            field.blockSignals(False)

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
            current = _field_text(getattr(self.ui, _TAG_FIELDS[key]))
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
            self.tag_locks[key] = _field_text(getattr(self.ui, _TAG_FIELDS[key]))
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
            field = getattr(self.ui, _TAG_FIELDS[key])
            field.setStyleSheet(
                f"{type(field).__name__} {{ border: {_REQUIRED_FIELD_BORDER}; }}"
                if label in missing else self._required_base_styles[key]
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
        # Reverting is not stepping through segments, so no boundary peek.
        self._activate(self.current_index, preview=False)

    def _snap_playhead_to_active_start(self, preview=False):
        """Seek mpv to the start of the current active segment.

        With preview=True the playhead first peeks a few frames past the
        boundary — which is a black frame — and comes back here; see
        shared.mpv.BoundaryPreview. Either way the resting position is the
        exact boundary, because that is where End Seg and Start Seg cut.
        """
        self.boundary_preview.cancel()
        if self.current_index >= self.segment_model.segment_count():
            return
        boundary = self.segment_model.start(self.current_index)
        if preview:
            self.boundary_preview.flash(boundary)
        else:
            self.bridge.seek_exact(boundary)

    def _refresh_timeline(self):
        """Push the current segment model + active index onto the timeline."""
        segments = [Segment(self.segment_model.start(i), self.segment_model.end(i),
                            self.segment_model.segments[i]["ignored"])
                    for i in range(self.segment_model.segment_count())]
        self.ui.timelineWidget.set_duration(self.segment_model.duration)
        self.ui.timelineWidget.set_segments(segments)
        self.ui.timelineWidget.set_active_index(self.current_index)
        self.ui.timelineWidget.set_zoom_mode(
            ZOOM_SEGMENT if self.ui.toggleZoom.isChecked() else ZOOM_FIT,
            self.current_index)
        self.ui.clipIgnore.blockSignals(True)
        self.ui.clipIgnore.setChecked(self.segment_model.segments[self.current_index]["ignored"])
        self.ui.clipIgnore.blockSignals(False)
        self._write_tags_to_form(self.segment_model.segments[self.current_index]["tags"])
        self._refresh_lock_buttons()
        self._refresh_required_fields()

    def on_end_segment(self):
        """Place the active segment's end boundary at the playhead.

        Inside the active segment this splits it, creating a new segment that
        keeps the active index. Past the end it moves the boundary forward
        within the following segment, so nudging an end boundary forward no
        longer needs a multi-step detour. Only one transition point is ever
        touched; a playhead far enough forward to cross a second boundary is
        refused rather than clamped.
        """
        # The playhead is read as the cut point, so a pending peek must not
        # still be armed to move it while this runs.
        self.boundary_preview.cancel()
        position = self.player.time_pos
        if position is None:
            return
        outcome = self.segment_model.place_end_boundary(
            self.current_index,
            position,
            self._inherited_tags(),
        )
        if outcome == END_BOUNDARY_BLOCKED:
            QMessageBox.warning(
                self,
                "Boundary not moved",
                "The playhead is past the end of the following segment, so End "
                "Seg cannot place the boundary there without crossing more "
                "than one boundary.\n\nUse Add Next Seg to absorb the following "
                "segment into this one, or navigate to it and cut there "
                "instead.",
            )
            return
        if outcome == END_BOUNDARY_NO_CHANGE:
            return
        self.dirty = True
        self._update_stage_button()
        self._refresh_timeline()

    def on_stage(self):
        """Lock in the active segment, advance to the next.

        A keep segment missing any of the four base record fields is refused
        before anything is written, so the .cmct sidecar never receives a
        staged record that export would later reject.
        """
        # A refused or completing stage never re-arms the peek, so drop any
        # pending one up front rather than leaving it to fire mid-dialog.
        self.boundary_preview.cancel()
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
        self._commit_form_tags_to_model()
        self.segment_model.save(sidecar_path(self.media_path))
        self.dirty = False
        self._update_stage_button()
        if self.current_index + 1 >= self.segment_model.segment_count():
            QMessageBox.information(
                self,
                "Every segment is staged",
                "That was the last segment.\n\n"
                "When the tags look right, press Finished - Export to write the "
                "clips into your library.",
            )
            return
        self._activate(self.current_index + 1)

    def on_export(self):
        """Validate and persist edits, then transcode every keep clip off-thread.

        The window is frozen and the batch runs on a worker thread, because
        planning alone walks the whole export tree and the transcode is a
        blocking subprocess per segment: doing either on the GUI thread leaves
        the editor looking hung for the length of a long source.
        """
        self._begin_export()

    def _begin_export(self, skip_destinations=None):
        """Prepare a batch and start it, asking about a resume only if asked to.

        `skip_destinations` is the already-decided answer, passed by the caller
        that has just asked the user -- the summary screen's "Export the rest".
        Going through `on_export` instead would ask the resume question a
        second time about a run the user had already answered for.
        """
        if self._export_thread is not None:
            return
        # Export runs for minutes; a peek firing during it would move the
        # playhead for no reason.
        self.boundary_preview.cancel()

        prepared = self._prepare_export()
        if prepared is None:
            return
        model, schemes, out_dir = prepared

        if skip_destinations is None:
            skip_destinations = self._choose_resume_skips()
            if skip_destinations is None:
                return
        self._start_export(model, schemes, out_dir, skip_destinations)

    def _prepare_export(self):
        """Check the form, persist the sidecar, and snapshot the batch.

        Returns (model snapshot, schemes, export root), or None after showing
        the reason it could not. This stays on the GUI thread: it touches the
        form and the live model, and it is fast, so the user still gets an
        immediate "Export could not start" instead of waiting on a worker.
        """
        try:
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
                self._commit_form_tags_to_model()
            validate_segment_model(self.segment_model)
            self.segment_model.save(sidecar_path(self.media_path))
            self.dirty = False
            self._update_stage_button()
            return (
                model_with_tag_locks(self.segment_model, self.tag_locks),
                load_export_schemes(os.path.join(PROJECT_ROOT, "settings.json")),
                os.path.join(PROJECT_ROOT, "export"),
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Export could not start", str(error))
            return None

    def _choose_resume_skips(self):
        """Ask whether to resume after a run that did not finish, or None to not
        start.

        A cancelled run and a run left with per-clip failures both leave clips
        on disk, and the preflight refuses any destination that exists, so a
        plain retry would fail on every one of them. "Start over" is kept as the
        honest alternative: it leaves the skip list empty, so the refusal names
        each existing file and the user can decide what to do with it.
        """
        if not self._resume_skips:
            return ()
        count = len(self._resume_skips)
        answer = QMessageBox.question(
            self,
            "Skip the clips already written?",
            f"{count} clip(s) from your last export are already written.\n\n"
            "Export the rest, skipping those?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            return tuple(self._resume_skips)
        self._resume_skips = frozenset()
        return ()

    def _start_export(self, model, schemes, out_dir, skip_destinations):
        """Freeze the editor and hand the batch to a worker thread."""
        # mpv's renderer would otherwise decode against libx264 for the length
        # of the batch. pauseChanged re-syncs the Play button from this.
        self.player.pause = True

        self._export_cancel = threading.Event()
        self._export_worker = ExportWorker(
            self.media_path,
            model,
            schemes,
            out_dir,
            skip_destinations=skip_destinations,
            cancel_event=self._export_cancel,
        )
        self._export_thread = QThread(self)
        self._export_worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(self._export_worker.run)
        self._export_worker.planned.connect(self._on_export_planned)
        self._export_worker.advanced.connect(self._on_export_advanced)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_thread.finished.connect(self._on_export_stopped)

        self.setEnabled(False)
        # Deliberately parentless: setEnabled(False) above cascades to child
        # widgets, and a disabled QProgressDialog's Cancel button does nothing.
        # self._export_dialog keeps it alive instead. The freeze, not the
        # dialog's modality, is what blocks input -- it is left non-modal so the
        # window's own close button still reaches closeEvent and can ask about
        # cancelling the run.
        dialog = QProgressDialog()
        dialog.setWindowTitle("Exporting clips")
        dialog.setLabelText("Planning destinations…")
        dialog.setRange(0, 0)
        dialog.setCancelButtonText("Cancel")
        dialog.setMinimumDuration(0)
        # Both default to closing (and emitting canceled) the moment the value
        # reaches the maximum, which would read as the user cancelling a
        # successful export.
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(self._on_export_cancel_requested)
        dialog.show()
        self._export_dialog = dialog

        self._export_thread.start()

    def _on_export_planned(self, total, skipped):
        dialog = self._export_dialog
        if dialog is None:
            return
        dialog.setRange(0, max(total, 1))
        dialog.setValue(0)
        if skipped:
            dialog.setLabelText(
                f"Skipping {skipped} already-written clip(s). "
                f"Exporting 0 of {total}…"
            )
        else:
            dialog.setLabelText(f"Clip 1 of {total}…")

    def _on_export_advanced(self, clips_done, total, current):
        dialog = self._export_dialog
        if dialog is None:
            return
        if not current:
            dialog.setValue(total)
            dialog.setLabelText(f"Wrote {total} of {total} clip(s).")
            return
        dialog.setValue(clips_done)
        dialog.setLabelText(
            f"Clip {min(clips_done + 1, total)} of {total} — {current}"
        )

    def _on_export_cancel_requested(self):
        """Ask the worker to stop. The encoder is terminated at its next check."""
        if self._export_cancel is not None:
            self._export_cancel.set()
        dialog = self._export_dialog
        if dialog is not None:
            # The button has already done its job; leaving it looking live is how
            # a cancel turns into "did that work?".
            dialog.setCancelButton(None)
            dialog.setLabelText("Cancelling…")

    def _on_export_finished(self, outcome):
        """Log the batch and stash what a resume would need. One code path."""
        self._last_export_outcome = outcome
        if outcome.error is not None:
            log(f"export failed to run: {outcome.error}")
            self._resume_skips = frozenset()
        else:
            result = outcome.result
            log(
                f"export to {outcome.out_dir}: "
                f"{result.succeeded} written, {result.failed} failed, "
                f"{'cancelled' if outcome.cancelled else 'complete'}"
                + (f", {outcome.skipped} skipped" if outcome.skipped else "")
            )
            # A run that did not finish is the only thing worth resuming, and
            # only the clips it actually committed -- so a cancelled run and a
            # run with per-clip failures both arm the list, and a clean one
            # leaves it empty. Either way the entries are this session's own
            # committed destinations and nothing else.
            self._resume_skips = (
                frozenset(result.written_relative_paths)
                if (outcome.cancelled or result.failures)
                else frozenset()
            )
        if self._export_thread is not None:
            self._export_thread.quit()

    def _on_export_stopped(self):
        """Tear the run down and report. Reached only from thread.finished.

        The window is deliberately left disabled here: every way out of
        `_report_export_outcome` either re-enables it or closes it, so a live
        looking editor is never sitting behind the summary screen.
        """
        dialog = self._export_dialog
        if dialog is not None:
            dialog.reset()
            dialog.deleteLater()
        thread = self._export_thread
        if thread is not None:
            thread.deleteLater()
        if self._export_worker is not None:
            self._export_worker.deleteLater()
        self._export_dialog = None
        self._export_thread = None
        self._export_worker = None
        self._export_cancel = None

        if self._close_after_export:
            self._close_after_export = False
            self.setEnabled(True)
            self.close()
            return
        outcome, self._last_export_outcome = self._last_export_outcome, None
        if outcome is None:
            log("export thread ended without reporting an outcome")
            self.setEnabled(True)
            return
        self._report_export_outcome(outcome)

    def _report_export_outcome(self, outcome):
        """Say what happened, then leave the window in the state it implies.

        A refusal and a cancel have nothing to summarize, so they keep their
        message boxes and hand the editor straight back. A run that actually
        transcoded gets the summary screen: what was written, where it went,
        and what failed, with the choice of what to do next -- because
        "Finished - Export" is the end of the wizard and the user is owed a way
        out of it.
        """
        if outcome.error is not None:
            QMessageBox.warning(self, "Export could not start", outcome.error)
            self.setEnabled(True)
            return
        result = outcome.result
        if outcome.cancelled:
            self.setEnabled(True)
            self._show_export_cancelled(result)
            return

        summary = _export_summary(outcome)
        action = self._ask_export_summary(summary)
        if action == ACTION_MENU:
            self.close()
            return
        if action == ACTION_EXPORT_REST:
            # The written clips are the ones this run produced, so they are the
            # only destinations skipped -- the same invariant a cancelled run
            # resumes under. The list is cleared because the question it stood
            # for has been asked and answered.
            written = tuple(result.written_relative_paths)
            self._resume_skips = frozenset()
            self.setEnabled(True)
            self._begin_export(skip_destinations=written)
            return
        self.setEnabled(True)

    def _ask_export_summary(self, summary):
        """Show the summary and return the button the user pressed.

        The dialog is released before the answer is acted on, so by the time
        anything else happens it is no longer a top-level window: the editor is
        then the last one, and closing it ends the process.
        """
        dialog = ExportSummaryDialog(
            summary, on_open_folder=self._open_export_folder, parent=self)
        dialog.exec()
        action = dialog.chosen_action()
        dialog.deleteLater()
        return action

    def _open_export_folder(self, out_dir):
        """Reveal the export folder in the desktop's file browser.

        A method on the window rather than something the dialog does itself, so
        the one place that touches the desktop is also the one a test can drive.
        """
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(out_dir))
        except OSError as error:
            log_exception(f"could not open {out_dir}", error)
            return
        if not opened:
            log(f"the desktop declined to open {out_dir}")

    def _show_export_cancelled(self, result):
        """Name what survived, since those files are the user's to keep."""
        written = result.succeeded
        body = (
            f"Export cancelled after {written} clip(s).\n\n"
            "The clips already written were kept."
        )
        if result.failed:
            body += f"\n\n{result.failed} clip(s) also failed; see commcut.log."
        if written:
            body += (
                "\n\nPress Export again and it will ask whether to skip those "
                "and write the rest."
            )
        QMessageBox.information(self, "Export cancelled", body)

    def closeEvent(self, event):
        """Shut the player down, and never destroy a running QThread.

        The two halves are in this order for two different reasons.

        ``bridge.shutdown()`` goes on the accept path and nowhere else, because
        it has to run while this window's video frame still has its native
        handle. Calling it at the top would kill the player underneath a session
        that is about to refuse to close. It is on the accept path rather than
        after it because the player must be gone before the window is destroyed,
        and closeEvent is the last code that runs before that.
        """
        if self._export_thread is None:
            self.bridge.shutdown()
            event.accept()
            return
        event.ignore()
        answer = QMessageBox.question(
            self,
            "Export in progress",
            "An export is in progress.\n\nCancel it and close?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._close_after_export = True
            self._on_export_cancel_requested()
            # The close happens again from _on_export_finished, once
            # _export_thread is done, and that second pass is the one that
            # accepts -- and so the one that shuts the player down.

    def on_file_loaded(self, path):
        """Called when mpv finishes loading a file: snap to the active segment start."""
        print(f"Loaded: {path}")
        self._snap_playhead_to_active_start()

    def on_transport_clicked(self):
        # Play/pause means the user has taken over the playhead.
        self.boundary_preview.cancel()
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


def create(app, source):
    """Build the editor window for `source`. Returns the window, unscaled.

    `app` is the process's QApplication, owned by main.py — this window does not
    make one and does not run an event loop, because it shares the loop with the
    menu and with whatever else is open. main.py shows it through the shell.

    `source` is the video to work on, and it arrives as an argument rather than
    being re-derived here: this window never guesses which video it is for.
    """
    media_path = require_source_video(source)
    log(f"editor working on {media_path}")

    pixmap = QPixmap(480, 270)
    pixmap.fill(QColor(30, 30, 30))
    splash = QSplashScreen(pixmap)
    splash.show()

    splash.showMessage(f"Now loading {os.path.basename(media_path)}…",
                       Qt.AlignCenter | Qt.AlignBottom, QColor(200, 200, 200))
    app.processEvents()

    keyframes = scan_keyframes(media_path)

    sidecar = sidecar_path(media_path)
    if not os.path.exists(sidecar):
        duration = probe_duration(media_path) or 0.0
        SegmentModel.placeholder(os.path.basename(media_path), duration).save(sidecar)

    # The splash is closed BEFORE MediaPlayer() constructs the mpv player. That
    # hazard was never reproduced -- experiments/mpv_foreground/ ran the splash
    # case 20 times against the shipped driver with no hang -- but closing a
    # splash is three lines, it costs nothing when it turns out to be
    # unnecessary, and it is the only thing standing between a driver update
    # and a frozen window. Kept until the packaged build has run on hardware
    # nobody here has tested.
    splash.close()
    window = MediaPlayer(media_path)
    window.bridge.set_keyframes(keyframes)
    window.segment_model = SegmentModel.load(sidecar_path(media_path))
    window.resize(1024, 768)
    return window
