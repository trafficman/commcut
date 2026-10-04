"""Tests for the Library Mesh Tag Editor.

The queue's whole job is to get one clip's title out of its file name and write a
record beside the video, so the tests are mostly about *which clips it visits*
and *what it does with the ones it cannot finish*:

- resume, which cannot be "a record exists" — a half-tagged record exists and is
  not finished
- the destructive skip, the one action here that loses files
- the report, the only place the user finds out what happened to the rest

mpv and the duration probe are stubbed throughout: a queue run needs eight
hundred of each, and neither is what these are about.
"""

import ast
import os
import time

import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QCloseEvent, QTextCursor

import importer.queue as queue_module
from editor_stub import ensure_qapp
from shared.mesh import MeshSession
from shared.mpv import MpvBridge
from shared.records import ClipRecord, load_record, write_record
from shared.vocabulary import get_vocabulary


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


REQUIRED = {
    "title": "Some Title",
    "network": "Cartoon Network",
    "filler_type": "Promo",
    "time_period": "2000s",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeVideo:
    """A `FoundVideo` for building a MeshSession over the same tree."""

    def __init__(self, relative_path):
        self.path = os.path.normpath(os.path.join("C:/library", relative_path))
        self.relative_path = relative_path
        self.has_record = False


class FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in self._slots:
            slot(*args)


class FakePlayer:
    """Just enough mpv for the **real** `MpvBridge` to attach to.

    `MpvBridge.__init__` only calls `observe_property`, so this does not have to
    guess at the bridge's surface — which is the point. An earlier hand-rolled
    fake here declared signals the real class does not have (`pausedChanged`,
    `loadfile`), so it agreed with a bug in the window instead of catching it,
    and the bug only surfaced when a person ran the app.
    """

    def __init__(self):
        self.observed = {}
        self.played = []
        self.pause = True
        self.time_pos = 0.0
        self.duration = 30.0
        self.idle_active = False

    def observe_property(self, name, callback):
        self.observed[name] = callback

    def play(self, path):
        self.played.append(path)


class RecordingBridge(MpvBridge):
    """The real bridge, recording what the queue asked of it.

    Every signal and every command is inherited, so a call to something that does
    not exist fails here exactly as it would in the app. Only `shutdown` is
    replaced, and it is deliberately not chained: the real one tears down libmpv,
    which is not what this test is about.
    """

    instances = []

    def __init__(self, player):
        super().__init__(player)
        self.toggles = 0
        self.seeks = []
        self.shutdowns = 0
        RecordingBridge.instances.append(self)

    def toggle_play(self):
        self.toggles += 1
        self.player.pause = not self.player.pause
        self.pauseChanged.emit(self.player.pause)

    def seek_exact(self, seconds):
        self.seeks.append(seconds)

    def shutdown(self):
        self.shutdowns += 1


class FakeThread(QThread):
    """A QThread that never starts on its own, so the probe runs synchronously.

    A real QThread, because the shipped code hands one to `moveToThread`; with
    `auto_run=False` the probe is parked, so a test can inspect the queue while a
    measurement is nominally in flight.
    """

    def __init__(self, parent=None):
        super().__init__()
        self.deleted = False
        self.auto_run = True

    def start(self):
        if self.auto_run:
            self.started.emit()
            self.finished.emit()

    def deleteLater(self):
        self.deleted = True


class StubProbeWorker(queue_module.ProbeWorker):
    """The real worker, with the one thing a fake thread cannot give it.

    `moveToThread` onto a QThread with no running event loop leaves the worker
    bound to it, so every signal it emits is *queued* to a loop that never runs
    and `run()` never executes at all. `tests/test_mesh_window.py` and
    `tests/editor_stub.py` hit the same thing and solve it the same way: keep
    the real worker and its real signals, and stub only the affinity.
    """

    def moveToThread(self, thread):
        pass


def teaching_dialog(teach):
    """A stand-in `RulesDialog` that returns instead of blocking, and teaches on
    the way out.

    The real one is a modal `exec()`, which no test can get past, so this
    replaces the whole interaction rather than the parts of it worth asserting
    on: what the queue does *after* the dialog closes, which is where the tag
    values come from. `teach` is handed the session, so it can assign a folder
    name as well as learn a rule -- a folder is already in the table, so
    `learn_rule` refuses it.
    """

    class TeachingDialog:
        def __init__(self, session, filename="", parent=None):
            self.session = session
            self.filename = filename

        def exec(self):
            teach(self.session)
            return 0

        def deleteLater(self):
            pass

    return TeachingDialog


class Recorder:
    """A `QMessageBox` that records instead of blocking."""

    Yes = 1
    No = 2
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
        return Recorder.No


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def put_clips(root, *relative_paths):
    """Real files on disk, so `find_videos` and the sidecar paths are real."""
    for relative in relative_paths:
        path = os.path.join(str(root), *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * 16)


def write_sidecar(root, relative, tags, source=None, duration=30.0):
    """Write a record beside a clip, creating the folder the way the queue would
    find it."""
    directory = os.path.join(str(root), *relative.split("/")[:-1])
    os.makedirs(directory, exist_ok=True)
    write_record(os.path.join(directory, relative.split("/")[-1][:-4]
                               + ".cnfo"),
                 ClipRecord(source=source or relative.split("/")[-1],
                            segment_index=0, start=0.0, duration=duration,
                            tags=tuple(tags.items())))


@pytest.fixture
def harness(qapp, monkeypatch, tmp_path):
    """Installs the fakes and returns a factory for queue windows."""
    import_root = tmp_path / "import"
    library_root = tmp_path / "library"
    os.makedirs(str(import_root), exist_ok=True)
    os.makedirs(str(library_root), exist_ok=True)

    RecordingBridge.instances = []
    Recorder.seen = []
    Recorder.answer = Recorder.No

    monkeypatch.setattr(queue_module, "QThread", FakeThread)
    monkeypatch.setattr(queue_module, "ProbeWorker", StubProbeWorker)
    monkeypatch.setattr(queue_module, "create_mpv_player",
                        lambda _container: FakePlayer())
    monkeypatch.setattr(queue_module, "MpvBridge", RecordingBridge)
    monkeypatch.setattr(queue_module, "QMessageBox", Recorder)
    monkeypatch.setattr(queue_module, "export_folder",
                        lambda: str(library_root))
    monkeypatch.setattr(queue_module, "vocabulary_path",
                        lambda: str(tmp_path / "vocabulary.json"))
    monkeypatch.setattr(queue_module, "import_folder",
                        lambda: str(import_root))
    monkeypatch.setattr(queue_module, "probe_duration", lambda _path: 30.0)

    def open_queue(*clips, session=None, duration=30.0):
        """A queue over the given clips, and the mesh session behind it."""
        if clips:
            put_clips(import_root, *clips)
        built = session or MeshSession(
            str(import_root), [FakeVideo(path) for path in clips],
            library=None,
            vocabulary=get_vocabulary(str(tmp_path / "vocabulary.json")))
        monkeypatch.setattr(queue_module, "probe_duration",
                            lambda _path: duration)
        window = queue_module.QueueWindow(session=built,
                                          root=str(import_root))
        window.import_root = str(import_root)
        window.library_root = str(library_root)
        return window

    yield open_queue, str(import_root), str(library_root)

    qapp.processEvents()


# ---------------------------------------------------------------------------
# Which clips get visited
# ---------------------------------------------------------------------------

def test_a_clip_already_settled_is_not_offered_again(harness):
    """Resume is the reason records are written as you go."""
    open_queue, root, _ = harness
    put_clips(root, "CN/Done.mp4", "CN/Not Done.mp4")
    write_sidecar(root, "CN/Done.mp4", REQUIRED)

    window = open_queue("CN/Done.mp4", "CN/Not Done.mp4")

    try:
        assert [clip.name for clip in window.clips] == ["Not Done.mp4"]
        assert window.done == 1
    finally:
        window.close()
        window.deleteLater()


def test_a_record_without_every_required_tag_is_not_settled(harness):
    """The check is the required-tag rule, not record existence.

    A clip the user tagged and then abandoned has a record with three of four
    tags. Treating that as finished would strand the clip forever, and it is the
    single most likely way a resume gets this wrong.
    """
    open_queue, root, _ = harness
    put_clips(root, "CN/Partial.mp4")
    partial = {key: value for key, value in REQUIRED.items()
               if key != "time_period"}
    write_sidecar(root, "CN/Partial.mp4", partial)

    window = open_queue("CN/Partial.mp4")

    try:
        assert [clip.name for clip in window.clips] == ["Partial.mp4"]
    finally:
        window.close()
        window.deleteLater()


def test_a_record_that_cannot_be_read_leaves_the_clip_in_the_queue(harness):
    open_queue, root, _ = harness
    put_clips(root, "CN/Broken.mp4")
    with open(os.path.join(root, "CN", "Broken.cnfo"), "w",
              encoding="utf-8") as handle:
        handle.write("not xml")

    window = open_queue("CN/Broken.mp4")

    try:
        assert [clip.name for clip in window.clips] == ["Broken.mp4"]
    finally:
        window.close()
        window.deleteLater()


def test_clips_are_visited_in_full_path_order(harness):
    """Case-folded, so `alpha/B` sorts before `alpha/C` whatever the filesystem
    thinks, and two runs over the same folder visit them in the same order."""
    open_queue = harness[0]
    window = open_queue("Zebra/A.mp4", "alpha/B.mp4", "Alpha/C.mp4")

    try:
        assert [clip.relative_path for clip in window.clips] == [
            "alpha/B.mp4", "alpha/C.mp4", "Zebra/A.mp4"]
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# What the folder answers contribute
# ---------------------------------------------------------------------------

def test_the_folders_answers_are_on_the_first_clip(harness):
    """The whole point of the hand-off: the folder names are already settled, so
    a clip needs one tag from the person, not ten."""
    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Toonami/2000s/A.mp4")],
                          vocabulary=None)
    session.assign("Toonami", "block", "Toonami")
    session.assign("2000s", "time_period", "2000s")

    window = open_queue("Toonami/2000s/A.mp4", session=session)

    try:
        form = window.ui.tagForm.read_tags()
        assert form["block"] == "Toonami"
        assert form["time_period"] == "2000s"
        assert form["title"] == "", "which is the one thing nobody could know"
        assert window.ui.labelProgress.text() == "1 of 1 left"
    finally:
        window.close()
        window.deleteLater()


