"""Tests for the Untagged Library Mesh window.

The wizard's *decisions* are `tests/test_mesh.py`'s subject — this file is about
rendering them, plus the two things only a window can do:

- running the vocabulary sync and the two walks off the GUI thread, and showing
  what the sync did rather than doing it silently
- asking before a conflict is committed, so declining leaves the table untouched

Both the thread and the message boxes are substituted, for the same reasons
`tests/test_settings.py` and `tests/test_editor_export.py` give theirs: a real
`QMessageBox` under `QT_QPA_PLATFORM=offscreen` blocks on nobody and hangs the run,
and `moveToThread` onto a thread with no running event loop makes every signal
queued to a loop that never runs.
"""

import os
import time

import pytest
from PySide6.QtCore import QThread

import importer.mesh as mesh_module
from editor_stub import ensure_qapp
from shared.mesh import MESHED, REJECTED, MeshSession
from shared.records import ClipRecord, RECORD_EXTENSION, write_record


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


def videos_in(root, *relative_paths):
    """Real videos on disk, so `find_videos` has something to walk."""
    for relative in relative_paths:
        path = os.path.join(str(root), *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * 8)
    return str(root)


def library_with(*relative_paths):
    """A destination library, so the mesh has evidence to rank."""
    for relative in relative_paths:
        path = os.path.join(str(relative))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * 8)
        write_record(os.path.splitext(path)[0] + RECORD_EXTENSION, ClipRecord(
            source="theirs.mp4", segment_index=0, start=0.0, duration=30.0,
            tags=(("network", "Cartoon Network"), ("time_period", "2000s"),
                  ("filler_type", "Promo")),
        ))


class StubWorker(mesh_module.MeshWorker):
    """The real worker, with the one thing a fake thread cannot give it.

    `moveToThread` onto a QThread with no running event loop leaves the worker
    bound to that thread, so every signal it emits is *queued* to a loop that
    never runs and the window's slots never fire.
    `tests/editor_stub.py` hits the same thing and solves it the same way: keep
    the real worker and its real signals, and stub only the affinity.
    """

    def moveToThread(self, thread):
        pass


