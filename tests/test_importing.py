"""Tests for the Library Importer's backend.

Covers the two modes a caller drives them through: turning a walked library into
import candidates, and turning candidates into clips written into this user's own
library. Also the untagged half's two pure functions, which have no caller until
there is a screen, and which are here because they are what keeps a guess from
becoming a value.

`tests/test_exporting.py` owns the destination half that both planners share, so
these tests use it rather than re-asserting where a clip lands.
"""

import os

import pytest

from shared.catalog import Catalog, CatalogClip, build_catalog
from shared.exporting import ExportSchemes
from shared.importing import (
    REASON_ALREADY_PRESENT,
    REASON_DESTINATION_TAKEN,
    REASON_DUPLICATE,
    REASON_INVALID_DURATION,
    REASON_MISSING_TAGS,
    REASON_NO_DURATION,
    REASON_OUT_OF_SPACE,
    REASON_RECORD_FAILED,
    REASON_TRANSFER_FAILED,
    TRANSFER_COPY,
    TRANSFER_LINK,
    TRANSFER_MOVE,
    FoundVideo,
    ImportCandidate,
    ImportSpaceError,
    candidates_from_catalog,
    check_free_space,
    execute_import,
    find_videos,
    match_value,
    plan_import,
)
from shared.paths import DEFAULT_FOLDER_SCHEME
from shared.records import ClipRecord, RECORD_EXTENSION, load_record, write_record
from shared.naming import DEFAULT_FILE_NAMING_SCHEME
from shared.vocabulary import Vocabulary


REQUIRED = {
    "title": "Worlds Finest", "network": "Cartoon Network",
    "filler_type": "Promo", "time_period": "2000s",
}


@pytest.fixture
def schemes():
    return ExportSchemes(
        file_scheme=DEFAULT_FILE_NAMING_SCHEME,
        folder_scheme=DEFAULT_FOLDER_SCHEME,
    )


def clip_in(root, stem, tags=None, *, source="compilation.mp4", duration=30.0,
            extension=".mp4", folder="Cartoon Network/Promo"):
    """A video with a record beside it, as another commcut install leaves one."""
    directory = os.path.join(str(root), *folder.split("/"))
    os.makedirs(directory, exist_ok=True)
    video = os.path.join(directory, stem + extension)
    with open(video, "wb") as handle:
        handle.write(b"\0" * 16)
    write_record(os.path.splitext(video)[0] + RECORD_EXTENSION, ClipRecord(
        source=source, segment_index=2, start=61.5, duration=duration,
        tags=tuple(tags.items() if tags is not None else REQUIRED.items()),
    ))
    return video


def candidate(source_path, tags=None, **overrides):
    values = {
        "source_path": str(source_path),
        "tags": tuple((tags or REQUIRED).items()),
        "provenance": os.path.basename(str(source_path)),
        "duration": 30.0,
    }
    values.update(overrides)
    return ImportCandidate(**values)


def catalog_with(*pairs):
    """A `Catalog` holding one clip per `(namespace, value)` pair.

    Real `CatalogClip`s rather than stand-ins: `Catalog.tag_index` reads `tags`
    and `CatalogClip` is what actually supplies them, so a fake here would be
    testing the fake.
    """
    clips = tuple(
        CatalogClip(
            path=f"/library/{namespace}/{index}.mp4",
            relative_path=f"{namespace}/{index}.mp4",
            record_path=f"/library/{namespace}/{index}.cnfo",
            tags=((namespace, value),),
            record=ClipRecord(
                source="theirs.mp4", segment_index=0, start=0.0, duration=1.0,
                tags=((namespace, value),)),
        )
        for index, (namespace, value) in enumerate(pairs)
    )
    return Catalog(clips=clips)


def library_with(namespace, *values):
    return catalog_with(*((namespace, value) for value in values))


# ---------------------------------------------------------------------------
# Records become candidates
# ---------------------------------------------------------------------------

