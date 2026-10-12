"""Tests for the Library Manager.

The window's whole job is to show the library the catalog finds
and save a clip's tags back to it, so the tests are about *what
the walk puts on screen* and *what saving does to the files*:

- the tree, which is derived state rebuilt from every walk
- selection, which loads the form and the preview
- the live destination label, which renders on every keystroke
- the save, which is the retagging transaction: the record
  published before the video moves, the clip re-selected by its
  ID, and the refusals that leave everything where it was

mpv is stubbed throughout — a preview is not what these are
about — and the walk runs on a thread that never really starts,
so a window is fully built the moment its constructor returns.
"""

import itertools
import os
import threading
import uuid

import pytest
from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent

import library.manager as manager_module
from editor_stub import ensure_qapp
from shared.catalog import Catalog, build_catalog
from shared.exporting import ExportSchemes, plan_clip_destination
from shared.mpv import MpvBridge
from shared.naming import compile_filename_scheme
from shared.paths import compile_folder_scheme
from shared.records import (
    RECORD_EXTENSION,
    ClipRecord,
    load_record,
    write_record,
)
from shared.tag_form import set_field_text

FOLDER_SCHEME_TEXT = "{network}/{filler_type}/{time_period}"
FILE_SCHEME_TEXT = "{title}"

FOLDER_SCHEME = compile_folder_scheme(FOLDER_SCHEME_TEXT)
FILE_SCHEME = compile_filename_scheme(FILE_SCHEME_TEXT)

#: A fixed ID for the one clip planted by hand — the orphaned
#: record — so it can be told apart from the IDs `plant_clip`
#: hands out.
TEST_CLIP_ID = "12345678-1234-4123-8123-123456789abc"

#: Every planted clip gets its own ID, the way the export
#: pipeline assigns one per clip: two clips that share an ID
#: are one clip as far as the catalog is concerned, and a
#: save onto a sibling's destination would not be refused.
_CLIP_IDS = itertools.count()


def next_clip_id():
    """A distinct clip ID, deterministic across a run."""
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"clip-{next(_CLIP_IDS)}"))


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


def tags_for(title, network="CN"):
    """The four tags the two schemes need, with one title."""
    return {
        "title": title,
        "network": network,
        "filler_type": "Promo",
        "time_period": "2000s",
    }


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePlayer:
    """Just enough mpv for the **real** `MpvBridge` to attach to.

    `MpvBridge.__init__` only calls `observe_property`, and
    `load_file` only plays and pauses, so this does not have to
    guess at the bridge's surface — which is the point. The
    command surface is here too: `toggle_play` reads
    `idle_active`, `eof_reached` and `pause` and seeks, and
    `seek_exact` seeks, so a call to a method the bridge does
    not have fails against this exactly as it would against mpv.
    """

    def __init__(self):
        self.observed = {}
        self.played = []
        self.seeks = []
        self.pause = True
        self.idle_active = False
        self.eof_reached = False

    def observe_property(self, name, callback):
        self.observed[name] = callback

    def play(self, path):
        self.played.append(path)

    def seek(self, seconds, reference="absolute", precision="exact"):
        self.seeks.append(seconds)


class RecordingBridge(MpvBridge):
    """The real bridge, recording what the window asked of it.

    Every signal and every command is inherited, so a call to
    something that does not exist fails here exactly as it would
    in the app. Only `shutdown` is replaced, and it is
    deliberately not chained: the real one tears down libmpv,
    which is not what this test is about.
    """

    instances = []

    def __init__(self, player):
        super().__init__(player)
        self.shutdowns = 0
        RecordingBridge.instances.append(self)

    def shutdown(self):
        self.shutdowns += 1


class FakeThread(QThread):
    """A QThread that never starts on its own, so the walk runs
    synchronously.

    A real QThread, because the shipped code hands one to
    `moveToThread`; with `auto_run=False` the walk is parked, so
    a test can inspect the window while a walk is nominally in
    flight.
    """

    auto_run = True

    def __init__(self, parent=None):
        super().__init__()
        self.deleted = False

    def start(self):
        if not self.auto_run:
            return
        self.started.emit()
        self.finished.emit()

    def deleteLater(self):
        self.deleted = True


class StubCatalogWorker(manager_module.CatalogWorker):
    """The real worker, with the one thing a fake thread cannot
    give it.

    `moveToThread` onto a QThread with no running event loop
    leaves the worker bound to it, so every signal it emits is
    *queued* to a loop that never runs and `run()` never executes
    at all. Keep the real worker and its real signals, and stub
    only the affinity.
    """

    def moveToThread(self, thread):
        pass


