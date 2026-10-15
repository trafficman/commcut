"""Tests for library sharing.

The feature is two halves, and the tests split the same way:

- `shared/sharing.py` holds the rules -- the filter's
  semantics, the plan's counts, the zip's naming and its
  no-clobber, cancel-safe write. None of it needs Qt, so it
  is tested directly, with clips built in memory for the
  rules and clips planted on disk for the zip.
- `sharing/sharing.py` is the window, and its job is to show
  the filter's result: the tree is derived state, rebuilt
  from a fresh walk filtered by the rows, so what the tree
  shows is always what the share would hold. That is tested
  through the real window over a temp export folder, with
  mpv stubbed and the walk and the zip parked on threads
  that never really start.

The filter's semantics are the thing to pin down: within one
namespace the rules are OR'd, across namespaces they are
AND'd, and an exclusion drops a clip the inclusions would
otherwise have kept. Matching happens in `value_dedup_key`
space, so a value typed into a row and a folder the library
rendered agree.
"""

import itertools
import os
import uuid
import zipfile

import pytest
from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QDialog

import sharing.sharing as sharing_module
from editor_stub import ensure_qapp
from shared.catalog import CatalogClip
from shared.exporting import plan_clip_destination
from shared.mpv import MpvBridge
from shared.naming import compile_filename_scheme
from shared.paths import compile_folder_scheme
from shared.records import (
    RECORD_EXTENSION,
    ClipRecord,
    write_record,
)
from shared.sharing import (
    filter_clips,
    share_plan,
    share_zip_name,
    write_share_zip,
    extract_share_zip,
    has_record_entries,
    ZipImportError,
)

FOLDER_SCHEME_TEXT = "{network}/{filler_type}/{time_period}"
FILE_SCHEME_TEXT = "{title}"

FOLDER_SCHEME = compile_folder_scheme(FOLDER_SCHEME_TEXT)
FILE_SCHEME = compile_filename_scheme(FILE_SCHEME_TEXT)

#: A fixed ID for one clip, so a test can tell it apart from
#: the IDs `next_clip_id` hands out.
TEST_CLIP_ID = "12345678-1234-4123-8123-123456789abc"

#: Every planted clip gets its own ID, the way the export
#: pipeline assigns one per clip.
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
# Clip builders
# ---------------------------------------------------------------------------

def clip(relative_path, tags, clip_id=None):
    """A `CatalogClip` with no files behind it.

    For the rules that never touch the disk -- the filter and
    the name -- a clip is its tags and its relative path, and
    building one in memory keeps those tests about the rules.
    """
    if clip_id is None:
        clip_id = next_clip_id()
    record = ClipRecord(
        source="compilation.mp4", segment_index=1,
        start=10.0, duration=20.0,
        tags=tuple(sorted(tags.items())), clip_id=clip_id)
    return CatalogClip(
        path=relative_path,
        relative_path=relative_path,
        record_path=os.path.splitext(relative_path)[0]
        + RECORD_EXTENSION,
        tags=tuple(sorted(tags.items())),
        record=record)


def plant_clip(root, tags, clip_id=None):
    """Plant a clip at the path its tags render to, with its
    record beside it -- the way the export pipeline left it."""
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


def planted(root, tags):
    """A `CatalogClip` whose video and record are on disk."""
    path = plant_clip(root, tags)
    relative = os.path.relpath(path, root).replace(os.sep, "/")
    return CatalogClip(
        path=path,
        relative_path=relative,
        record_path=os.path.splitext(path)[0] + RECORD_EXTENSION,
        tags=tuple(sorted(tags.items())),
        record=ClipRecord(
            source="compilation.mp4", segment_index=1,
            start=10.0, duration=20.0,
            tags=tuple(sorted(tags.items())),
            clip_id=next_clip_id()))


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------

def test_an_empty_include_is_every_clip():
    """No rows means the whole library, which is the default."""
    clips = (
        clip("A.mp4", tags_for("A")),
        clip("B.mp4", tags_for("B")),
    )

    assert filter_clips(clips) == clips
    assert filter_clips(clips, includes=[]) == clips