def test_a_learned_rule_fills_its_tag_from_the_file_name(harness):
    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Rips/Toonami - 30 Sec.mkv")],
                          vocabulary=None)
    session.learn_rule("Toonami", "block", "Toonami")
    session.learn_rule("30 Sec", "length", "30 Sec")

    window = open_queue("Rips/Toonami - 30 Sec.mkv", session=session)

    try:
        form = window.ui.tagForm.read_tags()
        assert form["block"] == "Toonami"
        assert form["length"] == "30 Sec"
    finally:
        window.close()
        window.deleteLater()


def test_a_rule_cannot_fill_the_title(harness):
    """The reason the title is still asked for: a rule is a standing instruction
    and a title is per clip."""
    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Rips/A.mp4")], vocabulary=None)
    session.learn_rule("Rips", "block", "Toonami")

    with pytest.raises(ValueError, match="standing"):
        session.learn_rule("A", "title", "Nope")

    window = open_queue("Rips/A.mp4", session=session)

    try:
        assert window.ui.tagForm.read_tags()["title"] == ""
    finally:
        window.close()
        window.deleteLater()


def test_the_rules_dialog_cannot_wipe_a_tag_the_user_typed(harness, monkeypatch):
    """Closing Manage Autofill Rules re-resolves the clip, which is how a rule
    taught on clip twenty reaches the one in front of you. That reload used to
    write the answers over the whole form, so a title typed by hand simply
    vanished when the dialog closed — work lost with nothing on screen saying so.

    A value the user put in is not an answer a rule may replace. The fields still
    empty are the ones a rule is *for*, and those still fill.
    """
    from shared.tag_form import set_field_text

    open_queue, root, _ = harness
    relative = "Rips/Toonami - 30 Sec.mkv"
    session = MeshSession(str(root), [FakeVideo(relative)], vocabulary=None)
    window = open_queue(relative, session=session)

    try:
        set_field_text(window.ui.tagForm.field("title"), "Worlds Finest")
        set_field_text(window.ui.tagForm.field("network"), "Cartoon Network")

        def teach(session):
            session.assign("Rips", "block", "Toonami")
            session.learn_rule("30 Sec", "length", "30 Sec")
            session.learn_rule("Toonami", "network", "Toonami USA")

        monkeypatch.setattr(queue_module, "RulesDialog", teaching_dialog(teach))

        window.on_manage_rules()

        tags = window.ui.tagForm.read_tags()
        assert tags["title"] == "Worlds Finest", (
            "a rule cannot set a title, so nothing the rules said could have "
            "competed with this one — and it is gone anyway")
        assert tags["network"] == "Cartoon Network", (
            "the user typed this after the clip loaded, so the reload must not "
            "answer for them")
        assert tags["block"] == "Toonami", "an empty field still takes the answer"
        assert tags["length"] == "30 Sec", "and so does the next one"
    finally:
        window.close()
        window.deleteLater()