class FakeThread(QThread):
    """A real QThread that runs its worker synchronously.

    `moveToThread` is stubbed out above, so `started.emit()` reaches `run` directly
    and the whole loading sequence happens in order.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.deleted = False

    def start(self):
        self.started.emit()
        self.finished.emit()

    def deleteLater(self):
        self.deleted = True


class Recorder:
    """A `QMessageBox` that records instead of blocking, with a settable answer."""

    Yes = 1
    No = 2
    answer = No

    @staticmethod
    def question(_parent, title, message, *_buttons, default=0):
        Recorder.seen.append((title, message))
        return Recorder.answer

    @staticmethod
    def warning(_parent, title, message, *_args):
        Recorder.seen.append((title, message))
        return Recorder.No

    seen = []


@pytest.fixture
def wizard(qapp, monkeypatch, tmp_path):
    """A mesh window over a temporary import folder and library."""
    import_folder_root = tmp_path / "import"
    library_root = tmp_path / "library"
    os.makedirs(str(import_folder_root), exist_ok=True)
    os.makedirs(str(library_root), exist_ok=True)

    monkeypatch.setattr(mesh_module, "MeshWorker", StubWorker)
    monkeypatch.setattr(mesh_module, "QThread", FakeThread)
    monkeypatch.setattr(mesh_module, "export_folder", lambda: str(library_root))
    monkeypatch.setattr(mesh_module, "vocabulary_path",
                        lambda: str(tmp_path / "vocabulary.json"))
    monkeypatch.setattr(mesh_module, "import_folder",
                        lambda: str(import_folder_root))
    monkeypatch.setattr(mesh_module, "QMessageBox", Recorder)
    Recorder.seen = []
    Recorder.answer = Recorder.No

    window = mesh_module.MeshWindow(str(import_folder_root))
    window.import_root = str(import_folder_root)
    window.library_root = str(library_root)
    yield window

    window.close()
    window.deleteLater()
    qapp.processEvents()


def build_wizard(wizard, *relative_paths):
    """A second window over the same fixture, with some videos in it.

    A second window rather than a mutation of the first, because the window reads
    its whole input once at construction, which is itself worth not working around.
    """
    videos_in(wizard.import_root, *relative_paths)
    return mesh_module.MeshWindow(wizard.import_root)


def build_tagged(wizard, relative, tags=(("network", "CN"), ("filler_type", "Ad"),
                                        ("time_period", "2000s"))):
    """A clip that already has a record beside it — somebody else's library.

    `tags` carries no `title` on purpose: the whole point of `has_record` is that the
    *tags* are known, and the folder names around it are a rendering, not an answer.
    """
    from shared.records import ClipRecord, write_record

    path = os.path.join(wizard.import_root, *relative.split("/"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * 8)
    write_record(os.path.splitext(path)[0] + RECORD_EXTENSION, ClipRecord(
        source=relative, segment_index=0, start=0.0, duration=30.0, tags=tags))


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------

def test_the_window_offers_the_namespace_dropdown_without_title(wizard):
    """A folder is a shared label; title is unique per clip."""
    namespaces = [wizard.ui.comboNamespace.itemText(index)
                  for index in range(wizard.ui.comboNamespace.count())]

    assert "title" not in namespaces
    assert "network" in namespaces


def test_the_window_carries_the_name_its_ui_gives_it(wizard):
    """The `.ui` is the one place the name is written down, and Qt does not copy
    `windowTitle` from a central widget to its `QMainWindow`."""
    assert wizard.ui.windowTitle() == "Untagged Library Mesh"
    assert wizard.windowTitle() == wizard.ui.windowTitle()


def test_an_empty_folder_says_so_and_offers_only_close(wizard):
    prompt = wizard.ui.labelQuestion.text()

    assert "No videos were found" in prompt
    assert wizard.ui.buttonReject.isEnabled() is False
    assert wizard.ui.buttonClose.isHidden() is False
    assert wizard.ui.buttonValues.isHidden() is True, (
        "an empty folder has nothing to review either")


def test_a_folder_where_every_clip_is_tagged_is_offered_the_value_mesh(wizard):
    """The front door for a library exported from another commcut.

    The folder names here are that install's *rendered* output, so asking what each
    one means would produce answers about a rendering rather than about the clips.
    `has_record` is the one fact that says so, and this is the branch it exists for.
    """
    build_tagged(wizard, "Cartoon Network/2000s/Promo/Toonami - Vol 3.mp4")

    window = mesh_module.MeshWindow(wizard.import_root)
    try:
        assert "already has a record" in window.ui.labelQuestion.text()
        assert wizard.ui.buttonReject.isEnabled() is False
        assert window.ui.buttonValues.isHidden() is False
        assert "Cartoon Network" not in window.ui.labelQuestion.text(), (
            "and the rendered folder names are not put up as questions")
    finally:
        window.close()
        window.deleteLater()


def test_the_value_mesh_hand_off_carries_only_the_folder(wizard, monkeypatch):
    """No clip list travels with it — the Tagged Library Mesh builds its own session
    from the folder, which is where the truth lives."""
    calls = []

    class FakeShell:
        def open_safely(self, name, **kwargs):
            calls.append((name, kwargs))

    monkeypatch.setattr(mesh_module, "shell", lambda: FakeShell())
    build_tagged(wizard, "CN/A.mp4")
    window = mesh_module.MeshWindow(wizard.import_root)
    try:
        window.on_review_values()
        assert calls == [("values", {"root": wizard.import_root})]
    finally:
        window.close()
        window.deleteLater()


def test_a_mixed_folder_meshes_the_untagged_half_and_says_what_it_skipped(wizard):
    """The interesting case, and the one `has_record` exists for: a folder holding
    both halves at once. The untagged half is meshed, and the tagged half is named
    rather than silently ignored."""
    build_tagged(wizard, "CN/Already Done.mp4")
    videos_in(wizard.import_root, "Toonami/A.mp4")

    window = mesh_module.MeshWindow(wizard.import_root)
    try:
        assert "Toonami" in window.ui.labelQuestion.text(), (
            "the untagged clip's folder is a question")
        assert window.session.pending(), "and it is meshable"

        while window.session.pending():
            window.on_reject()
        report = window.ui.textReport.toPlainText()
        assert "1 clip(s) in this folder already had a record" in report
        assert "Tagged Library Mesh" in report
    finally:
        window.close()
        window.deleteLater()


def test_a_mixed_folder_does_not_mesh_the_already_tagged_half(wizard):
    """Their folder names are somebody's rendering. Tying them to a namespace here
    would be answering a question nobody asked."""
    build_tagged(wizard, "Cartoon Network/Already Done.mp4")
    videos_in(wizard.import_root, "Toonami/A.mp4")

    window = mesh_module.MeshWindow(wizard.import_root)
    try:
        assert [entry.name for entry in window.session.entries()] == ["Toonami"]
    finally:
        window.close()
        window.deleteLater()


def test_the_first_page_shows_the_deepest_unmeshed_path(wizard):
    window = build_wizard(wizard, "CN/A.mp4", "one/two/three/B.mp4")
    try:
        assert "one" in window.ui.labelQuestion.text()
    finally:
        window.close()
        window.deleteLater()


def test_a_folder_name_is_the_question_not_the_file_name(wizard):
    """The narrowing to folder names: the file contributes nothing."""
    window = build_wizard(wizard, "Toonami Blocks/Worlds Finest.mp4")
    try:
        question = window.ui.labelQuestion.text()
        assert "Toonami Blocks" in question
        assert "Worlds Finest" not in question
    finally:
        window.close()
        window.deleteLater()


def test_the_path_bar_colours_each_segment_and_marks_the_current_one(wizard):
    window = build_wizard(wizard, "CN/2000s/Promo/A.mp4")
    try:
        html = window.ui.labelPathBar.text()

        assert html.count("background:#") == 3, (
            "each folder on the path must be individually coloured")
        assert "font-weight:700" in html, "and the current one must stand out"
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------

def test_assigning_advances_to_the_next_folder_name(wizard):
    window = build_wizard(wizard, "CN/2000s/A.mp4")
    try:
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Cartoon Network")
        window.on_assign()

        assert window.session.entry("CN").state == MESHED
        assert "2000s" in window.ui.labelQuestion.text()
    finally:
        window.close()
        window.deleteLater()


def test_assigning_records_the_answer_in_the_vocabulary(wizard):
    """The value the user just chose is the one they will be asked for again.

    `CN` might appear in the next library too, and the alternative is typing
    `Cartoon Network` once per folder name per run — which is the whole cost this
    window exists to remove. Recorded through `record_use` against the same
    resolved path the open-time sync used, so it lands in the file rather than in a
    copy of it.
    """
    from shared.vocabulary import Vocabulary

    window = build_wizard(wizard, "CN/2000s/A.mp4")
    try:
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Cartoon Network")

        window.on_assign()

        assert "Cartoon Network" in Vocabulary.load(
            mesh_module.vocabulary_path()).values("network")
    finally:
        window.close()
        window.deleteLater()


def test_a_rejected_name_records_nothing(wizard):
    """Rejecting is a decision that a folder name is *not* a tag, so there is no
    value to remember — and a name is not a value, whatever else it is."""
    from shared.vocabulary import Vocabulary

    window = build_wizard(wizard, "CN/A.mp4")
    try:
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Cartoon Network")

        window.on_reject()

        assert "Cartoon Network" not in Vocabulary.load(
            mesh_module.vocabulary_path()).values("network")
    finally:
        window.close()
        window.deleteLater()


def test_assign_is_refused_until_both_halves_are_filled(wizard):
    """The "never written without being tied" rule as a UI fact: there is no way
    to press a button that meshes a name onto nothing."""
    window = build_wizard(wizard, "CN/A.mp4")
    try:
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("")

        assert window.ui.buttonAssign.isEnabled() is False

        window.ui.comboValue.setCurrentText("Cartoon Network")

        assert window.ui.buttonAssign.isEnabled() is True
    finally:
        window.close()
        window.deleteLater()


def test_rejecting_marks_the_name_and_never_offers_it_again(wizard):
    window = build_wizard(wizard, "CN/2000s/A.mp4")
    try:
        window.on_reject()

        assert window.session.entry("CN").state == REJECTED
        assert "CN" not in window.ui.labelQuestion.text()
    finally:
        window.close()
        window.deleteLater()


def test_choosing_a_namespace_repopulates_the_value_list(wizard):
    """A user who changes their mind about where a folder name belongs must see
    the other namespace's values, not a list that silently kept the old ones."""
    window = build_wizard(wizard, "CN/A.mp4")
    try:
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.addItem("Cartoon Network")

        window.ui.comboNamespace.setCurrentText("special")

        assert window.ui.comboValue.findText("Cartoon Network") == -1
    finally:
        window.close()
        window.deleteLater()