def test_values_in_one_namespace_are_ored():
    """Two values in one namespace: a clip matching either counts."""
    clips = (
        clip("CN.mp4", tags_for("A", network="CN")),
        clip("Old.mp4", tags_for("B", network="Old")),
    )

    found = filter_clips(
        clips,
        includes=[("network", "CN"), ("network", "Old")])

    assert found == clips


def test_namespaces_are_anded():
    """Two namespaces: a clip has to match both."""
    clips = (
        clip("both.mp4", tags_for("A", network="CN")),
        clip("one.mp4", {"title": "B", "network": "CN",
                         "filler_type": "Commercial",
                         "time_period": "2000s"}),
    )

    found = filter_clips(
        clips,
        includes=[("network", "CN"), ("filler_type", "Promo")])

    assert found == (clips[0],)


def test_a_clip_missing_a_namespace_fails_that_rule():
    """An include on a namespace the clip does not have is a miss."""
    clips = (
        clip("no-type.mp4", {"title": "A", "network": "CN",
                             "time_period": "2000s"}),
    )

    found = filter_clips(
        clips, includes=[("network", "CN"), ("filler_type", "Promo")])

    assert found == ()


def test_an_exclusion_drops_a_clip_the_inclusions_kept():
    """The exclusion is applied after the inclusion, so it removes
    a clip the inclusions would otherwise have kept."""
    clips = (
        clip("cn.mp4", tags_for("A", network="CN")),
        clip("old.mp4", tags_for("B", network="Old")),
    )

    found = filter_clips(
        clips,
        includes=[("network", "CN"), ("network", "Old")],
        excludes=[("network", "CN")])

    assert found == (clips[1],)


def test_matching_happens_in_dedup_key_space():
    """`cartoon network` and `Cartoon Network` are one value, the
    same identity rule the vocabulary and the destination planner
    use, so a value typed into a row and a folder the library
    rendered agree."""
    clips = (
        clip("cn.mp4", tags_for("A", network="Cartoon Network")),
    )

    for typed in ("cartoon network", "Cartoon Network",
                  "  cartoon   network  "):
        found = filter_clips(
            clips, includes=[("network", typed)])
        assert found == clips, typed


def test_a_namespace_is_matched_by_alias_and_case():
    """The namespace combo is editable, so `type`, `Type` and
    `network` all name the field the record holds canonically."""
    clips = (
        clip("cn.mp4", tags_for("A", network="CN")),
        clip("old.mp4", {"title": "B", "network": "Old",
                         "filler_type": "Commercial",
                         "time_period": "2000s"}),
    )

    for namespace in ("type", "Type", "TYPE", "filler_type"):
        found = filter_clips(clips, includes=[(namespace, "Promo")])
        assert found == (clips[0],), namespace

    for namespace in ("Network", "NETWORK", "network"):
        found = filter_clips(clips, includes=[(namespace, "CN")])
        assert found == (clips[0],), namespace


def test_empty_rules_match_nothing():
    """A row the user has not finished typing is not an error; it
    is a rule that matches nothing, so it is dropped rather than
    refusing the whole filter."""
    clips = (clip("A.mp4", tags_for("A")),)

    assert filter_clips(
        clips, includes=[("network", "")]) == clips
    assert filter_clips(
        clips, includes=[("", "CN")]) == clips
    assert filter_clips(
        clips, includes=[("network", "   ")]) == clips


def test_the_input_order_is_kept():
    """The tree is built from the result, so a stable order is a
    stable tree."""
    clips = (
        clip("C.mp4", tags_for("C")),
        clip("A.mp4", tags_for("A")),
        clip("B.mp4", tags_for("B")),
    )

    assert filter_clips(clips, includes=[("network", "CN")]) == clips


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def test_the_plan_counts_folders_videos_and_bytes(tmp_path):
    """Folders are the distinct folders that directly hold a clip,
    and a video that cannot be stat'd counts as zero bytes."""
    clips = (
        planted(tmp_path, tags_for("A")),
        planted(tmp_path, tags_for("B")),
        planted(tmp_path, tags_for("C", network="Old")),
    )
    # A clip whose video is gone: still a clip, still counted,
    # but its size is zero rather than a refusal.
    os.remove(clips[2].path)

    folders, videos, size = share_plan(clips)

    assert folders == 2
    assert videos == 3
    assert size == os.path.getsize(clips[0].path) * 2