def test_a_tag_claimed_twice_is_named_rather_than_picked(harness):
    """The refusal from the other side: the queue says which one it could not
    fill, and leaves the field for the user."""
    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Promo/Toonami.mkv")],
                          vocabulary=None)
    session.assign("Promo", "filler_type", "Promo")
    session.learn_rule("Toonami", "filler_type", "Bumper")

    window = open_queue("Promo/Toonami.mkv", session=session)

    try:
        assert window.ui.tagForm.read_tags()["filler_type"] == "", (
            "and the field is left for the user rather than filled with one")
        assert "could not be filled" in window.ui.labelConflicts.text()
        assert "filler_type" in window.ui.labelConflicts.text()
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The title
# ---------------------------------------------------------------------------

def test_add_title_is_only_available_when_something_is_selected(harness):
    open_queue = harness[0]
    window = open_queue("CN/Worlds Finest.mp4")

    try:
        assert window.ui.buttonAddTitle.isEnabled() is False

        window.ui.textFileName.selectAll()

        assert window.ui.buttonAddTitle.isEnabled() is True
    finally:
        window.close()
        window.deleteLater()


def test_add_title_puts_the_selection_in_the_title_field(harness):
    """Highlight-then-click needs no custom drag handling: a selection in a
    read-only text field survives a button click."""
    open_queue, root, _ = harness
    put_clips(root, "CN/Toonami - Worlds Finest.mkv")
    session = MeshSession(str(root),
                          [FakeVideo("CN/Toonami - Worlds Finest.mkv")],
                          vocabulary=None)
    window = open_queue("CN/Toonami - Worlds Finest.mkv", session=session)

    try:
        title = "Worlds Finest"
        start = "Toonami - Worlds Finest".index(title)
        cursor = window.ui.textFileName.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + len(title), QTextCursor.MoveMode.KeepAnchor)
        window.ui.textFileName.setTextCursor(cursor)

        window.on_add_title()

        assert window.ui.tagForm.read_tags()["title"] == title
    finally:
        window.close()
        window.deleteLater()


def test_the_title_field_stays_editable_so_a_name_with_no_title_can_be_typed(
        harness,
):
    """Add Title is a shortcut into the field, not a replacement for it:
    `clip01.mp4` has no title in its name and the user still has to type one."""
    open_queue = harness[0]
    window = open_queue("CN/clip01.mp4")

    try:
        from shared.tag_form import set_field_text
        set_field_text(window.ui.tagForm.field("title"), "Typed By Hand")

        assert window.ui.tagForm.read_tags()["title"] == "Typed By Hand"
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Settling a clip
# ---------------------------------------------------------------------------

def fill_form(window, **overrides):
    from shared.tag_form import set_field_text
    tags = {**REQUIRED, **overrides}
    for namespace, value in tags.items():
        set_field_text(window.ui.tagForm.field(namespace), value)
    window._refresh_tag_state()


def test_next_is_disabled_until_the_required_tags_are_there(harness):
    open_queue = harness[0]
    window = open_queue("CN/A.mp4")

    try:
        assert window.ui.buttonNext.isEnabled() is False

        fill_form(window, title="Something")

        assert window.ui.buttonNext.isEnabled() is True
    finally:
        window.close()
        window.deleteLater()


def test_the_outline_goes_away_as_a_required_field_is_filled(harness):
    """Typing into an outlined field has to clear its own outline.

    The outline was refreshed when the clip loaded and never again, so a field the
    user went and filled kept the red box it was given for being empty. It also
    came back on the next clip with fields the user had never looked at, which is
    what made it read as a marking rather than as a question.

    Driven through the real signal connection rather than by calling the handler,
    because the bug was in what the handler was connected to and not in the
    handler.
    """
    from shared.tag_form import REQUIRED_FIELD_BORDER, set_field_text

    open_queue = harness[0]
    window = open_queue("CN/A.mp4")

    try:
        title = window.ui.tagForm.field("title")
        assert REQUIRED_FIELD_BORDER in title.styleSheet(), (
            "the clip loaded with nothing tagged, so the title is outlined")

        set_field_text(title, "Some Title")

        assert title.styleSheet() == "", (
            "the outline has to state what the form needs now, not what it "
            "needed when the clip was shown")

        for namespace in ("network", "filler_type", "time_period"):
            set_field_text(window.ui.tagForm.field(namespace), "x")
        for namespace in ("network", "filler_type", "time_period"):
            assert window.ui.tagForm.field(namespace).styleSheet() == ""
        assert title.styleSheet() == ""
    finally:
        window.close()
        window.deleteLater()