def test_a_pre_selected_namespace_is_still_only_a_suggestion(wizard):
    """The library is read so a value can be offered; it is not accepted for the
    user."""
    library_with(os.path.join(wizard.library_root, "Toonami", "A.mp4"))
    window = build_wizard(wizard, "Toonami/B.mp4")
    try:
        assert window.session.entry("Toonami").state != MESHED
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def answer_conflict(yes: bool):
    Recorder.seen = []
    Recorder.answer = Recorder.Yes if yes else Recorder.No


def test_a_conflict_is_asked_about_before_it_is_committed(wizard):
    """Declining has to leave the table untouched, or "Assign anyway?" is a
    question with no way to answer no."""
    window = build_wizard(wizard, "CN/Cartoon Network/A.mp4")
    try:
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Cartoon Network")
        window.on_assign()

        answer_conflict(yes=False)
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Nickelodeon")
        window.on_assign()

        assert Recorder.seen, "the collision must be raised before it is committed"
        title, message = Recorder.seen[0]
        assert "would get two values" in title, (
            "titled by consequence, not as though a namespace could be owned")
        assert "CN/Cartoon Network" in message, "and it says where it happens"
        assert "need editing by hand" in message
        assert window.session.entry("Cartoon Network").state != MESHED, (
            "declining must leave the table as it was")
    finally:
        window.close()
        window.deleteLater()


