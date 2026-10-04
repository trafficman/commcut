"""Tests for the Tagged Library Mesh window.

`shared/values.py` is the design and `tests/test_values.py` is most of the subject —
this file is about the things only a window can do:

- rendering a question, and refusing the buttons that would answer the wrong thing
- **not** writing anything until every value has been answered, and then writing
  once and saying what it did
- offering the Tag Editor afterwards, but only when there is residue for it

The thread and the message boxes are substituted for the reasons
`tests/test_mesh_window.py` gives its own: a real `QMessageBox` under
`QT_QPA_PLATFORM=offscreen` blocks on nobody and hangs the run, and `moveToThread`
onto a thread with no running event loop makes every signal queued to a loop that
never runs. The worker and its signals are kept real — only the affinity is stubbed —
so a test exercises the code the app runs.
"""

import os
import time

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QMessageBox

import importer.values as values_module
from editor_stub import ensure_qapp
from shared.catalog import build_catalog
from shared.records import ClipRecord, RECORD_EXTENSION, write_record
from shared.values import ValueSession


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


REQUIRED = {
    "title": "Some Title",
    "network": "Cartoon Network",
    "filler_type": "Promo",
    "time_period": "2000s",
}


def put_clip(root, relative, tags):
    """A video and a record beside it — what an imported library looks like."""
    path = os.path.join(str(root), *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * 16)
    write_record(os.path.splitext(path)[0] + RECORD_EXTENSION, ClipRecord(
        source=relative, segment_index=0, start=0.0, duration=30.0,
        tags=tuple(sorted(tags.items())),
    ))


class StubWorker(values_module.ValueWorker):
    """The real worker with its thread affinity stubbed.

    See `tests/test_mesh_window.py`'s version: `moveToThread` onto a thread with no
    running event loop leaves the worker bound to it, so every signal is queued to a
    loop that never runs and the window's slots never fire.
    """

    def moveToThread(self, thread):
        pass