def test_a_clip_at_the_root_adds_no_folder(tmp_path):
    """A clip with no folder above it contributes no folder."""
    root = str(tmp_path)
    video = os.path.join(root, "Solo.mp4")
    with open(video, "wb") as handle:
        handle.write(b"\0" * 8)
    write_record(
        os.path.splitext(video)[0] + RECORD_EXTENSION,
        ClipRecord(source="compilation.mp4", segment_index=1,
                    start=0.0, duration=5.0,
                    tags=tuple(sorted(tags_for("Solo").items())),
                    clip_id=next_clip_id()))
    tags = tuple(sorted(tags_for("Solo").items()))
    clips = (CatalogClip(
        path=video,
        relative_path="Solo.mp4",
        record_path=os.path.splitext(video)[0] + RECORD_EXTENSION,
        tags=tags,
        record=ClipRecord(source="compilation.mp4", segment_index=1,
                          start=0.0, duration=5.0, tags=tags,
                          clip_id=next_clip_id())),)

    folders, videos, size = share_plan(clips)

    assert folders == 0
    assert videos == 1


# ---------------------------------------------------------------------------
# The zip's name
# ---------------------------------------------------------------------------

def test_the_suffix_is_applied_before_the_extension():
    assert share_zip_name("My Library") == \
        "My Library (Created with CommCut).zip"


def test_the_suffix_is_not_stacked():
    """A name that already carries the suffix is left alone."""
    assert share_zip_name("My Library (Created with CommCut)") == \
        "My Library (Created with CommCut).zip"


def test_a_typed_extension_is_stripped():
    assert share_zip_name("My Library.zip") == \
        "My Library (Created with CommCut).zip"
    assert share_zip_name("My Library.ZIP") == \
        "My Library (Created with CommCut).zip"


def test_a_name_that_sanitizes_to_nothing_is_the_default():
    assert share_zip_name("") == \
        "CommCut Share (Created with CommCut).zip"
    assert share_zip_name("   ") == \
        "CommCut Share (Created with CommCut).zip"
    assert share_zip_name("///") == \
        "- (Created with CommCut).zip"


def test_unsafe_characters_are_sanitized():
    assert share_zip_name("a/b\\c:d") == \
        "a-b-c-d (Created with CommCut).zip"


# ---------------------------------------------------------------------------
# The zip's write
# ---------------------------------------------------------------------------

def test_the_zip_holds_videos_and_records_under_one_root(tmp_path):
    """One root folder named after the zip's stem, each clip's
    video and its record at their relative paths beneath it."""
    clips = (
        planted(tmp_path, tags_for("A")),
        planted(tmp_path, tags_for("B", network="Old")),
    )
    target = os.path.join(
        str(tmp_path), "My Library (Created with CommCut).zip")

    written = write_share_zip(clips, target)

    assert written == target
    with zipfile.ZipFile(written) as archive:
        names = archive.namelist()
    root = "My Library (Created with CommCut)"
    assert f"{root}/CN/Promo/2000s/A.mp4" in names
    assert f"{root}/CN/Promo/2000s/A.cnfo" in names
    assert f"{root}/Old/Promo/2000s/B.mp4" in names
    assert f"{root}/Old/Promo/2000s/B.cnfo" in names


def test_a_taken_name_is_not_clobbered(tmp_path):
    """A share already under that name becomes `stem (1).zip`,
    `stem (2).zip`, and so on."""
    clips = (planted(tmp_path, tags_for("A")),)
    share = tmp_path / "share"
    share.mkdir()
    target = str(share / "My Library (Created with CommCut).zip")

    first = write_share_zip(clips, target)
    second = write_share_zip(clips, target)
    third = write_share_zip(clips, target)

    assert os.path.basename(first) == \
        "My Library (Created with CommCut).zip"
    assert os.path.basename(second) == \
        "My Library (Created with CommCut) (1).zip"
    assert os.path.basename(third) == \
        "My Library (Created with CommCut) (2).zip"
    for path in (first, second, third):
        assert os.path.isfile(path)


