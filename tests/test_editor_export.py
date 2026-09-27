"""Tests for the named export: the worker thread, progress, cancel, and resume.

These drive the shipped `MediaPlayer` export methods through `EditorStub`, with
three things substituted and nothing else: `QThread` (so the batch is
synchronous and the suite cannot wait on an event loop), `QProgressDialog` (so
the bar's state is inspectable), and `QMessageBox` (so a dialog cannot block
the run). The one exception is `test_the_gui_thread_is_not_blocked_by_the_batch`,
which uses a real QThread, because that is the whole point of the change.
"""

import os
import threading
import time

import pytest
from PySide6.QtCore import QObject, Qt, Signal, QThread
from PySide6.QtWidgets import QApplication

import editor.editor as editor_module
from editor.editor import ExportOutcome, ExportWorker
from editor_stub import (
    EditorStub,
    FakeDialog,
    FakeExportWorker,
    FakeThread,
    ensure_qapp,
)
from shared.exporting import (
    ExportPlan,
    ExportPlanError,
    ExportSchemes,
    PlannedExportClip,
)
from shared.ffmpeg import (
    ExportClipFailure,
    ExportExecutionResult,
)
from shared.paths import DEFAULT_FOLDER_SCHEME
from shared.segments import SegmentModel, sidecar_path



# ---------------------------------------------------------------------------
# Dialog stand-ins
# ---------------------------------------------------------------------------

class FakeMessageBox:
    """QMessageBox without the modality.

    `answers` is consumed in order by `question`, so a test states what the user
    clicks before the code under test asks.
    """

    Yes = 0x00004000
    No = 0x00010000

    answers = []
    questions_seen = []
    warnings_seen = []
    information_seen = []

    @classmethod
    def reset(cls, *answers):
        cls.answers = list(answers)
        cls.questions_seen = []
        cls.warnings_seen = []
        cls.information_seen = []

    @classmethod
    def question(cls, parent, title, text, buttons=None, default=None):
        cls.questions_seen.append((title, text))
        return cls.answers.pop(0) if cls.answers else cls.No

    @classmethod
    def warning(cls, parent, title, text):
        cls.warnings_seen.append((title, text))

    @classmethod
    def information(cls, parent, title, text):
        cls.information_seen.append((title, text))


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    FakeMessageBox.reset()
    monkeypatch.setattr(editor_module, "QMessageBox", FakeMessageBox)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def keep_segment(title="Some Clip", start=0.0):
    return {
        "start": start,
        "ignored": False,
        "tags": {
            "title": title,
            "network": "Cartoon Network",
            "filler_type": "Promo",
            "time_period": "2000s",
        },
    }


def make_plan(clips):
    return ExportPlan(
        export_root="unused",
        clips=tuple(
            PlannedExportClip(
                segment_index=index,
                start=0.0,
                duration=2.0,
                relative_components=("Network", f"Clip {index + 1}.mp4"),
            )
            for index in range(clips)
        ),
    )


def make_result(written=(), failures=(), cancelled=False):
    return ExportExecutionResult(
        written_paths=tuple(f"written/{name}" for name in written),
        written_relative_paths=tuple(written),
        failures=tuple(failures),
        cancelled=cancelled,
    )


def install_batch(monkeypatch, plan=None, result=None):
    """Stub the planner and executor, recording how they were called.

    Returns the `seen` dict the stubs append to: the plan_export arguments
    (model, schemes, out_dir, skip destinations) and the execute_export_plan
    arguments.
    """
    seen = {"plans": [], "calls": []}

    def fake_plan_export(model, schemes, out_dir, skip_destinations=()):
        seen["plans"].append((model, schemes, out_dir, tuple(skip_destinations)))
        return plan if plan is not None else make_plan(1)

    def fake_execute(source_path, plan_, crf=18, ffmpeg_path=None,
                     on_progress=None, should_cancel=None):
        seen["calls"].append((plan_, on_progress, should_cancel))
        return result if result is not None else make_result()

    monkeypatch.setattr(editor_module, "plan_export", fake_plan_export)
    monkeypatch.setattr(editor_module, "execute_export_plan", fake_execute)
    return seen


