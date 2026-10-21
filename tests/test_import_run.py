"""The one screen both importer windows share: what to do with the files, and what
the summary says afterwards.

Applies to: `importer/importrun.py`, `shared/importing.py`.

`confirm_and_import` is the only place an import is started from, so it is where the
transfer question lives and where the summary's wording is decided. Both are
deliberate rather than incidental:

- The transfer is asked **every** run and defaults to `copy`. A remembered answer
  would quietly un-fix the backend's own rule that copy is the only transfer safe to
  assume.
- `move` is described by **what it does to the folder**, not by the word "move". It
  is the one option that removes the user's media, and a user who has not understood
  it cannot meaningfully consent to it.

The dialog is built and inspected rather than exec'd, for the reason
`tests/test_mesh_window.py` gives its own: a modal under `QT_QPA_PLATFORM=offscreen`
blocks on nobody and hangs the run.

The run itself is on a worker thread, so the last section here drives it: the copy
happens off the GUI thread, the read comes before the question and the plan after
it, the progress reaches a dialog, a cancel keeps what was already written, and the
thread ends by itself.
"""

import os
import threading
import time

import pytest
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton, QWidget

import importer.importrun as importrun_module
from editor_stub import ensure_qapp
from shared.importing import (
    TRANSFER_COPY,
    TRANSFER_LINK,
    TRANSFER_MOVE,
    TRANSFERS,
)
from shared.records import ClipRecord, RECORD_EXTENSION, write_record
from shared.session import widget_is_alive


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def choices(qapp, tmp_path):
    """The real widgets, so the wording under test is the wording on screen.

    `qapp` is a dependency and not a nicety: constructing a `QRadioButton` with no
    `QApplication` takes the interpreter down rather than raising, so the module
    fixture has to come first.
    """
    root = tmp_path / "import"
    root.mkdir()
    return importrun_module._transfer_choices(str(root))


def labels_in(widget) -> str:
    return " ".join(label.text() for label in widget.findChildren(QLabel))


# ---------------------------------------------------------------------------
# The question
# ---------------------------------------------------------------------------

def test_copy_is_the_only_thing_pre_selected(choices):
    """`shared/importing.py` fixes copy as the only transfer safe to assume — a
    cancelled run, a wrong tag or a wrong destination must not destroy the user's
    media — and the dialog has to agree with that on every single run rather than
    once and then remembering whatever was picked."""
    _, _, (copy, link, move) = choices

    assert (copy.isChecked(), link.isChecked(), move.isChecked()) == (
        True, False, False)


def test_copy_and_link_both_say_the_videos_stay_put(choices):
    """The two options a user is most likely to confuse, told apart by the only
    thing that differs: what they cost in disk."""
    _, _, (copy, link, move) = choices

    assert "leave them where they are" in copy.text()
    assert "leave them where they are" in link.text()
    assert "no extra room on disk" in link.toolTip(), (
        "so the difference between them is stated, rather than left to be inferred "
        "from two nearly identical labels")


def test_move_names_the_folder_it_empties_and_says_it_is_forever(choices):
    """Consent to a deletion has to be informed, and "move" on its own says neither
    from where nor whether it can be taken back."""
    _, group, (_, _, move) = choices

    assert "empty" in move.text()
    assert "no undo" in labels_in(group), (
        "the consequence is stated next to the choices, where it is read")
    assert "would not mind losing" in move.toolTip()


def test_move_names_the_actual_folder_rather_than_the_word_import(qapp, tmp_path):
    """A label reading "empty the import folder" is a claim about a folder the user
    may have redirected. The path is what makes it checkable."""
    root = tmp_path / "somewhere-else-entirely"
    root.mkdir()

    _, group, (_, _, move) = importrun_module._transfer_choices(str(root))

    assert str(root) in move.text()
    assert group is not None, "the group is what keeps these widgets alive"


