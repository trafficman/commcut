"""Tests for named-export settings, destination planning, and preflight."""

import errno
import json
import os

import pytest

from shared import environment
from shared import exporting
from shared.environment import IMPORT_FOLDER_NAME
from shared.exporting import (
    DEFAULT_FILE_NAMING_SCHEME,
    EXPORT_FOLDER_KEY,
    EXPORT_FOLDER_NAME,
    FILE_NAMING_SCHEME_KEY,
    FOLDER_ORGANIZATION_SCHEME_KEY,
    DestinationIndex,
    ExportPlanError,
    ExportSchemes,
    ExportSettingsError,
    default_export_folder,
    export_folder,
    export_folder_setting_error,
    load_export_schemes,
    model_with_tag_locks,
    plan_clip_destination,
    plan_export,
    preflight_export_plan,
)
from shared.naming import compile_filename_scheme
from shared.paths import DEFAULT_FOLDER_SCHEME
from shared.paths import compile_folder_scheme
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


@pytest.fixture
def install_root(tmp_path, monkeypatch):
    """Point the one install root at a temporary folder.

    `shared.exporting` reaches the root through the `environment` module rather
    than through its own binding, so this single patch moves the default export
    folder *and* the settings.json it is read from. Patching one without the
    other would leave a test reading the developer's real settings.json and
    failing for reasons that look like a product bug.
    """
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(environment, "install_root", lambda: str(root))
    return root