def test_a_walked_library_becomes_import_candidates(tmp_path):
    clip_in(tmp_path, "Worlds Finest", duration=29.9, source="Their Rip.mp4")

    candidates = candidates_from_catalog(build_catalog(str(tmp_path)))

    assert len(candidates) == 1
    assert dict(candidates[0].tags) == REQUIRED
    assert candidates[0].duration == 29.9, "the foreign record already knows this"
    assert candidates[0].provenance == "Their Rip.mp4"


def test_a_tagged_import_needs_no_ffprobe(tmp_path, monkeypatch):
    """The duration comes off the record rather than off the video, so a whole
    library can be imported without spawning a probe per clip."""
    clip_in(tmp_path, "Worlds Finest", duration=12.25)
    import shared.segments as segments
    monkeypatch.setattr(
        segments, "probe_duration",
        lambda *_a, **_k: pytest.fail("probed a video whose record knows its length"))

    plan = plan_import(candidates_from_catalog(build_catalog(str(tmp_path))),
                       ExportSchemes(DEFAULT_FILE_NAMING_SCHEME, DEFAULT_FOLDER_SCHEME),
                       str(tmp_path / "library"))

    assert plan.clips[0].duration == 12.25


def test_a_record_that_was_refused_never_becomes_a_candidate(tmp_path):
    """A record with a tag this build does not know is refused by the catalog, so
    it cannot reach the planner -- and the problem names which tag."""
    broken = clip_in(tmp_path, "Foreign")
    with open(broken[:-4] + RECORD_EXTENSION, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<commcut-clip version="1">\n'
            "  <source>their.mp4</source>\n"
            '  <segment index="0" start="0" duration="1" />\n'
            '  <tag key="colour">Red</tag>\n'
            "</commcut-clip>\n"
        )

    catalog = build_catalog(str(tmp_path))

    assert candidates_from_catalog(catalog) == ()
    assert "colour" in catalog.problems[0].message


# ---------------------------------------------------------------------------
# Candidates become clips
# ---------------------------------------------------------------------------

def test_a_candidate_lands_where_the_same_tags_would_be_exported(schemes,
                                                                 tmp_path):
    """The cross-check that matters most: an imported clip and an exported one are
    the same question, and must not be two different answers."""
    from shared.exporting import plan_clip_destination
    from shared.naming import compile_filename_scheme
    from shared.paths import compile_folder_scheme

    video = clip_in(tmp_path, "Worlds Finest")
    plan = plan_import(candidates_from_catalog(build_catalog(str(tmp_path))),
                       schemes, str(tmp_path / "library"))
    expected = plan_clip_destination(
        tags=REQUIRED,
        folder_scheme=compile_folder_scheme(DEFAULT_FOLDER_SCHEME),
        filename_scheme=compile_filename_scheme(DEFAULT_FILE_NAMING_SCHEME),
        label="Worlds Finest.mp4")

    assert plan.clips[0].destination.relative_components == (
        expected.relative_components)


def test_the_new_record_keeps_the_tags_and_says_where_the_clip_came_from(
    schemes, tmp_path):
    video = clip_in(tmp_path, "Worlds Finest", source="Their Rip.mp4",
                    duration=29.9)
    plan = plan_import([candidate(video, provenance="Their Rip.mp4", duration=29.9)],
                       schemes, str(tmp_path / "library"))

    record = plan.record_for(plan.clips[0])

    assert dict(record.tags) == REQUIRED
    assert record.source == "Their Rip.mp4"
    assert record.duration == 29.9
    assert (record.segment_index, record.start) == (0, 0.0), (
        "an imported clip was not cut from a segment, so there is no index")


def test_the_record_is_named_beside_its_video(schemes, tmp_path):
    video = clip_in(tmp_path, "Worlds Finest")
    plan = plan_import([candidate(video)], schemes, str(tmp_path / "library"))

    clip = plan.clips[0]
    assert clip.record_relative_path == clip.relative_path[:-4] + RECORD_EXTENSION