@pytest.fixture
def export_editor(tmp_path, monkeypatch):
    """An editor ready to export, with the thread and dialog stubbed out."""
    ensure_qapp()
    source = tmp_path / "import" / "compilation.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    editor = EditorStub(
        [keep_segment()],
        media_path=str(source),
    )
    editor.fill_required()
    threads = []

    def make_thread(parent=None):
        thread = FakeThread(parent)
        # Parked by default, so a test can look at a run in flight. Tests that
        # want the whole batch drive it with run_batch().
        thread.auto_run = False
        threads.append(thread)
        return thread

    monkeypatch.setattr(editor_module, "QThread", make_thread)
    monkeypatch.setattr(editor_module, "ExportWorker", FakeExportWorker)
    monkeypatch.setattr(editor_module, "QProgressDialog", FakeDialog)
    monkeypatch.setattr(editor_module, "PROJECT_ROOT", str(tmp_path))
    return editor, threads


def run_batch(threads):
    """Run the parked batch to completion, the way a real thread's loop would."""
    thread = threads[-1]
    thread.auto_run = True
    thread.start()


# ---------------------------------------------------------------------------
# The regression this change exists for
# ---------------------------------------------------------------------------

def test_the_gui_thread_is_not_blocked_by_the_batch(tmp_path, monkeypatch):
    """The bug: on_export transcoded on the GUI thread, so the event loop --
    and mpv with it -- stopped for the length of the batch.

    A real QThread here, and a real blocking executor, because a stubbed thread
    could not tell the difference between "runs elsewhere" and "returns fast".
    """
    ensure_qapp()
    source = tmp_path / "import" / "compilation.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    editor = EditorStub([keep_segment()], media_path=str(source))
    editor.fill_required()
    monkeypatch.setattr(editor_module, "PROJECT_ROOT", str(tmp_path))

    encoding = threading.Event()
    release = threading.Event()
    started = threading.Event()

    def blocking_execute(*args, **kwargs):
        started.set()
        release.wait(10)
        return make_result(written=("Network/Some Clip.mp4",))

    monkeypatch.setattr(editor_module, "plan_export", lambda *a, **k: make_plan(1))
    monkeypatch.setattr(editor_module, "execute_export_plan", blocking_execute)

    # A queued slot: it can only be delivered while the event loop is being
    # serviced, which is precisely what the bug prevented.
    class Probe(QObject):
        ticked = Signal()

        def __init__(self):
            super().__init__()
            self.ticked.connect(self._deliver, Qt.QueuedConnection)

        def _deliver(self):
            Probe.served.set()

    Probe.served = threading.Event()
    probe = Probe()
    app = QApplication.instance()
    # Drain anything queued from setup, so the assertions below start clean.
    probe.ticked.emit()
    app.processEvents()
    Probe.served.clear()

    thread = QThread()
    worker = ExportWorker(str(source), editor.segment_model, ExportSchemes(
        "{title}", DEFAULT_FOLDER_SCHEME), str(tmp_path / "export"))
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.start()
    try:
        assert started.wait(10), "the worker never started"
        assert thread.isRunning(), "the worker is not on its own thread"
        assert not Probe.served.is_set()

        probe.ticked.emit()
        assert not Probe.served.is_set(), "a queued slot cannot need the loop"
        for _ in range(200):
            app.processEvents()
            if Probe.served.is_set():
                break
            time.sleep(0.01)

        # The encoder is still running, and the GUI thread serviced a queued
        # signal while it did.
        assert not release.is_set()
        assert Probe.served.is_set()
    finally:
        release.set()
        thread.quit()
        assert thread.wait(10000)


# ---------------------------------------------------------------------------
# Starting a run
# ---------------------------------------------------------------------------

