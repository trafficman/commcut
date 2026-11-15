"""The Library Manager's backend: planning and executing a re-tag.

Covers `shared/retagging.py`: the pure plan (required-tag refusal,
the occupied-destination rule, the in-place/move answer) and the
transaction (record published before the video moves, step-4 failure
cleanup, clip-ID preservation, the v1 migration, and the case-only
change that is a move on both kinds of volume).
"""

import os
import re

import pytest

from conftest import windows_path_length_only
from shared.catalog import build_catalog
from shared.exporting import ExportPlanError
from shared.naming import compile_filename_scheme
from shared.paths import compile_folder_scheme
from shared.records import (
    RECORD_EXTENSION,
    ClipRecord,
    load_record,
    write_record,
)
from shared.retagging import (
    DuplicateScanResult,
    DuplicateClipGroup,
    execute_bulk_retag,
    execute_retag,
    find_duplicate_clip_ids,
    find_misaligned_clips,
    plan_bulk_retag,
    plan_retag,
    retag_is_in_place,
)

FOLDER_SCHEME = compile_folder_scheme(
    "{network}/{filler_type}/{time_period}")
FILENAME_SCHEME = compile_filename_scheme("{title}")

CANONICAL_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$")


def record_path(root, relative):
    path = os.path.join(root, *relative.split("/"))
    return os.path.splitext(path)[0] + RECORD_EXTENSION


def tags_for(title, network="CN"):
    """The four tags the two schemes need, with one title."""
    return {
        "title": title,
        "network": network,
        "filler_type": "Promo",
        "time_period": "2000s",
    }


def write_clip(root, relative, tags, clip_id=""):
    """Plant a clip — a video and its record — under `root`."""
    path = os.path.join(root, *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"video bytes")
    write_record(
        os.path.splitext(path)[0] + RECORD_EXTENSION,
        ClipRecord(
            source="compilation.mp4", segment_index=1, start=10.0,
            duration=20.0, tags=tuple(sorted(tags.items())),
            clip_id=clip_id,
        ),
    )
    return path


def write_v1_clip(root, relative, tags):
    """Plant a clip the way a v1 export left it: no clip ID."""
    path = os.path.join(root, *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"video bytes")
    record_path = os.path.splitext(path)[0] + RECORD_EXTENSION
    with open(record_path, "w", encoding="utf-8",
              newline="\n") as handle:
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
def library(tmp_path):
    """A two-clip library, walked into a catalog."""
    root = str(tmp_path / "export")
    write_clip(root, "CN/Promo/2000s/Alpha.mp4",
               tags_for("Alpha"),
               clip_id="11111111-1111-1111-1111-111111111111")
    write_clip(root, "CN/Promo/2000s/Beta.mp4",
               tags_for("Beta"),
               clip_id="22222222-2222-2222-2222-222222222222")
    catalog = build_catalog(root)
    clips = {clip.relative_path: clip for clip in catalog.clips}
    return root, catalog, clips


def bulk_plan(library, delta, *relative_paths):
    return plan_bulk_retag(
        delta=delta,
        clips=[library[2][path] for path in relative_paths],
        folder_scheme=FOLDER_SCHEME,
        filename_scheme=FILENAME_SCHEME,
        catalog=library[1])


def clip_of(library, relative):
    return library[2][relative]


def plan(library, clip, tags):
    return plan_retag(
        clip=clip, tags=tags, folder_scheme=FOLDER_SCHEME,
        filename_scheme=FILENAME_SCHEME, catalog=library[1])


def test_a_missing_required_tag_is_refused(library):
    clip = clip_of(library, "CN/Promo/2000s/Alpha.mp4")
    with pytest.raises(ExportPlanError, match="missing required tags"):
        plan(library, clip, {"network": "CN"})


def test_a_destination_another_clip_holds_is_refused_naming_it(library):
    clip = clip_of(library, "CN/Promo/2000s/Alpha.mp4")
    with pytest.raises(ExportPlanError, match="Beta.mp4") as raised:
        plan(library, clip, tags_for("Beta"))
    assert "CN/Promo/2000s/Beta.mp4" in str(raised.value)