class FakeLoadingDialog(QObject):
    """A `LoadingDialog` whose `exec()` returns at once.

    A parked walk would otherwise park inside `exec()`: the
    rebuild's dialog is modal, and a walk that never finishes
    never closes it, so a test that drives a parked walk by
    hand needs the block to be a no-op. Everything else is the
    shape `_begin_walk` and its handlers touch.
    """

    canceled = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.visible = True

    def set_range(self, *_args):
        pass

    def set_message(self, *_args):
        pass

    def show(self):
        pass

    def close(self):
        self.visible = False

    def isVisible(self):
        return self.visible

    def exec(self):
        pass

    def deleteLater(self):
        pass


class Recorder:
    """A `QMessageBox` that records instead of blocking."""

    Save = 1
    Discard = 2
    Cancel = 3
    answer = Cancel
    seen = []

    @staticmethod
    def warning(_parent, title, message, *_buttons, default=0):
        Recorder.seen.append((title, message))
        return Recorder.answer


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def plant_clip(root, tags, clip_id=None):
    """Plant a clip at the path its tags render to, with its
    record beside it — the way the export pipeline left it.

    `clip_id=None` assigns a fresh ID, so every clip planted
    without one is a clip of its own.
    """
    if clip_id is None:
        clip_id = next_clip_id()
    destination = plan_clip_destination(
        tags=tags, folder_scheme=FOLDER_SCHEME,
        filename_scheme=FILE_SCHEME, label=tags["title"])
    path = os.path.join(root, *destination.relative_components)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * 16)
    write_record(
        os.path.join(root, *destination.record_relative_components),
        ClipRecord(source="compilation.mp4", segment_index=1,
                    start=10.0, duration=20.0,
                    tags=tuple(sorted(tags.items())),
                    clip_id=clip_id))
    return path


def write_v1_clip(root, relative, tags):
    """Plant a clip the way a v1 export left it: no clip ID.

    `write_record` refuses a record with no ID — every writer
    assigns one — so a v1 record is written out by hand, the
    way the old export wrote it.
    """
    path = os.path.join(root, *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"video bytes")
    with open(os.path.splitext(path)[0] + RECORD_EXTENSION,
              "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<commcut-clip>\n"
            "  <source>compilation.mp4</source>\n"
            '  <segment index="1" start="10.0" duration="20.0" />\n'
            + "".join(
                f'  <tag key="{key}">{value}</tag>\n'
                for key, value in sorted(tags.items())
            )
            + "</commcut-clip>\n"
        )
    return path


@pytest.fixture
def harness(qapp, monkeypatch, tmp_path):
    """Installs the fakes and returns a factory for library
    windows over a temp export folder."""
    library_root = tmp_path / "library"
    os.makedirs(str(library_root), exist_ok=True)

    RecordingBridge.instances = []
    Recorder.seen = []
    Recorder.answer = Recorder.Cancel
    FakeThread.auto_run = True

    monkeypatch.setattr(manager_module, "QThread", FakeThread)
    monkeypatch.setattr(manager_module, "CatalogWorker",
                        StubCatalogWorker)
    monkeypatch.setattr(manager_module, "create_mpv_player",
                        lambda _container: FakePlayer())
    monkeypatch.setattr(manager_module, "MpvBridge",
                        RecordingBridge)
    monkeypatch.setattr(manager_module, "QMessageBox", Recorder)
    monkeypatch.setattr(manager_module, "LoadingDialog",
                        FakeLoadingDialog)
    monkeypatch.setattr(manager_module, "export_folder",
                        lambda: str(library_root))
    monkeypatch.setattr(manager_module, "vocabulary_path",
                        lambda: str(tmp_path / "vocabulary.json"))
    monkeypatch.setattr(
        manager_module, "load_export_schemes",
        lambda _path: ExportSchemes(
            file_scheme=FILE_SCHEME_TEXT,
            folder_scheme=FOLDER_SCHEME_TEXT))

    def open_library(*clips, parked=False):
        """A Library Manager over the given clips.

        Each clip is its tags; it is planted at the path its
        tags render to. `parked=True` leaves the walk in flight,
        so a test can drive it by hand.
        """
        for tags in clips:
            plant_clip(str(library_root), tags)
        FakeThread.auto_run = not parked
        try:
            window = manager_module.LibraryManagerWindow()
        finally:
            FakeThread.auto_run = True
        return window

    yield open_library, str(library_root)

    qapp.processEvents()


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def tree_texts(item):
    """The tree under `item`, as a nested (text, children) pair."""
    return (item.text(0),
            [tree_texts(item.child(index))
             for index in range(item.childCount())])