def test_accepting_a_conflict_commits_it_and_records_it(wizard):
    window = build_wizard(wizard, "CN/Cartoon Network/A.mp4")
    try:
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Cartoon Network")
        window.on_assign()

        answer_conflict(yes=True)
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Nickelodeon")
        window.on_assign()

        assert window.session.entry("Cartoon Network").state == MESHED
        assert window.session.conflicts()
    finally:
        window.close()
        window.deleteLater()


def test_a_name_whose_two_spellings_agree_does_not_ask(wizard):
    """Two folder names for one thing is the ordinary case and must not nag."""
    window = build_wizard(wizard, "CN/Cartoon Network/A.mp4")
    try:
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Cartoon Network")
        window.on_assign()

        answer_conflict(yes=False)
        window.ui.comboNamespace.setCurrentText("network")
        window.ui.comboValue.setCurrentText("Cartoon Network")
        window.on_assign()

        assert Recorder.seen == []
        assert window.session.entry("Cartoon Network").state == MESHED
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------

def test_answering_everything_shows_the_report(wizard):
    window = build_wizard(wizard, "CN/2000s/A.mp4")
    try:
        for namespace, value in (("network", "Cartoon Network"),
                                 ("time_period", "2000s")):
            window.ui.comboNamespace.setCurrentText(namespace)
            window.ui.comboValue.setCurrentText(value)
            window.on_assign()

        assert window.ui.textReport.isHidden() is False
        assert "CN  ->  network: Cartoon Network" in (
            window.ui.textReport.toPlainText())
        assert window.ui.buttonQueue.isHidden() is False, (
            "the report offers the next step, which is tagging the titles")
        assert window.ui.buttonClose.isHidden() is False
    finally:
        window.close()
        window.deleteLater()