def test_a_cancelled_share_leaves_nothing(tmp_path):
    """A cancel removes the partial file and returns None, so no
    half-file is left where a share is expected."""
    clips = (planted(tmp_path, tags_for("A")),)
    share = tmp_path / "share"
    share.mkdir()
    target = str(share / "My Library (Created with CommCut).zip")

    written = write_share_zip(
        clips, target, should_cancel=lambda: True)

    assert written is None
    assert not os.path.exists(target)
    assert not os.path.exists(target + ".part")


def test_a_missing_video_is_skipped_with_its_record(tmp_path):
    """One deleted clip on a network share should not cost the
    other eight hundred, so a clip with no video is skipped --
    and its record with it, since a record with no video
    describes nothing."""
    clips = (
        planted(tmp_path, tags_for("A")),
        planted(tmp_path, tags_for("B")),
    )
    os.remove(clips[1].path)
    target = os.path.join(
        str(tmp_path), "My Library (Created with CommCut).zip")

    written = write_share_zip(clips, target)

    with zipfile.ZipFile(written) as archive:
        names = archive.namelist()
    root = "My Library (Created with CommCut)"
    assert f"{root}/CN/Promo/2000s/A.mp4" in names
    assert f"{root}/CN/Promo/2000s/A.cnfo" in names
    assert f"{root}/CN/Promo/2000s/B.mp4" not in names
    assert f"{root}/CN/Promo/2000s/B.cnfo" not in names


def test_progress_carries_a_real_denominator(tmp_path):
    """The first report carries the total, so a progress bar can
    take a denominator before the first clip is written."""
    clips = (
        planted(tmp_path, tags_for("A")),
        planted(tmp_path, tags_for("B")),
    )
    target = os.path.join(
        str(tmp_path), "My Library (Created with CommCut).zip")
    reports = []

    write_share_zip(
        clips, target,
        on_progress=lambda done, total, relative:
            reports.append((done, total, relative)))

    assert reports[0] == (0, 2, "")
    assert reports[-1] == (2, 2, clips[1].relative_path)
    assert [done for done, _total, _relative in reports] == [0, 1, 2]


def test_a_failed_write_leaves_no_partial_file(tmp_path):
    """A clip whose record is gone raises, and the half-file is
    removed rather than left where a share is expected."""
    clips = (planted(tmp_path, tags_for("A")),)
    os.remove(clips[0].record_path)
    target = os.path.join(
        str(tmp_path), "My Library (Created with CommCut).zip")

    with pytest.raises(OSError):
        write_share_zip(clips, target)

    assert not os.path.exists(target)
    assert not os.path.exists(target + ".part")


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class FakePlayer:
    """Just enough mpv for the real `MpvBridge` to attach to."""

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
    """The real bridge, recording what the window asked of it."""

    instances = []

    def __init__(self, player):
        super().__init__(player)
        self.shutdowns = 0
        RecordingBridge.instances.append(self)

    def shutdown(self):
        self.shutdowns += 1


class FakeThread(QThread):
    """A QThread that never starts on its own, so the walk and
    the zip run synchronously."""

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


class StubCatalogWorker(sharing_module.CatalogWorker):
    """The real walk worker, with the thread affinity stubbed."""

    def moveToThread(self, thread):
        pass


class StubShareWorker(sharing_module.ShareWorker):
    """The real share worker, with the thread affinity stubbed."""

    def moveToThread(self, thread):
        pass


class FakeLoadingDialog(QObject):
    """A `LoadingDialog` whose `exec()` returns at once."""

    canceled = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.visible = True
        self.ranges = []
        self.values = []
        self.messages = []

    def set_range(self, minimum, maximum):
        self.ranges.append((minimum, maximum))

    def set_value(self, value):
        self.values.append(value)

    def set_message(self, message):
        self.messages.append(message)

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


class FakeShareDialog(QDialog):
    """A `ShareDialog` that accepts, with a name tests set."""

    name = "Test Share"
    should_accept = True

    def __init__(self, clips, parent=None):
        super().__init__(parent)
        self.clips = clips

    def exec(self):
        return QDialog.Accepted if self.should_accept \
            else QDialog.Rejected

    def zip_name(self):
        return share_zip_name(self.name)