class FakeThread(QThread):
    """A real QThread that runs its worker synchronously."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.deleted = False

    def start(self):
        self.started.emit()
        self.finished.emit()

    def deleteLater(self):
        self.deleted = True


class Recorder:
    """A `QMessageBox` that records instead of blocking, with a settable answer.

    The two answers are the **real** `StandardButton` values rather than 1 and 2.
    PySide6's enum does not compare equal to its own integer — `QMessageBox.Yes` is
    16384, and a fake that says `Yes = 1` silently never matches, so a test asking
    for confirmation and then asserting the deletion happened would instead assert
    that declining left the table untouched, and pass.
    """

    Yes = QMessageBox.StandardButton.Yes
    No = QMessageBox.StandardButton.No
    answer = No

    seen = []

    @staticmethod
    def question(_parent, title, message, *_buttons, default=0):
        Recorder.seen.append((title, message))
        return Recorder.answer

    @staticmethod
    def warning(_parent, title, message, *_args):
        Recorder.seen.append((title, message))
        return Recorder.No

    @staticmethod
    def information(_parent, title, message, *_args):
        Recorder.seen.append((title, message))
        return Recorder.Yes


@pytest.fixture
def folder(tmp_path):
    """Two clips from somebody else's library, sharing one network value.

    Both are **fully settled** — four required tags each — so the base folder has no
    residue and the tests that are about residue add their own incomplete clip. A
    fixture whose clips were already unfinished would make the "nothing to review"
    button visible everywhere and quietly invalidate half the assertions below.
    """
    root = tmp_path / "import"
    put_clip(root, "Toonami/A.mp4",
             {**REQUIRED, "network": "CN", "block": "Toonami"})
    put_clip(root, "Toonami/B.mp4",
             {"title": "Second", "network": "CN", "filler_type": "Promo",
              "time_period": "2000s"})
    return root


class FakeShell:
    """Stands in for the process's shell, which `main.py` installs and no test does.

    `shared.session.shell()` raises without one, and that is the right behaviour — a
    window is only ever opened through the menu — so the hand-off is tested by
    recording what it asked for rather than by building a real one.
    """

    def __init__(self):
        self.opened = []

    def open_safely(self, name, **kwargs):
        self.opened.append((name, kwargs))


@pytest.fixture
def window_factory(qapp, monkeypatch, tmp_path, folder):
    """Builds Tagged Library Mesh windows over temporary folders.

    Takes `folder` so every test starts from the same populated import folder; a
    window over an empty one legitimately shows nothing, which is its own test.
    """
    shell = FakeShell()
    monkeypatch.setattr(values_module, "ValueWorker", StubWorker)
    monkeypatch.setattr(values_module, "QThread", FakeThread)
    monkeypatch.setattr(values_module, "shell", lambda: shell)
    monkeypatch.setattr(values_module.QMessageBox, "question",
                        staticmethod(Recorder.question))
    monkeypatch.setattr(values_module.QMessageBox, "warning",
                        staticmethod(Recorder.warning))
    monkeypatch.setattr(values_module.QMessageBox, "information",
                        staticmethod(Recorder.information))
    monkeypatch.setattr(values_module, "vocabulary_path",
                        lambda: str(tmp_path / "vocabulary.json"))
    monkeypatch.setattr(values_module, "import_folder", lambda: str(tmp_path))
    monkeypatch.setattr(values_module, "export_folder",
                        lambda: str(tmp_path / "library"))
    Recorder.seen = []
    Recorder.answer = Recorder.No
    built = []

    def open_values(root=None, library_root=None):
        os.makedirs(str(library_root or tmp_path / "library"), exist_ok=True)
        window = values_module.ValuesWindow(
            root=str(root if root is not None else folder),
            library_root=str(library_root or tmp_path / "library"))
        built.append(window)
        return window

    open_values.shell = shell
    yield open_values

    for window in built:
        window.close()
        window.deleteLater()


def answering(window):
    """Whether the window is still asking a question.

    Asked of the **model**, not of a widget. `isVisible()` on a widget inside a
    window nobody called `show()` on is always False, and `isHidden()` would have
    made this helper depend on the window hiding the answer row on the plan screen —
    which it should, but that is a separate test and not the loop's business.
    """
    return window.session.pending() != ()


def question_is(window, key):
    """Whether the value on screen is `(namespace, value)`.

    Not the first question: which value comes first is a *presentation* decision
    (`ValueSession._best_key`, most clips first, ties on the sorted key) and a test
    about rendering one value should not break every time another value is added to
    the fixture.
    """
    prompt = window.session.next_prompt()
    return prompt is not None and (prompt.entry.namespace,
                                   prompt.entry.value) == key


def answer_until(window, key):
    """Keep everything until `key` is the value on screen."""
    while answering(window) and not question_is(window, key):
        window.on_keep()
    assert question_is(window, key), f"{key} never came up"


def answer_everything(window, translate=None):
    """Answer every value, and hand the caller each entry as it comes.

    `translate` maps `(namespace, value)` to the value to call it, so a test can say
    "`network: CN` becomes `Cartoon Network`, everything else is kept" without
    repeating the loop. Default is keep everything.
    """
    translate = translate or {}
    answered = []
    while answering(window):
        entry = window.session.next_prompt().entry
        key = (entry.namespace, entry.value)
        if key in translate:
            window.ui.comboValue.setCurrentText(translate[key])
            window.on_translate()
        else:
            window.on_keep()
        answered.append(key)
    return answered


# ---------------------------------------------------------------------------
# Reading the folder
# ---------------------------------------------------------------------------

def test_the_window_offers_a_value_the_library_already_uses(window_factory,
                                                             tmp_path):
    """The most common case on a good import: the author used the same words, so the
    right answer is one click and the window has to make that visible.

    The library has to hold the *same* value for this to be the interesting case.
    A library that calls it `Cartoon Network` and a folder that calls it `CN` is the
    *other* question — the one where the ranked evidence says nothing and the user
    has to type — which is its own test.
    """
    put_clip(tmp_path / "library", "Mine.mp4", {**REQUIRED, "network": "CN"})
    window = window_factory()
    answer_until(window, ("network", "CN"))

    prompt = window.session.next_prompt()
    assert prompt.matches, "the library has this value, so it must be offered"
    assert prompt.matches[0].value == "CN"
    assert prompt.matches[0].in_library is True
    assert prompt.matches[0].clip_count == 1
    assert "1 clip(s) in your library use this" in window.ui.labelEvidence.text()
    assert window.ui.comboValue.currentText() == "CN", (
        "pre-filled as a suggestion. It is a suggestion and nothing more — the "
        "answer still needs a button pressed.")


def test_the_tag_history_alone_is_the_weaker_kind_of_evidence(window_factory,
                                                             tmp_path):
    """Shown as what it is. A value no clip uses but that has been typed before is a
    hint, not a fact, and a user who knows that will weigh it differently."""
    from shared.vocabulary import get_vocabulary

    get_vocabulary(str(tmp_path / "vocabulary.json")).record({"network": "CN"})
    window = window_factory()
    answer_until(window, ("network", "CN"))

    prompt = window.session.next_prompt()

    assert prompt.matches[0].in_library is False
    assert "tag history" in window.ui.labelEvidence.text()


def test_a_value_the_library_has_never_seen_offers_nothing_and_says_so(window_factory):
    """Empty is not permission to guess: it means new here, and the screen says so."""
    window = window_factory()
    answer_until(window, ("block", "Toonami"))

    assert window.session.matches_for("block", "Toonami") == ()
    assert "new value here" in window.ui.labelEvidence.text()


def test_the_values_still_to_go_are_listed(window_factory):
    """The size of what is left, so an eight-hundred-clip import is not a surprise
    on the last question."""
    window = window_factory()

    listed = [window.ui.listPending.item(i).text()
              for i in range(window.ui.listPending.count())]
    assert any("network: CN" in text for text in listed)
    assert all("clip(s)" in text for text in listed)


def test_a_folder_with_nothing_tagged_points_at_the_untagged_window(
        window_factory, tmp_path):
    """The two modes are told apart by the one fact the scan produced, and this
    screen is where the user is told which one they are in."""
    empty = tmp_path / "empty"
    os.makedirs(str(empty), exist_ok=True)

    window = window_factory(empty)

    assert "Untagged Library Mesh" in window.ui.labelQuestion.text()
    assert window.ui.buttonImport.isHidden() is True, (
        "there is nothing to import either, so neither ending is offered")


def test_the_values_still_to_go_are_listed(window_factory):
    """The size of what is left, so an eight-hundred-clip import is not a surprise
    on the last question."""
    window = window_factory()

    listed = [window.ui.listPending.item(i).text()
              for i in range(window.ui.listPending.count())]
    assert any("network: CN" in text for text in listed)
    assert all("clip(s)" in text for text in listed)


# ---------------------------------------------------------------------------
# The three answers
# ---------------------------------------------------------------------------

def test_use_this_value_translates_and_moves_on(window_factory):
    window = window_factory()
    answer_until(window, ("network", "CN"))

    window.ui.comboValue.setCurrentText("Cartoon Network")
    window.on_translate()

    assert window.session.find("network", "CN").state == "translated"
    assert window.session.find("network", "CN").resolved_value() == "Cartoon Network"


def test_keep_as_it_is_is_an_answer_not_a_no_op(window_factory):
    """It is what takes a value out of the pending set, and it is the answer given
    most often on a library whose author used the same words this one does."""
    window = window_factory()
    answer_until(window, ("network", "CN"))

    window.on_keep()

    assert window.session.find("network", "CN").state == "kept"
    assert window.session.find("network", "CN") not in window.session.pending()


def test_removing_the_tag_asks_first_and_says_what_it_will_cost(window_factory):
    """It is the one answer here that can make a clip *incomplete*, so it is
    confirmed, and the confirmation names the clips and the consequence."""
    window = window_factory()
    answer_until(window, ("network", "CN"))

    Recorder.answer = Recorder.No
    window.on_delete()
    assert Recorder.seen, "nothing was asked"
    assert "2 clip(s)" in Recorder.seen[-1][1]
    assert "Library Mesh Tag Editor" in Recorder.seen[-1][1]
    assert window.session.find("network", "CN").state == "untranslated", (
        "declining leaves the table untouched")

    Recorder.answer = Recorder.Yes
    window.on_delete()
    assert window.session.find("network", "CN").state == "deleted"


def test_an_answer_the_record_would_refuse_is_a_label_not_a_crash(window_factory):
    """It is a thing about the value the user just typed. Replacing the window with
    an error would throw away every answer given so far."""
    window = window_factory()
    answer_until(window, ("network", "CN"))
    assert answering(window)

    window.ui.comboValue.setCurrentText("Cartoon\rNetwork")
    window.on_translate()

    assert "could not be stored" in window.ui.labelStatus.text()
    assert window.session.find("network", "CN").state == "untranslated"
    assert answering(window), "the window is still on the question"


def test_an_empty_box_cannot_translate(window_factory):
    """The "never written without being tied" rule as a UI fact: there is no way to
    press a button that translates a value onto nothing. An empty box is a mistyped
    value; `Remove This Tag` is the deliberate act."""
    window = window_factory()
    answer_until(window, ("network", "CN"))

    window.ui.comboValue.setCurrentText("")
    assert window.ui.buttonTranslate.isEnabled() is False

    window.on_translate()
    assert window.session.find("network", "CN").state == "untranslated", (
        "and calling the slot directly does not get around the model either")


# ---------------------------------------------------------------------------
# The plan, and writing it
# ---------------------------------------------------------------------------

def test_nothing_is_written_until_every_value_has_an_answer(window_factory,
                                                             tmp_path):
    """The one thing this window must never do: write half a library's vocabulary
    back into its own records."""
    window = window_factory()

    while answering(window):
        assert window.ui.buttonApply.isHidden(), (
            "Apply appears only once there is nothing left to ask")
        window.on_keep()

    tags = {clip.relative_path: clip.tag_dict()
            for clip in build_catalog(str(tmp_path / "import")).clips}
    assert tags["Toonami/A.mp4"]["network"] == "CN", (
        "answering a question changes a table, not a file")


def test_the_plan_screen_is_an_ending_even_with_nothing_to_write(window_factory):
    """The dead end this window used to have.

    Answer every value with *keep* and there is nothing for `plan_translation` to
    do, so Apply had nothing to do either. Apply was therefore disabled, the only
    remaining button was Close, and the screen's own text said "the clips are ready
    to import as they are" — an instruction the window provided no way to follow.

    So Import Now is offered on the plan screen, and Apply is **hidden** rather than
    greyed out: a disabled control on a screen that says there is nothing to do is
    a control that looks broken.
    """
    window = window_factory()
    answer_everything(window)

    assert window.ui.buttonImportPlan.isHidden() is False, (
        "the plan screen has to be able to start an import, or a user who kept "
        "everything has nowhere to go")
    assert window.ui.buttonClosePlan.isHidden() is False
    assert window.ui.buttonApply.isHidden() is True, (
        "and the button that has nothing to do is not merely sitting there "
        "disabled")


def test_applying_is_never_implied_by_importing(window_factory, tmp_path,
                                                monkeypatch):
    """The trap the plan screen's Import button could have opened.

    Importing without applying files the clips under the values they currently
    carry, and nothing parses a name back into a tag afterwards (invariant 12) — so
    the answers the user just gave would be silently dropped into a library they
    cannot take back. With something to write, importing asks and names the count.
    """
    window = window_factory()
    answer_everything(window, {("network", "CN"): "Cartoon Network"})
    assert window._pending_clips == 2

    asked = []
    answer = {"yes": False}

    def question(_parent, title, message, *_buttons, default=0):
        asked.append((title, message))
        return (QMessageBox.StandardButton.Yes if answer["yes"]
                else QMessageBox.StandardButton.No)

    monkeypatch.setattr(values_module.QMessageBox, "question",
                        staticmethod(question))
    calls = []
    monkeypatch.setattr(values_module, "confirm_and_import",
                        lambda *args: calls.append(args))

    window.on_import_now()

    assert calls == [], "declining the confirmation must not import"
    assert asked and "2 record(s)" in asked[0][1], (
        "and the question has to say what is being left behind")
    assert "cannot be changed this way later" in asked[0][1]

    answer["yes"] = True
    window.on_import_now()
    assert len(calls) == 1


def test_importing_with_nothing_pending_asks_nothing(window_factory,
                                                     monkeypatch):
    """Nothing to write means nothing to discard, so a confirmation would be a
    question about nothing — and a dialog the user cannot tell the meaning of is
    worse than no dialog."""
    window = window_factory()
    answer_everything(window)
    assert window._pending_clips == 0

    def refuse(*_args, **_kwargs):
        raise AssertionError("asked about a discard that cannot happen")

    monkeypatch.setattr(values_module.QMessageBox, "question",
                        staticmethod(refuse))
    calls = []
    monkeypatch.setattr(values_module, "confirm_and_import",
                        lambda *args: calls.append(args))

    window.on_import_now()

    assert len(calls) == 1


def test_the_plan_says_what_writing_would_touch(window_factory):
    window = window_factory()
    answer_everything(window)

    report = window.ui.textReport.toPlainText()

    assert "Nothing needs writing" in report, (
        "everything was kept, so the honest answer is that no record changes")


def test_applying_rewrites_the_records_and_says_how_many(window_factory,
                                                         tmp_path):
    window = window_factory()
    answer_everything(window, {("network", "CN"): "Cartoon Network"})

    window.on_apply()

    tags = {clip.relative_path: clip.tag_dict()
            for clip in build_catalog(str(tmp_path / "import")).clips}
    assert tags["Toonami/A.mp4"]["network"] == "Cartoon Network"
    assert tags["Toonami/B.mp4"]["network"] == "Cartoon Network"
    assert "2 record(s) rewritten" in window.ui.labelQuestion.text()


def test_the_videos_are_untouched_by_the_write(window_factory, tmp_path):
    """Invariant 12, checked on the window's own run: the video is never opened,
    moved or rewritten, so `video present` implies `record present` throughout."""
    videos = {name: os.path.getsize(
        os.path.join(str(tmp_path / "import"), *name.split("/")))
        for name in ("Toonami/A.mp4", "Toonami/B.mp4")}

    window = window_factory()
    answer_everything(window, {("network", "CN"): "Cartoon Network"})
    window.on_apply()

    for name, size in videos.items():
        assert os.path.getsize(
            os.path.join(str(tmp_path / "import"), *name.split("/"))) == size


def test_a_merge_is_reported_before_anything_is_written(window_factory, tmp_path):
    """Legal, and sometimes exactly the point — but it is the one outcome that makes
    two clips indistinguishable, so it is shown rather than discovered later in an
    import summary."""
    put_clip(tmp_path / "import", "Other/C.mp4",
             {"title": "Third", "network": "CBS"})

    window = window_factory()
    answer_everything(window, {("network", "CN"): "Cartoon Network",
                               ("network", "CBS"): "Cartoon Network"})

    report = window.ui.textReport.toPlainText()
    assert "indistinguishable" in report
    assert "'CBS', 'CN'" in report


# ---------------------------------------------------------------------------
# Onward
# ---------------------------------------------------------------------------

def test_the_residue_button_appears_only_when_a_clip_still_needs_a_tag(
        window_factory, tmp_path):
    """Removing a required tag does not fail — it sends the clips carrying it to the
    Tag Editor, which asks for it by name. That is the designed path, and this is
    the button that takes it."""
    put_clip(tmp_path / "import", "Other/D.mp4", {"title": "No Network"})
    Recorder.answer = Recorder.Yes

    window = window_factory()
    while answering(window):
        entry = window.session.next_prompt().entry
        if entry.namespace == "network" and entry.value == "CN":
            window.on_delete()
        else:
            window.on_keep()

    window.on_apply()

    assert window.ui.buttonQueue.isHidden() is False
    assert "Still Needing Tags" in window.ui.buttonQueue.text()


def test_a_fully_settled_folder_is_offered_straight_to_the_import(window_factory):
    """No residue means no second per-clip pass, which is the whole reason the
    residue is left to the queue instead of being given its own screen here."""
    window = window_factory()
    answer_everything(window)

    window.on_apply()

    assert window.ui.buttonQueue.isHidden() is True
    assert window.ui.buttonImport.isHidden() is False


def test_the_import_runs_through_the_one_shared_screen(window_factory,
                                                       tmp_path, monkeypatch):
    """One progress dialog and one summary for both callers.

    `importer/importrun.py` exists because the Tag Editor and this window both end
    at the same place, and two copies of that screen would drift.
    """
    calls = []
    monkeypatch.setattr(values_module, "confirm_and_import",
                        lambda *args: calls.append(args))

    window = window_factory()
    answer_everything(window)
    window.on_apply()
    window.on_import_now()

    assert len(calls) == 1
    assert calls[0][1] == str(tmp_path / "import")


def test_the_queue_is_opened_on_the_same_folder_and_re_derives_the_clips(
        window_factory, tmp_path):
    """No clip list travels with the hand-off.

    `QueueClip.is_already_done` asks `missing_required_tags`, so re-opening the queue
    picks up exactly the clips this window left unfinished. Nothing is passed but the
    folder, because the folder is where the truth lives.
    """
    window = window_factory()

    window.on_queue()

    assert window_factory.shell.opened == [
        ("queue", {"root": str(tmp_path / "import")})]


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------

def test_the_window_cannot_be_closed_while_the_folder_is_still_being_read(
        window_factory, tmp_path, monkeypatch):
    """A `QThread` still running when its owner is destroyed aborts the process, and
    `docs/status.md` records what that looks like: a `qFatal` with nothing in the
    log. A thread that never stops is the same failure from the other end."""
    class StuckThread(FakeThread):
        def start(self):
            self.started.emit()

    monkeypatch.setattr(values_module, "QThread", StuckThread)
    window = values_module.ValuesWindow(root=str(tmp_path / "import"),
                                        library_root=str(tmp_path / "library"))
    try:
        from PySide6.QtGui import QCloseEvent

        event = QCloseEvent()
        window.closeEvent(event)
        assert event.isAccepted() is False
    finally:
        window._thread = None
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The real thread
# ---------------------------------------------------------------------------
#
# The `window_factory` above runs the worker through `FakeThread` and `StubWorker`:
# `start()` emits `started` and `finished` by hand on the GUI thread, and
# `moveToThread` is a no-op. A faked thread has no `exec()` loop left spinning, so it
# cannot show a worker whose thread never ends — which is the one failure this window
# cannot survive, because `closeEvent` refuses to close for as long as `self._thread`
# is set. These two use a real `QThread` and the real event loop, which is why
# `tests/test_mesh_window.py` and `tests/test_queue.py` have the same pair.

def _running(thread):
    """Whether a QThread is still alive, tolerating a deleted C++ object."""
    if thread is None:
        return False
    try:
        return thread.isRunning()
    except RuntimeError:
        return False


def _pump_until(qapp, predicate, timeout_ms=10000):
    """Spin the real event loop until `predicate` holds. False if it never does."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        qapp.processEvents()
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