def test_a_clip_missing_a_required_tag_is_skipped_and_the_rest_are_not(
    schemes, tmp_path):
    """One bad clip out of two hundred must not take the batch with it. That is
    the whole reason this planner is per-clip where the export planner is not."""
    partial = {k: v for k, v in REQUIRED.items() if k != "time_period"}
    good = clip_in(tmp_path, "Good")
    bad = clip_in(tmp_path, "Bad", tags=partial)

    plan = plan_import([candidate(bad, tags=partial), candidate(good)], schemes,
                       str(tmp_path / "library"))

    assert len(plan.clips) == 1
    assert plan.clips[0].source_path == good
    assert len(plan.skipped) == 1
    assert plan.skipped[0].reason == REASON_MISSING_TAGS
    assert "time_period" in plan.skipped[0].reason_text


def test_a_skip_names_the_clip_rather_than_counting_it(schemes, tmp_path):
    partial = {k: v for k, v in REQUIRED.items() if k == "title"}
    bad = clip_in(tmp_path, "Nickelodeon Bumper", tags=partial)

    plan = plan_import([candidate(bad, tags=partial)], schemes,
                       str(tmp_path / "library"))

    assert plan.clips == ()
    assert "Nickelodeon Bumper.mp4" in str(plan.skipped[0])


def test_a_duration_of_zero_is_refused_rather_than_written(schemes, tmp_path):
    """A foreign record is only required to hold a *numeric* duration, so this
    reads perfectly and would otherwise produce a record claiming a clip of no
    length."""
    video = clip_in(tmp_path, "Zero", duration=0.0)

    plan = plan_import([candidate(video, duration=0.0)], schemes,
                       str(tmp_path / "library"))

    assert plan.clips == ()
    assert plan.skipped[0].reason == REASON_INVALID_DURATION


def test_a_missing_duration_is_refused(schemes, tmp_path):
    video = clip_in(tmp_path, "None")

    plan = plan_import([candidate(video, duration=None)], schemes,
                       str(tmp_path / "library"))

    assert plan.skipped[0].reason == REASON_NO_DURATION


def test_two_clips_resolving_to_one_destination_gives_one_clip_and_a_named_refusal(
    schemes, tmp_path,
):
    """No-clobber: the first wins and the second is reported, which is the
    property that stops an import quietly overwriting a clip."""
    first = clip_in(tmp_path, "One", folder="To Import/A")
    second = clip_in(tmp_path, "Two", folder="To Import/B")

    plan = plan_import([candidate(first), candidate(second)], schemes,
                       str(tmp_path / "library"))

    assert len(plan.clips) == 1
    assert plan.clips[0].source_path == first
    assert plan.skipped[0].reason == REASON_DUPLICATE
    assert "Two.mp4" in plan.skipped[0].reason_text
    assert "One.mp4" in plan.skipped[0].reason_text


# ---------------------------------------------------------------------------
# An occupied destination
# ---------------------------------------------------------------------------

def occupied_library(schemes, tmp_path, tags=None):
    """A destination library already holding the clip an import would write.

    Built by actually importing one rather than by arranging files, because the
    question is what happens when the destination is occupied *at the place the
    planner chooses* -- which is not a folder anybody would construct by hand.
    """
    library = tmp_path / "library"
    first = clip_in(tmp_path / "first", "Worlds Finest",
                    tags=tags or None)
    plan = plan_import([candidate(first, tags=tags or None)], schemes,
                       str(library))
    assert execute_import(plan).committed == 1
    return library, build_catalog(str(library))


def test_an_identical_clip_already_in_the_library_is_skipped_not_refused(
    schemes, tmp_path,
):
    """Which is what makes a re-run a no-op instead of a failure -- and therefore
    what makes an import retryable after a partial cancellation."""
    library, existing = occupied_library(schemes, tmp_path)
    incoming = clip_in(tmp_path / "incoming", "Worlds Finest")

    plan = plan_import([candidate(incoming)], schemes, str(library),
                       existing=existing)

    assert plan.clips == ()
    assert plan.already_present
    assert plan.refusals == ()


