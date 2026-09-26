"""Tests for named-export settings, destination planning, and preflight."""

import json

import pytest

from shared.exporting import (
    DEFAULT_FILE_NAMING_SCHEME,
    FILE_NAMING_SCHEME_KEY,
    FOLDER_ORGANIZATION_SCHEME_KEY,
    ExportPlanError,
    ExportSchemes,
    load_export_schemes,
    model_with_tag_locks,
    plan_export,
    preflight_export_plan,
)
from shared.paths import DEFAULT_FOLDER_SCHEME
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


def test_preflight_rejects_forged_unsafe_plan(tmp_path):
    from shared.exporting import ExportPlan, PlannedExportClip

    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(PlannedExportClip(
            segment_index=0,
            start=0.0,
            duration=1.0,
            relative_components=("..", "outside.mp4"),
        ),),
    )

    with pytest.raises(ExportPlanError, match="unsafe component"):
        preflight_export_plan(plan)


def test_preflight_rejects_forged_empty_and_duplicate_plans(tmp_path):
    from shared.exporting import ExportPlan, PlannedExportClip

    with pytest.raises(ExportPlanError, match="contains no clips"):
        preflight_export_plan(ExportPlan(
            export_root=str(tmp_path / "library"),
            clips=(),
        ))

    duplicate = PlannedExportClip(
        segment_index=0,
        start=0.0,
        duration=1.0,
        relative_components=("Network", "Clip.mp4"),
    )
    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(duplicate, duplicate),
    )
    with pytest.raises(ExportPlanError, match="more than once"):
        preflight_export_plan(plan)


def test_preflight_rejects_destination_used_as_parent_directory(tmp_path):
    from shared.exporting import ExportPlan, PlannedExportClip

    parent_file = PlannedExportClip(
        segment_index=0,
        start=0.0,
        duration=1.0,
        relative_components=("Network", "Clip.mp4"),
    )
    child_file = PlannedExportClip(
        segment_index=1,
        start=1.0,
        duration=1.0,
        relative_components=("Network", "Clip.mp4", "Child.mp4"),
    )
    plan = ExportPlan(
        export_root=str(tmp_path / "library"),
        clips=(parent_file, child_file),
    )

    with pytest.raises(ExportPlanError, match="parent directory"):
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