def test_next_writes_a_record_the_catalog_reads_back(harness):
    """The untagged path converging on the tagged one: the record beside the
    video is all `build_catalog` needs to see this as a finished clip."""
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4")

    try:
        fill_form(window)
        window.on_next()

        record = load_record(os.path.join(root, "CN", "A.cnfo"))
        assert record.tag_dict == REQUIRED
        assert record.source == "A.mp4", (
            "an imported clip was never cut from a compilation, so the file's "
            "own name is the one honest value `<source>` can hold")
        assert (record.segment_index, record.start) == (0, 0.0)
        assert window._current_clip() is None
    finally:
        window.close()
        window.deleteLater()


def test_the_record_takes_the_duration_the_probe_measured(harness):
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4", duration=12.5)

    try:
        fill_form(window)
        window.on_next()

        assert load_record(os.path.join(root, "CN", "A.cnfo")).duration == 12.5
    finally:
        window.close()
        window.deleteLater()


def test_settling_a_clip_never_touches_the_vocabulary(harness):
    """The inverse of what this window used to do, and the reason it changed.

    Every value in this window comes from somebody else's library. Recording them
    wrote `CN` and `Toonami - Vol 3` into the user's tag history on the way past —
    including the clip's *title*, since the queue passed the whole tag dict where the
    editor filters to `SUGGESTED_TAG_FIELDS` first — and then `sync_vocabulary`
    deleted them again on the next open, because the prune only ever counts the
    library in `export/`. So the user's own dropdowns churned on every import of a
    library they had not imported yet.

    Values reach the file the one way they should: read back out of `export/` by
    `sync_vocabulary`, once the user has actually imported them. Until then they are
    somebody else's words and nothing here is a confirmation of anything.
    """
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4")

    try:
        from shared.vocabulary import Vocabulary

        # Compared against a *fresh* vocabulary rather than against "is this value
        # absent", because `filler_type: Promo` is a shipped default and is in every
        # one of them. What must not happen is a **user** value appearing.
        before = Vocabulary.defaults(window.ui.tagForm.vocabulary_path)

        fill_form(window)
        window.on_next()

        stored = Vocabulary.load(window.ui.tagForm.vocabulary_path)
        for namespace in before.namespaces():
            assert stored.values(namespace) == before.values(namespace), (
                f"{namespace} gained {sorted(set(stored.values(namespace)) - set(before.values(namespace)))}"
                f" from an imported clip. A value reaches the file by being read back"
                f" out of export/, not by passing through this window.")
    finally:
        window.close()
        window.deleteLater()


def test_a_tag_removed_by_the_value_mesh_is_not_resurrected_by_the_folder_mesh(
        harness):
    """The record is authoritative once it exists.

    The folder answers used to sit *underneath* it, so re-opening the queue with a
    live `MeshSession` put back a tag the Tagged Library Mesh had just taken out of
    every clip — the residue pass, undoing the pass that preceded it. A clip's tags
    live in its record (invariant 12); the folder answers only have something to add
    on a clip that has never been settled at all.
    """
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4")
    write_sidecar(root, "CN/A.mp4", {key: value for key, value in REQUIRED.items()
                                     if key != "filler_type"})
    # A live session that would supply `filler_type: Promo` from the folder name.
    session = MeshSession(str(root), [FakeVideo("CN/A.mp4")], vocabulary=None)
    session.assign("CN", "filler_type", "Promo")
    assert session.tags_for_clip("CN/A.mp4").tags["filler_type"] == "Promo"

    window = open_queue("CN/A.mp4", session=session)
    try:
        assert window.ui.tagForm.read_tags()["filler_type"] == "", (
            "the folder mesh would have offered the tag the value mesh removed")
        assert window.ui.tagForm.read_tags()["title"] == "Some Title", (
            "and everything the record does hold is still there")
    finally:
        window.close()
        window.deleteLater()


def test_a_clip_with_no_record_still_gets_the_folder_answers(harness):
    """The other half of the same rule: on a clip that has never been settled, the
    folder answers are all there is."""
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4")
    session = MeshSession(str(root), [FakeVideo("CN/A.mp4")],
                          vocabulary=None)
    session.assign("CN", "network", "Cartoon Network")

    window = open_queue("CN/A.mp4", session=session)
    try:
        assert window.ui.tagForm.read_tags()["network"] == "Cartoon Network"
    finally:
        window.close()
        window.deleteLater()


def test_the_report_offers_the_value_mesh_for_the_clips_it_had_to_settle(harness):
    """The fork. Settling a clip is what puts a foreign record in `import/`, so the
    report is where the two halves of untagged import are offered side by side."""
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4")

    window = open_queue("CN/A.mp4")
    try:
        fill_form(window)
        window.on_next()

        assert window.ui.buttonValues.isHidden() is False
        report = window.ui.textReport.toPlainText()
        assert "tag value(s) in those records" in report
        assert "turns their words into yours" in report
    finally:
        window.close()
        window.deleteLater()


def test_the_report_offers_nothing_when_no_clip_was_settled(harness):
    """Nothing was written, so there is no record, so there is nothing to review.
    Offering the window anyway would be a button that can only say "there is
    nothing to do"."""
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4", "CN/B.mp4")

    window = open_queue("CN/A.mp4")
    try:
        window._show_report()

        assert window.ui.buttonValues.isHidden() is True
        assert window.ui.buttonImport.isHidden() is False
    finally:
        window.close()
        window.deleteLater()