#: Schemes that leave `block` out of both the folder and the filename. Every one
#: of the ten tags appears in the shipped schemes, so two clips differing in any
#: tag land in different folders and a same-destination conflict cannot arise
#: without one like this — which is a one-line edit away for any user who wants a
#: flatter library.
SPARSE_SCHEMES = ExportSchemes(
    file_scheme="{network} - {type} - {time_period} - {title}",
    folder_scheme="{network}/{type}/{time_period}",
)


def test_a_clip_in_the_way_with_different_tags_is_refused_and_says_which(tmp_path):
    """No-clobber for a real conflict, naming the tags that differ so the user
    can see it is not a re-import rather than a re-run."""
    library, existing = occupied_library(
        SPARSE_SCHEMES, tmp_path, tags={**REQUIRED, "block": "Toonami"})
    incoming = clip_in(tmp_path / "incoming", "Worlds Finest")

    plan = plan_import([candidate(incoming)], SPARSE_SCHEMES, str(library),
                       existing=existing)

    assert plan.clips == ()
    assert plan.already_present == ()
    assert plan.skipped[0].reason == REASON_DESTINATION_TAKEN
    assert "block" in plan.skipped[0].reason_text


def test_two_clips_colliding_on_one_destination_take_the_first(tmp_path):
    """The other way a conflict arrives, and the one needing no custom scheme at
    all: sanitation collapses two different titles onto one filename."""
    first = clip_in(tmp_path / "a", "First")
    second = clip_in(tmp_path / "b", "Second")
    candidates = [
        candidate(first, tags={**REQUIRED, "title": "A/B"}),
        candidate(second, tags={**REQUIRED, "title": "A:B"}),
    ]

    plan = plan_import(candidates, SPARSE_SCHEMES, str(tmp_path / "library"))

    assert len(plan.clips) == 1
    assert plan.clips[0].source_path == first
    assert plan.skipped[0].reason == REASON_DUPLICATE


def test_the_occupied_destination_is_matched_in_the_collision_key_space(
    schemes, tmp_path,
):
    """On a case-insensitive volume these are one file. Comparing the strings
    would let the incoming clip overwrite the existing one, which is the single
    outcome worse than a refusal."""
    library, existing = occupied_library(schemes, tmp_path)
    incoming = clip_in(tmp_path / "incoming", "Worlds Finest")

    # Rename the library's own folder to a different case, so only a
    # case-insensitive comparison can find the pair to be one destination.
    library_root = os.path.join(str(library), "cartoon network")
    original = os.path.join(str(library), "Cartoon Network")
    if os.path.isdir(original):
        os.rename(original, library_root)
    existing = build_catalog(str(library))

    plan = plan_import([candidate(incoming)], schemes, str(library),
                       existing=existing)

    assert plan.clips == ()
    assert plan.already_present


def test_an_existing_clip_carrying_an_extra_tag_is_a_conflict_not_a_match(
        tmp_path):
    """The bug this rule was written for, and worth pinning: comparing only the
    incoming tags calls a richer clip in the library "already present" and quietly
    keeps the poorer copy of the pair.

    `SPARSE_SCHEMES` leaves `block` out of both schemes, so both clips land on the
    same destination and only the tags differ.
    """
    library, existing = occupied_library(
        SPARSE_SCHEMES, tmp_path, tags={**REQUIRED, "block": "Toonami"})
    incoming = clip_in(tmp_path / "incoming", "Worlds Finest")

    plan = plan_import([candidate(incoming)], SPARSE_SCHEMES, str(library),
                       existing=existing)

    assert plan.clips == ()
    assert plan.already_present == ()
    assert plan.skipped[0].reason == REASON_DESTINATION_TAKEN
    assert "block" in plan.skipped[0].reason_text


def test_a_re_import_reports_everything_and_writes_nothing(schemes, tmp_path):
    library, existing = occupied_library(schemes, tmp_path)
    incoming = clip_in(tmp_path / "incoming", "Worlds Finest")

    plan = plan_import([candidate(incoming)], schemes, str(library),
                       existing=existing)

    assert len(plan.already_present) == 1
    assert len(plan.skipped) == 1, (
        "already-present is a skip too, so the count cannot disagree with the list")


# ---------------------------------------------------------------------------
# Free space
# ---------------------------------------------------------------------------

