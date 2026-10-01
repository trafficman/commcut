"""Tests for named-export settings, destination planning, and preflight."""

import json
import os

import pytest

from shared.exporting import (
    DEFAULT_FILE_NAMING_SCHEME,
    EXPORT_FOLDER_NAME,
    FILE_NAMING_SCHEME_KEY,
    FOLDER_ORGANIZATION_SCHEME_KEY,
    ExportPlanError,
    ExportSchemes,
    export_folder,
    load_export_schemes,
    model_with_tag_locks,
    plan_export,
    preflight_export_plan,
)
from shared.paths import DEFAULT_FOLDER_SCHEME
from shared.records import RECORD_EXTENSION
from shared.segments import SegmentModel


# ---------------------------------------------------------------------------
# Fixtures and settings snapshot
# ---------------------------------------------------------------------------

@pytest.fixture
def required_tags():
    return {
        "title": "Worlds Finale",
        "network": "Cartoon Network",
        "filler_type": "Promo",
        "time_period": "2000s",
    }


@pytest.fixture
def schemes():
    return ExportSchemes(
        file_scheme="{title}",
        folder_scheme=DEFAULT_FOLDER_SCHEME,
    )


def test_the_export_folder_follows_the_install_root(tmp_path, monkeypatch):
    """Not a project root derived from __file__: frozen, that is PyInstaller's
    extraction folder, which is deleted on exit along with every clip in it.

    This was `shared/ffmpeg.py:_export_dir`, then a literal in the editor, and
    about to be a third copy in the library walk. It is one function now, and
    this is the guard on the property all three spellings had in common.
    """
    monkeypatch.setattr("shared.exporting.install_root", lambda: str(tmp_path))

    assert export_folder() == os.path.join(str(tmp_path), EXPORT_FOLDER_NAME)


def test_the_export_folder_is_not_the_import_folders_neighbour_by_accident():
    """The import folder is a source-video folder and this is a clip folder.
    They sit side by side under the install root, and a mixed-up constant would
    put a library walk's records into the folder the picker offers as sources."""
    from shared.sources import IMPORT_FOLDER_NAME

    assert EXPORT_FOLDER_NAME != IMPORT_FOLDER_NAME
    assert export_folder().rsplit(os.sep, 1)[-1] == "export"


def make_model(duration=10.0, tags_list=None):
    tags_list = tags_list or [{}]
    starts = [index * (duration / len(tags_list)) for index in range(len(tags_list))]
    return SegmentModel(
        source="compilation.mp4",
        duration=duration,
        segments=[
            {
                "start": start,
                "ignored": False,
                "tags": tags,
            }
            for start, tags in zip(starts, tags_list)
        ],
    )


def test_load_export_schemes_uses_defaults_when_file_is_missing(tmp_path):
    loaded = load_export_schemes(str(tmp_path / "missing.json"))

    assert loaded.file_scheme == DEFAULT_FILE_NAMING_SCHEME
    assert loaded.folder_scheme == DEFAULT_FOLDER_SCHEME


def test_load_export_schemes_validates_both_values(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({
            FILE_NAMING_SCHEME_KEY: "{title}",
            FOLDER_ORGANIZATION_SCHEME_KEY: (
                "{network}/{type}/{time_period}"
            ),
        }),
        encoding="utf-8",
    )

    loaded = load_export_schemes(str(settings_path))

    assert loaded.file_scheme == "{title}"
    assert loaded.folder_scheme == "{network}/{type}/{time_period}"


# ---------------------------------------------------------------------------
# Model snapshot and tag locks
# ---------------------------------------------------------------------------

def test_model_with_tag_locks_materializes_only_fully_untedited_segments():
    model = SegmentModel(
        duration=4.0,
        segments=[
            {"start": 0.0, "ignored": False, "tags": {}},
            {"start": 2.0, "ignored": False, "tags": {"title": "Existing"}},
        ],
    )

    snapshot = model_with_tag_locks(
        model,
        {"network": "Cartoon Network", "filler_type": "Promo"},
    )

    assert snapshot.segments[0]["tags"] == {
        "network": "Cartoon Network",
        "filler_type": "Promo",
    }
    assert snapshot.segments[1]["tags"] == {"title": "Existing"}
    assert model.segments[0]["tags"] == {}


# ---------------------------------------------------------------------------
# Destination planning
# ---------------------------------------------------------------------------