def test_the_report_counts_a_shared_value_once(harness):
    """`network: CN` on two hundred clips is **one** question. The number on the
    report is the number of questions, because that is what the user is about to be
    asked — and counting tag-clip pairs instead would quote a number that means
    nothing."""
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4", "CN/B.mp4", "CN/C.mp4")
    for name in ("A", "B", "C"):
        write_sidecar(root, f"CN/{name}.mp4", REQUIRED)

    window = open_queue("CN/A.mp4")
    try:
        window._show_report()

        report = window.ui.textReport.toPlainText()
        assert "3 tag value(s)" in report, (
            "three clips sharing the same three values is three questions, not nine")
        assert "9 tag value(s)" not in report
    finally:
        window.close()
        window.deleteLater()


def test_the_value_mesh_is_opened_on_the_same_folder(harness, monkeypatch):
    """No clip list travels with the hand-off — the folder is where the truth lives,
    and the mesh re-derives what it needs from it."""
    open_queue, root, _ = harness
    calls = []

    class FakeShell:
        def open_safely(self, name, **kwargs):
            calls.append((name, kwargs))

    monkeypatch.setattr(queue_module, "shell", lambda: FakeShell())
    put_clips(root, "CN/A.mp4")

    window = open_queue("CN/A.mp4")
    try:
        fill_form(window)
        window.on_next()
        window.on_review_values()

        assert calls == [("values", {"root": str(root)})]
    finally:
        window.close()
        window.deleteLater()


def test_the_report_offers_a_way_back_to_the_menu(harness):
    """The report is an **ending**, and it did not have one.

    It offered two ways *on* — to the value mesh, or back to unfinished clips — and
    a user who had just imported everything was left holding the window manager's X.
    Same button and the same wording as the editor's export summary, because it is
    the same decision: the work is finished, so go back rather than stay.
    """
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4")

    window = open_queue("CN/A.mp4")
    try:
        window._show_report()

        assert window.ui.buttonMenu.isHidden() is False
        assert window.ui.buttonMenu.text() == "Back to main menu"
    finally:
        window.close()
        window.deleteLater()


def test_going_back_to_the_menu_asks_nothing(harness):
    """Nothing is lost by leaving, so nothing is asked.

    The records written so far stay in `import/`, unfinished clips stay unfinished,
    and `is_already_done` picks the run back up where it stopped — which is why the
    button's tooltip says so rather than the press asking about it.
    """
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4")

    window = open_queue("CN/A.mp4")
    try:
        window.on_back_to_menu()
        assert Recorder.seen == []
    finally:
        window._thread = None
        window.close()
        window.deleteLater()


def test_the_report_hides_it_again_when_the_run_reopens(harness):
    """Otherwise going back to the clips leaves a menu button on a per-clip screen,
    where it would close the run the user just resumed."""
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4", "CN/B.mp4")
    window = open_queue("CN/A.mp4")
    try:
        fill_form(window)
        window.on_next()
        window.on_reopen_unfinished()

        assert window.ui.buttonMenu.isHidden() is True
        assert window.ui.buttonImport.isHidden() is True
    finally:
        window.close()
        window.deleteLater()


def test_reopening_a_partial_record_keeps_what_was_already_tagged(harness):
    open_queue, root, _ = harness
    partial = {key: value for key, value in REQUIRED.items()
               if key != "time_period"}
    write_sidecar(root, "CN/A.mp4", partial)
    session = MeshSession(str(root), [FakeVideo("CN/A.mp4")], vocabulary=None)

    window = open_queue("CN/A.mp4", session=session)

    try:
        form = window.ui.tagForm.read_tags()
        assert form["title"] == "Some Title", "the work already done survives"
        assert form["time_period"] == ""
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Skip - Delete
# ---------------------------------------------------------------------------

def test_skip_delete_removes_the_video_and_its_record(harness):
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4")
    Recorder.answer = Recorder.Yes

    try:
        window.on_skip_delete()

        assert not os.path.exists(os.path.join(root, "CN", "A.mp4"))
    finally:
        Recorder.answer = Recorder.No
        window.close()
        window.deleteLater()


def test_skip_delete_removes_a_record_left_by_an_abandoned_session(harness):
    """A partial record beside a deleted video is litter, and `build_catalog`
    ignores a record with no sibling video -- so the folder should not
    accumulate them."""
    open_queue, root, _ = harness
    put_clips(root, "CN/A.mp4")
    write_sidecar(root, "CN/A.mp4", {"title": "Only This"})
    session = MeshSession(str(root), [FakeVideo("CN/A.mp4")], vocabulary=None)
    window = open_queue("CN/A.mp4", session=session)
    Recorder.answer = Recorder.Yes

    try:
        window.on_skip_delete()

        assert not os.path.exists(os.path.join(root, "CN", "A.mp4"))
        assert not os.path.exists(os.path.join(root, "CN", "A.cnfo"))
    finally:
        Recorder.answer = Recorder.No
        window.close()
        window.deleteLater()


def test_declining_the_confirmation_deletes_nothing_and_does_not_advance(harness):
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4")
    Recorder.answer = Recorder.No

    try:
        window.on_skip_delete()

        assert os.path.exists(os.path.join(root, "CN", "A.mp4"))
        assert window._current_clip() is not None
        assert Recorder.seen, "the confirmation was asked at all"
    finally:
        window.close()
        window.deleteLater()


def test_the_confirmation_names_the_file_and_states_the_consequence(harness):
    """The one destructive action in the feature, one stray keystroke from an
    eight-hundred-clip session. The dialog says what happens rather than asking
    "are you sure" about a button whose label already says what it does."""
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4")

    try:
        window.on_skip_delete()

        title, message = Recorder.seen[-1]
        assert "Delete" in title
        assert "A.mp4" in message
        assert "not in your library" in message
        assert "cannot be undone" in message
    finally:
        window.close()
        window.deleteLater()