def test_a_copy_that_does_not_fit_is_refused_once_before_anything_is_written(
    schemes, tmp_path, monkeypatch
):
    video = clip_in(tmp_path, "Big")
    plan = plan_import([candidate(video)], schemes, str(tmp_path / "library"),
                       transfer=TRANSFER_COPY)
    monkeypatch.setattr(
        "shared.importing.shutil.disk_usage",
        lambda _root: type("Usage", (), {"free": 0})())

    with pytest.raises(ImportSpaceError) as error:
        check_free_space(plan)

    assert "library" in str(error.value)
    assert not os.path.exists(str(tmp_path / "library" / "Cartoon Network")), (
        "the refusal has to arrive before the first clip, not on clip four hundred")


def test_a_transfer_that_is_not_a_copy_needs_no_space_check(schemes, tmp_path,
                                                           monkeypatch):
    """A hard link writes a directory entry and a move relocates; neither
    duplicates the bytes."""
    video = clip_in(tmp_path, "Big")
    for transfer in (TRANSFER_LINK, TRANSFER_MOVE):
        plan = plan_import([candidate(video)], schemes, str(tmp_path / "library"),
                           transfer=transfer)
        monkeypatch.setattr(
            "shared.importing.shutil.disk_usage",
            lambda _root: pytest.fail("asked for free space on a transfer that "
                                      "does not copy"))

        assert check_free_space(plan) is None


def test_an_empty_import_asks_nothing_of_the_volume(schemes, tmp_path,
                                                    monkeypatch):
    plan = plan_import([], schemes, str(tmp_path / "library"))
    monkeypatch.setattr(
        "shared.importing.shutil.disk_usage",
        lambda _root: pytest.fail("asked about a volume with nothing to write"))

    assert check_free_space(plan) is None


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------

def landed(library, clip):
    """Where `clip` actually landed, from the plan rather than from a guess.

    Also the assertion that matters: the executor writes where the planner said,
    which is the whole of "plan then execute".
    """
    return os.path.join(str(library), *clip.destination.relative_components)


def landed_record(library, clip):
    return os.path.join(str(library), *clip.destination.record_relative_components)


def test_copying_writes_the_video_and_its_record_and_leaves_the_source(schemes,
                                                                      tmp_path):
    video = clip_in(tmp_path, "Worlds Finest")
    library = tmp_path / "library"
    plan = plan_import([candidate(video)], schemes, str(library))

    result = execute_import(plan)

    assert result.committed == 1
    assert result.cancelled is False
    assert os.path.isfile(landed(library, plan.clips[0]))
    assert os.path.isfile(landed_record(library, plan.clips[0]))
    assert os.path.exists(video), "copy must not consume the user's file"
    assert result.written == (plan.clips[0].relative_path,)


def test_the_record_that_lands_describes_the_clip_that_landed(schemes, tmp_path):
    """Invariant 12, for an imported clip rather than an exported one: after the
    run, `video present` implies `record present`."""
    video = clip_in(tmp_path, "Worlds Finest")
    library = tmp_path / "library"
    plan = plan_import([candidate(video)], schemes, str(library))

    execute_import(plan)

    catalog = build_catalog(str(library))
    assert len(catalog.clips) == 1
    assert catalog.clips[0].tag_dict() == REQUIRED
    assert catalog.clips[0].record.source == "Worlds Finest.mp4"


def cancel_after(n):
    """A `should_cancel` that trips once `n` clips have been reported."""
    seen = {"count": 0}

    def should_cancel():
        return seen["count"] >= n

    return should_cancel, seen


def test_a_cancelled_run_keeps_what_it_committed_and_says_so(schemes, tmp_path):
    videos = [clip_in(tmp_path, f"Clip {index}") for index in range(3)]
    candidates = [
        candidate(path, tags={**REQUIRED, "title": os.path.basename(path)[:-4]})
        for path in videos
    ]
    plan = plan_import(candidates, schemes, str(tmp_path / "library"))
    assert len(plan.clips) == 3
    should_cancel, _ = cancel_after(0)

    result = execute_import(plan, should_cancel=should_cancel)

    assert result.cancelled is True
    assert result.written == ()
    assert result.failed == ()


