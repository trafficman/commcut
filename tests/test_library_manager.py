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
from PySide6.QtCore import QObject, QThread, Qt, Signal, QPoint
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
from shared.retagging import ScanResult
from shared.tag_form import set_field_text
from shared.tree_view import leaf_display

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


class StubBulkRetagWorker(manager_module.BulkRetagWorker):
    """Like `StubMaintenanceAlignWorker`, for the bulk re-tag worker."""

    def moveToThread(self, thread):
        pass


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


class StubMaintenanceScanWorker(manager_module.MaintenanceScanWorker):
    """Like `StubCatalogWorker`, for the maintenance scan worker."""

    def moveToThread(self, thread):
        pass


class StubMaintenanceAlignWorker(manager_module.MaintenanceAlignWorker):
    """Like `StubCatalogWorker`, for the maintenance align worker."""

    def moveToThread(self, thread):
        pass


class StubMaintenanceDuplicateScanWorker(
        manager_module.MaintenanceDuplicateScanWorker):
    """Like `StubCatalogWorker`, for the duplicate ID scan worker."""

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

    def set_value(self, *_args):
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
    Yes = 4
    No = 8
    Ok = 16
    Apply = 32
    answer = Cancel
    seen = []

    @staticmethod
    def warning(_parent, title, message, *_buttons, default=0):
        Recorder.seen.append((title, message))
        return Recorder.answer

    @staticmethod
    def question(_parent, title, message, *_buttons, default=0):
        Recorder.seen.append((title, message))
        return Recorder.answer

    @staticmethod
    def information(_parent, title, message, *_buttons, default=0):
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