def whole_tree(window):
    tree = window.ui.treeLibrary
    return [tree_texts(tree.topLevelItem(index))
            for index in range(tree.topLevelItemCount())]


def clip_leaves(window):
    """Every clip leaf in the tree, in order."""
    found = []

    def walk(item):
        for index in range(item.childCount()):
            child = item.child(index)
            if child.childCount() == 0:
                found.append(child)
            else:
                walk(child)

    tree = window.ui.treeLibrary
    for index in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(index))
    return found


def leaf_for(window, relative_path):
    """The leaf standing for the clip at `relative_path`."""
    for leaf in clip_leaves(window):
        if leaf.data(0, Qt.UserRole + 1) == relative_path:
            return leaf
    raise AssertionError(f"no leaf for {relative_path}")


def select(window, relative_path):
    """Select the clip at `relative_path`, as a click would."""
    window.ui.treeLibrary.setCurrentItem(leaf_for(window, relative_path))


def video_path(root, relative_path):
    return os.path.join(root, *relative_path.split("/"))


def record_path(root, relative_path):
    return os.path.splitext(video_path(root, relative_path))[0] + ".cnfo"


# ---------------------------------------------------------------------------
# The walk and the tree
# ---------------------------------------------------------------------------

def test_the_tree_is_built_from_the_walk(harness):
    """Folders and clips, casefolded, and only folders that hold
    clips — the tree is clip-driven."""
    open_library, root = harness

    window = open_library(
        tags_for("Alpha"), tags_for("Beta"),
        tags_for("Gamma", network="Old"))

    try:
        assert whole_tree(window) == [
            ("CN", [("Promo", [("2000s", [
                ("Alpha.mp4", []), ("Beta.mp4", [])])])]),
            ("Old", [("Promo", [("2000s", [
                ("Gamma.mp4", [])])])]),
        ]
    finally:
        window.close()
        window.deleteLater()


def test_the_leaf_display_strips_only_leading_folder_values():
    """The row strikes the folder values its name leads
    with, case-insensitively, and only while something is
    left: a value that appears mid-name is kept, and a
    name that is nothing but its folder values stays whole
    rather than showing an empty row."""
    assert manager_module._leaf_display(
        ["Cartoon Network", "Promo", "2000s",
         "Cartoon Network Promo 2000s - Alpha.mp4"]) == "Alpha.mp4"
    assert manager_module._leaf_display(
        ["CN", "Promo", "2000s",
         "cn promo 2000s - alpha.mp4"]) == "alpha.mp4"
    assert manager_module._leaf_display(
        ["CN", "Promo", "2000s",
         "Alpha - CN Promo.mp4"]) == "Alpha - CN Promo.mp4"
    assert manager_module._leaf_display(
        ["CN", "Promo", "2000s", "Alpha.mp4"]) == "Alpha.mp4"
    assert manager_module._leaf_display(["CN", "CN.mp4"]) == "CN.mp4"


def test_the_tree_strips_folder_values_the_name_repeats(harness):
    """A clip's filename is its tags projected, and the
    folder above the row is those tags again — so a name
    that leads with a folder value shows it twice. The row
    strikes the leading folder values, and still names the
    clip it stands for."""
    open_library, root = harness
    window = open_library(tags_for("CN Promo 2000s - Alpha"))

    try:
        leaf = leaf_for(
            window, "CN/Promo/2000s/CN Promo 2000s - Alpha.mp4")
        assert leaf.text(0) == "Alpha.mp4"
        assert leaf.data(0, Qt.UserRole + 1) == (
            "CN/Promo/2000s/CN Promo 2000s - Alpha.mp4")
    finally:
        window.close()
        window.deleteLater()


def test_the_problems_area_shows_what_the_walk_found(harness):
    """A record with no video beside it is reported, not dropped,
    and the area is visible only when there is something to say."""
    open_library, root = harness
    directory = os.path.join(root, "CN", "Promo", "2000s")
    os.makedirs(directory, exist_ok=True)
    write_record(
        os.path.join(directory, "Orphan.cnfo"),
        ClipRecord(source="compilation.mp4", segment_index=1,
                    start=10.0, duration=20.0,
                    tags=tuple(sorted(tags_for("Orphan").items())),
                    clip_id=TEST_CLIP_ID))

    window = open_library(tags_for("Alpha"))
    window.show()

    try:
        assert window.ui.textProblems.isVisible()
        assert "Orphan.cnfo" in window.ui.textProblems.toPlainText()
    finally:
        window.close()
        window.deleteLater()