def test_a_cancel_after_one_clip_commits_that_one_and_stops(schemes, tmp_path):
    videos = [clip_in(tmp_path, f"Clip {index}") for index in range(3)]
    candidates = [
        candidate(path, tags={**REQUIRED, "title": os.path.basename(path)[:-4]})
        for path in videos
    ]
    plan = plan_import(candidates, schemes, str(tmp_path / "library"))

    should_cancel, seen = cancel_after(1)

    def count(_done, _path):
        seen["count"] += 1

    result = execute_import(plan, on_progress=count, should_cancel=should_cancel)

    assert result.cancelled is True
    assert len(result.written) == 1


def test_a_re_plan_after_a_cancellation_finishes_the_job(schemes, tmp_path):
    """The reason `already_present` is part of planning: after a partial run, the
    same folder re-planned skips what landed and completes the rest."""
    videos = [clip_in(tmp_path, f"Clip {index}") for index in range(3)]
    candidates = [
        candidate(path, tags={**REQUIRED, "title": os.path.basename(path)[:-4]})
        for path in videos
    ]
    library = tmp_path / "library"

    should_cancel, seen = cancel_after(1)

    def count(_done, _path):
        seen["count"] += 1

    execute_import(plan_import(candidates, schemes, str(library)),
                   on_progress=count, should_cancel=should_cancel)

    second = plan_import(candidates, schemes, str(library),
                         existing=build_catalog(str(library)))

    assert len(second.already_present) == 1
    assert len(second.clips) == 2

    result = execute_import(second)
    assert result.committed == 2
    assert len(build_catalog(str(library)).clips) == 3


def test_one_failing_clip_does_not_stop_the_run(schemes, tmp_path, monkeypatch):
    videos = [clip_in(tmp_path, f"Clip {index}") for index in range(3)]
    candidates = [
        candidate(path, tags={**REQUIRED, "title": os.path.basename(path)[:-4]})
        for path in videos
    ]
    plan = plan_import(candidates, schemes, str(tmp_path / "library"))
    doomed = plan.clips[1]

    def fail_on_one(source, destination, transfer):
        if source == doomed.source_path:
            raise OSError(28, "No space left on device")
        import shutil as real
        return real.copy2(source, destination)

    monkeypatch.setattr("shared.importing._transfer_file", fail_on_one)

    result = execute_import(plan)

    assert len(result.written) == 2
    assert len(result.failed) == 1
    assert result.failed[0].reason == REASON_TRANSFER_FAILED
    assert "No space left" in result.failed[0].reason_text