def test_the_report_names_a_rejection(wizard):
    window = build_wizard(wizard, "CN/A.mp4")
    try:
        window.on_reject()

        assert "Rejected" in window.ui.textReport.toPlainText()
    finally:
        window.close()
        window.deleteLater()


def test_the_report_is_the_same_text_the_model_produces(wizard):
    window = build_wizard(wizard, "CN/A.mp4")
    try:
        window.on_reject()

        assert window.ui.textReport.toPlainText() == window.session.report()
    finally:
        window.close()
        window.deleteLater()


def test_the_report_hands_the_session_to_the_queue(wizard):
    """The alias table crosses with the window rather than being written to disk.

    It is a statement about *this* folder tree, so a saved copy would be stale
    the moment the user reorganises — and re-running that window rebuilds it from
    the tree, which is where its truth lives.
    """
    import mainwindow

    opened = []
    original = mesh_module.shell
    mesh_module.shell = lambda: type("Shell", (), {
        "open_safely": lambda _self, name, **kwargs:
            opened.append((name, kwargs))})()
    window = build_wizard(wizard, "CN/2000s/A.mp4")
    try:
        for namespace, value in (("network", "Cartoon Network"),
                                 ("time_period", "2000s")):
            window.ui.comboNamespace.setCurrentText(namespace)
            window.ui.comboValue.setCurrentText(value)
            window.on_assign()

        window.on_queue()

        assert [name for name, _ in opened] == ["queue"]
        assert opened[0][1]["session"] is window.session, (
            "the answers travel with the window, not through a file")
        assert opened[0][1]["root"] == wizard.import_root
    finally:
        mesh_module.shell = original
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The sync, and showing it
# ---------------------------------------------------------------------------

def test_the_sync_runs_before_the_first_page_and_is_reported():
    """It prunes, so it cannot happen invisibly in a constructor."""
    from shared.catalog import VocabularySync

    summary = mesh_module.vocabulary_sync_summary(VocabularySync(
        root="C:/library", clips_found=3, values_added=2,
        values_removed=(("block", "Toonami"),)))

    assert "3 clip(s)" in summary
    assert "block: Toonami" in summary


def test_a_pruned_vocabulary_is_named_in_the_sync_summary():
    from shared.catalog import VocabularySync

    summary = mesh_module.vocabulary_sync_summary(VocabularySync(
        root="C:/library", clips_found=2,
        values_removed=(("block", "A"), ("block", "B"))))

    assert "2 unused value(s) were removed" in summary
    assert "block: A" in summary


def test_an_empty_library_says_the_vocabulary_was_not_changed():
    from shared.catalog import VocabularySync

    summary = mesh_module.vocabulary_sync_summary(
        VocabularySync(root="C:/library", skipped_prune=True))

    assert "was not changed" in summary


def test_unreadable_library_records_are_named_and_grouped_by_reason():
    from shared.catalog import CatalogProblem
    from shared.records import REASON_NOT_WELL_FORMED, REASON_UNKNOWN_TAG_KEY

    text = mesh_module.problem_summary((
        CatalogProblem("a.cnfo", "Record holds an unknown tag key 'colour'",
                       REASON_UNKNOWN_TAG_KEY),
        CatalogProblem("b.cnfo", "not well-formed", REASON_NOT_WELL_FORMED),
        CatalogProblem("c.cnfo", "not well-formed", REASON_NOT_WELL_FORMED),
    ))

    assert "3 record(s)" in text
    assert f"{REASON_UNKNOWN_TAG_KEY} (1)" in text
    assert f"{REASON_NOT_WELL_FORMED} (2)" in text, (
        "grouped by reason, so a friend using an unknown tag reads"
        "differently from a corrupt file")


def test_a_clean_library_produces_no_problem_block():
    assert mesh_module.problem_summary(()) == ""


def test_the_vocabulary_sync_is_summarised_before_the_first_question(wizard):
    """Whatever the sync did is on screen while the user answers, not afterwards."""
    window = build_wizard(wizard, "CN/A.mp4")
    try:
        assert window.ui.labelEvidence.text().strip() != ""
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------