def plant_clip_at(root, relative, tags, clip_id=None):
    """Plant a clip at an arbitrary path, regardless of what its
    tags render to — the way a clip ends up after its scheme changes,
    or it is renamed by hand."""
    if clip_id is None:
        clip_id = next_clip_id()
    path = os.path.join(root, *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * 16)
    write_record(
        os.path.splitext(path)[0] + RECORD_EXTENSION,
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
    monkeypatch.setattr(manager_module, "MaintenanceScanWorker",
                        StubMaintenanceScanWorker)
    monkeypatch.setattr(manager_module, "MaintenanceAlignWorker",
                        StubMaintenanceAlignWorker)
    monkeypatch.setattr(manager_module, "MaintenanceDuplicateScanWorker",
                        StubMaintenanceDuplicateScanWorker)
    monkeypatch.setattr(manager_module, "BulkRetagWorker",
                        StubBulkRetagWorker)
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


def folder_for(window, text):
    """The folder row displaying `text`, at any depth."""
    tree = window.ui.treeLibrary

    def walk(item):
        if (item.text(0) == text
                and not bool(item.flags() & Qt.ItemIsSelectable)):
            return item
        for index in range(item.childCount()):
            child = item.child(index)
            found = walk(child)
            if found is not None:
                return found
        return None

    for index in range(tree.topLevelItemCount()):
        found = walk(tree.topLevelItem(index))
        if found is not None:
            return found
    raise AssertionError(f"no folder row for {text!r}")


def check_leaf(window, relative_path, checked=True):
    """Toggle a leaf's check, as a click on its indicator would."""
    leaf = leaf_for(window, relative_path)
    window._set_checked(leaf, checked)
    return leaf


def checked_relatives(window):
    return {leaf.data(0, Qt.UserRole + 1) for leaf in clip_leaves(window)
            if leaf.checkState(0) == Qt.Checked}


def field(window, name):
    from shared.tag_form import field_text
    return field_text(window.ui.tagForm.field(name))


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
    assert leaf_display(
        ["Cartoon Network", "Promo", "2000s",
         "Cartoon Network Promo 2000s - Alpha.mp4"]) == "Alpha.mp4"
    assert leaf_display(
        ["CN", "Promo", "2000s",
         "cn promo 2000s - alpha.mp4"]) == "alpha.mp4"
    assert leaf_display(
        ["CN", "Promo", "2000s",
         "Alpha - CN Promo.mp4"]) == "Alpha - CN Promo.mp4"
    assert leaf_display(
        ["CN", "Promo", "2000s", "Alpha.mp4"]) == "Alpha.mp4"
    assert leaf_display(["CN", "CN.mp4"]) == "CN.mp4"


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


def test_the_lock_toggles_are_hidden(harness):
    """The manager shows one clip at a time, and a lock carries a value to the
    *next* clip.

    The Library Mesh Tag Editor shows the same toggles, because a queue does
    have a next clip; here there is nothing to carry a value to, so the
    toggles are hidden rather than left to do nothing.
    """
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")

        assert window.ui.tagForm.lock_button("network").isHidden()
        assert window.ui.tagForm.lock_button("filler_type").isHidden()
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


# ---------------------------------------------------------------------------
# Maintenance: scan and align
# ---------------------------------------------------------------------------

def test_scan_reports_no_issues_for_an_aligned_library(harness):
    """A clip planted at the path its tags render to is not misaligned."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        Recorder.answer = Recorder.No
        window.on_scan_library()

        assert Recorder.seen[-1][0] == "Scan complete"
        assert "No issues found" in Recorder.seen[-1][1]
    finally:
        window.close()
        window.deleteLater()


def test_scan_detects_misaligned_clips(harness):
    """A clip at the wrong path is detected and alignment offered."""
    open_library, root = harness
    plant_clip_at(root, "CN/Promo/2000s/WrongName.mp4", tags_for("Alpha"))
    window = open_library()

    try:
        Recorder.answer = Recorder.No
        window.on_scan_library()

        assert Recorder.seen[-1][0] == "Scan complete"
        assert "1 clip(s) can be aligned" in Recorder.seen[-1][1]
        assert "Align them now?" in Recorder.seen[-1][1]

        assert os.path.isfile(
            video_path(root, "CN/Promo/2000s/WrongName.mp4"))
        assert not os.path.exists(
            video_path(root, "CN/Promo/2000s/Alpha.mp4"))
    finally:
        window.close()
        window.deleteLater()


def test_align_moves_misaligned_clips(harness):
    """Accepting the align prompt moves the clip to its rendered path
    and rebuilds the tree."""
    open_library, root = harness
    plant_clip_at(root, "CN/Promo/2000s/WrongName.mp4", tags_for("Alpha"))
    window = open_library()

    try:
        Recorder.answer = Recorder.Yes
        window.on_scan_library()

        assert os.path.isfile(
            video_path(root, "CN/Promo/2000s/Alpha.mp4"))
        assert not os.path.exists(
            video_path(root, "CN/Promo/2000s/WrongName.mp4"))
        assert os.path.isfile(
            record_path(root, "CN/Promo/2000s/Alpha.mp4"))
        assert not os.path.exists(
            record_path(root, "CN/Promo/2000s/WrongName.mp4"))

        leaf = leaf_for(window, "CN/Promo/2000s/Alpha.mp4")
        assert leaf is not None

        titles = [seen[0] for seen in Recorder.seen]
        assert "Scan complete" in titles
        assert "Align complete" in titles
        assert any("Aligned 1 clip" in seen[1] for seen in Recorder.seen)
    finally:
        window.close()
        window.deleteLater()


def test_scan_reports_clips_with_unrenderable_tags(harness):
    """A clip missing required tags is reported as an error, not a
    misalignment — it cannot be aligned until the tags are fixed by
    hand."""
    open_library, root = harness
    plant_clip_at(root, "CN/Promo/2000s/Broken.mp4", {"title": "Broken"})
    window = open_library()

    try:
        Recorder.answer = Recorder.No
        window.on_scan_library()

        assert Recorder.seen[-1][0] == "Scan complete"
        assert "could not be checked" in Recorder.seen[-1][1]
    finally:
        window.close()
        window.deleteLater()


def test_scan_refuses_while_walk_running(harness):
    """The scan button does nothing while the initial walk is still
    running, because the scan reads the catalog the walk populates."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), parked=True)

    try:
        assert window._thread is not None
        window.on_scan_library()
        assert window._scan_thread is None

        window._on_catalog_built(build_catalog(root))
        window._on_walk_stopped()
    finally:
        window.close()
        window.deleteLater()


def test_closeevent_refuses_over_a_scan_thread(harness):
    """Closing while a scan runs is deferred, the same as a walk."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        window._scan_thread = FakeThread()
        window._scan_cancel = threading.Event()
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert window._scan_cancel.is_set()
        assert window._close_after_scan

        window._scan_thread = None
        assert window.close()
        assert not window.isVisible()
    finally:
        window.close()
        window.deleteLater()


def test_closeevent_refuses_over_an_align_thread(harness):
    """Closing while an align runs is deferred, the same as a walk."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        window._align_thread = FakeThread()
        window._align_cancel = threading.Event()
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert window._align_cancel.is_set()
        assert window._close_after_align

        window._align_thread = None
        assert window.close()
        assert not window.isVisible()
    finally:
        window.close()
        window.deleteLater()


def test_closing_during_a_scan_closes_when_it_stops(harness):
    """A close requested during a scan is honoured once the scan
    thread has really stopped — the same deferred-close pattern the
    walk uses."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        FakeThread.auto_run = False
        try:
            window.on_scan_library()
            assert window._scan_thread is not None
            assert not window.isEnabled()

            event = QCloseEvent()
            window.closeEvent(event)
            assert not event.isAccepted()
            assert window._scan_cancel.is_set()
            assert window._close_after_scan

            window._on_scan_done(ScanResult(cancelled=True))
            window._on_scan_stopped()

            assert window._scan_thread is None
            assert not window.isVisible()
        finally:
            FakeThread.auto_run = True
    finally:
        window.close()
        window.deleteLater()


def test_duplicate_scan_reports_none_for_a_clean_library(harness):
    """A library where every clip has its own clip ID reports no duplicates."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        window.on_scan_duplicate_ids()

        assert Recorder.seen[-1][0] == "Duplicate Clip ID Scan Complete"
        assert "No duplicate clip IDs found" in Recorder.seen[-1][1]
    finally:
        window.close()
        window.deleteLater()


def test_duplicate_scan_reports_shared_clip_ids(harness):
    """Two clips planted with the same clip ID are reported as a group."""
    open_library, root = harness
    # Plant two clips with the same clip ID.
    plant_clip(root, tags_for("Alpha"),
               clip_id="11111111-1111-1111-1111-111111111111")
    plant_clip(root, tags_for("Beta"),
               clip_id="11111111-1111-1111-1111-111111111111")
    plant_clip(root, tags_for("Gamma"))  # its own ID
    window = open_library()

    try:
        window.on_scan_duplicate_ids()

        assert Recorder.seen[-1][0] == "Duplicate Clip ID Scan Complete"
        message = Recorder.seen[-1][1]
        assert "1 clip ID(s) are shared by 2 clip(s)" in message
        assert "11111111-1111-1111-1111-111111111111" in message
    finally:
        window.close()
        window.deleteLater()


def test_closeevent_refuses_over_a_duplicate_scan_thread(harness):
    """Closing while a duplicate ID scan runs is deferred, the same as a walk."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"))

    try:
        window._dup_scan_thread = FakeThread()
        window._dup_scan_cancel = threading.Event()
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert window._dup_scan_cancel.is_set()
        assert window._close_after_dup_scan

        window._dup_scan_thread = None
        assert window.close()
        assert not window.isVisible()
    finally:
        window.close()
        window.deleteLater()



# ---------------------------------------------------------------------------
# Bulk editing
# ---------------------------------------------------------------------------

def _enter_bulk(window):
    check_leaf(window, "CN/Promo/2000s/Alpha.mp4")
    check_leaf(window, "CN/Promo/2000s/Beta.mp4")


def _set_field(window, name, value):
    from shared.tag_form import set_field_text
    set_field_text(window.ui.tagForm.field(name), value)


def test_the_pane_starts_in_singular_mode(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        assert window._mode == "singular"
        assert window.ui.buttonSave.text() == "Save"
        assert not window.ui.tagForm.field("title").isReadOnly()
        assert not window.ui.videoContainer.isHidden()
    finally:
        window.close()
        window.deleteLater()


def test_checking_one_clip_switches_the_pane_to_bulk_mode(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        assert window._mode == "singular"
        # The user loads a clip singular first: the title carries over
        # into the blended form this opens.
        select(window, "CN/Promo/2000s/Alpha.mp4")
        assert field(window, "title") == "Alpha"

        check_leaf(window, "CN/Promo/2000s/Alpha.mp4")

        assert window._mode == "bulk"
        assert field(window, "title") == ""
        assert window.ui.labelClipName.text() == "1 clip selected"
        assert window.ui.buttonSave.text() == "Apply to 1 clip"
        assert window.ui.tagForm.field("title").isReadOnly()
        title = window.ui.tagForm.field("title")
        assert title.cursor().shape() == Qt.ForbiddenCursor
        assert "unique" in title.toolTip()
        assert window.ui.videoContainer.isHidden()
        assert window.ui.buttonPlay.isHidden()
        assert window.ui.sliderPosition.isHidden()
        assert window.ui.labelDestination.isHidden()
        # The singular player that was playing Alpha was shut down on
        # the way into bulk mode.
        assert len(RecordingBridge.instances) == 1
        assert RecordingBridge.instances[-1].shutdowns == 1

        check_leaf(window, "CN/Promo/2000s/Beta.mp4")

        assert window._mode == "bulk"
        assert window.ui.labelClipName.text() == "2 clips selected"
        assert window.ui.buttonSave.text() == "Apply to 2 clips"
    finally:
        window.close()
        window.deleteLater()


def test_unchecking_the_last_clip_returns_to_singular_mode(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        _enter_bulk(window)

        check_leaf(window, "CN/Promo/2000s/Beta.mp4", checked=False)

        assert window._mode == "bulk"
        assert window.ui.labelClipName.text() == "1 clip selected"

        check_leaf(window, "CN/Promo/2000s/Alpha.mp4", checked=False)

        assert window._mode == "singular"
        assert window.ui.buttonSave.text() == "Save"
        assert not window.ui.buttonSave.isEnabled()
        assert not window.ui.tagForm.field("title").isReadOnly()
        assert not window.ui.videoContainer.isHidden()
        # Nothing is loaded in singular mode again.
        assert window.ui.labelClipName.text() == ""
        assert len(RecordingBridge.instances) == 0
        title = window.ui.tagForm.field("title")
        assert title.cursor().shape() == Qt.IBeamCursor
        assert title.toolTip() == ""
    finally:
        window.close()
        window.deleteLater()


def test_exiting_bulk_mode_has_no_stale_selection(harness):
    """After unchecking the last clip, there must be no phantom current
    clip: nothing on the left should read as selected, and picking
    another clip must not prompt about unsaved edits that do not
    exist."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        check_leaf(window, "CN/Promo/2000s/Alpha.mp4")
        assert window._mode == "bulk"

        check_leaf(window, "CN/Promo/2000s/Alpha.mp4", checked=False)

        tree = window.ui.treeLibrary
        assert window._mode == "singular"
        assert tree.selectedItems() == []
        assert window._clip is None
        assert window._current_leaf is None
        assert window.ui.labelClipName.text() == ""
        assert window.ui.tagForm.read_tags()["title"] == ""

        Recorder.seen.clear()
        select(window, "CN/Promo/2000s/Beta.mp4")

        assert window.ui.labelClipName.text() == \
            "CN/Promo/2000s/Beta.mp4"
        assert not any(
            title == "Unsaved changes" for title, _ in Recorder.seen)
        assert field(window, "title") == "Beta"
    finally:
        window.close()
        window.deleteLater()


def test_clip_row_click_toggles_the_check_in_bulk_mode(harness):
    open_library, root = harness
    window = open_library(
        tags_for("Alpha"), tags_for("Beta"), tags_for("Gamma"))

    try:
        _enter_bulk(window)
        leaf = leaf_for(window, "CN/Promo/2000s/Gamma.mp4")

        window._on_item_clicked(leaf, 0)
        assert checked_relatives(window) == {
            "CN/Promo/2000s/Alpha.mp4",
            "CN/Promo/2000s/Beta.mp4",
            "CN/Promo/2000s/Gamma.mp4",
        }

        window._on_item_clicked(leaf, 0)
        assert checked_relatives(window) == {
            "CN/Promo/2000s/Alpha.mp4",
            "CN/Promo/2000s/Beta.mp4",
        }
    finally:
        window.close()
        window.deleteLater()


def test_folder_row_click_expands_and_indicator_click_does_not(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        folder = folder_for(window, "CN")
        tree = window.ui.treeLibrary
        rect = tree.visualRect(tree.indexFromItem(folder))
        y = rect.center().y()

        # Pick a viewport position that lands on the indicator and one
        # that lands on the row body, through the same geometry the
        # handler uses — whichever DPI/style the test runs under.
        x_inside = next(x for x in range(rect.left(), rect.right() + 1)
                        if window._on_indicator(folder, QPoint(x, y)))
        x_inside_body = next(x for x in range(rect.right(), rect.left() - 1, -1)
                             if not window._on_indicator(folder, QPoint(x, y)))

        window._on_item_clicked(folder, 0, pos=QPoint(x_inside, y))
        assert not folder.isExpanded()

        window._on_item_clicked(folder, 0, pos=QPoint(x_inside_body, y))
        assert folder.isExpanded()

        # Indicators of nested folders also discriminate. The nested
        # row must be visible for Qt to give it a visual rect.
        for parent in ("CN", "Promo"):
            folder_for(window, parent).setExpanded(True)
        nested = folder_for(window, "2000s")
        rect = tree.visualRect(tree.indexFromItem(nested))
        y = rect.center().y()
        x_inside = next(x for x in range(rect.left(), rect.right() + 1)
                        if window._on_indicator(nested, QPoint(x, y)))
        window._on_item_clicked(nested, 0, pos=QPoint(x_inside, y))
        assert not nested.isExpanded()
    finally:
        window.close()
        window.deleteLater()


def test_folder_check_cascades_to_descendants(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        folder = folder_for(window, "CN")
        window._set_checked(folder, True)

        assert checked_relatives(window) == {
            "CN/Promo/2000s/Alpha.mp4",
            "CN/Promo/2000s/Beta.mp4",
        }
        assert folder.checkState(0) == Qt.Checked
        # Videos checked: the pane entered bulk mode.
        assert window._mode == "bulk"

        check_leaf(window, "CN/Promo/2000s/Alpha.mp4", checked=False)

        assert checked_relatives(window) == {"CN/Promo/2000s/Beta.mp4"}
        assert folder.checkState(0) == Qt.PartiallyChecked
        assert window._mode == "bulk"

        check_leaf(window, "CN/Promo/2000s/Beta.mp4", checked=False)

        assert checked_relatives(window) == set()
        assert folder.checkState(0) == Qt.Unchecked
        # Unchecking the last clip leaves bulk mode.
        assert window._mode == "singular"
    finally:
        window.close()
        window.deleteLater()


def test_the_blended_form_renders_uniform_and_mixed_fields(harness):
    open_library, root = harness
    window = open_library(
        tags_for("Alpha", network="CN"),
        {**tags_for("Beta", network="CN"), "special": "Bump"})

    try:
        _enter_bulk(window)

        # Network is uniform: an ordinary field with the common value.
        assert field(window, "network") == "CN"
        special = window.ui.tagForm.field("special")
        assert field(window, "special") == ""
        assert special.lineEdit().placeholderText() == "(multiple values)"
        assert "Bump" in special.toolTip()
    finally:
        window.close()
        window.deleteLater()


def test_an_untouched_mixed_field_keeps_each_clips_values(harness):
    open_library, root = harness
    window = open_library(
        tags_for("Alpha", network="CN"),
        {**tags_for("Beta", network="CN"), "special": "Bump"})

    try:
        _enter_bulk(window)
        _set_field(window, "network", "Old")
        Recorder.answer = Recorder.Apply

        assert window.on_save()

        records = {}
        for title in ("Alpha", "Beta"):
            with open(os.path.join(
                    root, "Old", "Promo", "2000s",
                    f"{title}.cnfo"),
                    encoding="utf-8") as handle:
                records[title] = handle.read()
        assert '<tag key="special">Bump</tag>' in records["Beta"]
        assert "special" not in records["Alpha"]
        assert '<tag key="network">Old</tag>' in records["Alpha"]
        assert '<tag key="network">Old</tag>' in records["Beta"]
    finally:
        window.close()
        window.deleteLater()


def test_a_mixed_field_cleared_by_the_user_removes_the_tag_everywhere(harness):
    open_library, root = harness
    window = open_library(
        {**tags_for("Alpha", network="CN"), "special": "Bump"},
        {**tags_for("Beta", network="CN"), "special": "Promo"})

    try:
        _enter_bulk(window)
        _set_field(window, "special", "x")
        _set_field(window, "special", "")
        Recorder.answer = Recorder.Apply

        assert window.on_save()

        for title in ("Alpha", "Beta"):
            with open(os.path.join(
                    root, "CN", "Promo", "2000s", f"{title}.cnfo"),
                    encoding="utf-8") as handle:
                assert "special" not in handle.read()
    finally:
        window.close()
        window.deleteLater()


def test_the_preflight_cancel_leaves_every_file_alone(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        _enter_bulk(window)
        _set_field(window, "network", "Old")
        Recorder.answer = Recorder.Cancel

        assert window.on_save() is False

        assert os.path.isfile(
            video_path(root, "CN/Promo/2000s/Alpha.mp4"))
        assert '<tag key="network">CN</tag>' in open(
            record_path(root, "CN/Promo/2000s/Alpha.mp4"),
            encoding="utf-8").read()
        # Still in bulk mode with the edit pending.
        assert window._mode == "bulk"
        assert checked_relatives(window) == {
            "CN/Promo/2000s/Alpha.mp4",
            "CN/Promo/2000s/Beta.mp4",
        }
    finally:
        window.close()
        window.deleteLater()


def test_the_preflight_lists_counts_and_intended_tags(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        _enter_bulk(window)
        _set_field(window, "network", "Old")
        Recorder.answer = Recorder.Cancel

        window.on_save()

        assert Recorder.seen[-1][1].count("Network → Old") == 1
        assert "2 clips will be re-tagged" in Recorder.seen[-1][1]
        assert "2 moves" in Recorder.seen[-1][1]
    finally:
        window.close()
        window.deleteLater()


def test_an_off_catalog_file_at_a_destination_is_refused_not_clobbered(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        destination = os.path.join(root, "Old", "Promo", "2000s")
        os.makedirs(destination, exist_ok=True)
        stray = os.path.join(destination, "Alpha.mp4")
        with open(stray, "wb") as handle:
            handle.write(b"stray bytes")

        _enter_bulk(window)
        _set_field(window, "network", "Old")
        Recorder.answer = Recorder.Apply

        assert window.on_save()

        with open(stray, "rb") as handle:
            assert handle.read() == b"stray bytes"
        # The after-action summary names the refused clip.
        summary = Recorder.seen[-1]
        assert summary[0] == "Bulk re-tag complete"
        assert "Alpha" in summary[1]
        # Beta moved.
        assert os.path.isfile(os.path.join(destination, "Beta.mp4"))
        # The selection is dumped after a save.
        assert checked_relatives(window) == set()
        assert window._mode == "singular"
    finally:
        window.close()
        window.deleteLater()


def test_the_batch_is_refused_whole_when_a_vital_tag_is_cleared(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        _enter_bulk(window)
        _set_field(window, "network", "")
        Recorder.answer = Recorder.Apply

        assert window.on_save() is False

        assert Recorder.seen[-1][0] == "The batch cannot be saved"
        with open(record_path(root, "CN/Promo/2000s/Alpha.mp4"),
                  encoding="utf-8") as handle:
            assert '<tag key="network">CN</tag>' in handle.read()
    finally:
        window.close()
        window.deleteLater()


def test_entering_bulk_mode_with_a_dirty_form_can_be_cancelled(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        select(window, "CN/Promo/2000s/Alpha.mp4")
        _set_field(window, "special", "Bump")
        Recorder.answer = Recorder.Cancel

        # The very first check enters bulk mode, and asks about
        # the pending edit before going there.
        check_leaf(window, "CN/Promo/2000s/Alpha.mp4")

        assert checked_relatives(window) == set()
        assert window._mode == "singular"
        assert window.ui.labelClipName.text() == \
            "CN/Promo/2000s/Alpha.mp4"
    finally:
        window.close()
        window.deleteLater()


def test_leaving_bulk_with_edits_can_be_discarded(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        _enter_bulk(window)
        _set_field(window, "special", "Bump")
        Recorder.answer = Recorder.Discard

        check_leaf(window, "CN/Promo/2000s/Beta.mp4", checked=False)

        assert window._mode == "bulk"
        assert checked_relatives(window) == {"CN/Promo/2000s/Alpha.mp4"}

        check_leaf(window, "CN/Promo/2000s/Alpha.mp4", checked=False)

        assert window._mode == "singular"
        assert window.ui.labelClipName.text() == ""
        assert field(window, "special") == ""
        assert not window.ui.buttonSave.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_leaving_bulk_with_edits_can_stay_put(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        _enter_bulk(window)
        _set_field(window, "special", "Bump")
        Recorder.answer = Recorder.Cancel

        check_leaf(window, "CN/Promo/2000s/Beta.mp4", checked=False)

        # Unchecking to one video is still bulk mode.
        assert window._mode == "bulk"
        assert checked_relatives(window) == {"CN/Promo/2000s/Alpha.mp4"}

        # The last uncheck prompts; Cancel restores it.
        check_leaf(window, "CN/Promo/2000s/Alpha.mp4", checked=False)

        assert window._mode == "bulk"
        assert checked_relatives(window) == {"CN/Promo/2000s/Alpha.mp4"}
    finally:
        window.close()
        window.deleteLater()


def test_leaving_bulk_with_edits_can_save_first(harness):
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    try:
        _enter_bulk(window)
        _set_field(window, "network", "Old")

        # The leaving prompt answers Save; the pre-flight answers Apply;
        # the after-action summary's warning is answered silently.
        def warning(*_args, **_kwargs):
            return Recorder.Save

        def question(*_args, **_kwargs):
            return Recorder.Apply

        original_question = Recorder.question
        original_warning = Recorder.warning
        Recorder.warning = warning
        Recorder.question = question
        try:
            # Unchecking one clip leaves bulk mode alone; the last
            # uncheck prompts, and Save applies the edits to what
            # was still checked — Beta was explicitly unselected.
            check_leaf(window, "CN/Promo/2000s/Beta.mp4", checked=False)
            assert window._mode == "bulk"
            check_leaf(window, "CN/Promo/2000s/Alpha.mp4", checked=False)
        finally:
            Recorder.warning = original_warning
            Recorder.question = original_question

        # The save ran on the remaining checked clip; the selection
        # was dumped afterwards.
        assert checked_relatives(window) == set()
        assert window._mode == "singular"
        with open(record_path(root, "Old/Promo/2000s/Alpha.mp4"),
                  encoding="utf-8") as handle:
            assert '<tag key="network">Old</tag>' in handle.read()
        with open(record_path(root, "CN/Promo/2000s/Beta.mp4"),
                  encoding="utf-8") as handle:
            assert '<tag key="network">CN</tag>' in handle.read()
    finally:
        window.close()
        window.deleteLater()

from PySide6.QtTest import QTest


def _indicator_pos(tree, leaf):
    """The center of the row's checkbox indicator, via the same style
    geometry the event filter uses to decide what is a checkbox click."""
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
    index = tree.indexFromItem(leaf)
    rect = tree.visualRect(index)
    option = QStyleOptionViewItem()
    option.initFrom(tree)
    option.rect = rect
    option.features = QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
    indicator = tree.style().subElementRect(
        QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, tree)
    return indicator.center()


def _body_pos(tree, leaf):
    index = tree.indexFromItem(leaf)
    rect = tree.visualRect(index)
    return QPoint(rect.right() - 4, rect.center().y())


def _click(tree, pos):
    QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, pos)


def test_rapid_toggles_coalesce_into_one_drain(harness, monkeypatch):
    """A burst of indicator clicks must not re-run the bookkeeping per
    click; the drain coalesces them into one commit after the burst
    settles."""
    open_library, root = harness
    window = open_library(
        *(tags_for(f"Clip{i}") for i in range(100)))

    sync_calls = []

    original_sync = window._sync_mode

    def spy(before_ids):
        sync_calls.append(before_ids)
        return original_sync(before_ids)

    monkeypatch.setattr(window, "_sync_mode", spy)

    folder = folder_for(window, "CN")

    try:
        for _ in range(10):
            window._toggle_item_check(folder)
        assert sync_calls == []

        window._drain_checks()
        assert len(sync_calls) == 1
        assert len(window._checked_ids) == 0
        assert window._mode == "singular"
    finally:
        window.close()
        window.deleteLater()


def test_checked_set_and_mode_stay_in_sync_with_the_tree(harness):
    """Every toggle that reaches the tree — our machinery or a raw
    style-native flip — must leave the checked set and the mode in
    agreement with the rows' real check states. A quick tap of the
    same folder on and off is the failure mode that used to diverge."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))

    def leaves_checked():
        return {leaf.data(0, Qt.UserRole + 1) for leaf in clip_leaves(window)
                if leaf.checkState(0) == Qt.Checked}

    def ids_checked():
        return {leaf.data(0, Qt.UserRole) for leaf in clip_leaves(window)
                if leaf.checkState(0) == Qt.Checked}

    try:
        folder = folder_for(window, "CN")

        window._toggle_item_check(folder)
        window._drain_checks()
        assert window._checked_ids == ids_checked()
        assert len(window._checked_ids) == 2
        assert window._mode == "bulk"

        window._toggle_item_check(folder)
        window._drain_checks()
        assert window._checked_ids == set()
        assert leaves_checked() == set()
        assert window._mode == "singular"

        # A raw flip the machinery did not drive — the exact-click
        # discrimination misfiring or queued native emits. The model
        # fallback catches the change, coalesced onto the drain.
        raw = leaf_for(window, "CN/Promo/2000s/Alpha.mp4")
        raw.setCheckState(0, Qt.Checked)
        window._drain_checks()

        assert window._checked_ids == ids_checked()
        assert len(window._checked_ids) == 1
        assert leaves_checked() == {"CN/Promo/2000s/Alpha.mp4"}
        assert window._mode == "bulk"

        raw.setCheckState(0, Qt.Unchecked)
        window._drain_checks()
        assert window._checked_ids == set()
        assert leaves_checked() == set()
        assert window._mode == "singular"
    finally:
        window.close()
        window.deleteLater()


def test_real_clicks_on_indicators_toggle_exactly_once(harness):
    """A click on a leaf's checkbox fires Qt's native toggle and the
    row-click handler once each; the handler must not also fire, or
    the leaf flips back off behind the user's back."""
    open_library, root = harness
    window = open_library(tags_for("Alpha"), tags_for("Beta"))
    window.show()
    tree = window.ui.treeLibrary
    tree.expandAll()
    a = leaf_for(window, "CN/Promo/2000s/Alpha.mp4")
    b = leaf_for(window, "CN/Promo/2000s/Beta.mp4")

    select(window, "CN/Promo/2000s/Alpha.mp4")
    assert window._mode == "singular"

    _click(tree, _indicator_pos(tree, a))
    window._drain_checks()
    assert a.checkState(0) == Qt.Checked
    assert window._mode == "bulk"
    assert window.ui.labelClipName.text() == "1 clip selected"

    _click(tree, _indicator_pos(tree, b))
    window._drain_checks()
    assert b.checkState(0) == Qt.Checked
    assert window._mode == "bulk"
    assert window.ui.labelClipName.text() == "2 clips selected"

    # A row-body click toggles in bulk mode without touching the
    # current-item machinery.
    _click(tree, _body_pos(tree, b))
    window._drain_checks()
    assert b.checkState(0) == Qt.Unchecked
    assert window._mode == "bulk"

    _click(tree, _body_pos(tree, a))
    window._drain_checks()
    assert a.checkState(0) == Qt.Unchecked
    assert window._mode == "singular"
    assert not window.ui.videoContainer.isHidden()



from PySide6.QtCore import QObject, QEvent, Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyleOptionViewItem,
)


CLIP_COUNT = 30


class Spy(QObject):
    def __init__(self, tree):
        super().__init__()
        self.events = []

    def eventFilter(self, obj, event):
        t = event.type()
        if t in (QEvent.Type.MouseButtonPress,
                 QEvent.Type.MouseButtonRelease,
                 QEvent.Type.MouseButtonDblClick):
            label = {
                QEvent.Type.MouseButtonPress: "Press",
                QEvent.Type.MouseButtonRelease: "Release",
                QEvent.Type.MouseButtonDblClick: "DblClick",
            }[t]
            pos = event.position()
            self.events.append((label, int(pos.x()), int(pos.y())))
        return False