@windows_path_length_only
def test_a_destination_the_platform_cannot_address_is_refused(library):
    """The clip's own root is the export root, so the length of the path is
    known before anything moves."""
    clip = clip_of(library, "CN/Promo/2000s/Alpha.mp4")
    deep = compile_folder_scheme(
        "{network}/{filler_type}/{time_period}/{title}/{title}/{title}")

    with pytest.raises(ExportPlanError, match="too long"):
        plan_retag(
            clip=clip, tags=tags_for("T" * 200),
            folder_scheme=deep, filename_scheme=FILENAME_SCHEME,
            catalog=library[1],
        )


@windows_path_length_only
def test_the_align_scan_reports_a_too_long_destination_as_an_error(tmp_path):
    """Not a mismatch: a clip whose destination cannot be addressed cannot be
    aligned either, and offering a fix that cannot work is worse than saying
    so."""
    from shared.catalog import build_catalog
    from shared.retagging import find_misaligned_clips

    root = str(tmp_path / "export")
    write_clip(root, "CN/Promo/2000s/Long.mp4", tags_for("T" * 200),
               clip_id="33333333-3333-3333-3333-333333333333")
    catalog = build_catalog(root)
    deep = compile_folder_scheme(
        "{network}/{filler_type}/{time_period}/{title}/{title}/{title}")

    result = find_misaligned_clips(catalog, deep, FILENAME_SCHEME)

    assert result.misaligned == ()
    assert result.errors
    assert "too long" in result.errors[0][1]



def test_saving_unchanged_tags_rewrites_the_record_in_place(library):
    root, catalog, clips = library
    clip = clips["CN/Promo/2000s/Alpha.mp4"]
    tags = tags_for("Alpha")

    destination = plan(library, clip, tags)
    assert retag_is_in_place(clip, destination)

    execute_retag(clip, destination, tags)

    assert os.path.isfile(os.path.join(root, "CN", "Promo", "2000s",
                                       "Alpha.mp4"))
    record = load_record(
        os.path.join(root, "CN", "Promo", "2000s", "Alpha.cnfo"))
    assert record.tag_dict == tags
    assert record.clip_id == "11111111-1111-1111-1111-111111111111"


def test_the_record_is_published_before_the_video_moves(
        library, monkeypatch):
    root, catalog, clips = library
    clip = clips["CN/Promo/2000s/Alpha.mp4"]
    tags = tags_for("Alpha Two")
    destination = plan(library, clip, tags)
    record_destination = os.path.join(
        root, "CN", "Promo", "2000s", "Alpha Two.cnfo")

    seen = []
    real_replace = os.replace

    def spying_replace(source, target):
        if os.path.abspath(source) == os.path.abspath(clip.path):
            seen.append(os.path.isfile(record_destination))
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", spying_replace)
    execute_retag(clip, destination, tags)

    assert seen == [True]


def test_a_failed_video_move_restores_the_old_pair(library, monkeypatch):
    root, catalog, clips = library
    clip = clips["CN/Promo/2000s/Alpha.mp4"]
    tags = tags_for("Alpha Two")
    destination = plan(library, clip, tags)
    record_destination = os.path.join(
        root, "CN", "Promo", "2000s", "Alpha Two.cnfo")

    real_replace = os.replace

    def failing_replace(source, target):
        if os.path.abspath(source) == os.path.abspath(clip.path):
            raise OSError("the volume is full")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        execute_retag(clip, destination, tags)

    assert not os.path.exists(record_destination)
    assert os.path.isfile(clip.path)
    assert load_record(clip.record_path).tag_dict == tags_for("Alpha")


def test_a_move_removes_the_old_record_and_keeps_the_clip_id(library):
    root, catalog, clips = library
    clip = clips["CN/Promo/2000s/Alpha.mp4"]
    tags = tags_for("Alpha Two")
    destination = plan(library, clip, tags)

    execute_retag(clip, destination, tags)

    assert not os.path.exists(clip.record_path)
    record = load_record(
        os.path.join(root, "CN", "Promo", "2000s", "Alpha Two.cnfo"))
    assert record.tag_dict == tags
    assert record.clip_id == "11111111-1111-1111-1111-111111111111"
    assert record.source == "compilation.mp4"
    assert (record.segment_index, record.start,
            record.duration) == (1, 10.0, 20.0)