def test_plan_export_returns_named_relative_components(schemes, required_tags, tmp_path):
    model = make_model(tags_list=[{**required_tags, "block": "Toonami"}])

    plan = plan_export(model, schemes, str(tmp_path / "library"))

    assert len(plan.clips) == 1
    assert plan.clips[0].relative_components == (
        "Cartoon Network",
        "Blocks",
        "Toonami",
        "Promo",
        "2000s",
        "Worlds Finale.mp4",
    )
    assert not (tmp_path / "library").exists()


def test_plan_export_skips_ignored_segments_before_tag_validation(schemes):
    model = SegmentModel(
        duration=4.0,
        segments=[
            {"start": 0.0, "ignored": True, "tags": {}},
            {
                "start": 2.0,
                "ignored": False,
                "tags": {
                    "title": "Keep",
                    "network": "Network",
                    "filler_type": "Promo",
                    "time_period": "2000s",
                },
            },
        ],
    )

    plan = plan_export(model, schemes, "unused-library")

    assert [clip.segment_index for clip in plan.clips] == [1]


@pytest.mark.parametrize("missing", ["title", "network", "filler_type", "time_period"])
def test_plan_export_requires_all_base_record_tags(
    schemes,
    required_tags,
    tmp_path,
    missing,
):
    tags = {key: value for key, value in required_tags.items() if key != missing}
    model = make_model(tags_list=[tags])

    with pytest.raises(ExportPlanError, match=f"missing required tags: {missing}"):
        plan_export(model, schemes, str(tmp_path / "library"))


def test_plan_export_rejects_invalid_duration(schemes, required_tags, tmp_path):
    model = SegmentModel(
        duration=0.0,
        segments=[{"start": 0.0, "ignored": False, "tags": required_tags}],
    )

    with pytest.raises(ExportPlanError, match="invalid duration"):
        plan_export(model, schemes, str(tmp_path / "library"))


@pytest.mark.parametrize(
    ("first_title", "second_title"),
    [
        ("A/B", "A-B"),
        ("Café", "Cafe\u0301"),
        ("Same", "same"),
    ],
)
def test_plan_export_rejects_normalized_destination_collisions(
    schemes,
    required_tags,
    tmp_path,
    first_title,
    second_title,
):
    model = make_model(
        tags_list=[
            {**required_tags, "title": first_title},
            {**required_tags, "title": second_title},
        ],
    )

    with pytest.raises(ExportPlanError, match="same destination"):
        plan_export(model, schemes, str(tmp_path / "library"))


def test_plan_export_rejects_existing_destination(
    schemes,
    required_tags,
    tmp_path,
):
    root = tmp_path / "library"
    destination = root / "Cartoon Network" / "Promo" / "2000s" / "Worlds Finale.mp4"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")

    with pytest.raises(ExportPlanError, match="already exists"):
        plan_export(make_model(tags_list=[required_tags]), schemes, str(root))


def make_clip(segment_index=0, start=0.0, duration=1.0, components=None,
              record_components=None, tags=()):
    """A hand-built clip, for the tests that exercise the preflight directly.

    `plan_export` is the only thing that should produce a real plan; these tests
    need to hand it one that `plan_export` could never emit.
    """
    from shared.exporting import PlannedExportClip

    if components is None:
        components = ("Network", "Clip.mp4")
    if record_components is None:
        record_components = (
            *components[:-1],
            os.path.splitext(components[-1])[0] + RECORD_EXTENSION,
        )
    return PlannedExportClip(
        segment_index=segment_index,
        start=start,
        duration=duration,
        relative_components=components,
        tags=tags,
        record_relative_components=record_components,
    )


def test_preflight_rejects_forged_unsafe_plan(tmp_path):
    from shared.exporting import ExportPlan

    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(make_clip(components=("..", "outside.mp4")),),
    )

    with pytest.raises(ExportPlanError, match="unsafe component"):
        preflight_export_plan(plan)


def test_preflight_rejects_forged_empty_and_duplicate_plans(tmp_path):
    from shared.exporting import ExportPlan

    with pytest.raises(ExportPlanError, match="contains no clips"):
        preflight_export_plan(ExportPlan(
            export_root=str(tmp_path / "library"),
            clips=(),
        ))

    duplicate = make_clip()
    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(duplicate, duplicate),
    )
    with pytest.raises(ExportPlanError, match="more than once"):
        preflight_export_plan(plan)


def test_preflight_rejects_destination_used_as_parent_directory(tmp_path):
    from shared.exporting import ExportPlan

    parent_file = make_clip()
    child_file = make_clip(
        segment_index=1,
        start=1.0,
        components=("Network", "Clip.mp4", "Child.mp4"),
    )
    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(parent_file, child_file),
    )

    with pytest.raises(ExportPlanError, match="parent directory"):
        preflight_export_plan(plan)