def test_clicking_a_folder_row_expands_and_collapses_it(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        folder = window.ui.treeLibrary.topLevelItem(0)
        assert not folder.isExpanded()
        window._on_item_clicked(folder, 0)
        assert folder.isExpanded()
        window._on_item_clicked(folder, 0)
        assert not folder.isExpanded()
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_selecting_a_clip_loads_the_form_and_the_preview(harness):
    open_library, root = harness
    alpha = tags_for("Alpha")
    window = open_library(alpha)

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")

        assert {key: value for key, value
                in window.ui.tagForm.read_tags().items()
                if value} == alpha
        assert window.ui.labelClipName.text() == (
            "CN/Promo/2000s/Alpha.mp4")
        assert window.ui.buttonSave.isEnabled()
        assert RecordingBridge.instances[-1].player.played == [
            video_path(root, "CN/Promo/2000s/Alpha.mp4")]
    finally:
        window.close()
        window.deleteLater()


def test_the_destination_label_renders_live(harness):
    """The label re-renders on every field change: in place while
    the tags are unchanged, the new destination once they are not,
    and nothing at all when a required tag is missing."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        assert window.ui.labelDestination.text() == (
            "Saving updates this clip's record in place.")

        set_field_text(window.ui.tagForm.field("title"), "Alpha Two")
        assert window.ui.labelDestination.text() == (
            "Saving moves this clip to: "
            "CN/Promo/2000s/Alpha Two.mp4")

        set_field_text(window.ui.tagForm.field("title"), "")
        assert window.ui.labelDestination.text() == ""
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def test_saving_a_move_moves_the_clip_and_rewrites_the_record(
        harness):
    """The transaction's observable outcome: the record is at the
    new path with the new tags and the same ID, the video is with
    it, the old pair is gone, and the tree re-selected the clip
    by its ID — never by path, which just changed."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        clip_id = window.ui.treeLibrary.currentItem().data(
            0, Qt.UserRole)
        set_field_text(window.ui.tagForm.field("title"), "Alpha Two")

        assert window.on_save()

        assert not os.path.exists(
            video_path(root, "CN/Promo/2000s/Alpha.mp4"))
        assert not os.path.exists(
            record_path(root, "CN/Promo/2000s/Alpha.mp4"))
        record = load_record(
            record_path(root, "CN/Promo/2000s/Alpha Two.mp4"))
        assert record.tag_dict["title"] == "Alpha Two"
        assert record.clip_id == clip_id
        assert record.source == "compilation.mp4"

        assert window.ui.treeLibrary.currentItem().text(0) == (
            "Alpha Two.mp4")
        assert window.ui.labelStatus.text() == (
            "Saved. Moved CN/Promo/2000s/Alpha.mp4 → "
            "CN/Promo/2000s/Alpha Two.mp4")
    finally:
        window.close()
        window.deleteLater()


def test_the_save_status_outlives_the_walk_that_reselects(harness):
    """The status names the move after the walk the save triggers
    has re-selected the clip. A real thread is still walking when
    `on_save` returns, and that re-selection reloads the clip —
    which clears the status — so the report is deferred to after
    it. A parked walk lets the test drive that order by hand."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        set_field_text(window.ui.tagForm.field("title"), "Alpha Two")

        FakeThread.auto_run = False
        try:
            assert window.on_save()
            assert window._thread is not None
            assert window.ui.labelStatus.text() == ""

            window._on_catalog_built(build_catalog(root))
            window._on_walk_stopped()
        finally:
            FakeThread.auto_run = True

        assert window.ui.labelStatus.text() == (
            "Saved. Moved CN/Promo/2000s/Alpha.mp4 → "
            "CN/Promo/2000s/Alpha Two.mp4")
        assert window.ui.treeLibrary.currentItem().text(0) == (
            "Alpha Two.mp4")
    finally:
        window.close()
        window.deleteLater()


def test_saving_unchanged_tags_rewrites_the_record_in_place(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        clip_id = window.ui.treeLibrary.currentItem().data(
            0, Qt.UserRole)

        assert window.on_save()

        assert os.path.isfile(
            video_path(root, "CN/Promo/2000s/Alpha.mp4"))
        record = load_record(
            record_path(root, "CN/Promo/2000s/Alpha.mp4"))
        assert record.tag_dict["title"] == "Alpha"
        assert record.clip_id == clip_id
        assert window.ui.labelStatus.text() == (
            "Saved CN/Promo/2000s/Alpha.mp4.")
    finally:
        window.close()
        window.deleteLater()


def test_a_v1_clip_is_assigned_an_id_when_saved(harness):
    """A record written before clip IDs existed is readable, and
    saving it — even in place — gives it the ID the re-select
    needs."""
    open_library, root = harness
    write_v1_clip(root, "CN/Promo/2000s/Alpha.mp4",
                  tags_for("Alpha"))
    window = open_library()

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")

        assert window.on_save()

        record = load_record(
            record_path(root, "CN/Promo/2000s/Alpha.mp4"))
        assert record.clip_id != ""
        assert window.ui.treeLibrary.currentItem().text(0) == (
            "Alpha.mp4")
    finally:
        window.close()
        window.deleteLater()


def test_a_save_that_would_collide_is_refused_naming_the_clip(
        harness):
    """The occupied-destination rule: re-tagging one clip onto
    another's destination is refused by name, and nothing moves."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        set_field_text(window.ui.tagForm.field("title"), "Beta")

        assert not window.on_save()

        assert Recorder.seen and "Beta.mp4" in Recorder.seen[-1][1]
        assert os.path.isfile(
            video_path(root, "CN/Promo/2000s/Alpha.mp4"))
        assert os.path.isfile(
            video_path(root, "CN/Promo/2000s/Beta.mp4"))
    finally:
        window.close()
        window.deleteLater()


