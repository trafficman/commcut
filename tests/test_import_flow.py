"""The whole import flow, end to end, on real files and real windows.

Applies to: `importer/mesh.py`, `importer/queue.py`, `importer/values.py`,
`importer/importrun.py`, `shared/mesh.py`, `shared/values.py`.

The other suites each cover one window. This one covers the thing none of them can:
that the four fit together. An untagged folder goes in and a tagged library comes
out, passing through the folder-name pass, the per-clip pass, the value pass and the
import — and every stage is asserted on the files on disk afterwards rather than on
any window's internal state, because the files are what the next stage reads.

Deliberately not a Qt test: no window is built here. `tests/test_values_window.py`
and `tests/test_queue.py` cover rendering, and a second fake-thread harness for the
same four windows would be a lot of machinery guarding the same code.
"""

import os

import pytest

from shared.catalog import build_catalog
from shared.exporting import load_export_schemes
from shared.importing import candidates_from_catalog, execute_import, plan_import
from shared.mesh import MeshSession
from shared.records import ClipRecord, load_record, write_record
from shared.values import ValueSession, execute_translation, plan_translation

#: The four tags an export requires. The foreign library does not use these words.
FOREIGN = {
    "title": "Toonami Ep 12",
    "network": "CN",
    "filler_type": "Promo",
    "time_period": "2000s",
}


class FakeVideo:
    """A `FoundVideo` for building a `MeshSession` without walking a tree.

    `find_videos` is covered by `tests/test_importing.py`; what is under test here is
    what the four stages do with what it returns.
    """

    def __init__(self, relative_path, has_record=False):
        self.relative_path = relative_path
        self.path = relative_path
        self.has_record = has_record


def untagged_clip(root, relative):
    """A video with no record beside it — the untagged half."""
    path = os.path.join(str(root), *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * 16)
    return path


def settle(absolute_path, relative_path, tags):
    """What `QueueWindow.on_next` does, at the level the next stage reads."""
    record = ClipRecord(source=relative_path, segment_index=0, start=0.0,
                        duration=30.0, tags=tuple(sorted(tags.items())))
    write_record(os.path.splitext(absolute_path)[0] + ".cnfo", record)


@pytest.fixture
def library(tmp_path):
    """A destination library holding the words *this* library uses."""
    root = tmp_path / "export"
    path = os.path.join(str(root), "Cartoon Network", "2000s", "Promo", "Mine.mp4")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * 16)
    write_record(os.path.splitext(path)[0] + ".cnfo", ClipRecord(
        source="Mine.mp4", segment_index=0, start=0.0, duration=30.0,
        tags=(("title", "Mine"), ("network", "Cartoon Network"),
              ("filler_type", "Promo"), ("time_period", "2000s")),
    ))
    return root


def test_a_mixed_folder_goes_through_both_halves_and_lands_in_the_library(
        tmp_path, library):
    """The whole journey, and the only test that runs all four stages.

    A **mixed** folder, because that is the case that exercises the hand-off between
    the halves and nothing else does: one clip nobody has answered for, and one that
    arrived with somebody's records already beside it. The mixed folder is why the
    Untagged Library Mesh's worker partitions on `has_record` at all, and why the
    value pass reads the whole folder rather than only what the queue just wrote.

    The foreign value is `CB`, in the clip that came tagged — so the value pass has
    something to do that stage 1 could not have done, and it moves exactly one clip.
    """
    root = tmp_path / "import"

    # The untagged half.
    untagged_clip(root, "Cartoon Network/2000s/Promo/A.mp4")

    # The tagged half: same tags, wrong words.
    tagged_path = untagged_clip(root, "CBS/1990s/Commercial/B.mp4")
    write_record(os.path.splitext(tagged_path)[0] + ".cnfo", ClipRecord(
        source="B.mp4", segment_index=0, start=0.0, duration=30.0,
        tags=(("title", "B"), ("network", "CB"), ("filler_type", "Promo"),
              ("time_period", "1990s"))))

    # --- 1. the untagged half only ---------------------------------------------
    videos = [FakeVideo("Cartoon Network/2000s/Promo/A.mp4", has_record=False)]
    mesh = MeshSession(str(root), videos, library=build_catalog(str(library)))

    while mesh.pending():
        name = mesh.next_prompt().name
        mesh.assign(name, {"Cartoon Network": "network", "2000s": "time_period",
                           "Promo": "filler_type"}[name], name)

    # --- 2. the per-clip pass: a title, and a record written immediately ----------
    for video in videos:
        derived = mesh.tags_for_clip(video.relative_path).tags
        settle(os.path.join(str(root), *video.relative_path.split("/")),
               video.relative_path, {**derived, "title": "A"})

    # The folder is now entirely the tagged half — including the clip that already
    # was one, which is the point.
    tagged = build_catalog(str(root))
    assert {clip.relative_path for clip in tagged.clips} == {
        "Cartoon Network/2000s/Promo/A.mp4", "CBS/1990s/Commercial/B.mp4"}
    assert not tagged.problems, "every record commcut wrote must read back"

    # --- 3. the value pass: this library's words, over the whole folder ----------
    values = ValueSession(str(root), tagged, library=build_catalog(str(library)))
    while values.pending():
        entry = values.next_prompt().entry
        if entry.value == "CB":
            values.translate(entry.namespace, entry.value, "CBS")
        else:
            values.keep(entry.namespace, entry.value)

    assert values.merges() == (), (
        "one value becoming another is not a merge; two becoming one would be, and "
        "this is not that")
    result = execute_translation(plan_translation(values))
    assert result.failed == ()
    assert result.written == ("CBS/1990s/Commercial/B.mp4",), (
        "only the clip that carried `CB`")

    # --- 4. the import -----------------------------------------------------------
    plan = plan_import(
        candidates_from_catalog(build_catalog(str(root))),
        load_export_schemes(str(tmp_path / "settings.json")),
        str(library),
        existing=build_catalog(str(library)),
    )
    imported = execute_import(plan)

    assert imported.failed == ()
    assert len(imported.written) == 2
    landed = build_catalog(str(library))
    assert landed.tag_index["network"] == ("CBS", "Cartoon Network"), (
        "`CB` is now `CBS`, and nothing lost the words it arrived with")