# ---------------------------------------------------------------------------
# The clip record's destination
# ---------------------------------------------------------------------------

def test_the_plan_carries_the_tags_the_record_is_written_from(schemes, tmp_path):
    """The record and the filename have to come from one canonicalization, or
    they can disagree about what a clip is."""
    root = tmp_path / "library"
    tags = {
        "title": "Worlds Finale",
        "network": "Cartoon Network",
        "filler_type": "Promo",
        "time_period": "2000s",
        "block": "Toonami",
    }

    plan = plan_export(make_model(tags_list=[tags]), schemes, str(root))

    assert plan.clips[0].tags == tuple(sorted(tags.items()))


def test_the_record_sits_beside_the_clip_it_describes(schemes, required_tags,
                                                      tmp_path):
    root = tmp_path / "library"

    plan = plan_export(make_model(tags_list=[required_tags]), schemes, str(root))
    clip = plan.clips[0]

    assert clip.record_relative_components[:-1] == clip.relative_components[:-1]
    assert clip.record_relative_components[-1] == "Worlds Finale" + RECORD_EXTENSION
    assert clip.record_relative_path == clip.relative_path.replace(
        ".mp4", RECORD_EXTENSION
    )


def test_a_skipped_clip_still_carries_its_record_destination(
    schemes, required_tags, tmp_path
):
    """A resume never revisits a skipped clip, so its record destination has to
    be on the plan entry -- it is the first run that wrote it."""
    root = tmp_path / "library"
    model = make_model(
        duration=20.0,
        tags_list=[required_tags, {**required_tags, "title": "Second"}],
    )
    first = plan_export(
        make_model(tags_list=[required_tags]), schemes, str(root)
    ).clips[0].relative_path

    plan = plan_export(model, schemes, str(root), skip_destinations=(first,))

    assert len(plan.skipped) == 1
    assert plan.skipped[0].record_relative_components[-1].endswith(RECORD_EXTENSION)
    assert plan.clips[0].record_relative_components[-1].endswith(RECORD_EXTENSION)


def test_a_stem_the_record_cannot_name_refuses_the_batch(tmp_path):
    """`.cnfo` is one character longer than `.mp4`, so there is exactly one stem
    length that is a legal video filename and an illegal record name. Refusing
    it here means the batch reports it before anything encodes."""
    root = tmp_path / "library"
    stem = "T" * 251
    model = make_model(tags_list=[{
        "title": stem,
        "network": "Cartoon Network",
        "filler_type": "Promo",
        "time_period": "2000s",
    }])
    schemes = ExportSchemes(file_scheme="{title}",
                            folder_scheme=DEFAULT_FOLDER_SCHEME)

    assert len(stem + ".mp4") <= 255, "the fixture must be legal as a video name"

    with pytest.raises(ExportPlanError, match="Clip record filename exceeds"):
        plan_export(model, schemes, str(root))


def test_a_stem_that_fits_both_names_is_accepted(tmp_path):
    root = tmp_path / "library"
    stem = "T" * 250
    model = make_model(tags_list=[{
        "title": stem,
        "network": "Cartoon Network",
        "filler_type": "Promo",
        "time_period": "2000s",
    }])
    schemes = ExportSchemes(file_scheme="{title}",
                            folder_scheme=DEFAULT_FOLDER_SCHEME)

    plan = plan_export(model, schemes, str(root))

    assert plan.clips[0].record_relative_components[-1].endswith(RECORD_EXTENSION)


def test_preflight_rejects_a_record_written_outside_its_clips_folder(tmp_path):
    """A record beside nothing is a record nothing will ever find."""
    from shared.exporting import ExportPlan

    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(make_clip(record_components=("Elsewhere", "Clip" + RECORD_EXTENSION)),),
    )

    with pytest.raises(ExportPlanError, match="record is not beside its clip"):
        preflight_export_plan(plan)


def test_preflight_rejects_a_record_named_with_the_wrong_extension(tmp_path):
    from shared.exporting import ExportPlan

    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(make_clip(record_components=("Network", "Clip.mp4")),),
    )

    with pytest.raises(ExportPlanError, match=f"does not end in {RECORD_EXTENSION}"):
        preflight_export_plan(plan)


def test_preflight_rejects_an_unsafe_record_component(tmp_path):
    from shared.exporting import ExportPlan

    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(make_clip(record_components=("Network", "...")),),
    )

    with pytest.raises(ExportPlanError, match="unsafe component"):
        preflight_export_plan(plan)