def test_a_v1_record_is_assigned_an_id(library):
    root, catalog, clips = library
    write_v1_clip(root, "Old/Promo/2000s/No ID.mp4",
                  tags_for("No ID"))
    catalog = build_catalog(root)
    clip = next(clip for clip in catalog.clips
                if clip.relative_path == "Old/Promo/2000s/No ID.mp4")
    assert clip.record.clip_id == ""

    tags = tags_for("No ID", network="Old")
    destination = plan_retag(
        clip=clip, tags=tags, folder_scheme=FOLDER_SCHEME,
        filename_scheme=FILENAME_SCHEME, catalog=catalog)
    execute_retag(clip, destination, tags)

    record = load_record(
        os.path.join(root, "Old", "Promo", "2000s", "No ID.cnfo"))
    assert CANONICAL_ID.fullmatch(record.clip_id)


def test_a_case_only_change_is_a_move(library):
    root, catalog, clips = library
    clip = clips["CN/Promo/2000s/Alpha.mp4"]
    tags = tags_for("Alpha", network="cn")
    destination = plan(library, clip, tags)

    assert not retag_is_in_place(clip, destination)
    assert destination.relative_components == (
        "cn", "Promo", "2000s", "Alpha.mp4")

    execute_retag(clip, destination, tags)

    assert os.path.isfile(
        os.path.join(root, "cn", "Promo", "2000s", "Alpha.mp4"))
    record = load_record(
        os.path.join(root, "cn", "Promo", "2000s", "Alpha.cnfo"))
    assert record.tag_dict == tags


def test_find_duplicate_clip_ids_finds_groups(library):
    root, catalog, clips = library
    # Add a third clip that shares Alpha's clip ID.
    write_clip(root, "CN/Promo/2000s/Gamma.mp4",
               tags_for("Gamma"),
               clip_id="11111111-1111-1111-1111-111111111111")
    catalog = build_catalog(root)

    result = find_duplicate_clip_ids(catalog)

    assert not result.cancelled
    assert result.clips_found == 3
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.clip_id == "11111111-1111-1111-1111-111111111111"
    assert len(group.clips) == 2


def test_find_duplicate_clip_ids_excludes_empty_ids(library):
    root, catalog, clips = library
    # v1 records with empty clip IDs should not form a duplicate group.
    write_v1_clip(root, "Old/Promo/2000s/No ID.mp4", tags_for("No ID"))
    write_v1_clip(root, "Old/Promo/2000s/No ID Two.mp4",
                  tags_for("No ID Two"))

    result = find_duplicate_clip_ids(build_catalog(root))

    assert not result.cancelled
    assert len(result.groups) == 0


def test_find_duplicate_clip_ids_returns_none_for_clean_library(library):
    root, catalog, clips = library

    result = find_duplicate_clip_ids(catalog)

    assert not result.cancelled
    assert result.clips_found == 2
    assert len(result.groups) == 0


# ---------------------------------------------------------------------------
# Bulk re-tagging
# ---------------------------------------------------------------------------

ALPHA = "CN/Promo/2000s/Alpha.mp4"
BETA = "CN/Promo/2000s/Beta.mp4"


def test_plan_bulk_retag_applies_a_delta_to_every_selected_clip(library):
    result = bulk_plan(library, {"network": "Old"}, ALPHA, BETA)

    assert result.skipped == 0
    assert result.moves == 2
    assert result.in_place == 0
    for item in result.items:
        assert item.tags["network"] == "Old"
        assert item.tags["title"] in {"Alpha", "Beta"}


def test_plan_bulk_retag_skips_no_ops(library):
    result = bulk_plan(library, {"network": "CN"}, ALPHA, BETA)

    assert result.skipped == 2
    assert result.items == ()