def test_skip_delete_is_not_the_default_button_and_sits_away_from_next(harness):
    """A stray Enter in a long session must not reach the one action that loses
    files."""
    open_queue = harness[0]
    window = open_queue("CN/A.mp4")

    try:
        assert window.ui.buttonNext.isDefault() is True
        assert window.ui.buttonSkipDelete.isDefault() is False
    finally:
        window.close()
        window.deleteLater()


def test_progress_counts_what_is_left_so_a_deletion_decrements_it(harness):
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4", "CN/B.mp4", "CN/C.mp4")
    Recorder.answer = Recorder.Yes

    try:
        assert "1 of 3 left" in window.ui.labelProgress.text()

        window.on_skip_delete()

        assert "2 of 3 left" in window.ui.labelProgress.text(), (
            "a denominator that never shrinks reads as a stalled queue")
    finally:
        Recorder.answer = Recorder.No
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Unprobeable clips
# ---------------------------------------------------------------------------

def test_an_unreadable_clip_is_left_alone_named_and_the_queue_carries_on(harness,
                                                                         qapp,
                                                                         monkeypatch):
    """Not the same as skipping on purpose: an unprobeable clip stays, comes back
    on the next run, and does not block the others."""
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4", "CN/B.mp4")
    monkeypatch.setattr(queue_module, "probe_duration", lambda _path: None)

    try:
        window._load_current()
        qapp.processEvents()

        assert window._current_clip().problem
        assert os.path.exists(os.path.join(root, "CN", "A.mp4")), (
            "a clip that will not measure is left in place, not deleted")
        assert window.unprobeable
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def test_the_report_distinguishes_deleted_from_unprobeable_from_left(harness,
                                                                     qapp,
                                                                     monkeypatch):
    open_queue, root, _ = harness
    window = open_queue("CN/A.mp4", "CN/B.mp4")
    Recorder.answer = Recorder.Yes

    try:
        window.on_skip_delete()          # A deleted
        window._load_current()
        monkeypatch.setattr(queue_module, "probe_duration", lambda _path: None)
        window._load_current()           # B unprobeable
        qapp.processEvents()

        body = window._report_text()
        assert "Deleted from the import folder (1)" in body
        assert "CN/A.mp4" in body
        assert "Left alone" in body and "CN/B.mp4" in body
        assert "could not be written" in body
    finally:
        Recorder.answer = Recorder.No
        window.close()
        window.deleteLater()


def test_the_report_says_the_tags_are_written_beside_the_videos(harness):
    open_queue = harness[0]
    window = open_queue("CN/A.mp4")

    try:
        fill_form(window)
        window.on_next()

        assert "written beside" in window._report_text()
    finally:
        window.close()
        window.deleteLater()


def test_a_finished_queue_offers_the_import_and_a_way_back(harness):
    open_queue = harness[0]
    window = open_queue("CN/A.mp4")

    try:
        fill_form(window)
        window.on_next()

        assert window.ui.textReport.isHidden() is False
        assert window.ui.buttonImport.isHidden() is False
        assert window.ui.buttonNext.isHidden() is True
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The player
# ---------------------------------------------------------------------------

def test_every_clip_gets_its_own_player_and_the_old_one_is_shut_down(harness):
    """The player is embedded into the video frame's native handle, so it has to
    be shut down before the frame it lives in goes away -- which is why this
    happens on every load rather than only at close."""
    open_queue = harness[0]
    window = open_queue("CN/A.mp4", "CN/B.mp4")

    try:
        first = RecordingBridge.instances[0]
        assert first.player.played, "the clip was loaded into the player"

        window.on_next()
        fill_form(window)
        window.on_next()

        assert first.shutdowns == 1, "the previous clip's player was released"
        assert len(RecordingBridge.instances) >= 2
    finally:
        window.close()
        window.deleteLater()


def test_closing_shuts_the_player_down(harness):
    """Invoked directly, because a window that was never shown does not get a
    close event from `close()` -- and the property under test is that closing
    releases mpv, not that Qt routes the event."""
    open_queue = harness[0]
    window = open_queue("CN/A.mp4")
    bridge = RecordingBridge.instances[-1]

    window.closeEvent(QCloseEvent())

    assert bridge.shutdowns == 1
    assert window._bridge is None


def test_a_finished_queue_offers_the_import_and_a_way_back(harness):
    open_queue = harness[0]
    window = open_queue("CN/A.mp4")

    try:
        fill_form(window)
        window.on_next()

        assert window.ui.textReport.isHidden() is False
        assert window.ui.buttonImport.isHidden() is False
        assert window.ui.buttonNext.isHidden() is True
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The player
# ---------------------------------------------------------------------------

def test_every_clip_gets_its_own_player_and_the_old_one_is_shut_down(harness):
    """The player is embedded into the video frame's native handle, so it has to
    be shut down before the frame it lives in goes away -- which is why this
    happens on every load rather than only at close."""
    open_queue = harness[0]
    window = open_queue("CN/A.mp4", "CN/B.mp4")

    try:
        first = RecordingBridge.instances[0]
        
        window.on_next()
        fill_form(window)
        window.on_next()

        assert first.shutdowns == 1, "the previous clip's player was released"
        assert len(RecordingBridge.instances) >= 2
    finally:
        window.close()
        window.deleteLater()