@pytest.fixture
def real_thread_window(qapp, monkeypatch, tmp_path, folder):
    """A window over a real loading thread, with only the roots stubbed."""
    monkeypatch.setattr(values_module, "export_folder",
                        lambda: str(tmp_path / "library"))
    monkeypatch.setattr(values_module, "vocabulary_path",
                        lambda: str(tmp_path / "vocabulary.json"))
    monkeypatch.setattr(values_module, "import_folder", lambda: str(tmp_path))

    os.makedirs(str(tmp_path / "library"), exist_ok=True)
    window = values_module.ValuesWindow(root=str(folder),
                                        library_root=str(tmp_path / "library"))
    window._thread.deleteLater = lambda: None
    yield window, qapp

    thread = window._thread
    if _running(thread):
        # Stopped rather than left to Qt: a spinning thread destroyed at interpreter
        # shutdown is a `qFatal`, and would abort the whole run.
        thread.quit()
        thread.wait(10000)
    qapp.processEvents()
    window._thread = None
    window.close()
    window.deleteLater()


def test_the_loading_thread_terminates_by_itself(real_thread_window):
    """`thread.started.connect(worker.run)` runs the worker's slot inside the
    thread's `exec()` loop, and a slot returning does not leave that loop, so
    nothing but `worker.quit()` ends the thread."""
    window, qapp = real_thread_window
    thread = window._thread
    assert thread is not None, "a worker was started, so there is a thread"

    assert _pump_until(qapp, lambda: not _running(thread)), (
        "the loading thread never terminated: the worker's slot returned but "
        "nothing quit the QThread's event loop, so `thread.finished` "
        "never fired")