def test_the_editor_is_frozen_and_mpv_paused_for_the_run(export_editor, monkeypatch):
    editor, _ = export_editor
    install_batch(monkeypatch)

    editor.on_export()

    # One flag, not a list of controls: the whole window is disabled, which in
    # Qt cascades to every child. Freezing editing rather than just the export
    # button is deliberate -- the worker holds a snapshot, so an edit made
    # during the run would silently not reach the files.
    assert editor.editor_enabled is False
    assert editor.player.pause is True


def test_the_dialog_starts_in_the_planning_phase(export_editor, monkeypatch):
    """Planning walks the whole export tree twice, so the bar must not claim
    a total before the plan exists."""
    editor, _ = export_editor
    install_batch(monkeypatch)

    editor.on_export()

    dialog = editor._export_dialog
    assert dialog.shown is True
    assert (dialog.minimum, dialog.maximum) == (0, 0)
    assert "Planning" in dialog.label


def test_a_second_export_while_one_runs_is_ignored(export_editor, monkeypatch):
    editor, threads = export_editor
    install_batch(monkeypatch)
    editor.on_export()
    worker = editor._export_worker

    editor.on_export()

    assert len(threads) == 1
    assert editor._export_worker is worker


def test_the_batch_receives_a_snapshot_not_the_live_model(export_editor, monkeypatch):
    editor, threads = export_editor
    seen = install_batch(monkeypatch)

    editor.on_export()
    run_batch(threads)
    model = seen["plans"][0][0]

    assert model is not editor.segment_model
    # The sidecar is written before the worker starts, so a crash mid-batch
    # cannot lose the record the filenames were built from.
    assert os.path.exists(sidecar_path(editor.media_path))


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

def test_planning_switches_the_bar_to_a_determinate_total(export_editor, monkeypatch):
    editor, threads = export_editor
    install_batch(monkeypatch, plan=make_plan(3))
    editor.on_export()
    dialog = editor._export_dialog

    editor._on_export_planned(3, 0)

    assert (dialog.minimum, dialog.maximum) == (0, 3)
    assert dialog.value == 0
    assert "Clip 1 of 3" in dialog.label


def test_a_resumed_run_says_how_many_clips_it_is_skipping(export_editor, monkeypatch):
    editor, _ = export_editor
    install_batch(monkeypatch)
    editor.on_export()
    dialog = editor._export_dialog

    editor._on_export_planned(4, 2)

    assert (dialog.minimum, dialog.maximum) == (0, 4)
    assert "Skipping 2" in dialog.label


def test_advancing_names_the_clip_being_written(export_editor, monkeypatch):
    editor, _ = export_editor
    install_batch(monkeypatch)
    editor.on_export()
    dialog = editor._export_dialog

    editor._on_export_advanced(1, 3, "Network/Clip 2.mp4")

    assert dialog.value == 1
    assert dialog.label == "Clip 2 of 3 — Network/Clip 2.mp4"


def test_the_final_tick_reports_the_batch_as_written(export_editor, monkeypatch):
    editor, _ = export_editor
    install_batch(monkeypatch)
    editor.on_export()
    dialog = editor._export_dialog

    editor._on_export_advanced(3, 3, "")

    assert dialog.value == 3
    assert "Wrote 3 of 3" in dialog.label


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------

def test_cancel_asks_the_worker_to_stop_and_kills_the_button(export_editor, monkeypatch):
    editor, _ = export_editor
    install_batch(monkeypatch)
    editor.on_export()
    dialog = editor._export_dialog

    dialog.press_cancel()

    assert editor._export_cancel.is_set()
    # A Cancel button that stays on screen after it was pressed is how a cancel
    # turns into "did that even work?".
    assert dialog.cancel_button is None
    assert dialog.label == "Cancelling…"


def test_a_cancelled_run_stashes_what_it_wrote_for_a_resume(export_editor, monkeypatch):
    editor, threads = export_editor
    install_batch(
        monkeypatch,
        result=make_result(
            written=("Network/Clip 1.mp4", "Network/Clip 2.mp4"),
            cancelled=True,
        ),
    )

    editor.on_export()
    run_batch(threads)

    assert editor._resume_skips == {
        "Network/Clip 1.mp4",
        "Network/Clip 2.mp4",
    }