def test_a_clip_whose_record_cannot_be_written_is_removed_again(schemes, tmp_path,
                                                               monkeypatch):
    """Invariant 12 says `video present => record present`. Landing the video and
    failing on the record breaks it, so the video goes back."""
    video = clip_in(tmp_path, "Worlds Finest")
    library = tmp_path / "library"
    plan = plan_import([candidate(video)], schemes, str(library))

    def refuse(_record, *_a, **_k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("shared.importing.render_record_xml", refuse)

    result = execute_import(plan)

    assert result.written == ()
    assert result.failed[0].reason == REASON_RECORD_FAILED
    assert not any(library.rglob("*.mp4")), (
        "a video with no record is exactly what the invariant forbids")


def test_moving_takes_the_file_out_of_the_import_folder(schemes, tmp_path):
    video = clip_in(tmp_path, "Worlds Finest")
    plan = plan_import([candidate(video)], schemes, str(tmp_path / "library"),
                       transfer=TRANSFER_MOVE)

    result = execute_import(plan)

    assert result.committed == 1
    assert not os.path.exists(video)


def test_linking_gives_one_file_not_two(schemes, tmp_path):
    video = clip_in(tmp_path, "Worlds Finest")
    library = tmp_path / "library"
    plan = plan_import([candidate(video)], schemes, str(library),
                       transfer=TRANSFER_LINK)

    result = execute_import(plan)

    assert result.committed == 1
    assert os.path.exists(video)
    landed_path = landed(library, plan.clips[0])
    assert os.path.isfile(landed_path)
    assert os.stat(landed_path).st_ino == os.stat(video).st_ino, (
        "a hard link, not a second copy of the bytes")


def test_linking_falls_back_to_a_copy_where_there_are_no_hard_links(
    schemes, tmp_path, monkeypatch
):
    """exFAT and FAT32 have none, and a link cannot cross a volume -- so an
    import folder on a different drive must still work rather than fail every
    clip."""
    video = clip_in(tmp_path, "Worlds Finest")
    library = tmp_path / "library"
    plan = plan_import([candidate(video)], schemes, str(library),
                       transfer=TRANSFER_LINK)

    def refuse(*_a, **_k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr("shared.importing.os.link", refuse)

    result = execute_import(plan)

    assert result.committed == 1
    assert os.path.exists(video), "the fallback copies rather than moving"
    assert os.path.isfile(landed(library, plan.clips[0]))


def test_an_unknown_transfer_is_refused_at_planning_time(schemes, tmp_path):
    video = clip_in(tmp_path, "Worlds Finest")

    with pytest.raises(ValueError, match="transfer must be one of"):
        plan_import([candidate(video)], schemes, str(tmp_path / "library"),
                    transfer="teleport")


def test_progress_names_the_clip_being_written(schemes, tmp_path):
    videos = [clip_in(tmp_path, f"Clip {index}") for index in range(2)]
    candidates = [
        candidate(path, tags={**REQUIRED, "title": os.path.basename(path)[:-4]})
        for path in videos
    ]
    plan = plan_import(candidates, schemes, str(tmp_path / "library"))
    seen = []

    execute_import(plan, on_progress=lambda done, path: seen.append((done, path)))

    assert [done for done, _ in seen] == [0, 1, 2]
    assert all(isinstance(path, str) for _, path in seen)


# ---------------------------------------------------------------------------
# Untagged discovery
# ---------------------------------------------------------------------------

def test_find_videos_lists_a_video_with_no_record(tmp_path):
    """The inverse of the catalog's rule, and the whole point of it: a folder of
    somebody's rips has no records, and that is the normal case rather than
    corruption."""
    directory = tmp_path / "Rips" / "Untagged"
    directory.mkdir(parents=True)
    (directory / "Friday Night.mkv").write_bytes(b"\0")

    found = find_videos(str(tmp_path))

    assert [video.relative_path for video in found] == [
        "Rips/Untagged/Friday Night.mkv"]
    assert found[0].has_record is False


def test_find_videos_notes_which_ones_already_have_a_record(tmp_path):
    clip_in(tmp_path, "Tagged", folder="A")
    (tmp_path / "A" / "Untagged.mp4").write_bytes(b"\0")

    found = {video.relative_path: video.has_record
             for video in find_videos(str(tmp_path))}

    assert found == {"A/Tagged.mp4": True, "A/Untagged.mp4": False}


def test_find_videos_ignores_a_record_with_no_video(tmp_path):
    """Not a clip to import: there is nothing to import."""
    directory = tmp_path / "Rips"
    directory.mkdir()
    write_record(str(directory / "Orphan.cnfo"), ClipRecord(
        source="theirs.mp4", segment_index=0, start=0.0, duration=30.0,
        tags=tuple(REQUIRED.items())))

    assert find_videos(str(tmp_path)) == ()


def test_find_videos_accepts_any_container_and_rejects_other_files(tmp_path):
    directory = tmp_path / "Rips"
    directory.mkdir()
    for name in ("a.mp4", "b.mkv", "c.txt", "d.cnfo", "e"):
        (directory / name).write_bytes(b"\0")

    assert [video.relative_path for video in find_videos(str(tmp_path))] == [
        "Rips/a.mp4", "Rips/b.mkv"]


def test_find_videos_on_a_missing_folder_is_empty_not_an_error(tmp_path):
    assert find_videos(str(tmp_path / "never-existed")) == ()


def test_find_videos_descends_but_does_not_follow_directory_symlinks(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "hidden.mkv").write_bytes(b"\0")
    inside = tmp_path / "library"
    inside.mkdir()
    (inside / "real.mp4").write_bytes(b"\0")
    try:
        os.symlink(str(outside), str(inside / "linked"))
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create symlinks")

    assert [video.relative_path for video in find_videos(str(inside))] == [
        "real.mp4"]


def test_find_videos_keeps_walking_past_a_folder_it_cannot_read(tmp_path,
                                                               monkeypatch):
    """A folder someone has been filling by hand for years has locked subfolders
    in it, and refusing to return anything at all would make the walk useless."""
    (tmp_path / "fine.mp4").write_bytes(b"\0")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x.mp4").write_bytes(b"\0")
    real_scandir = os.scandir

    def failing(target):
        if os.path.abspath(str(target)) == os.path.abspath(str(locked)):
            raise PermissionError(13, "Permission denied", str(locked))
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", failing)

    assert [video.relative_path for video in find_videos(str(tmp_path))] == [
        "fine.mp4"]


def test_find_videos_stops_when_cancelled(tmp_path):
    for index in range(3):
        (tmp_path / f"{index}.mp4").write_bytes(b"\0")

    found = find_videos(str(tmp_path), should_cancel=lambda: True)

    assert found == ()


# ---------------------------------------------------------------------------
# Evidence for the value question
# ---------------------------------------------------------------------------

def test_a_value_in_the_library_is_matched_with_a_clip_count():
    """The count is the difference between "use this" and "which one?"."""
    matches = match_value("Toonami", library=library_with("block", "Toonami",
                                                          "Toonami"))

    assert len(matches) == 1
    assert matches[0].namespace == "block"
    assert matches[0].clip_count == 2
    assert matches[0].in_library is True


def test_a_value_in_several_namespaces_is_ranked_by_evidence_not_chosen():
    matches = match_value("Saturday", library=catalog_with(
        ("block", "Saturday"), ("special", "Saturday"), ("special", "Saturday")))

    assert [match.namespace for match in matches] == ["special", "block"]
    assert [match.clip_count for match in matches] == [2, 1]


def test_a_value_nobody_uses_matches_nothing():
    """Which is not permission to guess -- it means the value is new here, and the
    caller has to ask."""
    assert match_value("Brand New", library=library_with("block", "Toonami")) == ()


def test_a_vocabulary_only_hit_is_marked_as_such_and_ranks_below_the_library():
    """The distinction the two columns exist for: what the user has, against what
    they once typed."""
    vocabulary = Vocabulary()
    vocabulary.record({"special": "Ghost"})
    vocabulary.record({"block": "Real"})
    library = library_with("block", "Real", "Real", "Real")

    matches = match_value("Ghost", library=library, vocabulary=vocabulary)

    assert matches[0].namespace == "special"
    assert matches[0].in_library is False
    assert matches[0].in_vocabulary is True
    assert matches[0].clip_count == 0


def test_a_match_holding_both_columns_is_marked():
    vocabulary = Vocabulary()
    vocabulary.record({"block": "Toonami"})
    matches = match_value("Toonami", library=library_with("block", "Toonami"),
                          vocabulary=vocabulary)

    assert matches[0].in_library is True
    assert matches[0].in_vocabulary is True


def test_matching_normalizes_case_the_way_collisions_do():
    """NFC and case, which is what `normalized_validation_key` does. Punctuation
    is *not* normalized, and a test that assumed it was would be asserting a rule
    the collision check does not have."""
    library = library_with("network", "Cartoon Network")

    assert len(match_value("CARTOON NETWORK", library=library)) == 1
    assert len(match_value("Cartoon Network", library=library)) == 1
    assert match_value("Cartoon/Network", library=library) == ()


def test_the_namespace_list_never_offers_title():
    """A folder is a shared label; title is unique per clip. Offering it would let
    a whole folder be given one title."""
    from shared.importing import _suggestible_namespaces

    namespaces = _suggestible_namespaces()

    assert "title" not in namespaces
    assert len(namespaces) == 9
