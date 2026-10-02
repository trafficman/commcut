"""Tests for the Library Mesh Wizard window.

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
    """A destination library, so the wizard has evidence to rank."""
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

    A second window rather than a mutation of the first, because the wizard reads
    its whole input once at construction, which is itself worth not working around.
    """
    videos_in(wizard.import_root, *relative_paths)
    return mesh_module.MeshWindow(wizard.import_root)


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------

def test_the_window_offers_the_namespace_dropdown_without_title(wizard):
    """A folder is a shared label; title is unique per clip."""
    namespaces = [wizard.ui.comboNamespace.itemText(index)
                  for index in range(wizard.ui.comboNamespace.count())]

    assert "title" not in namespaces
    assert "network" in namespaces


def test_an_empty_folder_says_so_and_offers_only_close(wizard):
    prompt = wizard.ui.labelQuestion.text()

    assert "No videos were found" in prompt
    assert wizard.ui.buttonReject.isEnabled() is False
    assert wizard.ui.buttonClose.isHidden() is False


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

        assert Recorder.seen and "two values" in Recorder.seen[0][1]
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
# The builder
# ---------------------------------------------------------------------------

def test_the_builder_takes_the_application_first_and_an_optional_root():
    import inspect

    parameters = list(inspect.signature(mesh_module.create).parameters)

    assert parameters[:2] == ["app", "root"]