def test_a_save_with_a_missing_required_tag_is_refused(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        set_field_text(window.ui.tagForm.field("title"), "")

        assert not window.on_save()

        assert Recorder.seen and "title" in Recorder.seen[-1][1]
        assert os.path.isfile(
            video_path(root, "CN/Promo/2000s/Alpha.mp4"))
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# Unsaved edits
# ---------------------------------------------------------------------------

def test_the_dirty_prompt_asks_on_switch(harness):
    """Switching away from unsaved edits asks about them: Cancel
    abandons the switch, Discard drops the edits and loads the
    clip that was clicked."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        set_field_text(window.ui.tagForm.field("title"),
                       "Alpha Edited")

        Recorder.answer = Recorder.Cancel
        select(window, "CN/Promo/2000s/Beta.mp4")
        assert window.ui.tagForm.read_tags()["title"] == (
            "Alpha Edited")
        assert window.ui.treeLibrary.currentItem().text(0) == (
            "Alpha.mp4")

        Recorder.answer = Recorder.Discard
        select(window, "CN/Promo/2000s/Beta.mp4")
        assert window.ui.tagForm.read_tags()["title"] == "Beta"
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The walk's lifecycle
# ---------------------------------------------------------------------------

def test_cancelling_the_walk_closes_the_window(harness):
    """A cancelled walk has nothing to show, so the window closes —
    once the thread it owns has really stopped, which is what
    `closeEvent` waits for."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), parked=True)

    try:
        window.show()
        assert window._thread is not None
        assert not window.isEnabled()

        window._dialog.canceled.emit()
        assert window._cancel.is_set()

        window._on_catalog_built(Catalog(cancelled=True))
        assert window._close_after_walk

        window._on_walk_stopped()
        assert window._thread is None
        assert not window.isVisible()
    finally:
        window.close()
        window.deleteLater()


def test_closeevent_refuses_over_a_live_thread(harness):
    """A window that owns a running thread does not close: the
    close is refused and the walk is asked to stop, and the close
    goes through once the thread is gone."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        window._thread = FakeThread()
        window._cancel = threading.Event()
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert window._cancel.is_set()

        window._thread = None
        assert window.close()
        assert not window.isVisible()
    finally:
        window.close()
        window.deleteLater()


def test_closing_the_window_shuts_the_player_down(harness):
    """The bridge is shut down from `closeEvent`, while the video
    frame still has its native handle — the one ordering that
    keeps libmpv attached to a window that still exists."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        bridge = RecordingBridge.instances[-1]
        assert bridge.shutdowns == 0

        window.close()
        assert bridge.shutdowns == 1
    finally:
        window.deleteLater()


def test_the_play_button_toggles_the_clip_through_the_bridge(harness):
    """Play is the bridge's `toggle_play` — the one command the
    scanner and the editor both use — not a pause and a resume
    the bridge does not have."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        bridge = window._bridge
        assert bridge is not None
        assert bridge.paused  # load_file pauses at the start

        window.ui.buttonPlay.click()
        assert not bridge.paused

        window.ui.buttonPlay.click()
        assert bridge.paused
    finally:
        window.close()
        window.deleteLater()


def test_releasing_the_transport_seeks_the_bridge(harness):
    """The position slider seeks on release, through the bridge's
    `seek_exact` — the frame-exact seek the scanner's timeline
    uses."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        player = window._bridge.player

        window.ui.sliderPosition.setValue(7)
        window.ui.sliderPosition.sliderReleased.emit()
        assert player.seeks == [7]
    finally:
        window.close()
        window.deleteLater()