@pytest.fixture
def harness(qapp, monkeypatch, tmp_path):
    """Installs the fakes and returns a factory for sharing
    windows over a temp export folder and a temp share folder."""
    library_root = tmp_path / "library"
    share_root = tmp_path / "share"
    os.makedirs(str(library_root), exist_ok=True)
    os.makedirs(str(share_root), exist_ok=True)

    RecordingBridge.instances = []
    FakeThread.auto_run = True

    monkeypatch.setattr(sharing_module, "QThread", FakeThread)
    monkeypatch.setattr(sharing_module, "CatalogWorker",
                        StubCatalogWorker)
    monkeypatch.setattr(sharing_module, "ShareWorker",
                        StubShareWorker)
    monkeypatch.setattr(sharing_module, "create_mpv_player",
                        lambda _container: FakePlayer())
    monkeypatch.setattr(sharing_module, "MpvBridge",
                        RecordingBridge)
    monkeypatch.setattr(sharing_module, "LoadingDialog",
                        FakeLoadingDialog)
    monkeypatch.setattr(sharing_module, "ShareDialog",
                        FakeShareDialog)
    monkeypatch.setattr(sharing_module, "export_folder",
                        lambda: str(library_root))
    monkeypatch.setattr(sharing_module, "share_folder",
                        lambda: str(share_root))

    def open_sharing(*clips):
        """A Library Sharing window over the given clips.

        Each clip is its tags; it is planted at the path its
        tags render to. The walk runs synchronously, so the
        window is fully built the moment its constructor returns.
        """
        for tags in clips:
            plant_clip(str(library_root), tags)
        FakeThread.auto_run = True
        return sharing_module.SharingWindow()

    yield open_sharing, str(library_root), str(share_root)

    qapp.processEvents()


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


def leaf_paths(window):
    """The relative path every leaf stands for, in order."""
    return [leaf.data(0, Qt.UserRole + 1)
            for leaf in clip_leaves(window)]


def combo_values(combo):
    """The values a combo offers."""
    return [combo.itemText(index)
            for index in range(combo.count())]


def test_the_tree_is_built_from_the_walk(harness):
    """Folders and clips, casefolded, and only folders that hold
    clips -- the tree is clip-driven, and it is the filter's
    result, so it is derived state rebuilt from every walk."""
    open_sharing, root, share_root = harness

    window = open_sharing(
        tags_for("Alpha"), tags_for("Beta"),
        tags_for("Gamma", network="Old"))

    try:
        assert whole_tree(window) == [
            ("CN", [("Promo", [("2000s", [
                ("Alpha.mp4", []), ("Beta.mp4", [])])])]),
            ("Old", [("Promo", [("2000s", [
                ("Gamma.mp4", [])])])]),
        ]
        assert window.ui.buttonShare.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_a_filter_row_narrows_the_tree(harness):
    """A row the user adds re-runs the filter synchronously, and
    the tree is rebuilt from the result -- the tree is the
    oracle, so what it shows is what the share would hold."""
    open_sharing, root, share_root = harness

    window = open_sharing(
        tags_for("Alpha", network="CN"),
        tags_for("Beta", network="Old"))

    try:
        assert len(clip_leaves(window)) == 2

        window._add_row(excluded=False)
        row = window._included_rows[0]
        row.set_namespace("network")
        row.set_value("CN")

        assert leaf_paths(window) == ["CN/Promo/2000s/Alpha.mp4"]
    finally:
        window.close()
        window.deleteLater()


def test_an_excluded_row_drops_a_clip(harness):
    """An exclusion drops a clip the inclusions would otherwise
    have kept, and the tree shows it gone."""
    open_sharing, root, share_root = harness

    window = open_sharing(
        tags_for("Alpha", network="CN"),
        tags_for("Beta", network="Old"))

    try:
        window._add_row(excluded=True)
        row = window._excluded_rows[0]
        row.set_namespace("network")
        row.set_value("Old")

        assert leaf_paths(window) == ["CN/Promo/2000s/Alpha.mp4"]
    finally:
        window.close()
        window.deleteLater()


