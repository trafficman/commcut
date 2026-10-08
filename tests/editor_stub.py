"""Shared widget-backed stub for exercising the real editor methods.

The editor window itself needs libmpv and a real video, so tests bind the
real ``MediaPlayer`` methods onto this stub, which supplies only the ``ui``
namespace and the segment model. That keeps the code under test the shipped
code rather than a re-implementation.

The stub supplies only the ``ui`` namespace and the segment model; the tag form
is the *real* one from ``shared/tag_form.py``, promoted into ``editorwindow.ui``
in the shipped editor. That is not cosmetic: the form used to be ten widgets
hand-built to mirror what ``editorwindow.ui`` described, which meant a rename or
a dropped widget property could leave every test green while the shipped form
was wrong. Now there is one form and the stub uses it.
"""

import os
import tempfile

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton

from editor.editor import (
    ACTION_KEEP_EDITING,
    ExportWorker,
    MediaPlayer,
)
from shared.mpv import BoundaryPreview
from shared.segments import SegmentModel
from shared.tag_form import (
    LOCK_BUTTONS,
    REQUIRED_TAG_LABELS,
    TAG_FIELDS,
    TagForm,
    field_change_signal,
    set_field_text,
)
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
        # The real bridge's teardown. It has to be called before the window is
        # destroyed, because the player is embedded into the video frame's
        # native handle -- so the order of shutdown() relative to destruction
        # is the thing under test, and the count is how a test sees it.
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1

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


class FakeSignal:
    """A stand-in for a Qt signal: collect the slots, then fire them in order.

    Signals rather than hand-called methods, so the shipped wiring
    (`thread.started.connect(worker.run)`) runs exactly as written instead of
    being re-implemented to fit the fake.
    """

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class FakeExportWorker:
    """Stands in for ExportWorker so the window's wiring runs synchronously.

    It forwards to a real, un-moved ExportWorker, so the shipped `run()` and its
    signals still execute. The substitute is needed because the shipped code
    calls `moveToThread` on the worker: a QObject moved to a thread that never
    started delivers its signals *queued*, so a stubbed thread would quietly
    turn every wiring test into a no-op that asserts nothing. The real worker
    against a real QThread is covered by the responsiveness test.
    """

    def __init__(self, source_path, model, schemes, out_dir,
                 skip_destinations=(), cancel_event=None):
        self.planned = FakeSignal()
        self.advanced = FakeSignal()
        self.finished = FakeSignal()
        self.thread = None
        self.deleted = False
        self._real = ExportWorker(
            source_path,
            model,
            schemes,
            out_dir,
            skip_destinations=skip_destinations,
            cancel_event=cancel_event,
        )

    def moveToThread(self, thread):
        self.thread = thread

    def run(self):
        self._real.planned.connect(self.planned.emit)
        self._real.advanced.connect(self.advanced.emit)
        self._real.finished.connect(self.finished.emit)
        self._real.run()

    def deleteLater(self):
        self.deleted = True


class FakeThread(QThread):
    """A QThread that never starts, so the export wiring runs synchronously.

    A real thread would make these tests wait on an event loop, which is how a
    test file starts flaking. This is still a real QThread, because the shipped
    code hands one to `QObject.moveToThread`; it simply emits the same signals a
    running one would, in the same order, from the calling thread. With
    `auto_run=False` the batch is left parked, so a test can inspect the frozen
    editor while the run is nominally in flight.
    """

    def __init__(self, parent=None):
        # The window is not a QObject in the stub, so `parent` cannot be used.
        super().__init__()
        self.quit_calls = 0
        self.running = False
        self.deleted = False
        self.auto_run = True

    def start(self):
        self.running = True
        if not self.auto_run:
            return
        self.started.emit()
        # What the real thread does once its event loop ends. The worker has
        # already called quit() by now, so this is the shipped order.
        self.running = False
        self.finished.emit()

    def quit(self):
        self.quit_calls += 1

    def deleteLater(self):
        self.deleted = True