def test_preflight_rejects_a_record_name_the_policy_would_rewrite(tmp_path):
    """`CON` becomes `_CON` on every render, so a plan asking for `CON` is
    asking for a file the export would never write."""
    from shared.exporting import ExportPlan

    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(make_clip(record_components=("Network", "CON")),),
    )

    with pytest.raises(ExportPlanError, match="unsanitized path component"):
        preflight_export_plan(plan)


def test_preflight_rejects_a_plan_entry_that_is_not_a_clip(tmp_path):
    from shared.exporting import ExportPlan

    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=("not a clip",),
    )

    with pytest.raises(ExportPlanError, match="not a PlannedExportClip"):
        preflight_export_plan(plan)


def test_plan_export_rejects_existing_file_used_as_parent(
    schemes,
    required_tags,
    tmp_path,
):
    root = tmp_path / "library"
    blocked_parent = root / "Cartoon Network"
    blocked_parent.parent.mkdir(parents=True)
    blocked_parent.write_bytes(b"not a directory")

    with pytest.raises(ExportPlanError, match="parent is not a directory"):
        plan_export(make_model(tags_list=[required_tags]), schemes, str(root))


def test_plan_export_rejects_invalid_model_shape(tmp_path, schemes):
    model = SegmentModel(
        duration=5.0,
        segments=[{
            "start": 1.0,
            "ignored": 0,
            "tags": None,
        }],
    )

    with pytest.raises(ExportPlanError, match="Invalid segment model"):
        plan_export(model, schemes, str(tmp_path / "library"))


def test_plan_export_converts_numeric_overflow_to_typed_error(
    tmp_path,
    schemes,
):
    model = SegmentModel(
        duration=10 ** 10000,
        segments=[{"start": 0, "ignored": False, "tags": {}}],
    )

    with pytest.raises(ExportPlanError, match="duration must be a finite"):
        plan_export(model, schemes, str(tmp_path / "library"))


def test_plan_export_checks_complete_relative_path_byte_limit(
    tmp_path,
):
    year_components = "/".join(["{year}"] * 16)
    text = f"{year_components}/{{network}}/{{type}}/{{time_period}}"
    model = make_model(tags_list=[{
        "title": "X" * 250,
        "network": "N",
        "filler_type": "T",
        "time_period": "P",
        "year": "Y" * 245,
    }])

    with pytest.raises(ExportPlanError, match="relative destination exceeds 4096"):
        plan_export(model, ExportSchemes("{title}", text), str(tmp_path / "library"))


def test_plan_export_rejects_symlinked_root_ancestor(
    tmp_path,
    schemes,
    required_tags,
):
    target = tmp_path / "outside"
    real_directory = target / "real"
    real_directory.mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks unavailable: {error}")

    with pytest.raises(ExportPlanError, match="symbolic link or reparse point"):
        plan_export(
            make_model(tags_list=[required_tags]),
            schemes,
            str(link / "real" / "library"),
        )


def test_plan_export_rejects_empty_keep_set(schemes, tmp_path):
    model = SegmentModel(
        duration=2.0,
        segments=[{"start": 0.0, "ignored": True, "tags": {}}],
    )

    with pytest.raises(ExportPlanError, match="no non-ignored clips"):
        plan_export(model, schemes, str(tmp_path / "library"))


def test_plan_export_normalizes_alias_tag_keys(schemes, required_tags, tmp_path):
    tags = {
        "title": required_tags["title"],
        "network": required_tags["network"],
        "type": required_tags["filler_type"],
        "time_period": required_tags["time_period"],
    }

    plan = plan_export(
        make_model(tags_list=[tags]),
        schemes,
        str(tmp_path / "library"),
    )

    assert plan.clips[0].relative_components[-1] == "Worlds Finale.mp4"


# ---------------------------------------------------------------------------
# Resume: skipping what a cancelled run already wrote
# ---------------------------------------------------------------------------

def planned_path(model, schemes, root, index=0):
    """The relative path segment `index` resolves to, '/' joined."""
    plan = plan_export(model, schemes, str(root))
    return plan.clips[index].relative_path


def test_skipped_clips_leave_the_plan_but_are_reported(
    schemes,
    required_tags,
    tmp_path,
):
    """The resume path. A cancelled run's clips are excluded from the work,
    not dropped silently: the caller names them in the dialog that offers to
    resume, so they have to come back in plan.skipped."""
    model = make_model(
        tags_list=[
            {**required_tags, "title": "First"},
            {**required_tags, "title": "Second"},
        ],
    )
    root = tmp_path / "library"
    first = planned_path(model, schemes, root)

    plan = plan_export(model, schemes, str(root), skip_destinations=[first])

    assert [clip.relative_components[-1] for clip in plan.clips] == ["Second.mp4"]
    assert [clip.relative_path for clip in plan.skipped] == [first]
    assert [clip.segment_index for clip in plan.skipped] == [0]