def test_plan_bulk_retag_carries_untouched_fields_forward(library):
    result = bulk_plan(library, {"special": "Bump"}, ALPHA)

    item = result.items[0]
    # Untouched network/type/period ride along from the clip's record.
    assert item.tags["network"] == "CN"
    assert item.tags["time_period"] == "2000s"
    assert item.tags["special"] == "Bump"
    # In place: special is not part of either scheme.
    assert result.in_place == 1
    assert result.moves == 0


def test_plan_bulk_retag_none_removes_the_tag(library):
    root, catalog, clips = library
    write_clip(root, "CN/Promo/2000s/Special.mp4",
               {**tags_for("Special"), "special": "Bump"},
               clip_id="33333333-3333-3333-3333-333333333333")
    catalog = build_catalog(root)
    catalog_clips = {clip.relative_path: clip for clip in catalog.clips}
    result = plan_bulk_retag(
        delta={"special": None},
        clips=[catalog_clips["CN/Promo/2000s/Special.mp4"]],
        folder_scheme=FOLDER_SCHEME, filename_scheme=FILENAME_SCHEME,
        catalog=catalog)

    assert result.items[0].tags.get("special") is None


def test_plan_bulk_retag_refuses_the_batch_on_a_missing_required_tag(library):
    with pytest.raises(ExportPlanError, match="missing the required tag"):
        bulk_plan(library, {"network": None}, ALPHA, BETA)


def test_plan_bulk_retag_refuses_the_batch_on_a_blank_required_value(library):
    with pytest.raises(ExportPlanError, match="missing the required tag"):
        bulk_plan(library, {"time_period": "   "}, ALPHA)


def test_plan_bulk_retag_refuses_the_whole_batch_before_writing(library, tmp_path):
    """The fatal check happens against the merged records, before the
    window even offers to run: a vital tag cleared in bulk must not be
    writable to any clip, not just some of them."""
    with pytest.raises(ExportPlanError):
        bulk_plan(library, {"time_period": None}, ALPHA, BETA)
    assert load_record(record_path(library[0], ALPHA)).tag_dict == tags_for("Alpha")


def test_plan_bulk_retag_refuses_an_occupied_destination(library):
    """The destination a clip the batch did not select already holds:
    refused per clip, its pair left exactly where it was."""
    result = bulk_plan(library, {"title": "Beta"}, ALPHA)

    assert result.items == ()
    assert result.refused
    assert "Alpha" in result.refused[0][0]
    assert "Beta" in result.refused[0][1]


def test_execute_bulk_retag_moves_clips_and_preserves_their_ids(library):
    root, catalog, clips = library
    plan = bulk_plan(library, {"network": "Old"}, ALPHA, BETA)

    result = execute_bulk_retag(plan)

    assert len(result.written) == 2
    assert result.moved == 2
    assert result.in_place == 0
    for title in ("Alpha", "Beta"):
        path = os.path.join(root, "Old", "Promo", "2000s",
                            f"{title}.mp4")
        assert os.path.isfile(path)
        record = load_record(os.path.splitext(path)[0] + RECORD_EXTENSION)
        assert record.tag_dict["network"] == "Old"
    # The old CN/… records are gone; the IDs travelled with the clips.
    assert not os.path.exists(record_path(root, ALPHA))
    assert not os.path.exists(record_path(root, BETA))


def test_execute_bulk_retag_refuses_a_destination_that_appeared_since_the_plan(library, tmp_path):
    """The catalog index only sees clips; a file that appeared at the
    destination after the walk refuses the move rather than being
    overwritten."""
    root, catalog, clips = library
    plan = bulk_plan(library, {"network": "Old"}, ALPHA, BETA)

    destination = os.path.join(root, "Old", "Promo", "2000s")
    os.makedirs(destination, exist_ok=True)
    stray = os.path.join(destination, "Alpha.mp4")
    with open(stray, "wb") as handle:
        handle.write(b"stray bytes")

    result = execute_bulk_retag(plan)

    refused_labels = [label for label, _ in result.refused]
    assert refused_labels and "Alpha" in refused_labels[0]
    with open(stray, "rb") as handle:
        assert handle.read() == b"stray bytes"
    # Beta, whose destination was free, still moved.
    assert os.path.isfile(os.path.join(destination, "Beta.mp4"))
    assert result.written == ("Old/Promo/2000s/Beta.mp4",)