def test_the_three_buttons_map_to_the_three_transfers_in_order(choices):
    """Positional pairing, and therefore order-sensitive.

    Not a detail: reordering the radio buttons without reordering the walk in
    `ask_transfer` would mean the button labelled "copy" silently moved the user's
    files.
    """
    _, _, buttons = choices

    assert len(buttons) == 3
    assert list(TRANSFERS) == [TRANSFER_COPY, TRANSFER_LINK, TRANSFER_MOVE]


def test_the_heading_names_the_folder_the_question_is_about(qapp, tmp_path):
    root = tmp_path / "import"
    root.mkdir()

    heading, _, _ = importrun_module._transfer_choices(str(root))

    assert str(root) in heading.text()
    assert "What should happen to the videos" in heading.text()


# ---------------------------------------------------------------------------
# The whole question, pressed
# ---------------------------------------------------------------------------
#
# The tests above build the real widgets but never press anything, and that is
# exactly the gap this bug lived in: the failure was in the wiring *around* the radio
# buttons, on a code path no test executed, because `exec()` blocks under
# `QT_QPA_PLATFORM=offscreen` and hangs the run. So `exec` is replaced by a function
# that actually clicks the button — which is what a user does, and which fires the
# same signal a real click fires.


def _press(monkeypatch, source_root, standard_button, pick=None):
    """Run `ask_transfer` with `exec` replaced by a real click of one button."""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QRadioButton

    def fake_exec(dialog):
        if pick is not None:
            for button in dialog.findChildren(QRadioButton):
                if pick in button.text():
                    button.setChecked(True)
        box = dialog.findChild(QDialogButtonBox)
        box.button(standard_button).click()
        return (QDialog.DialogCode.Accepted
                if standard_button == QDialogButtonBox.StandardButton.Ok
                else QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    return importrun_module.ask_transfer(None, str(source_root))


@pytest.fixture
def import_root(tmp_path):
    root = tmp_path / "import"
    root.mkdir()
    return root


def test_pressing_import_returns_the_checked_transfer(monkeypatch, import_root):
    """The default answer.

    This one would **not** have caught the reported bug, and that is worth knowing:
    the exception happens inside a Qt signal handler, so PySide6 prints it to stderr
    and carries on — the dialog stays open, the default survives, and `copy` comes
    back as if nothing had gone wrong. Only the two tests below, which check a
    *non-default* choice, turn the silent failure into a visible one. All three stay
    for that reason.
    """
    from PySide6.QtWidgets import QDialogButtonBox

    answer = _press(monkeypatch, import_root, QDialogButtonBox.StandardButton.Ok)

    assert answer == TRANSFER_COPY, (
        "the pre-selected option is the answer when nothing was changed")


def test_pressing_import_returns_move_when_move_was_chosen(monkeypatch,
                                                          import_root):
    """The test that would have caught it.

    A wrong transfer here does not just file the wrong way round — `move` is the one
    option that deletes the user's media, so a broken wiring that silently answered
    with `copy` on a folder of eight hundred clips would leave the user believing
    their import folder had been emptied when it had not.
    """
    from PySide6.QtWidgets import QDialogButtonBox

    answer = _press(monkeypatch, import_root, QDialogButtonBox.StandardButton.Ok,
                    pick="Move them")

    assert answer == TRANSFER_MOVE


def test_pressing_import_returns_link_when_link_was_chosen(monkeypatch,
                                                          import_root):
    from PySide6.QtWidgets import QDialogButtonBox

    answer = _press(monkeypatch, import_root, QDialogButtonBox.StandardButton.Ok,
                     pick="Link them")

    assert answer == TRANSFER_LINK


def test_cancelling_the_question_returns_none(monkeypatch, import_root):
    """Cancelling is how a user backs out of an import they did not mean to start,
    so it has to be distinguishable from answering with copy."""
    from PySide6.QtWidgets import QDialogButtonBox

    assert _press(monkeypatch, import_root,
                  QDialogButtonBox.StandardButton.Cancel) is None


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------

class _Plan:
    def __init__(self, transfer):
        self.transfer = transfer


def test_the_summary_says_what_the_transfer_left_behind():
    """The half of a transfer the word "imported" says nothing about.

    A user who chose copy and then sees the folder unchanged may reasonably wonder
    whether anything happened; a user who chose move needs to know the videos are
    gone from *there*, not merely somewhere else now.
    """
    assert "still in the import folder" in (
        importrun_module._source_fate(_Plan(TRANSFER_COPY)))
    assert "links to them" in (
        importrun_module._source_fate(_Plan(TRANSFER_LINK)))


def test_the_move_summary_says_the_folders_went_too():
    """Because that is the visible part of the cleanup, and a user watching the
    folder empty needs to know it was meant to rather than half-finished."""
    fate = importrun_module._source_fate(_Plan(TRANSFER_MOVE))

    assert "moved out of the import folder" in fate
    assert "folder that emptied was removed" in fate


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
#
# The question and the summary are decided above. What is decided here is the run
# they hang off: that the work happens off the GUI thread, that the read comes
# before the question and the plan after it, that the progress reaches a dialog,
# that a cancel keeps what was already written, and that the thread ends by itself.

#: The four tags an export needs, so a clip has somewhere to land.
REQUIRED = {
    "title": "Worlds Finest", "network": "Cartoon Network",
    "filler_type": "Promo", "time_period": "2000s",
}


def plant_clip(root, relative, tags=None):
    """A video with a record beside it, as another commcut install leaves one."""
    path = os.path.join(str(root), *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * 16)
    write_record(os.path.splitext(path)[0] + RECORD_EXTENSION, ClipRecord(
        source="theirs.mp4", segment_index=0, start=0.0, duration=30.0,
        tags=tuple((tags or REQUIRED).items()),
        clip_id="12345678-1234-4123-8123-123456789abc",
    ))
    return path


class Recorder:
    """A `QMessageBox` that records instead of blocking.

    The **parent** is recorded as well as the text, because it is part of the
    call: PySide6 raises a `TypeError` naming the wrong signature if the parent
    is anything but a widget or `None`, and a stub that accepts whatever it is
    given would never notice.
    """

    answer = 0
    seen: list = []
    parents: list = []

    @staticmethod
    def information(parent, title, message, *_buttons, default=0):
        Recorder.parents.append(parent)
        Recorder.seen.append((title, message))
        return Recorder.answer

    @staticmethod
    def warning(parent, title, message, *_buttons, default=0):
        Recorder.parents.append(parent)
        Recorder.seen.append((title, message))
        return Recorder.answer


class FakeSummary:
    """The import summary, without a modal in the way.

    A real `ImportSummaryDialog` blocks on `exec()` until somebody presses a
    button, and under `QT_QPA_PLATFORM=offscreen` nobody does. The stub records
    the body and the parent it was given, and answers with the action the test
    asked for.
    """

    #: Every summary that was shown, in order.
    shown: list = []
    #: What the next summary answers with.
    action = importrun_module.ACTION_STAY

    def __init__(self, body, parent=None):
        self.body = body
        self.parent = parent
        FakeSummary.shown.append(self)

    def exec(self):
        return 1

    def chosen_action(self):
        return FakeSummary.action

    def deleteLater(self):
        pass


class FakeDialog(QObject):
    """A progress dialog that records what it was told.

    The real one is a widget, and this one records the same calls, so the test can
    assert that the progress reached a dialog without a window on screen.
    """

    canceled = Signal()

    def __init__(self, message, cancellable=False, parent=None, modal=True,
                 title="commcut"):
        super().__init__()
        self.messages = [message]
        self.ranges = []
        self.values = []
        self.shown = False
        self.closed = False

    def set_range(self, minimum, maximum):
        self.ranges.append((minimum, maximum))

    def set_value(self, value):
        self.values.append(value)

    def set_message(self, message):
        self.messages.append(message)

    def show(self):
        self.shown = True

    def close(self):
        self.closed = True

    def deleteLater(self):
        pass


@pytest.fixture
def run_env(qapp, tmp_path, monkeypatch):
    """An import folder holding one clip, an empty library, and nothing blocking.

    The transfer question and the summary are stubbed out: both are modals under
    `offscreen` and they block on nobody. What is left is the run.
    """
    import_root = tmp_path / "import"
    library_root = tmp_path / "export"
    os.makedirs(str(import_root), exist_ok=True)
    os.makedirs(str(library_root), exist_ok=True)
    plant_clip(import_root, "CN/Promo/Worlds Finest.mp4")

    Recorder.seen = []
    Recorder.parents = []
    FakeSummary.shown = []
    FakeSummary.action = importrun_module.ACTION_STAY
    monkeypatch.setattr(importrun_module, "QMessageBox", Recorder)
    monkeypatch.setattr(importrun_module, "ImportSummaryDialog", FakeSummary)
    monkeypatch.setattr(importrun_module, "ask_transfer",
                        lambda _parent, _root: TRANSFER_COPY)
    return str(import_root), str(library_root), str(tmp_path / "settings.json")


def _library_videos(library_root):
    """Every video in the library, however deep it landed."""
    found = []
    for _directory, _subdirectories, names in os.walk(str(library_root)):
        found.extend(name for name in names if name.endswith(".mp4"))
    return found


def test_the_copy_runs_off_the_gui_thread(run_env, monkeypatch):
    """The whole point of the change.

    The GUI thread is the one that would freeze, so the copy has to happen on
    another one. The assertion is on the effect — the thread the copy ran on —
    and not on the wiring that is supposed to produce it.
    """
    import_root, library_root, settings = run_env
    gui = threading.get_ident()
    seen = []
    real = importrun_module.execute_import

    def spy(plan, **kwargs):
        seen.append(threading.get_ident())
        return real(plan, **kwargs)

    monkeypatch.setattr(importrun_module, "execute_import", spy)

    importrun_module.confirm_and_import(None, import_root, library_root, settings)

    assert seen, "the import never ran"
    assert seen[0] != gui, "the copy ran on the GUI thread"


def test_the_gui_thread_is_not_blocked_while_the_copy_runs(run_env, monkeypatch):
    """The freeze the user reported: the whole import ran on the GUI thread, so
    the app did not answer for the length of the copy.

    A queued slot on the GUI thread is the proof: it can only be delivered while
    the event loop is running, so a slot that arrives *before* the copy finishes
    is a slot the GUI thread was free to deliver.
    """
    import_root, library_root, settings = run_env
    order = []

    class Probe(QObject):
        ticked = Signal()

        def __init__(self):
            super().__init__()
            self.ticked.connect(self._deliver, Qt.QueuedConnection)

        def _deliver(self):
            order.append("gui")

    probe = Probe()
    real = importrun_module.execute_import

    def slow_execute(plan, **kwargs):
        probe.ticked.emit()
        time.sleep(0.2)
        order.append("copy")
        return real(plan, **kwargs)

    monkeypatch.setattr(importrun_module, "execute_import", slow_execute)

    importrun_module.confirm_and_import(None, import_root, library_root, settings)

    assert order[:2] == ["gui", "copy"], (
        "the GUI thread was blocked while the copy ran — a queued slot could not "
        "be delivered until the copy had finished"
    )


def test_the_read_comes_before_the_question_and_the_plan_after_it(
        run_env, monkeypatch):
    """The order the question depends on.

    Before the read, the question would be asked about a folder that turns out to
    hold nothing. After the plan, `move` could be chosen into an out-of-space
    failure — the space preflight is part of the plan.
    """
    import_root, library_root, settings = run_env
    order = []
    asked_under = []
    dialogs = []
    real_build = importrun_module.build_catalog
    real_plan = importrun_module.plan_import

    class Recording(FakeDialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            dialogs.append(self)

    def spy_build(root, *args, **kwargs):
        order.append("read")
        return real_build(root, *args, **kwargs)

    def spy_plan(*args, **kwargs):
        order.append("plan")
        return real_plan(*args, **kwargs)

    def ask(parent, _root):
        order.append("question")
        asked_under.append(parent)
        return TRANSFER_COPY

    monkeypatch.setattr(importrun_module, "build_catalog", spy_build)
    monkeypatch.setattr(importrun_module, "plan_import", spy_plan)
    monkeypatch.setattr(importrun_module, "ask_transfer", ask)
    monkeypatch.setattr(importrun_module, "CancelOnCloseDialog", Recording)

    importrun_module.confirm_and_import(None, import_root, library_root, settings)

    assert order.index("read") < order.index("question")
    assert order.index("question") < order.index("plan")
    # The question is a child of the progress dialog rather than of the window:
    # a dialog that is a child of a modal window is not blocked by it, so the
    # question is still usable while the progress dialog is up.
    assert asked_under and asked_under[0] is dialogs[0]


def test_an_empty_folder_says_so_without_asking(qapp, tmp_path, monkeypatch):
    """The question is not asked about eight hundred videos that are not there."""
    import_root = tmp_path / "import"
    library_root = tmp_path / "export"
    os.makedirs(str(import_root), exist_ok=True)
    os.makedirs(str(library_root), exist_ok=True)

    asked = []
    Recorder.seen = []
    monkeypatch.setattr(importrun_module, "QMessageBox", Recorder)
    monkeypatch.setattr(importrun_module, "ask_transfer",
                        lambda _parent, _root: asked.append(True) or TRANSFER_COPY)

    result = importrun_module.confirm_and_import(
        None, str(import_root), str(library_root), str(tmp_path / "settings.json"))

    assert result is None
    assert not asked, "the question was asked about a folder with nothing in it"
    assert Recorder.seen, "the run said nothing at all"
    assert Recorder.seen[0][0] == "Nothing to import"


def test_the_progress_reaches_the_dialog(run_env, monkeypatch):
    """The dialog is the only way the user knows how far along the run is."""
    import_root, library_root, settings = run_env
    dialogs = []

    class Recording(FakeDialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            dialogs.append(self)

    monkeypatch.setattr(importrun_module, "CancelOnCloseDialog", Recording)

    importrun_module.confirm_and_import(None, import_root, library_root, settings)

    assert dialogs, "no progress dialog was built"
    dialog = dialogs[0]
    assert dialog.shown, "the progress dialog was never shown"
    assert any("Imported" in message for message in dialog.messages), (
        f"the dialog never reported a copy: {dialog.messages}"
    )
    assert dialog.ranges and dialog.ranges[-1][1] == 1, (
        "the progress bar never got a denominator"
    )


def test_the_run_copies_the_clip_and_says_so(run_env):
    """The end of the happy path: the clip is in the library, and the summary says
    it is."""
    import_root, library_root, settings = run_env

    result = importrun_module.confirm_and_import(
        None, import_root, library_root, settings)

    assert result is not None
    assert len(result.written) == 1
    assert _library_videos(library_root), "nothing landed in the library"
    assert FakeSummary.shown, "the run said nothing at all"
    assert "Imported 1 clip(s)" in FakeSummary.shown[0].body


def test_a_failure_is_reported_and_nothing_further_is_written(run_env, monkeypatch):
    """A failure is answered with a message rather than raised: this is called from
    a button handler, and an exception escaping one of those takes the window down.
    """
    import_root, library_root, settings = run_env

    def boom(plan, **kwargs):
        raise RuntimeError("the disk fell over")

    monkeypatch.setattr(importrun_module, "execute_import", boom)

    result = importrun_module.confirm_and_import(
        None, import_root, library_root, settings)

    assert result is None
    assert Recorder.seen, "the failure was not reported"
    title, message = Recorder.seen[0]
    assert title == "The import could not be run"
    assert "the disk fell over" in message
    assert not _library_videos(library_root)


def test_a_cancelled_run_keeps_what_it_had_already_written(qapp, tmp_path):
    """The rule a cancel leans on.

    A run that did not finish keeps the clips it committed and says so, and a
    re-run skips what landed — so cancelling is a pause, not a mess.
    """
    import_root = tmp_path / "import"
    library_root = tmp_path / "export"
    os.makedirs(str(import_root), exist_ok=True)
    os.makedirs(str(library_root), exist_ok=True)
    plant_clip(import_root, "CN/Promo/A.mp4")
    # A second clip with its own title, so the two land in different places: two
    # clips with the same tags are one destination, and the second is refused
    # before the run starts.
    plant_clip(import_root, "CN/Promo/B.mp4", tags={**REQUIRED, "title": "Toonami"})

    cancel = threading.Event()
    worker = importrun_module.ImportWorker(
        str(import_root), str(library_root), str(tmp_path / "settings.json"),
        cancel_event=cancel)

    results = []
    worker.done.connect(lambda result, plan: results.append(result))
    worker.progressed.connect(lambda done, path: cancel.set())

    worker.read()
    worker.import_clips(TRANSFER_COPY)

    assert len(results) == 1, "the run did not finish"
    result = results[0]
    assert result.cancelled, "the run was not reported as cancelled"
    assert len(result.written) == 1, (
        "the clip copied before the cancel was thrown away"
    )
    assert len(_library_videos(library_root)) == 1


def test_the_import_thread_ends_by_itself(run_env, monkeypatch):
    """`thread.started.connect(worker.run)` runs the worker's slot inside the
    thread's `exec()` loop, and a slot returning does not leave it — so nothing but
    the worker's own `finished` ends the thread.

    Asserted on the effect: a thread still running when its owner is destroyed
    aborts the process, which is invisible in the log.
    """
    import_root, library_root, settings = run_env
    run = importrun_module.ImportRun(None, import_root, library_root, settings)
    thread = run._thread

    run.go()

    try:
        assert not thread.isRunning(), "the import thread was still running"
    except RuntimeError:
        # A deleted C++ object is not running either, which is the outcome the
        # teardown wants.
        pass


def test_the_summary_is_shown_on_the_window_that_started_the_run(run_env, qapp):
    """The summary is a dialog *on* the window that started the run.

    PySide6 raises a `TypeError` naming the wrong signature when the parent is
    anything but a widget or `None` — and a bool is not a widget — so this pins
    the shape of the argument, not just that a summary appeared.
    """
    import_root, library_root, settings = run_env
    window = QWidget()
    try:
        importrun_module.confirm_and_import(
            window, import_root, library_root, settings)
    finally:
        window.deleteLater()

    assert FakeSummary.shown, "the run showed nothing at all"
    assert FakeSummary.shown[0].parent is window, (
        f"the summary was shown on {FakeSummary.shown[0].parent!r} rather than on "
        f"the window that started the run"
    )


def test_the_summary_survives_its_window_being_closed(qapp, tmp_path, monkeypatch):
    """The run outlives the window that started it — a half-written library is
    worse than a closed window — so the summary cannot be parented to a window
    whose C++ object has gone. PySide6 raises a `RuntimeError` on that.
    """
    import_root = tmp_path / "import"
    library_root = tmp_path / "export"
    os.makedirs(str(import_root), exist_ok=True)
    os.makedirs(str(library_root), exist_ok=True)
    plant_clip(import_root, "CN/Promo/Worlds Finest.mp4")

    Recorder.seen = []
    Recorder.parents = []
    FakeSummary.shown = []
    monkeypatch.setattr(importrun_module, "QMessageBox", Recorder)
    monkeypatch.setattr(importrun_module, "ImportSummaryDialog", FakeSummary)
    monkeypatch.setattr(importrun_module, "ask_transfer",
                        lambda _parent, _root: TRANSFER_COPY)

    window = QWidget()
    window.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not widget_is_alive(window), "the window was not actually closed"

    importrun_module.confirm_and_import(
        window, str(import_root), str(library_root), str(tmp_path / "settings.json"))

    assert FakeSummary.shown, "the run showed nothing at all"
    assert FakeSummary.shown[0].parent is None, (
        "the summary was parented to a window whose C++ object is gone"
    )


def test_back_to_main_menu_closes_the_window_that_started_the_import(run_env,
                                                                      monkeypatch):
    """The summary is the end of the session.

    The window that started the import has nothing left to offer — its own
    **Import Now** would find nothing left to import — so the summary offers
    the main menu, and choosing it closes that window. Closing is what brings
    the menu back: the shell watches for the window's `destroyed` signal.
    """
    import_root, library_root, settings = run_env
    FakeSummary.action = importrun_module.ACTION_MENU
    window = QWidget()
    closed = []
    window.close = lambda: (closed.append(True), True)[1]

    importrun_module.confirm_and_import(window, import_root, library_root, settings)

    assert closed, "the window that started the import was left open"


def test_staying_leaves_the_window_that_started_the_import_open(run_env,
                                                                monkeypatch):
    """The other half of the rule: a user who wants to look at the folder first
    should not have to leave the app to do it.
    """
    import_root, library_root, settings = run_env
    FakeSummary.action = importrun_module.ACTION_STAY
    window = QWidget()
    closed = []
    window.close = lambda: (closed.append(True), True)[1]

    importrun_module.confirm_and_import(window, import_root, library_root, settings)

    assert not closed, "the window was closed although the user chose to stay"


def test_the_summary_offers_the_way_back_to_the_main_menu(qapp):
    """The real dialog, not the stub: the summary is the end of the session, so
    it has to carry the way out of it.

    **Back to main menu** is the default, so pressing Enter takes the user where
    they are going rather than back to a screen they are finished with.
    """
    dialog = importrun_module.ImportSummaryDialog("Imported 1 clip(s).")
    try:
        buttons = {button.text(): button
                   for button in dialog.findChildren(QPushButton)}
        assert "Back to main menu" in buttons, (
            f"the summary offered {sorted(buttons)}"
        )
        assert buttons["Back to main menu"].isDefault(), (
            "Back to main menu is not the default, so Enter does not take the "
            "user where they are going"
        )
        assert "Stay in this window" in buttons
        assert dialog.windowTitle() == "Import finished"
    finally:
        dialog.deleteLater()


def test_choosing_the_main_menu_is_recorded(qapp):
    """The button the user pressed, which is what the run acts on."""
    dialog = importrun_module.ImportSummaryDialog("Imported 1 clip(s).")
    try:
        buttons = {button.text(): button
                   for button in dialog.findChildren(QPushButton)}
        buttons["Back to main menu"].click()

        assert dialog.chosen_action() == importrun_module.ACTION_MENU
    finally:
        dialog.deleteLater()


def test_a_dismissed_summary_says_staying(qapp):
    """Escape and the window close button land here too, and staying is the safe
    reading of an unasked question: the import is done either way, and the window
    behind still has its own way out.
    """
    dialog = importrun_module.ImportSummaryDialog("Imported 1 clip(s).")
    try:
        assert dialog.chosen_action() == importrun_module.ACTION_STAY
    finally:
        dialog.deleteLater()


def test_the_summary_shows_what_the_run_did(qapp):
    """The body is the run's own account of itself, in a box that can be read
    and copied."""
    dialog = importrun_module.ImportSummaryDialog("Imported 12 clip(s).\n"
                                                  "The videos are still there.")
    try:
        boxes = dialog.findChildren(QPlainTextEdit)
        assert boxes, "the summary had nowhere to show what the run did"
        assert "Imported 12 clip(s)." in boxes[0].toPlainText()
        assert boxes[0].isReadOnly()
    finally:
        dialog.deleteLater()