def test_the_window_becomes_closable_once_it_finished_reading(real_thread_window):
    """The consequence that made this the worst place for the bug.

    `closeEvent` refuses while `self._thread` is set, and only `_on_stopped` clears
    it — on `thread.finished`, which a thread that never ends never emits. So this
    window could not be closed at all, and the shell would ignore the refusal and
    put the tag editor on top of it: two live windows, each holding a thread that was
    never going to stop.
    """
    window, qapp = real_thread_window

    assert _pump_until(qapp, lambda: window._thread is None), (
        "the window still holds its loading thread, so its closeEvent refuses to "
        "close and the shell can never replace it")

    assert window.close(), "and with no thread left, closing it must work"


def test_the_builder_takes_the_application_first_and_the_folders_after():
    import inspect

    parameters = list(inspect.signature(values_module.create).parameters)

    assert parameters[:4] == ["app", "session", "root", "library_root"]


def test_the_window_carries_the_name_its_ui_gives_it(window_factory):
    """The `.ui` is the one place a name is written down.

    Qt does not copy `windowTitle` from a central widget to its `QMainWindow`, so a
    window that only calls `setCentralWidget` has an empty title bar and an
    indistinguishable taskbar entry — which matters here more than it used to,
    because this window is now told apart from the other two importer windows by its
    name alone.
    """
    window = window_factory()

    assert window.ui.windowTitle() == "Tagged Library Mesh"
    assert window.windowTitle() == window.ui.windowTitle()
