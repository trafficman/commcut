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
    execute_retag,
    plan_retag,
    retag_is_in_place,
)

FOLDER_SCHEME = compile_folder_scheme(
    "{network}/{filler_type}/{time_period}")
FILENAME_SCHEME = compile_filename_scheme("{title}")

CANONICAL_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$")


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