def test_the_share_button_follows_the_filter(harness):
    """A filter that matches nothing cannot be shared."""
    open_sharing, root, share_root = harness

    window = open_sharing(tags_for("Alpha"))

    try:
        assert window.ui.buttonShare.isEnabled()

        window._add_row(excluded=False)
        row = window._included_rows[0]
        row.set_namespace("network")
        row.set_value("NoSuchNetwork")

        assert not window.ui.buttonShare.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_the_value_combo_offers_the_catalog_values(harness):
    """The value combo is populated from the catalog's index for
    the chosen namespace -- the library's own values, not the
    advisory vocabulary."""
    open_sharing, root, share_root = harness

    window = open_sharing(
        tags_for("Alpha", network="CN"),
        tags_for("Beta", network="Old"))

    try:
        window._add_row(excluded=False)
        row = window._included_rows[0]

        assert combo_values(row._value) == ["CN", "Old"]
    finally:
        window.close()
        window.deleteLater()


def test_removing_a_row_widens_the_share_again(harness):
    """A row's Remove button drops it and the filter re-runs, so
    the share widens back to what it matched without it."""
    open_sharing, root, share_root = harness

    window = open_sharing(
        tags_for("Alpha", network="CN"),
        tags_for("Beta", network="Old"))

    try:
        window._add_row(excluded=False)
        row = window._included_rows[0]
        row.set_namespace("network")
        row.set_value("CN")
        assert leaf_paths(window) == ["CN/Promo/2000s/Alpha.mp4"]

        row._remove.click()

        assert len(clip_leaves(window)) == 2
        assert not window._included_rows
    finally:
        window.close()
        window.deleteLater()


def test_sharing_writes_a_zip(harness, qapp):
    """The Share button, through the dialog and the worker, writes
    a zip into the share folder: each clip's video and its record,
    under one root folder named after the zip."""
    open_sharing, root, share_root = harness

    window = open_sharing(tags_for("Alpha"), tags_for("Beta"))

    try:
        FakeShareDialog.name = "My Library"
        FakeShareDialog.should_accept = True
        window.on_share()
        qapp.processEvents()

        expected = os.path.join(
            share_root, "My Library (Created with CommCut).zip")
        assert os.path.isfile(expected)
        with zipfile.ZipFile(expected) as archive:
            names = archive.namelist()
        root_folder = "My Library (Created with CommCut)"
        assert f"{root_folder}/CN/Promo/2000s/Alpha.mp4" in names
        assert f"{root_folder}/CN/Promo/2000s/Alpha.cnfo" in names
        assert f"{root_folder}/CN/Promo/2000s/Beta.mp4" in names
        assert f"{root_folder}/CN/Promo/2000s/Beta.cnfo" in names
        assert "Wrote" in window.ui.labelStatus.text()
        assert window.ui.buttonShare.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_a_declined_share_writes_nothing(harness, qapp):
    """Cancelling the dialog leaves the library untouched."""
    open_sharing, root, share_root = harness

    window = open_sharing(tags_for("Alpha"))

    try:
        FakeShareDialog.should_accept = False
        window.on_share()
        qapp.processEvents()

        assert os.listdir(share_root) == []
        assert window.ui.labelStatus.text() == ""
    finally:
        window.close()
        window.deleteLater()


def test_selecting_a_clip_previews_it(harness):
    """Selecting a leaf loads its preview, through the real
    `MpvBridge` onto the fake player."""
    open_sharing, root, share_root = harness

    window = open_sharing(tags_for("Alpha"), tags_for("Beta"))

    try:
        leaf = clip_leaves(window)[0]
        window.ui.treeLibrary.setCurrentItem(leaf)

        assert window.ui.labelClipName.text() == \
            "CN/Promo/2000s/Alpha.mp4"
        assert RecordingBridge.instances
        assert RecordingBridge.instances[-1].player.played == [
            os.path.join(root, "CN", "Promo", "2000s", "Alpha.mp4")]
    finally:
        window.close()
        window.deleteLater()


def test_closing_the_window_shuts_the_player_down(harness):
    """An mpv-backed window must shut its player down before it is
    destroyed (invariant 4), and closing is what runs it."""
    open_sharing, root, share_root = harness

    window = open_sharing(tags_for("Alpha"))
    leaf = clip_leaves(window)[0]
    window.ui.treeLibrary.setCurrentItem(leaf)
    bridge = RecordingBridge.instances[-1]

    window.close()

    assert bridge.shutdowns == 1
    window.deleteLater()