class FakeLoadingDialog:
    """The LoadingDialog surface the export handlers drive."""

    def __init__(self, message: str = "", cancellable: bool = False,
                 parent=None, modal: bool = True, title: str = "commcut"):
        self.message = message
        self.title = title
        self.parent = parent
        self.modal = modal
        self.cancellable = cancellable
        self.minimum = 0
        self.maximum = 0
        self.value = 0
        self.shown = False
        self.deleted = False
        self.cancel_disabled = False
        self.canceled = FakeSignal()

    @property
    def progress_bar(self):
        return self

    @property
    def label(self):
        return self.message

    def set_message(self, text):
        self.message = text

    def set_range(self, minimum, maximum):
        self.minimum = minimum
        self.maximum = maximum

    def set_value(self, value):
        self.value = value

    def setEnabled(self, enabled):
        if not enabled:
            self.cancel_disabled = True

    def show(self):
        self.shown = True

    def deleteLater(self):
        self.deleted = True

    def press_cancel(self):
        """What clicking the Cancel button does."""
        self.cancel_disabled = True
        self.canceled.emit()


class FakeSummaryDialog:
    """Stands in for ExportSummaryDialog, whose exec() would block the run.

    Substituted as a class, so the shipped `_ask_export_summary` still runs --
    including the release-before-close ordering that stops the editor's process
    from lingering with the dialog's window still counted. Records itself on
    the stub it is parented to, and reads its answer from that stub's queue.
    """

    def __init__(self, summary, on_open_folder=None, parent=None):
        self.summary = summary
        self.on_open_folder = on_open_folder
        self.parent = parent
        self.exec_calls = 0
        self.deleted = False
        # The freeze has to be asserted at the moment the screen goes up, since
        # the answer comes back synchronously here and the editor is live again
        # by the time a test could look.
        self.editor_enabled_when_shown = parent.editor_enabled
        parent.summaries_seen.append(summary)
        parent.summary_dialogs_seen.append(self)

    def exec(self):
        self.exec_calls += 1
        return 1

    def chosen_action(self):
        answers = self.parent.summary_answers
        return answers.pop(0) if answers else ACTION_KEEP_EDITING

    def deleteLater(self):
        self.deleted = True

    def open_export_folder(self):
        """What the Open export folder button does."""
        self.on_open_folder(self.summary.out_dir)