def test_a_skipped_destination_may_already_exist(
    schemes,
    required_tags,
    tmp_path,
):
    """The whole reason the skip exists. The clipped-up file from the
    cancelled run is on disk, and the existing-destination check must not
    refuse the batch over it -- it is excluded from plan.clips before
    preflight ever sees it."""
    model = make_model(
        tags_list=[
            {**required_tags, "title": "First"},
            {**required_tags, "title": "Second"},
        ],
    )
    root = tmp_path / "library"
    first = planned_path(model, schemes, root)
    committed = root / first
    committed.parent.mkdir(parents=True)
    committed.write_bytes(b"already exported")

    plan = plan_export(model, schemes, str(root), skip_destinations=[first])

    assert [clip.relative_components[-1] for clip in plan.clips] == ["Second.mp4"]


def test_an_unskipped_existing_destination_still_refuses(
    schemes,
    required_tags,
    tmp_path,
):
    """Resume must not weaken no-clobber. A file the caller did not claim to
    have written is still a conflict, so a re-tagged clip can never be
    silently skipped or silently overwritten."""
    model = make_model(
        tags_list=[
            {**required_tags, "title": "First"},
            {**required_tags, "title": "Second"},
        ],
    )
    root = tmp_path / "library"
    first = planned_path(model, schemes, root)
    committed = root / first
    committed.parent.mkdir(parents=True)
    committed.write_bytes(b"already exported")

    with pytest.raises(ExportPlanError, match="already exists"):
        plan_export(
            model,
            schemes,
            str(root),
            skip_destinations=["Network/Some Other Clip.mp4"],
        )


def test_a_skip_does_not_suppress_a_validation_error(tmp_path, required_tags):
    """A skip is a destination decision, not a validation escape.

    One segment is named in the skip list and is fine; the other has an
    incomplete record. The batch must refuse and name the bad one, rather than
    quietly planning the segment the skip covered.
    """
    schemes = ExportSchemes("{title}", "{network}/{filler_type}/{time_period}")
    root = tmp_path / "library"
    skippable = {**required_tags, "title": "First"}
    incomplete = {
        key: value
        for key, value in required_tags.items()
        if key != "filler_type"
    }
    incomplete["title"] = "Second"
    model = make_model(
        duration=4.0,
        tags_list=[skippable, incomplete],
    )
    destination = planned_path(make_model(tags_list=[skippable]), schemes, root)

    with pytest.raises(ExportPlanError, match="Segment 2 is missing required tags"):
        plan_export(model, schemes, str(root), skip_destinations=[destination])


def test_a_skip_matches_case_insensitively(
    schemes,
    required_tags,
    tmp_path,
):
    """Skips are matched in the same normalized key space as the conflict
    check, so a case-variant cannot make a skip miss and re-cut a clip."""
    model = make_model(
        tags_list=[
            {**required_tags, "title": "First"},
            {**required_tags, "title": "Second"},
        ],
    )
    root = tmp_path / "library"
    first = planned_path(model, schemes, root)

    plan = plan_export(model, schemes, str(root), skip_destinations=[first.swapcase()])

    assert [clip.relative_components[-1] for clip in plan.clips] == ["Second.mp4"]
    assert [clip.relative_path for clip in plan.skipped] == [first]


def test_skipping_everything_says_why(
    schemes,
    required_tags,
    tmp_path,
):
    """Otherwise the user is told the model has no non-ignored clips, which is
    false and explains nothing about why the export came back empty."""
    model = make_model(tags_list=[{**required_tags, "title": "First"}])
    root = tmp_path / "library"
    destination = planned_path(model, schemes, root)

    with pytest.raises(ExportPlanError, match="already written"):
        plan_export(model, schemes, str(root), skip_destinations=[destination])


def test_skip_destinations_must_be_relative_paths(
    schemes,
    required_tags,
    tmp_path,
):
    """A bare string is iterable, so without the guard every character would
    become a one-component skip key and silently match nothing."""
    model = make_model(tags_list=[{**required_tags, "title": "First"}])
    root = str(tmp_path / "library")

    with pytest.raises(TypeError, match="collection of relative paths"):
        plan_export(model, schemes, root, skip_destinations="Network/First.mp4")

    with pytest.raises(TypeError, match="non-empty strings"):
        plan_export(model, schemes, root, skip_destinations=[""])