# ---------------------------------------------------------------------------
# Zip import
# ---------------------------------------------------------------------------

def _make_zip(path, entries):
    """Write `entries` `{name: content}` into `path` as a zip."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_has_record_entries_detects_a_cnfo(tmp_path):
    """A zip with a `.cnfo` file is recognized as a share."""
    zip_path = _make_zip(str(tmp_path / "share.zip"), {
        "Share/clip.mp4": b"video",
        "Share/clip.cnfo": b"record",
    })
    assert has_record_entries(zip_path)


def test_has_record_entries_rejects_a_zip_without_records(tmp_path):
    """A zip with no `.cnfo` files is not a share."""
    zip_path = _make_zip(str(tmp_path / "no-records.zip"), {
        "Share/clip.mp4": b"video",
    })
    assert not has_record_entries(zip_path)


def test_has_record_entries_is_case_insensitive_for_the_extension(tmp_path):
    """`.cnfo`, `.CNFO`, `.Cnfo` all count."""
    zip_path = _make_zip(str(tmp_path / "share.zip"), {
        "Share/clip.cnfo": b"lower",
        "Share/clip2.CNFO": b"upper",
    })
    assert has_record_entries(zip_path)


def test_extract_share_zip_writes_files_under_the_root(tmp_path):
    """The zip's internal root folder becomes the folder in `import/`."""
    zip_path = _make_zip(str(tmp_path / "share.zip"), {
        "My Share/clip.mp4": b"video",
        "My Share/clip.cnfo": b"record",
    })
    dest = str(tmp_path / "import")
    os.makedirs(dest)

    result = extract_share_zip(zip_path, dest)

    assert os.path.basename(result) == "My Share"
    assert os.path.isfile(os.path.join(result, "clip.mp4"))
    assert os.path.isfile(os.path.join(result, "clip.cnfo"))


def test_extract_share_zip_does_not_clobber_an_existing_folder(tmp_path):
    """A folder with the same root name gets the `(1)` treatment."""
    existing = tmp_path / "import" / "share"
    existing.mkdir(parents=True)
    (existing / "old.mp4").write_bytes(b"old")

    zip_path = _make_zip(str(tmp_path / "share.zip"), {
        "share/clip.mp4": b"video",
        "share/clip.cnfo": b"record",
    })
    dest = str(tmp_path / "import")

    result = extract_share_zip(zip_path, dest)

    assert os.path.basename(result) == "share (1)"
    assert os.path.isfile(os.path.join(result, "clip.mp4"))
    assert os.path.isfile(os.path.join(result, "clip.cnfo"))
    assert os.path.isfile(os.path.join(existing, "old.mp4"))


def test_extract_share_zip_reports_progress(tmp_path):
    """Progress is called once before the first member and once per member."""
    zip_path = _make_zip(str(tmp_path / "share.zip"), {
        "share/a.mp4": b"aa",
        "share/a.cnfo": b"r1",
        "share/b.mp4": b"bb",
        "share/b.cnfo": b"r2",
    })
    dest = str(tmp_path / "import")
    reports = []

    extract_share_zip(
        zip_path, dest,
        on_progress=lambda done, total: reports.append((done, total)))

    assert reports[0] == (0, 4)
    assert reports[-1] == (4, 4)
    assert len(reports) == 5


def test_extract_share_zip_cancels_cleanly(tmp_path):
    """A cancel leaves no staging directory behind."""
    zip_path = _make_zip(str(tmp_path / "share.zip"), {
        "share/a.mp4": b"aa",
        "share/a.cnfo": b"r1",
    })
    dest = str(tmp_path / "import")

    cancelled = []
    result = extract_share_zip(
        zip_path, dest,
        should_cancel=lambda: (cancelled.append(True), True)[1])

    assert result is None
    assert not os.path.exists(os.path.join(dest, "share"))
    assert not os.path.exists(os.path.join(dest, "share.part"))
    assert cancelled