def test_execute_bulk_retag_refuses_an_occupied_record_destination(library):
    root, catalog, clips = library
    plan = bulk_plan(library, {"network": "Old"}, ALPHA, BETA)

    destination = os.path.join(root, "Old", "Promo", "2000s")
    os.makedirs(destination, exist_ok=True)
    stray = os.path.join(destination, "Alpha.cnfo")
    with open(stray, "wb") as handle:
        handle.write(b"stray record")

    result = execute_bulk_retag(plan)

    refused_labels = [label for label, _ in result.refused]
    assert refused_labels and "Alpha" in refused_labels[0]
    with open(stray, "rb") as handle:
        assert handle.read() == b"stray record"
    assert os.path.isfile(os.path.join(destination, "Beta.mp4"))


def test_execute_bulk_retag_cancels_at_a_clip_boundary(tmp_path):
    root = str(tmp_path / "export")
    paths = [f"CN/Promo/2000s/Clip{i}.mp4" for i in range(5)]
    for i, path in enumerate(paths):
        write_clip(root, path, tags_for(f"Clip{i}"),
                   clip_id=f"55555555-5555-5555-5555-{i:012d}")
    catalog = build_catalog(root)
    clips = [clip for clip in catalog.clips
             if clip.relative_path in paths]
    plan = plan_bulk_retag(
        delta={"network": "Old"}, clips=clips,
        folder_scheme=FOLDER_SCHEME, filename_scheme=FILENAME_SCHEME,
        catalog=catalog)

    calls = {"count": 0}

    def cancel_after_first():
        calls["count"] += 1
        return calls["count"] > 1

    result = execute_bulk_retag(plan, should_cancel=cancel_after_first)

    assert result.cancelled
    assert len(result.written) == 1
    assert result.written[0].startswith("Old/Promo/2000s/")


def test_execute_retag_refuses_to_overwrite_an_off_catalog_file(library):
    root, catalog, clips = library
    clip = clips[ALPHA]
    tags = {**tags_for("Alpha"), "network": "Old"}
    destination = plan_retag(
        clip=clip, tags=tags, folder_scheme=FOLDER_SCHEME,
        filename_scheme=FILENAME_SCHEME, catalog=catalog)

    stray = os.path.join(root, "Old", "Promo", "2000s", "Alpha.mp4")
    os.makedirs(os.path.dirname(stray), exist_ok=True)
    with open(stray, "wb") as handle:
        handle.write(b"stray bytes")

    with pytest.raises(ExportPlanError, match="different file"):
        execute_retag(clip, destination, tags)

    with open(stray, "rb") as handle:
        assert handle.read() == b"stray bytes"


def test_execute_retag_case_only_rename_still_works_with_the_guard(library):
    root, catalog, clips = library
    clip = clips[ALPHA]
    tags = {**tags_for("Alpha"), "network": "cn"}
    destination = plan_retag(
        clip=clip, tags=tags, folder_scheme=FOLDER_SCHEME,
        filename_scheme=FILENAME_SCHEME, catalog=catalog)

    execute_retag(clip, destination, tags)

    assert os.path.isfile(
        os.path.join(root, "cn", "Promo", "2000s", "Alpha.mp4"))


@windows_path_length_only
def test_plan_bulk_retag_reports_too_long_destinations_as_refusals(tmp_path):
    root = str(tmp_path / "export")
    write_clip(root, "CN/Promo/2000s/Long.mp4", tags_for("T" * 200),
               clip_id="66666666-6666-6666-6666-666666666666")
    catalog = build_catalog(root)
    clip = next(iter(catalog.clips))
    deep = compile_folder_scheme(
        "{network}/{filler_type}/{time_period}/{title}/{title}/{title}")

    result = plan_bulk_retag(
        delta={"special": "Bump"}, clips=[clip],
        folder_scheme=deep, filename_scheme=FILENAME_SCHEME,
        catalog=catalog)

    assert result.items == ()
    assert result.refused