def test_a_completed_run_stashes_nothing(export_editor, monkeypatch):
    editor, threads = export_editor
    install_batch(monkeypatch, result=make_result(written=("Network/Clip 1.mp4",)))

    editor.on_export()
    run_batch(threads)

    assert editor._resume_skips == frozenset()


def test_a_cancelled_run_says_what_survived(export_editor, monkeypatch):
    editor, threads = export_editor
    install_batch(
        monkeypatch,
        result=make_result(written=("Network/Clip 1.mp4",), cancelled=True),
    )

    editor.on_export()
    run_batch(threads)

    assert len(FakeMessageBox.information_seen) == 1
    title, text = FakeMessageBox.information_seen[0]
    assert title == "Export cancelled"
    assert "after 1 clip(s)" in text
    assert "kept" in text


# ---------------------------------------------------------------------------
# Resuming
# ---------------------------------------------------------------------------

def test_resume_offers_to_skip_and_hands_the_list_to_the_planner(
    export_editor,
    monkeypatch,
):
    editor, threads = export_editor
    editor._resume_skips = frozenset({"Network/Clip 1.mp4"})
    seen = install_batch(monkeypatch)
    FakeMessageBox.reset(FakeMessageBox.Yes)

    editor.on_export()
    run_batch(threads)

    assert FakeMessageBox.questions_seen
    assert "1 clip(s) were written" in FakeMessageBox.questions_seen[0][1]
    assert seen["plans"][0][3] == ("Network/Clip 1.mp4",)


def test_start_over_clears_the_skips_so_the_preflight_refuses(
    export_editor,
    monkeypatch,
):
    editor, threads = export_editor
    editor._resume_skips = frozenset({"Network/Clip 1.mp4"})
    seen = install_batch(monkeypatch)
    FakeMessageBox.reset(FakeMessageBox.No)

    editor.on_export()
    run_batch(threads)

    assert seen["plans"][0][3] == ()
    assert editor._resume_skips == frozenset()


def test_a_second_run_after_a_cancel_skips_what_the_first_wrote(
    export_editor,
    monkeypatch,
):
    """The two halves together: cancel, then resume, and the resumed plan does
    not ask for a destination that is already on disk."""
    editor, threads = export_editor
    install_batch(
        monkeypatch,
        result=make_result(written=("Network/Clip 1.mp4",), cancelled=True),
    )
    editor.on_export()
    run_batch(threads)
    assert editor._resume_skips == {"Network/Clip 1.mp4"}

    seen = install_batch(monkeypatch, plan=make_plan(2))
    FakeMessageBox.reset(FakeMessageBox.Yes)
    editor.on_export()
    run_batch(threads)

    assert seen["plans"][0][3] == ("Network/Clip 1.mp4",)
    assert editor._resume_skips == frozenset()


def test_a_planning_refusal_is_reported_not_swallowed(export_editor, monkeypatch):
    editor, threads = export_editor

    def refusing_plan(*args, **kwargs):
        raise ExportPlanError("Export preflight failed:\n- Network/A.mp4 already exists")

    monkeypatch.setattr(editor_module, "plan_export", refusing_plan)
    monkeypatch.setattr(editor_module, "execute_export_plan", lambda *a, **k: None)

    editor.on_export()
    run_batch(threads)

    assert FakeMessageBox.warnings_seen[0][0] == "Export could not start"
    assert "already exists" in FakeMessageBox.warnings_seen[0][1]
    assert editor.editor_enabled is True
    assert editor._export_thread is None


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------

def test_a_clean_export_reports_nothing_and_re_enables_the_editor(
    export_editor,
    monkeypatch,
):
    editor, threads = export_editor
    install_batch(monkeypatch, result=make_result(written=("Network/Clip 1.mp4",)))

    editor.on_export()
    run_batch(threads)

    # A modal "done!" on a 40-minute success is noise; the log has it.
    assert FakeMessageBox.warnings_seen == []
    assert FakeMessageBox.information_seen == []
    assert editor.editor_enabled is True
    assert editor._export_thread is None
    assert editor._export_dialog is None