def test_extract_share_zip_rejects_a_path_escape(tmp_path):
    """A member that escapes `dest_root/..` is refused."""
    zip_path = _make_zip(str(tmp_path / "evil.zip"), {
        "../escape.txt": b"escaped",
    })
    dest = str(tmp_path / "import")
    os.makedirs(dest)

    with pytest.raises(ZipImportError) as info:
        extract_share_zip(zip_path, dest)

    assert "escape" in str(info.value)
    assert not os.path.isfile(str(tmp_path / "escape.txt"))


def test_extract_share_zip_rejects_a_symlink(tmp_path):
    """A member that is a symlink is refused."""
    zip_path = str(tmp_path / "evil.zip")
    info_entry = zipfile.ZipInfo("share/link")
    info_entry.external_attr = (0o120000 << 16)
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(info_entry, b"/etc/passwd")

    dest = str(tmp_path / "import")
    os.makedirs(dest)

    with pytest.raises(ZipImportError) as info:
        extract_share_zip(zip_path, dest)

    assert "symlink" in str(info.value)


def test_extract_share_zip_rejects_too_many_entries(tmp_path):
    """A zip exceeding `MAX_ZIP_ENTRIES` is refused."""
    from shared.sharing import MAX_ZIP_ENTRIES
    entries = {f"share/f{i}.mp4": b"x" for i in range(MAX_ZIP_ENTRIES + 1)}
    zip_path = _make_zip(str(tmp_path / "big.zip"), entries)
    dest = str(tmp_path / "import")
    os.makedirs(dest)

    with pytest.raises(ZipImportError) as info:
        extract_share_zip(zip_path, dest)

    assert "exceeds" in str(info.value)


def test_extract_share_zip_cleans_up_on_failure(tmp_path, monkeypatch):
    """A mid-extraction error removes the staging directory."""
    zip_path = str(tmp_path / "share.zip")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("share/a.mp4", b"aa")
        archive.writestr("share/a.cnfo", b"r1")

    dest = str(tmp_path / "import")
    os.makedirs(dest)

    original_open = open

    def failing_open(file, mode="r", *args, **kwargs):
        if str(file).endswith("a.mp4"):
            raise OSError("simulated write failure")
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)

    with pytest.raises(OSError):
        extract_share_zip(zip_path, dest)

    assert not os.path.exists(os.path.join(dest, "share.part"))
    assert not os.path.exists(os.path.join(dest, "share"))


def test_extract_share_zip_skips_directory_entries(tmp_path):
    """Directory entries in the zip are not extracted as files."""
    zip_path = str(tmp_path / "share.zip")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("share/", b"")
        archive.writestr("share/clip.mp4", b"video")
        archive.writestr("share/clip.cnfo", b"record")

    dest = str(tmp_path / "import")
    os.makedirs(dest)

    result = extract_share_zip(zip_path, dest)

    assert os.path.isfile(os.path.join(result, "clip.mp4"))
    assert os.path.isfile(os.path.join(result, "clip.cnfo"))


def test_extract_share_zip_checks_free_space(tmp_path, monkeypatch):
    """A zip that would not fit is refused before any file is written."""
    zip_path = _make_zip(str(tmp_path / "share.zip"), {
        "share/big.bin": b"x" * 100,
    })
    dest = str(tmp_path / "import")
    os.makedirs(dest)

    import shared.sharing as sharing_module
    monkeypatch.setattr(
        sharing_module, "shutil",
        type("FakeShutil", (), {
            "disk_usage": lambda root: type("Usage", (), {"free": 50})()
        }))

    with pytest.raises(ZipImportError) as info:
        extract_share_zip(zip_path, dest)
    assert "free" in str(info.value)


def test_extract_share_zip_without_a_common_root_uses_zip_stem(tmp_path):
    """A zip with no single top-level folder falls back to the zip's stem."""
    zip_path = _make_zip(str(tmp_path / "mystery.zip"), {
        "clip.mp4": b"video",
        "clip.cnfo": b"record",
    })
    dest = str(tmp_path / "import")
    os.makedirs(dest)

    result = extract_share_zip(zip_path, dest)

    assert os.path.basename(result) == "mystery"
    assert os.path.isfile(os.path.join(result, "clip.mp4"))
    assert os.path.isfile(os.path.join(result, "clip.cnfo"))