def test_the_queue_only_uses_bridge_members_that_exist(harness):
    """Every `self._bridge.x` the queue reaches for is a real one.

    This pins the bug this file's fake was invented around: the queue connected
    `pausedChanged` and called `loadfile`, and the real bridge has neither — it
    spells them `pauseChanged` and `load_file`. The hand-rolled fake agreed with
    the mistake, so every test passed and a person found it by running the app.

    `RecordingBridge` subclasses the real `MpvBridge`, so a call would now fail
    here too -- but only on a path some test actually takes. This is the belt to
    that braces: every member named anywhere in the module, checked.
    """
    tree = ast.parse(open(queue_module.__file__, encoding="utf-8").read())
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_bridge"
    }
    missing = sorted(name for name in used
                     if not hasattr(MpvBridge, name))

    assert missing == [], (
        f"the queue reaches for MpvBridge.{missing}, which does not exist")


def test_play_and_seek_reach_the_player(harness):
    open_queue = harness[0]
    window = open_queue("CN/A.mp4")

    try:
        bridge = RecordingBridge.instances[-1]
        window.on_play_pause()
        assert bridge.toggles == 1

        # The slider's range comes from the player's duration, which a fake does
        # not announce until a test asks it to.
        window.ui.sliderPosition.setRange(0, 60000)
        window.ui.sliderPosition.setValue(5000)
        window._on_seek()

        assert bridge.seeks == [5.0]
    finally:
        window.close()
        window.deleteLater()


def test_the_queue_never_uses_a_keyframe_scanner(harness):
    """`scan_keyframes` is an ffprobe pass over every frame. A queue has no
    segments to mark and its clips are thirty seconds long, so the module must
    not even reach for it.

    Checked against the parsed source rather than the text: the module's own
    docstring names the scanner to explain why it does not call it, and a search
    for the word would match that.
    """
    tree = ast.parse(open(queue_module.__file__, encoding="utf-8").read())
    used = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "scan_keyframes"
        or isinstance(node, ast.Attribute) and node.attr == "scan_keyframes"
        or isinstance(node, (ast.Import, ast.ImportFrom))
        and "scan_keyframes" in ast.dump(node)
    ]

    assert used == [], "the queue must not import or call scan_keyframes"


# ---------------------------------------------------------------------------
# The rules dialog
# ---------------------------------------------------------------------------

def test_the_rules_dialog_says_a_literal_already_means_something(harness, qapp):
    """One table, so Add cannot quietly add a second meaning for a literal the
    Wizard already has."""
    from importer.rules import RulesDialog

    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("30 Sec/A.mp4")],
                          vocabulary=None)
    session.assign("30 Sec", "length", "Short")
    dialog = RulesDialog(session, filename="A.mp4")

    try:
        dialog.editLiteral.setCurrentText("30 Sec")

        assert "Already in your table" in dialog.labelEvidence.text()
        assert "length: Short" in dialog.labelEvidence.text()

        dialog.on_add()

        assert "already in your table" in dialog.labelStatus.text()
        assert session.entry("30 Sec").value == "Short", "unchanged"
    finally:
        dialog.deleteLater()


def test_the_rules_dialog_adds_a_new_literal(harness, qapp):
    from importer.rules import RulesDialog

    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Rips/A.mp4")], vocabulary=None)
    dialog = RulesDialog(session, filename="A.mp4")

    try:
        dialog.editLiteral.setCurrentText("30 Sec")
        dialog.comboNamespace.setCurrentText("length")
        dialog.comboValue.setCurrentText("30 Sec")

        dialog.on_add()

        assert [entry.name for entry in session.rules()] == ["30 Sec"]
        assert "Added" in dialog.labelStatus.text()
    finally:
        dialog.deleteLater()


def test_the_rules_dialog_cannot_even_ask_for_a_title(harness, qapp):
    """A stronger form of the same guarantee than `learn_rule`'s refusal: the
    dialog's namespace list has no `title` in it, so a rule for the title cannot
    be written down here even by accident.

    A rule is a standing instruction and a title is per clip, which is exactly
    why the title is the one tag still asked for.
    """
    from importer.rules import RulesDialog

    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Rips/A.mp4")], vocabulary=None)
    dialog = RulesDialog(session, filename="A.mp4")

    try:
        offered = [dialog.comboNamespace.itemText(index)
                   for index in range(dialog.comboNamespace.count())]
        assert "title" not in offered
        assert "block" in offered
    finally:
        dialog.deleteLater()


def test_the_rules_dialog_refuses_something_the_table_rejects(harness, qapp):
    """The dialog reports the refusal rather than raising it into the event loop
    -- a modal with no handler is an abort."""
    from importer.rules import RulesDialog

    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Rips/A.mp4")], vocabulary=None)
    dialog = RulesDialog(session, filename="A.mp4")

    try:
        dialog.editLiteral.setCurrentText("  ")
        dialog.comboValue.setCurrentText("Something")

        dialog.on_add()

        assert dialog.labelStatus.text(), "the refusal is shown"
        assert session.rules() == ()
    finally:
        dialog.deleteLater()


def test_the_rules_dialog_suggests_a_namespace_from_the_library(harness, qapp):
    """The same evidence the folder mesh offers, so one table means one vocabulary."""
    from importer.rules import RulesDialog

    open_queue, root, _ = harness
    library = build_library(("block", "Toonami"), ("block", "Toonami"))
    session = MeshSession(str(root), [FakeVideo("Rips/A.mp4")],
                          library=library, vocabulary=None)
    dialog = RulesDialog(session, filename="A.mp4")

    try:
        dialog.editLiteral.setCurrentText("Toonami")

        assert dialog.comboNamespace.currentText() == "block"
        assert "Suggested from your library" in dialog.labelEvidence.text()
    finally:
        dialog.deleteLater()