def test_a_completed_export_is_recorded_in_the_log(export_editor, monkeypatch):
    """The success path used to be a bare print(), which goes nowhere in a
    windowed build -- so a finished export left no record at all. The line has
    to name the directory the batch actually used, not a re-derived one."""
    editor, threads = export_editor
    lines = []
    monkeypatch.setattr(editor_module, "log", lines.append)
    seen = install_batch(
        monkeypatch, result=make_result(written=("Network/Clip 1.mp4",))
    )

    editor.on_export()
    run_batch(threads)

    out_dir = seen["plans"][0][2]
    assert f"export to {out_dir}: 1 written, 0 failed, complete" in lines


def test_per_clip_failures_still_name_the_segments(export_editor, monkeypatch):
    editor, threads = export_editor
    install_batch(
        monkeypatch,
        result=make_result(
            written=("Network/Clip 1.mp4",),
            failures=(
                ExportClipFailure(1, "Network/Clip 2.mp4", "encoder failed"),
            ),
        ),
    )

    editor.on_export()
    run_batch(threads)

    title, text = FakeMessageBox.warnings_seen[0]
    assert title == "Export completed with errors"
    assert "Segment 2 — Network/Clip 2.mp4" in text
    assert "encoder failed" in text


def test_the_dialog_is_reset_and_released_when_the_batch_ends(
    export_editor,
    monkeypatch,
):
    editor, threads = export_editor
    install_batch(monkeypatch)
    editor.on_export()
    dialog = editor._export_dialog
    thread = threads[-1]
    worker = editor._export_worker

    run_batch(threads)

    assert thread.quit_calls == 1
    assert dialog.reset_calls == 1
    assert dialog.deleted is True
    assert thread.deleted is True
    assert editor._export_worker is None
    assert editor._export_dialog is None


# ---------------------------------------------------------------------------
# Closing mid-export
# ---------------------------------------------------------------------------

class RecordingEvent:
    def __init__(self):
        self.ignored = False
        self.accepted = False

    def ignore(self):
        self.ignored = True

    def accept(self):
        self.accepted = True


def test_closing_during_an_export_asks_before_cancelling(export_editor, monkeypatch):
    editor, _ = export_editor
    install_batch(monkeypatch)
    editor.on_export()
    event = RecordingEvent()
    FakeMessageBox.reset(FakeMessageBox.No)

    editor.closeEvent(event)

    # Ignoring the close is what keeps a running QThread from being destroyed.
    assert event.ignored is True
    assert editor._close_after_export is False
    assert editor._export_cancel.is_set() is False


def test_confirming_the_close_cancels_the_batch_and_closes_when_it_stops(
    export_editor,
    monkeypatch,
):
    editor, _ = export_editor
    install_batch(monkeypatch)
    editor.on_export()
    event = RecordingEvent()
    FakeMessageBox.reset(FakeMessageBox.Yes)

    editor.closeEvent(event)

    assert event.ignored is True
    assert editor._close_after_export is True
    assert editor._export_cancel.is_set() is True
    # The window only closes once the thread has actually stopped.
    assert editor.closed is False
    editor._on_export_stopped()
    assert editor.closed is True
    # And closing is not a second "something went wrong" dialog.
    assert FakeMessageBox.information_seen == []


def test_closing_while_idle_just_closes(export_editor):
    editor, _ = export_editor
    event = RecordingEvent()

    editor.closeEvent(event)

    assert event.accepted is True
    assert event.ignored is False


# ---------------------------------------------------------------------------
# The worker itself
# ---------------------------------------------------------------------------