def test_the_flow_is_the_same_one_a_foreign_library_takes(tmp_path, library):
    """The convergence, stated as a test.

    A folder that arrived *already* tagged skips stages 1 and 2 entirely — the
    Untagged Library Mesh's worker hands it straight to the value pass — and from
    stage 3 onward it is on exactly the same code as the folder this test built by
    hand. That is the property that keeps the two halves from drifting: there is only
    one import, and untagged import is a way of arriving at its input.
    """
    root = tmp_path / "import"
    video = untagged_clip(root, "Cartoon Network/2000s/Theirs.mp4")
    write_record(os.path.splitext(video)[0] + ".cnfo", ClipRecord(
        source="Theirs.mp4", segment_index=0, start=0.0, duration=30.0,
        tags=(("title", "Theirs"), ("network", "Cartoon Network"),
              ("filler_type", "Promo"), ("time_period", "2000s"))))

    tagged = build_catalog(str(root))
    values = ValueSession(str(root), tagged, library=build_catalog(str(library)))

    # Nothing is left to ask: this library already says what it says. And answering
    # "keep" writes nothing at all, rather than rewriting an identical record.
    while values.pending():
        entry = values.next_prompt().entry
        values.keep(entry.namespace, entry.value)

    translation = execute_translation(plan_translation(values))
    assert translation.written == ()
    assert build_catalog(str(root)).clips, "and the record is untouched"


def test_a_record_survives_the_round_trip_through_both_passes(tmp_path, library):
    """What each stage is not allowed to do to a record.

    A translation changes what a clip is *called*. It does not change where the clip
    came from, and it does not change the file. This walks a record through the value
    pass and a write and compares everything but the tags, which is the invariant
    stated as a test rather than as prose.
    """
    root = tmp_path / "import"
    video = untagged_clip(root, "CN/A.mp4")
    record_path = os.path.splitext(video)[0] + ".cnfo"
    write_record(record_path, ClipRecord(
        source="A.mp4", segment_index=3, start=61.5, duration=30.25,
        tags=(("title", "A"), ("network", "CN"), ("filler_type", "Promo"))))
    before = load_record(record_path)

    values = ValueSession(str(root), build_catalog(str(root)),
                          library=build_catalog(str(library)))
    while values.pending():
        entry = values.next_prompt().entry
        if entry.value == "CN":
            values.translate(entry.namespace, entry.value, "Cartoon Network")
        else:
            values.keep(entry.namespace, entry.value)
    execute_translation(plan_translation(values))

    after = load_record(record_path)
    assert (after.source, after.segment_index, after.start, after.duration) == (
        before.source, before.segment_index, before.start, before.duration)
    assert after.tag_dict["network"] == "Cartoon Network"
    assert after.tag_dict["title"] == "A"
    assert os.path.getsize(video) == 16, "the video is never opened or rewritten"