def write_settings(install_root, settings):
    (install_root / "settings.json").write_text(
        json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    return str(install_root / "settings.json")


def test_the_export_folder_follows_the_install_root(install_root):
    """Not a project root derived from __file__: frozen, that is PyInstaller's
    extraction folder, which is deleted on exit along with every clip in it.

    This was `shared/ffmpeg.py:_export_dir`, then a literal in the editor, and
    about to be a third copy in the library walk. It is one function now, and
    this is the guard on the property all three spellings had in common.
    """
    assert export_folder() == os.path.join(str(install_root), EXPORT_FOLDER_NAME)
    assert default_export_folder() == os.path.join(str(install_root), EXPORT_FOLDER_NAME)


def test_the_export_folder_is_not_the_import_folders_neighbour_by_accident(
        install_root):
    """The import folder is where the importer reads and the export folder is
    where the library is. They sit side by side under the install root, and a
    mixed-up constant would put a library walk's records into the folder the
    importer deletes from."""
    assert EXPORT_FOLDER_NAME != IMPORT_FOLDER_NAME
    assert export_folder().rsplit(os.sep, 1)[-1] == "export"


def test_a_chosen_export_folder_is_used(install_root, tmp_path):
    """The whole point: an arbitrary folder anywhere on the machine."""
    chosen = tmp_path / "elsewhere" / "filler"
    write_settings(install_root, {EXPORT_FOLDER_KEY: str(chosen)})

    assert export_folder() == str(chosen)


def test_an_empty_setting_means_the_default(install_root):
    """Blank is how the user asks for the default, not an error.

    The Settings window stores that as an absent key, so a blank string is only
    reachable by hand -- and it has to mean the same thing there, or a hand edit
    would break the next export with nothing on screen to explain it.
    """
    write_settings(install_root, {EXPORT_FOLDER_KEY: "   "})

    assert export_folder() == os.path.join(str(install_root), EXPORT_FOLDER_NAME)


def test_the_stored_path_is_normalized(install_root, tmp_path):
    """One canonical spelling in settings.json, so a value with a trailing
    separator or a `..` in it cannot make two settings files disagree about
    whether they name the same folder."""
    messy = str(tmp_path / "clips") + os.sep + ".." + os.sep + "clips" + os.sep
    write_settings(install_root, {EXPORT_FOLDER_KEY: messy})

    assert export_folder() == str(tmp_path / "clips")
    assert export_folder() == os.path.normpath(export_folder())


@pytest.mark.parametrize("stored", [42, None, ["C:/clips"], {"path": "C:/clips"}])
def test_a_non_string_setting_is_refused(install_root, stored):
    """Hand-edited files are supported, so the reader has to say no rather than
    raise something the user cannot act on. `None` is the JSON null, which is
    what a hand editor writes for "unset" -- and it is refused rather than read
    as the default, because a null key and an absent key are different claims."""
    write_settings(install_root, {EXPORT_FOLDER_KEY: stored})

    with pytest.raises(ExportSettingsError) as raised:
        export_folder()

    assert EXPORT_FOLDER_KEY in str(raised.value)


def test_a_relative_setting_is_refused(install_root):
    """It would resolve against whatever the working directory happens to be
    when the export runs, which is not a folder anybody chose."""
    write_settings(install_root, {EXPORT_FOLDER_KEY: "clips"})

    with pytest.raises(ExportSettingsError) as raised:
        export_folder()

    assert "full path" in str(raised.value)


def test_a_settings_file_that_cannot_be_read_is_refused_not_ignored(install_root):
    """A corrupt file already stops an export through `load_export_schemes`, so
    resolving the root from a *different* answer -- the default -- would make
    one broken file produce two different behaviours depending on which module
    asked first."""
    (install_root / "settings.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ExportSettingsError):
        export_folder()


def test_the_export_folder_cannot_be_the_import_folder(install_root):
    write_settings(install_root, {EXPORT_FOLDER_KEY: str(install_root / "import")})

    with pytest.raises(ExportSettingsError) as raised:
        export_folder()

    assert "import folder" in str(raised.value)


def test_the_export_folder_cannot_live_inside_the_import_folder(install_root):
    write_settings(
        install_root,
        {EXPORT_FOLDER_KEY: str(install_root / "import" / "sorted")},
    )

    with pytest.raises(ExportSettingsError):
        export_folder()


def test_the_export_folder_cannot_contain_the_import_folder(install_root):
    """The install root itself would make `build_catalog` walk `import/` and
    `temp/` as if they were library members."""
    write_settings(install_root, {EXPORT_FOLDER_KEY: str(install_root)})

    with pytest.raises(ExportSettingsError):
        export_folder()


def test_the_overlap_check_compares_case_the_way_windows_does(install_root):
    """Two spellings of one folder are one folder. Without `normcase` this
    check is decoration on Windows and the only thing enforcing it is the
    spelling the picker happened to hand back."""
    stored = str(install_root / "IMPORT")

    assert export_folder_setting_error(stored) is not None


def test_a_file_is_not_a_folder(install_root):
    a_file = install_root / "clips.mp4"
    a_file.write_bytes(b"\0")

    assert "not a folder" in export_folder_setting_error(str(a_file))
    write_settings(install_root, {EXPORT_FOLDER_KEY: str(a_file)})
    with pytest.raises(ExportSettingsError):
        export_folder()


def test_a_folder_that_does_not_exist_yet_is_fine(install_root, tmp_path):
    """Not an error. `plan_export` already accepts a root that has not been
    created as long as its nearest existing ancestor is a directory, and
    `shared/ffmpeg.py` creates the tree when it writes -- so refusing here would
    refuse something that works, and would refuse the folder a user just typed
    before they had made it."""
    chosen = tmp_path / "not" / "made" / "yet"
    write_settings(install_root, {EXPORT_FOLDER_KEY: str(chosen)})

    assert export_folder() == str(chosen)
    assert export_folder_setting_error(str(chosen)) is None


def test_the_refusal_says_where_to_fix_it(install_root):
    """Three windows reach this and none of them rewords it: the editor shows it
    as "Export could not start", and the shell reports the other two. So the key
    and Settings both have to be in the message or nobody can act on it."""
    write_settings(install_root, {EXPORT_FOLDER_KEY: "clips"})

    with pytest.raises(ExportSettingsError) as raised:
        export_folder()

    message = str(raised.value)
    assert EXPORT_FOLDER_KEY in message
    assert "Settings" in message
    assert str(install_root / "settings.json") in message


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


def test_the_same_refusal_is_named_when_lstat_answers_enotdir(
    monkeypatch, schemes, required_tags, tmp_path
):
    """The refusal above is the specification; this pins *how* it survives POSIX.

    Windows answers `FileNotFoundError` when `lstat` is given a path whose
    ancestor is a file, and macOS and Linux answer `NotADirectoryError`. Catching
    only the first made the preflight platform-dependent: on the latter, the raw
    ENOTDIR escaped `plan_export` and a user would have seen a stray OSError
    instead of the one problem this module is supposed to name.

    Found by the macOS source-release CI leg, which is the only reason this is
    known. The failure is injected rather than waited for, because the real
    behaviour is only reachable on a POSIX filesystem -- and an injected
    ENOTDIR runs identically on the Windows leg, so all three platforms hold this
    down rather than only the one that happens to break.
    """
    root = tmp_path / "library"
    blocked_parent = root / "Cartoon Network"
    blocked_parent.parent.mkdir(parents=True)
    blocked_parent.write_bytes(b"not a directory")

    real_lstat = os.lstat

    def posix_lstat(path, *args, **kwargs):
        parent = os.path.dirname(os.fspath(path))
        while parent and parent != os.path.dirname(parent):
            if os.path.lexists(parent) and not os.path.isdir(parent):
                raise NotADirectoryError(
                    errno.ENOTDIR, os.strerror(errno.ENOTDIR), os.fspath(path))
            parent = os.path.dirname(parent)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(exporting.os, "lstat", posix_lstat)

    with pytest.raises(ExportPlanError, match="parent is not a directory"):
        plan_export(make_model(tags_list=[required_tags]), schemes, str(root))


def test_is_link_or_reparse_point_never_raises_for_an_unreachable_path(tmp_path):
    """The contract the caller relies on: it wants a yes/no answer, and there is
    no path whose answer is an exception."""
    unreachable = tmp_path / "a-file" / "below-it"
    (tmp_path / "a-file").write_bytes(b"not a directory")

    assert exporting._is_link_or_reparse_point(str(unreachable)) is False
    assert exporting._is_link_or_reparse_point(str(tmp_path / "absent")) is False


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


# ---------------------------------------------------------------------------
# The pieces the export planner and the importer share
# ---------------------------------------------------------------------------
#
# `plan_export` no longer computes a destination itself: it delegates the
# per-clip half to `plan_clip_destination` and the cross-clip half to
# `DestinationIndex`, so `shared/importing.py` can put somebody else's library
# through the identical rules. These pin both halves, because the property that
# matters is not "export still works" — the rest of this file covers that — but
# that the shared halves behave the same way whoever calls them.

REQUIRED_TAGS = {
    "title": "A Clip", "network": "CN", "filler_type": "Promo",
    "time_period": "2000s",
}


def resolve(tags, label="Segment 1", file_scheme=DEFAULT_FILE_NAMING_SCHEME):
    return plan_clip_destination(
        tags=tags,
        folder_scheme=compile_folder_scheme(DEFAULT_FOLDER_SCHEME),
        filename_scheme=compile_filename_scheme(file_scheme),
        label=label,
    )


def test_one_clip_destination_resolves_under_the_shipped_schemes():
    destination = resolve(REQUIRED_TAGS)

    assert destination.relative_components[-1] == "CN - Promo - 2000s - A Clip.mp4"
    assert destination.record_relative_components[-1] == (
        f"CN - Promo - 2000s - A Clip{RECORD_EXTENSION}")
    assert dict(destination.tags) == REQUIRED_TAGS


def test_the_record_sits_beside_its_video_and_shares_its_folders():
    """One clip, two files. The record's components are the video's with the last
    one swapped, which is the whole of `video present => record present`."""
    destination = resolve(REQUIRED_TAGS)

    assert (destination.record_relative_components[:-1]
            == destination.relative_components[:-1])
    assert (destination.record_relative_components[-1]
            != destination.relative_components[-1])


def test_the_destination_hands_back_the_tags_it_rendered_from():
    """So a planner cannot build the clip's record from a second, separately
    canonicalized dict and have the two disagree about where it went."""
    destination = resolve({**REQUIRED_TAGS, "type": "Bumper"})

    assert dict(destination.tags)["filler_type"] == "Bumper"
    assert "Bumper" in destination.relative_components[-1]


def test_an_error_names_the_label_it_was_given():
    """The only difference between a segment's complaint and an imported clip's
    is which clip it is about, so the label is a parameter."""
    with pytest.raises(ExportPlanError) as error:
        resolve({**REQUIRED_TAGS, "title": ""}, label="Friday Night Bump.mkv")

    assert "Friday Night Bump.mkv" in str(error.value)


def test_a_missing_required_tag_is_refused_with_the_same_rule_the_editor_uses():
    partial = {k: v for k, v in REQUIRED_TAGS.items() if k != "time_period"}

    with pytest.raises(ExportPlanError) as error:
        resolve(partial)

    assert "time_period" in str(error.value)


def test_an_unknown_tag_is_refused_rather_than_dropped():
    with pytest.raises(ExportPlanError) as error:
        resolve({**REQUIRED_TAGS, "colour": "Red"})

    assert "colour" in str(error.value)


def test_two_resolutions_of_the_same_tags_land_in_the_same_place():
    """The importer's core safety property: the same tags under the same schemes
    cannot resolve differently depending on which planner asked."""
    other = plan_clip_destination(
        tags=dict(reversed(list(REQUIRED_TAGS.items()))),
        folder_scheme=compile_folder_scheme(DEFAULT_FOLDER_SCHEME),
        filename_scheme=compile_filename_scheme(DEFAULT_FILE_NAMING_SCHEME),
        label="An Imported Clip.mp4",
    )

    assert other.relative_components == resolve(REQUIRED_TAGS).relative_components


class TestDestinationIndex:
    """The cross-clip rule, which has to hold however the clips were produced."""

    def test_the_first_claim_wins_and_the_second_collides(self):
        index = DestinationIndex()
        components = ("Network", "A Clip.mp4")

        first = index.claim(components, owner=7)
        second = index.claim(components, owner=9)

        assert first.ok and not first.skipped
        assert not second.ok
        assert second.conflict_with == 7

    def test_a_collision_is_in_the_normalized_key_space(self):
        """Case is not a difference to the filesystem on Windows or macOS, and
        NFC and stray control characters are not differences anywhere, so two
        clips differing only in those must not both be planned."""
        index = DestinationIndex()

        index.claim(("Cartoon Network", "A Clip.mp4"), owner=0)
        clash = index.claim(("cartoon network", "a clip.mp4"), owner=1)

        assert not clash.ok
        assert clash.conflict_with == 0

    def test_a_different_spelling_is_a_different_destination(self):
        """The other half of the rule: the key normalizes case and Unicode, not
        punctuation. Over-colliding would refuse two clips that are genuinely
        different files."""
        index = DestinationIndex()

        assert index.claim(("Network", "A Clip.mp4"), owner=0).ok
        assert index.claim(("Network", "a-clip.mp4"), owner=1).ok

    def test_distinct_destinations_do_not_collide(self):
        index = DestinationIndex()

        assert index.claim(("A", "x.mp4"), owner=0).ok
        assert index.claim(("B", "x.mp4"), owner=1).ok
        assert index.claim(("A", "y.mp4"), owner=2).ok

    def test_a_skipped_destination_is_left_alone_and_free_to_be_claimed(self):
        """Resume behaviour: the clip an earlier run wrote is skipped, and a
        duplicate of it is not what the skip is protecting — nothing is
        overwritten either way."""
        index = DestinationIndex(skip_destinations=["Network/A Clip.mp4"])

        claim = index.claim(("Network", "A Clip.mp4"), owner=0)

        assert claim.skipped
        assert claim.ok
        assert index.claim(("Network", "A Clip.mp4"), owner=1).ok, (
            "a skipped destination must not block a later claim")

    def test_owns_reports_a_claimed_destination(self):
        index = DestinationIndex()

        assert not index.owns(("A", "x.mp4"))
        index.claim(("A", "x.mp4"), owner=0)
        assert index.owns(("A", "x.mp4"))
        assert not index.owns(("A", "y.mp4"))

    def test_an_owner_is_whatever_the_caller_wants_back(self):
        """The two planners word a collision differently, so the index stores the
        token rather than a name one of them would have to parse."""
        index = DestinationIndex()

        index.claim(("A", "x.mp4"), owner="Friday Night Bump.mkv")

        assert index.claim(("A", "x.mp4"), owner=1).conflict_with == (
            "Friday Night Bump.mkv")


def test_plan_export_agrees_with_the_shared_destination_half(schemes,
                                                             required_tags,
                                                             tmp_path):
    """The cross-check that makes the extraction safe: a segment planned through
    `plan_export` and the same tags resolved through `plan_clip_destination`
    under the *same* schemes must produce the same path. If they ever diverge, an
    imported clip would land somewhere an exported one would not."""
    model = make_model(tags_list=[dict(required_tags)])
    plan = plan_export(model, schemes, str(tmp_path / "library"))

    expected = resolve(required_tags, file_scheme=schemes.file_scheme)

    assert plan.clips[0].relative_components == expected.relative_components
    assert plan.clips[0].record_relative_components == (
        expected.record_relative_components)
    assert plan.clips[0].tags == expected.tags