def test_the_worker_announces_the_plan_before_it_transcodes(monkeypatch, tmp_path):
    """The bar needs a total before the first clip runs, which is why the
    worker plans and executes rather than calling the one-shot helper."""
    ensure_qapp()
    order = []

    def fake_plan_export(*args, **kwargs):
        order.append("plan")
        return make_plan(4)

    def fake_execute(*args, **kwargs):
        order.append("execute")
        on_progress = kwargs["on_progress"]
        on_progress(0, 4, "Network/Clip 1.mp4")
        on_progress(4, 4, "")
        return make_result(written=("Network/Clip 1.mp4",))

    monkeypatch.setattr(editor_module, "plan_export", fake_plan_export)
    monkeypatch.setattr(editor_module, "execute_export_plan", fake_execute)

    worker = ExportWorker(
        "source.mp4", SegmentModel("s", 4.0, [keep_segment()]),
        ExportSchemes("{title}", DEFAULT_FOLDER_SCHEME), str(tmp_path / "export"),
    )
    planned = []
    advanced = []
    outcomes = []
    worker.planned.connect(lambda total, skipped: planned.append((total, skipped)))
    worker.advanced.connect(lambda *args: advanced.append(args))
    worker.finished.connect(outcomes.append)

    worker.run()

    assert order == ["plan", "execute"]
    assert planned == [(4, 0)]
    assert advanced == [(0, 4, "Network/Clip 1.mp4"), (4, 4, "")]
    assert len(outcomes) == 1
    assert outcomes[0].result.succeeded == 1
    assert outcomes[0].error is None


def test_the_worker_reports_a_planning_failure_as_an_outcome(monkeypatch, tmp_path):
    """A refused preflight still has to come back through finished, or the
    window waits on a thread that will never report."""
    ensure_qapp()

    def refusing_plan(*args, **kwargs):
        raise ExportPlanError("Export preflight failed:\n- already exists")

    monkeypatch.setattr(editor_module, "plan_export", refusing_plan)
    monkeypatch.setattr(
        editor_module,
        "execute_export_plan",
        lambda *a, **k: pytest.fail("must not transcode a refused plan"),
    )

    worker = ExportWorker(
        "source.mp4", SegmentModel("s", 4.0, [keep_segment()]),
        ExportSchemes("{title}", DEFAULT_FOLDER_SCHEME), str(tmp_path / "export"),
    )
    outcomes = []
    worker.finished.connect(outcomes.append)

    worker.run()

    assert len(outcomes) == 1
    assert outcomes[0].result is None
    assert "already exists" in outcomes[0].error


def test_the_worker_hands_the_executor_its_cancel_predicate(monkeypatch, tmp_path):
    ensure_qapp()
    captured = {}

    def fake_execute(*args, **kwargs):
        captured["should_cancel"] = kwargs["should_cancel"]
        return make_result(cancelled=True)

    monkeypatch.setattr(editor_module, "plan_export", lambda *a, **k: make_plan(2))
    monkeypatch.setattr(editor_module, "execute_export_plan", fake_execute)

    cancel = threading.Event()
    worker = ExportWorker(
        "source.mp4", SegmentModel("s", 4.0, [keep_segment()]),
        ExportSchemes("{title}", DEFAULT_FOLDER_SCHEME), str(tmp_path / "export"),
        cancel_event=cancel,
    )
    worker.run()

    assert captured["should_cancel"]() is False
    cancel.set()
    assert captured["should_cancel"]() is True


def test_a_worker_exception_becomes_an_outcome_not_a_process_death(monkeypatch, tmp_path):
    """An unhandled exception in a worker slot reaches PySide6's abort path.
    The window has to be able to tear the run down instead."""
    ensure_qapp()

    def exploding_plan(*args, **kwargs):
        raise RuntimeError("the disk fell over")

    monkeypatch.setattr(editor_module, "plan_export", exploding_plan)
    monkeypatch.setattr(editor_module, "execute_export_plan", lambda *a, **k: None)

    worker = ExportWorker(
        "source.mp4", SegmentModel("s", 4.0, [keep_segment()]),
        ExportSchemes("{title}", DEFAULT_FOLDER_SCHEME), str(tmp_path / "export"),
    )
    outcomes = []
    worker.finished.connect(outcomes.append)

    worker.run()

    assert len(outcomes) == 1
    assert outcomes[0].error == "the disk fell over"
    assert ExportOutcome(error="x").result is None