def test_removing_a_rule_through_the_dialog(harness, qapp):
    from importer.rules import RulesDialog

    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Rips/A.mp4")], vocabulary=None)
    session.learn_rule("30 Sec", "length", "30 Sec")
    dialog = RulesDialog(session, filename="A.mp4")

    try:
        dialog.listRules.setCurrentRow(0)
        dialog.on_remove()

        assert session.rules() == ()
        assert "Removed" in dialog.labelStatus.text()
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# The probe thread, on a real QThread
# ---------------------------------------------------------------------------
#
# Everything above runs the probe through `FakeThread` and `StubProbeWorker`:
# `start()` emits `started` and `finished` by hand on the GUI thread,
# `moveToThread` is a no-op, and `deleteLater()` sets a flag. That is what makes
# these tests fast and deterministic, and it is also why a probe thread which
# *never terminates* sailed through all of them — a faked thread has no `exec()`
# loop left spinning and no C++ object left to destroy, so the two things that
# actually go wrong cannot happen to it. The tests below keep the real thread,
# the real worker and the real event loop.

def _running(thread):
    """Whether a QThread is still alive, tolerating a deleted C++ object."""
    if thread is None:
        return False
    try:
        return thread.isRunning()
    except RuntimeError:
        return False


def _pump_until(qapp, predicate, timeout_ms=10000):
    """Spin the real event loop until `predicate` holds. False if it
    never does."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        qapp.processEvents()
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _dispose(window, qapp):
    """Stop whatever the window still owns, then get rid of it.

    The forced stop is the point. A thread still spinning when the interpreter
    exits is destroyed by Qt, which is a `qFatal` and takes the whole test run
    down with it — so a regression here would be a crashed suite with no report
    instead of a failed test. Stopping it here makes the failure legible.
    """
    thread = getattr(window, "_thread", None)
    if _running(thread):
        thread.quit()
        thread.wait(10000)
    qapp.processEvents()
    window.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.fixture
def real_thread_queue(qapp, monkeypatch, tmp_path):
    """A queue over a real probe thread, with only mpv and ffprobe stubbed."""
    import_root = tmp_path / "import"
    library_root = tmp_path / "library"
    os.makedirs(str(import_root), exist_ok=True)
    os.makedirs(str(library_root), exist_ok=True)
    put_clips(import_root, "CN/A.mp4")

    RecordingBridge.instances = []
    Recorder.seen = []
    Recorder.answer = Recorder.No

    monkeypatch.setattr(queue_module, "create_mpv_player",
                        lambda _container: FakePlayer())
    monkeypatch.setattr(queue_module, "MpvBridge", RecordingBridge)
    monkeypatch.setattr(queue_module, "QMessageBox", Recorder)
    monkeypatch.setattr(queue_module, "export_folder",
                        lambda: str(library_root))
    monkeypatch.setattr(queue_module, "vocabulary_path",
                        lambda: str(tmp_path / "vocabulary.json"))
    monkeypatch.setattr(queue_module, "import_folder", lambda: str(import_root))
    monkeypatch.setattr(queue_module, "probe_duration", lambda _path: 30.0)

    session = MeshSession(
        str(import_root), [FakeVideo("CN/A.mp4")],
        vocabulary=get_vocabulary(str(tmp_path / "vocabulary.json")))
    window = queue_module.QueueWindow(session=session, root=str(import_root))

    # The question these tests ask is whether the thread *stops*. Letting the
    # teardown go on to delete it would answer it destructively: with the bug
    # present, deleting a running QThread aborts the process, so the regression
    # would be a test run that dies with `QThread: Destroyed while thread is
    # still running` and reports nothing. Stubbing this one method turns that
    # into the plain failed assertion it should have been.
    window._thread.deleteLater = lambda: None

    yield window, qapp
    _dispose(window, qapp)


def test_the_probe_thread_terminates_by_itself(real_thread_queue):
    """The crash, as a test: a worker thread that does not end.

    `thread.started.connect(worker.run)` runs the worker's slot inside the
    thread's `exec()` loop, and a slot returning does not leave that loop, so
    nothing but `thread.quit()` ends the thread. While it spins, three things
    break together: the teardown hung off `thread.finished` never runs, the
    `closeEvent` guard never comes off, and Qt destroys a running `QThread` on
    the way out — `QThread: Destroyed while thread is still running`, a
    `qFatal`, and an abort that no Python `except` can catch.
    """
    window, qapp = real_thread_queue
    thread = window._thread
    assert thread is not None, "a probe was started, so there is a thread"

    assert _pump_until(qapp, lambda: not _running(thread)), (
        "the probe thread never terminated: the worker's slot returned but "
        "nothing quit the QThread's event loop, so `thread.finished` never "
        "fired and the thread is still spinning")


def test_the_close_guard_comes_off_only_after_the_thread_stops(
        real_thread_queue):
    """The guard is `_thread`, and clearing it early is what lets a live thread
    be destroyed with its window. So teardown waits for `thread.finished`
    rather than the worker's own `finished`, which is emitted from inside
    the still-running thread."""
    window, qapp = real_thread_queue
    thread = window._thread

    assert _pump_until(qapp, lambda: window._thread is None), (
        "the window never let go of its probe thread, so its closeEvent "
        "refuses to close and the shell can never replace it")

    assert not _running(thread), (
        "the guard came off while the thread was still running, so closing the "
        "window now destroys a live QThread")
    assert window.ui.labelProgress.text(), "a clip got as far as being shown"


def build_library(*pairs):
    """A destination library holding one clip per `(namespace, value)`."""
    from shared.catalog import Catalog, CatalogClip

    clips = tuple(
        CatalogClip(
            path=f"/library/{namespace}/{index}.mp4",
            relative_path=f"{namespace}/{index}.mp4",
            record_path=f"/library/{namespace}/{index}.cnfo",
            tags=((namespace, value),),
            record=ClipRecord(source="theirs.mp4", segment_index=0, start=0.0,
                              duration=1.0, tags=((namespace, value),)),
        )
        for index, (namespace, value) in enumerate(pairs)
    )
    return Catalog(clips=clips)