def test_closing_while_reading_is_refused(wizard):
    """A `QThread` still running when its owner is destroyed aborts the process,
    so there is no path that lets this window go first."""
    window = build_wizard(wizard, "CN/A.mp4")
    try:
        refused = []
        window._thread = object()  # pretend the worker is still going

        window.closeEvent(type("Event", (), {
            "ignore": lambda _self: refused.append(1)})())

        assert refused, "close must be ignored while a worker is running"
        assert window._cancel.is_set()
    finally:
        window._thread = None
        window.close()
        window.deleteLater()


def test_a_window_with_no_worker_left_closes_normally(wizard):
    window = build_wizard(wizard, "CN/A.mp4")

    window.close()
    window.deleteLater()


# ---------------------------------------------------------------------------
# The loading thread, on a real QThread
# ---------------------------------------------------------------------------
#
# The `wizard` fixture above runs the worker through `FakeThread` and
# `StubWorker`: `start()` emits `started` and `finished` by hand on the GUI
# thread and `moveToThread` is a no-op. A faked thread has no `exec()` loop left
# spinning, so it cannot show a worker whose thread never ends -- which is the
# one failure this window cannot survive, because `closeEvent` refuses to close
# for as long as `self._thread` is set.

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
def real_thread_wizard(qapp, monkeypatch, tmp_path):
    """A mesh window over a real loading thread, with only the roots stubbed."""
    import_folder_root = tmp_path / "import"
    library_root = tmp_path / "library"
    os.makedirs(str(import_folder_root), exist_ok=True)
    os.makedirs(str(library_root), exist_ok=True)
    videos_in(import_folder_root, "CN/A.mp4")

    monkeypatch.setattr(mesh_module, "export_folder", lambda: str(library_root))
    monkeypatch.setattr(mesh_module, "vocabulary_path",
                        lambda: str(tmp_path / "vocabulary.json"))
    monkeypatch.setattr(mesh_module, "import_folder",
                        lambda: str(import_folder_root))
    monkeypatch.setattr(mesh_module, "QMessageBox", Recorder)
    Recorder.seen = []
    Recorder.answer = Recorder.No

    window = mesh_module.MeshWindow(str(import_folder_root))
    window._thread.deleteLater = lambda: None
    yield window, qapp

    thread = window._thread
    if _running(thread):
        # Stopped rather than left to Qt: a spinning thread destroyed at
        # interpreter shutdown is a `qFatal`, and would abort the whole run.
        thread.quit()
        thread.wait(10000)
    qapp.processEvents()
    window._thread = None
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_the_loading_thread_terminates_by_itself(real_thread_wizard):
    """`thread.started.connect(worker.run)` runs the worker's slot inside the
    thread's `exec()` loop, and a slot returning does not leave that loop, so
    nothing but `thread.quit()` ends the thread."""
    window, qapp = real_thread_wizard
    thread = window._thread
    assert thread is not None, "a worker was started, so there is a thread"

    assert _pump_until(qapp, lambda: not _running(thread)), (
        "the loading thread never terminated: the worker's slot returned but "
        "nothing quit the QThread's event loop, so `thread.finished` "
        "never fired")


def test_the_wizard_becomes_closable_once_it_finished_reading(
        real_thread_wizard):
    """The consequence that made this the worst place for the bug.

    `closeEvent` refuses while `self._thread` is set, and only `_on_stopped`
    clears it -- on `thread.finished`, which a thread that never ends never
    emits. So that window could not be closed at all, and the shell ignored the
    refusal and put the tag editor on top of it: two live windows, each holding
    a thread that was never going to stop.
    """
    window, qapp = real_thread_wizard

    assert _pump_until(qapp, lambda: window._thread is None), (
        "the window still holds its loading thread, so its closeEvent refuses "
        "to close and the shell can never replace it")

    assert window.close(), "and with no thread left, closing it must work"

def test_the_builder_takes_the_application_first_and_an_optional_root():
    import inspect

    parameters = list(inspect.signature(mesh_module.create).parameters)

    assert parameters[:2] == ["app", "root"]