class EditorStub:
    """Minimal MediaPlayer surface covering the tag and segment code paths."""

    _is_segment_edited = MediaPlayer._is_segment_edited
    _write_tags_to_form = MediaPlayer._write_tags_to_form
    _refresh_lock_buttons = MediaPlayer._refresh_lock_buttons
    _refresh_required_fields = MediaPlayer._refresh_required_fields
    _read_tags_from_form = MediaPlayer._read_tags_from_form
    _commit_form_tags_to_model = MediaPlayer._commit_form_tags_to_model
    _init_tag_vocabulary = MediaPlayer._init_tag_vocabulary
    _refresh_tag_combos = MediaPlayer._refresh_tag_combos
    _note_recent_tags = MediaPlayer._note_recent_tags
    _update_stage_button = MediaPlayer._update_stage_button
    _inherited_tags = MediaPlayer._inherited_tags
    _missing_required_labels = MediaPlayer._missing_required_labels
    _save_sidecar = MediaPlayer._save_sidecar
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
    # The export path, driven through the same stub: on_export and its handlers
    # are the shipped wiring, and the QThread is the one thing stubbed out.
    on_export = MediaPlayer.on_export
    _begin_export = MediaPlayer._begin_export
    _prepare_export = MediaPlayer._prepare_export
    _choose_resume_skips = MediaPlayer._choose_resume_skips
    _start_export = MediaPlayer._start_export
    _on_export_planned = MediaPlayer._on_export_planned
    _on_export_advanced = MediaPlayer._on_export_advanced
    _on_export_cancel_requested = MediaPlayer._on_export_cancel_requested
    _on_export_finished = MediaPlayer._on_export_finished
    _on_export_stopped = MediaPlayer._on_export_stopped
    _report_export_outcome = MediaPlayer._report_export_outcome
    _show_export_cancelled = MediaPlayer._show_export_cancelled
    _ask_export_summary = MediaPlayer._ask_export_summary
    closeEvent = MediaPlayer.closeEvent

    def __init__(self, segments, duration=120.0, media_path=None):
        ensure_qapp()
        self.ui = type("Ui", (), {})()
        # The real shared form, not a hand-built imitation of it.
        #
        # This used to construct ten widgets to mirror what editorwindow.ui
        # described, which meant a rename or a dropped widget property in the
        # .ui could leave every test green while the shipped form was wrong. The
        # form is one widget now, so the stub uses it.
        self.ui.tagForm = TagForm()
        for attr in ("stageButton", "undoButton", "clipIgnore"):
            setattr(self.ui, attr, QPushButton())
        # The Skip toggle is checkable in editorwindow.ui; without that,
        # setChecked would be a no-op and the ignored flag would never stick.
        self.ui.clipIgnore.setCheckable(True)
        for namespace in LOCK_BUTTONS:
            self.ui.tagForm.lock_button(namespace).toggled.connect(
                lambda checked, k=namespace: self.on_toggle_lock(k, checked))
        for namespace in TAG_FIELDS:
            field_change_signal(self.ui.tagForm.field(namespace)).connect(
                lambda text, k=namespace: self.on_tag_edited(k, text))
        # The tag dropdowns, pointed at a private temp folder per instance so a
        # suite run cannot write to the real install root and no two editors
        # share a vocabulary. The production path is covered by
        # test_vocabulary.py, which has no widgets to get wrong.
        self._init_tag_vocabulary(
            os.path.join(tempfile.mkdtemp(prefix="commcut-editor-vocab-"),
                         "vocabulary.json")
        )
        for attr in ("clipEnd", "clipStart", "exportButton"):
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
        # The required-field outline's base stylesheets are cached by TagForm
        # itself, in init_vocabulary, so the stub does not repeat that here.

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
        # The export lifecycle, in the state MediaPlayer.__init__ leaves it in.
        self._export_thread = None
        self._export_worker = None
        self._export_dialog = None
        self._export_cancel = None
        self._resume_skips = frozenset()
        self._close_after_export = False
        self._last_export_outcome = None
        # What the summary screen was shown, what it answered, and the dialogs
        # themselves so a test can assert on their lifecycle. The dialog is the
        # one substituted widget, for the same reason LoadingDialog is:
        # exec() would block the run.
        self.summaries_seen = []
        self.summary_dialogs_seen = []
        self.summary_answers = []
        self.opened_folders = []
        # setEnabled() is a QWidget method the stub does not inherit, so the
        # export freeze is observable through a recorded flag instead. That is
        # the property the tests care about: the editor is frozen for the run.
        self.editor_enabled = True
        self.closed = False
        # Mirrors the _refresh_timeline() call at the end of
        # MediaPlayer.__init__, so the initial form/lock/outline/zoom state
        # matches.
        self._refresh_timeline()

    # --- export seams ---

    def setEnabled(self, enabled):
        self.editor_enabled = enabled

    def close(self):
        self.closed = True

    # --- summary screen seams ---

    def _open_export_folder(self, out_dir):
        """Records the request instead of reaching the desktop."""
        self.opened_folders.append(out_dir)

    def last_summary(self):
        return self.summaries_seen[-1] if self.summaries_seen else None

    # --- helpers ---

    def set_tag(self, key, value):
        # Deliberately not `TagForm.write_tags`, which blocks the change signals
        # so a programmatic fill cannot mark a segment edited. A test's set_tag
        # is meant to behave like a person typing: the field's signal fires, the
        # required-field outline re-derives, and the tests that assert on the
        # outline see it change.
        set_field_text(self.ui.tagForm.field(key), value)

    def get_tag(self, key):
        return self.ui.tagForm.read_tags()[key]

    def combo_for(self, key):
        """The tag field's combo, so a test can read the offered values.

        Raises for a field that is not a dropdown -- title -- which is the
        assertion a test that pokes at this is really making.
        """
        combo = self.ui.tagForm.field(key)
        assert isinstance(combo, QComboBox), f"{key} is not a dropdown"
        return combo

    def offered_values(self, key):
        """The values a dropdown currently lists, in the order it lists them."""
        return tuple(
            self.combo_for(key).itemText(index)
            for index in range(self.combo_for(key).count())
        )

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
        self.ui.tagForm.lock_button(key).click()

    def checked_locks(self):
        return sorted(
            key for key in LOCK_BUTTONS
            if self.ui.tagForm.lock_button(key).isChecked()
        )

    def outlined_required_fields(self):
        """Tag keys of required fields currently showing the warning style."""
        return sorted(
            key for key in REQUIRED_TAG_LABELS
            if self.ui.tagForm.field(key).styleSheet()
        )

    def form(self):
        return {
            key: value for key, value in self.ui.tagForm.read_tags().items()
            if value
        }

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
