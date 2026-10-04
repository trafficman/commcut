"""Tests for the library catalog: the walk that finds clips, and what it ignores.

Applies to: `shared/catalog.py`, `shared/records.py`, `shared/sources.py`.
"""

import os

import pytest

from shared.catalog import (
    REASON_ORPHANED_RECORD,
    REASON_UNREADABLE,
    REASON_WALK_ERROR,
    Catalog,
    CatalogClip,
    build_catalog,
    pending_record_tags,
    sync_vocabulary,
)
from shared.records import (
    REASON_INVALID,
    REASON_NOT_A_RECORD,
    REASON_NOT_WELL_FORMED,
    REASON_UNKNOWN_ELEMENT,
    REASON_UNKNOWN_TAG_KEY,
    REASON_UNSUPPORTED_VERSION,
    ClipRecord,
    RECORD_SCHEMA_VERSION,
    render_record_xml,
    write_record,
)
from shared.vocabulary import (
    DEFAULT_VALUES,
    Vocabulary,
    get_vocabulary,
)


DEFAULT_TAGS = (
    ("block", "Toonami"),
    ("filler_type", "Promo"),
    ("network", "Cartoon Network"),
    ("time_period", "2000s"),
    ("title", "Worlds Finest"),
)


def make_record(**overrides):
    values = {
        "source": "compilation.mp4",
        "segment_index": 3,
        "start": 314.2,
        "duration": 29.9,
        "tags": DEFAULT_TAGS,
    }
    values.update(overrides)
    return ClipRecord(**values)


def write_clip(root, relative_stem, tags=DEFAULT_TAGS, extension=".mp4", **overrides):
    """Leave a video and its record the way the export pipeline would.

    Returns the record path, so a test can corrupt it afterwards.
    """
    directory = os.path.join(str(root), "Cartoon Network", "Promo")
    os.makedirs(directory, exist_ok=True)
    video_path = os.path.join(directory, relative_stem + extension)
    with open(video_path, "wb") as handle:
        handle.write(b"video")
    record_path = write_record(
        os.path.splitext(video_path)[0] + ".cnfo",
        make_record(tags=tags, **overrides),
    )
    return record_path


def relative_paths(catalog):
    return [clip.relative_path for clip in catalog.clips]


def tags_of(catalog, relative_path):
    clip = next(c for c in catalog.clips if c.relative_path == relative_path)
    return clip.tag_dict()


# ---------------------------------------------------------------------------
# What a clip is
# ---------------------------------------------------------------------------

def test_a_record_with_a_sibling_video_is_a_clip(tmp_path):
    write_clip(tmp_path, "Worlds Finest")

    catalog = build_catalog(str(tmp_path))

    assert len(catalog.clips) == 1
    assert catalog.clips[0].relative_path == (
        "Cartoon Network/Promo/Worlds Finest.mp4")
    assert catalog.clips[0].tags == tuple(sorted(DEFAULT_TAGS))
    assert catalog.clips[0].path.endswith("Worlds Finest.mp4")
    assert catalog.problems == ()


def test_a_record_with_no_video_is_not_a_clip_but_is_reported(tmp_path):
    """The orphan a previous export failure left behind.

    It is **not** a clip, and that is the part that has not changed: a scan that
    turned it into one would offer a tag set for a file that is not there.

    It *is* now reported. It used to be dropped silently, which meant a folder could
    fill with records that nothing would read, delete, or even mention — the state
    `move` used to leave behind by the hundred, and the state a user gets when they
    delete a video out from under a library.
    """
    directory = tmp_path / "Cartoon Network" / "Promo"
    directory.mkdir(parents=True)
    write_record(str(directory / "Orphan.cnfo"), make_record())

    catalog = build_catalog(str(tmp_path))

    assert catalog.clips == ()
    assert [problem.reason for problem in catalog.problems] == [
        REASON_ORPHANED_RECORD]
    assert "no video beside it" in catalog.problems[0].message
    assert catalog.problems[0].path == "Cartoon Network/Promo/Orphan.cnfo"


def test_a_video_with_no_record_is_ignored(tmp_path):
    """Half a library the user dragged in by hand yields nothing, rather than a
    clip whose tags were guessed out of its folder names."""
    directory = tmp_path / "Toonami" / "2000s"
    directory.mkdir(parents=True)
    (directory / "Some Random Rip.mp4").write_bytes(b"video")

    catalog = build_catalog(str(tmp_path))

    assert catalog.clips == ()
    assert catalog.problems == ()


def test_a_record_whose_sibling_is_not_a_video_is_ignored(tmp_path):
    directory = tmp_path / "Promo"
    directory.mkdir(parents=True)
    (directory / "Notes.txt").write_text("not a clip", encoding="utf-8")
    write_record(str(directory / "Notes.cnfo"), make_record())

    catalog = build_catalog(str(tmp_path))

    assert catalog.clips == ()


@pytest.mark.parametrize("extension", [".mp4", ".mkv", ".MP4", ".webm"])
def test_any_video_extension_beside_the_record_is_a_clip(tmp_path, extension):
    """The writer only ever produces `.mp4`, but the reader accepts anything
    `shared/sources.py` calls a video -- a user who transcodes their library to
    another container keeps their catalog instead of losing it silently."""
    write_clip(tmp_path, "Worlds Finest", extension=extension)

    catalog = build_catalog(str(tmp_path))

    assert relative_paths(catalog) == [
        f"Cartoon Network/Promo/Worlds Finest{extension}"]


def test_a_record_extension_is_matched_without_regard_to_case(tmp_path):
    record_path = write_clip(tmp_path, "Worlds Finest")
    upper = os.path.splitext(record_path)[0] + ".CNFO"
    os.replace(record_path, upper)

    catalog = build_catalog(str(tmp_path))

    assert len(catalog.clips) == 1


def test_the_walk_reaches_every_folder_of_the_library(tmp_path):
    write_clip(tmp_path, "First")
    directory = tmp_path / "Nickelodeon" / "Commercial" / "1990s"
    directory.mkdir(parents=True)
    with open(directory / "Second.mkv", "wb") as handle:
        handle.write(b"video")
    write_record(str(directory / "Second.cnfo"), make_record())

    catalog = build_catalog(str(tmp_path))

    assert relative_paths(catalog) == [
        "Cartoon Network/Promo/First.mp4",
        "Nickelodeon/Commercial/1990s/Second.mkv",
    ]


def test_a_broken_symlink_beside_a_record_is_not_a_clip(tmp_path):
    """A dangling link is listed as a file by `os.walk` and is not one, so the
    name it hands over is not enough to call it a video."""
    directory = tmp_path / "Promo"
    directory.mkdir(parents=True)
    write_record(str(directory / "Gone.cnfo"), make_record())
    try:
        os.symlink(
            str(directory / "missing-target.mp4"), str(directory / "Gone.mp4"))
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create symlinks")

    catalog = build_catalog(str(tmp_path))

    assert catalog.clips == ()


# ---------------------------------------------------------------------------
# The one thing this module must never do
# ---------------------------------------------------------------------------

def test_tags_come_from_the_record_and_never_from_the_filename(tmp_path):
    """The guard on invariant 12.

    The filename here is exactly what commcut's own schemes render from four of
    these tags, and the record deliberately disagrees with all of them. Anything
    that parses a filename back into tags reports these values; anything that
    reads the record reports the values below. Only one of those is correct, and
    `docs/naming-and-organization.md` explains why the other cannot exist.
    """
    deceptive = "Cartoon Network - Promo - 2000s - Toonami Worlds Finest.mp4"
    directory = tmp_path / "Cartoon Network" / "Promo"
    directory.mkdir(parents=True)
    with open(directory / deceptive, "wb") as handle:
        handle.write(b"video")
    write_record(str(directory / (deceptive[:-4] + ".cnfo")), make_record(tags=(
        ("filler_type", "Bumper"),
        ("network", "Nickelodeon"),
        ("time_period", "1990s"),
        ("title", "Something Else Entirely"),
    )))

    catalog = build_catalog(str(tmp_path))

    assert tags_of(catalog, f"Cartoon Network/Promo/{deceptive}") == {
        "filler_type": "Bumper",
        "network": "Nickelodeon",
        "time_period": "1990s",
        "title": "Something Else Entirely",
    }


def test_a_tag_value_containing_the_separators_the_filename_uses_is_intact(tmp_path):
    """Records round-trip arbitrary text, so a title may hold the dashes and
    brackets a scheme renders from. Nothing may be re-split on them."""
    awkward = "Toonami - Kids [2000s] - 30 Sec (Remastered)"
    write_clip(tmp_path, awkward, tags=(("title", awkward),))

    catalog = build_catalog(str(tmp_path))

    assert tags_of(catalog, f"Cartoon Network/Promo/{awkward}.mp4") == {
        "title": awkward}


# ---------------------------------------------------------------------------
# What could not be read
# ---------------------------------------------------------------------------

def test_a_malformed_record_is_reported_and_the_walk_continues(tmp_path):
    """One corrupt record on a network share costs that record, not the library.
    A silent skip would be a clip the user could never find again."""
    write_clip(tmp_path, "First")
    broken = write_clip(tmp_path, "Second")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("<commcut-clip><not-closed>")
    write_clip(tmp_path, "Third")

    catalog = build_catalog(str(tmp_path))

    assert relative_paths(catalog) == [
        "Cartoon Network/Promo/First.mp4",
        "Cartoon Network/Promo/Third.mp4",
    ]
    assert len(catalog.problems) == 1
    assert catalog.problems[0].path == "Cartoon Network/Promo/Second.cnfo"
    assert "well-formed" in catalog.problems[0].message


def test_a_record_from_a_newer_schema_is_reported_not_crashed_on(tmp_path):
    write_clip(tmp_path, "First")
    newer = write_clip(tmp_path, "Second")
    with open(newer, "w", encoding="utf-8") as handle:
        handle.write(render_record_xml(make_record()).replace(
            f'version="{RECORD_SCHEMA_VERSION}"', 'version="99"'))

    catalog = build_catalog(str(tmp_path))

    assert len(catalog.clips) == 1
    assert len(catalog.problems) == 1
    assert "version 99" in catalog.problems[0].message


def test_a_record_holding_an_unknown_tag_key_is_reported(tmp_path):
    """`parse_record_xml` refuses rather than dropping data, and so does the
    walk: a record commcut cannot read whole is named, not half-remembered."""
    bad = write_clip(tmp_path, "Bad")
    with open(bad, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<commcut-clip version="1">\n'
            "  <source>compilation.mp4</source>\n"
            '  <segment index="1" start="0.0" duration="1.0" />\n'
            '  <tag key="colour">Red</tag>\n'
            "</commcut-clip>\n"
        )

    catalog = build_catalog(str(tmp_path))

    assert catalog.clips == ()
    assert "colour" in catalog.problems[0].message


def test_an_unreadable_directory_is_reported_and_the_walk_continues(
    tmp_path, monkeypatch
):
    """Real `os.walk` failure, injected at `os.scandir` because a portable way
    to make a directory unreadable does not exist -- on Windows, chmod does not
    remove access. `shared/exporting.py` re-raises the same error because it is
    about to write into the tree; a scan has nothing to protect."""
    write_clip(tmp_path, "First")
    locked = tmp_path / "Locked"
    locked.mkdir()
    write_clip(locked, "Second")
    write_clip(tmp_path, "Third")

    real_scandir = os.scandir

    def failing_scandir(target):
        if os.path.abspath(str(target)) == os.path.abspath(str(locked)):
            raise PermissionError(13, "Permission denied", str(locked))
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", failing_scandir)

    catalog = build_catalog(str(tmp_path))

    assert relative_paths(catalog) == [
        "Cartoon Network/Promo/First.mp4",
        "Cartoon Network/Promo/Third.mp4",
    ]
    assert len(catalog.problems) == 1
    assert "Locked" in catalog.problems[0].path


# ---------------------------------------------------------------------------
# The shape of the result
# ---------------------------------------------------------------------------

def test_a_missing_root_is_an_empty_catalog_not_an_error(tmp_path):
    """An empty export folder is the expected state of a fresh install, so it is
    the same answer `shared/sources.py:list_source_videos` gives."""
    catalog = build_catalog(str(tmp_path / "never-existed"))

    assert catalog == Catalog()
    assert catalog.cancelled is False


def test_an_empty_library_is_an_empty_catalog(tmp_path):
    tmp_path.mkdir(exist_ok=True)

    assert build_catalog(str(tmp_path)) == Catalog()


def test_the_clip_order_is_deterministic(tmp_path):
    for stem in ("Zebra", "apple", "Mango", "banana"):
        write_clip(tmp_path, stem)

    first = relative_paths(build_catalog(str(tmp_path)))
    second = relative_paths(build_catalog(str(tmp_path)))

    assert first == second
    assert first == sorted(first, key=str.casefold)


def test_the_tag_index_is_derived_from_the_records(tmp_path):
    write_clip(tmp_path, "First", tags=(("network", "Cartoon Network"),))
    write_clip(tmp_path, "Second", tags=(
        ("network", "Nickelodeon"),
        ("special", "Kids"),
    ))

    assert build_catalog(str(tmp_path)).tag_index == {
        "network": ("Cartoon Network", "Nickelodeon"),
        "special": ("Kids",),
    }


def test_an_empty_tag_value_is_left_out_of_the_index(tmp_path):
    """A record stores raw values and omits the empty ones, so an empty value
    reaching here is a hand-edited record rather than a tag."""
    write_clip(tmp_path, "First", tags=(("network", "CN"), ("block", "")))

    assert build_catalog(str(tmp_path)).tag_index == {"network": ("CN",)}


def test_the_clip_carries_its_record_for_a_consumer_that_needs_provenance(tmp_path):
    write_clip(tmp_path, "Worlds Finest", segment_index=11)

    clip = build_catalog(str(tmp_path)).clips[0]

    assert isinstance(clip, CatalogClip)
    assert clip.record.segment_index == 11
    assert clip.record.source == "compilation.mp4"


def test_the_clip_names_its_record_separately_from_its_video(tmp_path):
    """Two files sharing a stem, and a consumer that wants to re-read, re-render
    or report on one needs to name it."""
    record_path = write_clip(tmp_path, "Worlds Finest")

    clip = build_catalog(str(tmp_path)).clips[0]

    assert clip.path.endswith("Worlds Finest.mp4")
    assert clip.record_path == os.path.abspath(record_path)
    assert clip.record_path != clip.path


def test_a_clip_reaches_the_record_even_through_a_differently_named_video(tmp_path):
    """The record is found by stem, so the sibling does not have to match the
    record's own name for the walk to connect them."""
    directory = tmp_path / "Cartoon Network" / "Promo"
    directory.mkdir(parents=True)
    (directory / "Toonami.mp4").write_bytes(b"video")
    write_record(str(directory / "Toonami.cnfo"), make_record())

    clip = build_catalog(str(tmp_path)).clips[0]

    assert clip.relative_path == "Cartoon Network/Promo/Toonami.mp4"
    assert clip.record_path.endswith("Toonami.cnfo")


# ---------------------------------------------------------------------------
# Why a record could not be read
# ---------------------------------------------------------------------------

def reasons_in(catalog):
    return {problem.path: problem.reason for problem in catalog.problems}


def test_a_corrupt_record_is_reported_as_malformed_rather_than_unknown(tmp_path):
    """The two are the same symptom to a walk and completely different things to
    a person, which is why the reason is a code and not prose."""
    broken = write_clip(tmp_path, "Broken")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("<commcut-clip><not-closed>")
    write_clip(tmp_path, "Fine")

    catalog = build_catalog(str(tmp_path))

    assert reasons_in(catalog) == {
        "Cartoon Network/Promo/Broken.cnfo": REASON_NOT_WELL_FORMED,
    }


def test_an_unknown_tag_key_gets_its_own_reason(tmp_path):
    """The Library Importer's headline case: a friend's export used a tag this
    build does not know. The user needs to be told *which* tag, not that the file
    is bad."""
    bad = write_clip(tmp_path, "Bad")
    with open(bad, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<commcut-clip version="1">\n'
            "  <source>compilation.mp4</source>\n"
            '  <segment index="1" start="0.0" duration="1.0" />\n'
            '  <tag key="colour">Red</tag>\n'
            "</commcut-clip>\n"
        )

    catalog = build_catalog(str(tmp_path))

    assert reasons_in(catalog) == {
        "Cartoon Network/Promo/Bad.cnfo": REASON_UNKNOWN_TAG_KEY,
    }
    assert "colour" in catalog.problems[0].message


def test_a_newer_schema_is_its_own_reason(tmp_path):
    newer = write_clip(tmp_path, "Future")
    with open(newer, "w", encoding="utf-8") as handle:
        handle.write(render_record_xml(make_record()).replace(
            f'version="{RECORD_SCHEMA_VERSION}"', 'version="99"'))

    catalog = build_catalog(str(tmp_path))

    assert reasons_in(catalog) == {
        "Cartoon Network/Promo/Future.cnfo": REASON_UNSUPPORTED_VERSION,
    }


def test_an_unrecognized_element_is_its_own_reason(tmp_path):
    odd = write_clip(tmp_path, "Odd")
    with open(odd, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<commcut-clip version="1">\n'
            "  <source>compilation.mp4</source>\n"
            '  <segment index="1" start="0.0" duration="1.0" />\n'
            "  <mood>grumpy</mood>\n"
            "</commcut-clip>\n"
        )

    assert reasons_in(build_catalog(str(tmp_path))) == {
        "Cartoon Network/Promo/Odd.cnfo": REASON_UNKNOWN_ELEMENT,
    }


def test_a_record_that_is_not_a_record_gets_its_own_reason(tmp_path):
    """A `.cnfo` holding some other XML entirely. Easy to produce by accident
    when hand-copying files between libraries."""
    odd = write_clip(tmp_path, "Wrong")
    with open(odd, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n<playlist><entry/></playlist>\n')

    assert reasons_in(build_catalog(str(tmp_path))) == {
        "Cartoon Network/Promo/Wrong.cnfo": REASON_NOT_A_RECORD,
    }


def test_a_record_missing_its_required_shape_is_invalid_rather_than_malformed(
    tmp_path,
):
    """Well-formed XML, right root, right version, no `<source>`. The remaining
    bucket, and named so it is not confused with any of the specific causes."""
    odd = write_clip(tmp_path, "Headless")
    with open(odd, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<commcut-clip version="1">\n'
            '  <segment index="1" start="0.0" duration="1.0" />\n'
            "</commcut-clip>\n"
        )

    assert reasons_in(build_catalog(str(tmp_path))) == {
        "Cartoon Network/Promo/Headless.cnfo": REASON_INVALID,
    }


def test_a_record_that_cannot_be_read_at_all_says_so(tmp_path):
    """Not a record problem at all: the bytes never became text. Distinct from a
    record commcut read and refused, which is what `is_record_problem` is for."""
    unreadable = write_clip(tmp_path, "Locked")
    os.chmod(unreadable, 0o000)

    try:
        catalog = build_catalog(str(tmp_path))
    finally:
        os.chmod(unreadable, 0o666)

    if not catalog.problems:
        pytest.skip("this platform will not refuse a read for the current user")
    assert reasons_in(catalog) == {
        "Cartoon Network/Promo/Locked.cnfo": REASON_UNREADABLE,
    }
    assert catalog.problems[0].is_record_problem is False


def test_a_walk_error_is_its_own_reason(tmp_path, monkeypatch):
    write_clip(tmp_path, "First")
    locked = tmp_path / "Locked"
    locked.mkdir()
    real_scandir = os.scandir

    def failing_scandir(target):
        if os.path.abspath(str(target)) == os.path.abspath(str(locked)):
            raise PermissionError(13, "Permission denied", str(locked))
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", failing_scandir)

    catalog = build_catalog(str(tmp_path))

    assert [problem.reason for problem in catalog.problems] == [
        REASON_WALK_ERROR]
    assert catalog.problems[0].is_record_problem is False


def test_a_refused_record_is_distinguishable_from_an_unreadable_one(tmp_path):
    """The property the codes exist for: a screen can separate "this build does
    not know that tag" from "this file will not open" without reading either
    message."""
    broken = write_clip(tmp_path, "Broken")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("not xml at all")

    catalog = build_catalog(str(tmp_path))

    assert len(catalog.problems) == 1
    assert catalog.problems[0].is_record_problem is True


# ---------------------------------------------------------------------------
# Progress and cancelling
# ---------------------------------------------------------------------------

def test_progress_is_reported_without_a_total(tmp_path):
    """`os.walk` cannot know how many records are ahead of it. A progress bar
    that invents a denominator is worse than an indeterminate one."""
    write_clip(tmp_path, "First")
    write_clip(tmp_path, "Second")
    seen = []

    build_catalog(str(tmp_path), on_progress=lambda found, path: seen.append(
        (found, path)))

    assert seen == [
        (0, "Cartoon Network/Promo/First.cnfo"),
        (1, "Cartoon Network/Promo/Second.cnfo"),
    ]
    assert all(len(call) == 2 for call in seen)
    assert all(isinstance(found, int) and isinstance(path, str) for found, path in seen)


def test_cancelling_before_the_first_folder_finds_nothing(tmp_path):
    write_clip(tmp_path, "First")

    catalog = build_catalog(str(tmp_path), should_cancel=lambda: True)

    assert catalog.cancelled is True
    assert catalog.clips == ()


def test_a_cancel_mid_walk_says_so_and_keeps_what_it_had(tmp_path):
    """The partial list is returned rather than thrown away, but `cancelled` is
    what tells a caller not to act on it: pruning a vocabulary against half a
    library would delete values the other half is using."""
    for stem in ("First", "Second", "Third"):
        write_clip(tmp_path, stem)
    reached = []

    def stop_once_the_first_record_is_read():
        return bool(reached)

    catalog = build_catalog(
        str(tmp_path),
        on_progress=lambda _found, path: reached.append(path),
        should_cancel=stop_once_the_first_record_is_read,
    )

    assert catalog.cancelled is True
    assert relative_paths(catalog) == ["Cartoon Network/Promo/First.mp4"]


def test_an_uncancelled_walk_says_so(tmp_path):
    write_clip(tmp_path, "First")

    assert build_catalog(str(tmp_path)).cancelled is False


# ---------------------------------------------------------------------------
# Reconciling the vocabulary against the library
# ---------------------------------------------------------------------------

def test_a_sync_puts_the_librarys_tags_into_an_empty_vocabulary(tmp_path):
    write_clip(tmp_path, "Worlds Finest", tags=(
        ("network", "Cartoon Network"),
        ("filler_type", "Bumper"),
    ))
    vocabulary = Vocabulary(path=str(tmp_path / "vocabulary.json"))

    result = sync_vocabulary(str(tmp_path), vocabulary)

    assert result.clips_found == 1
    assert result.values_removed == ()
    assert vocabulary.values("network") == ("Cartoon Network",)
    assert vocabulary.values("filler_type") == ("Bumper",)
    assert Vocabulary.load(str(tmp_path / "vocabulary.json")).values(
        "network") == ("Cartoon Network",)


def test_a_sync_is_a_union_and_not_a_replacement(tmp_path):
    """The half of the operation that keeps rather than discards.

    A value already in the file that a library clip also uses survives even when
    the two spellings differ, because `record()` is what dedupes and it keeps the
    casing it had. Replacing the file from the library instead would rewrite
    `cartoon network` to `Cartoon Network` -- a library module editing the user's
    data, which `docs/tag-vocabulary.md` rules out for the same reason it keeps
    a wrongly-cased value wrong.
    """
    write_clip(tmp_path, "First", tags=(("network", "Cartoon Network"),))
    vocabulary = Vocabulary(path=str(tmp_path / "vocabulary.json"))
    vocabulary.record({"network": "cartoon network"})
    vocabulary.save()

    sync_vocabulary(str(tmp_path), vocabulary)

    assert vocabulary.values("network") == ("cartoon network",)
    assert Vocabulary.load(str(tmp_path / "vocabulary.json")).values(
        "network") == ("cartoon network",)


def test_a_sync_drops_a_value_whose_only_clip_is_gone(tmp_path):
    write_clip(tmp_path, "First", tags=(("network", "Cartoon Network"),))
    vocabulary = Vocabulary(path=str(tmp_path / "vocabulary.json"))
    vocabulary.record({"block": "Toonami"})
    vocabulary.save()

    result = sync_vocabulary(str(tmp_path), vocabulary)

    assert result.values_removed == (("block", "Toonami"),)
    assert vocabulary.values("block") == ()
    assert Vocabulary.load(str(tmp_path / "vocabulary.json")).values("block") == ()


def test_a_sync_spares_a_value_a_staged_record_still_uses(tmp_path):
    """The prune asks what is *in use*, and a record waiting in `import/` is a use.

    The Tag Editor records a value the moment it is confirmed, which is before the
    clip has been imported. Without the staging folder counted, the next sync would
    find nothing in `export/` using that value and delete it again — so the record
    would be churn, and the user's dropdowns would empty between one clip and the
    next.
    """
    library = tmp_path / "export"
    staging = tmp_path / "import"
    write_clip(library, "Kept", tags=(("network", "Cartoon Network"),))
    write_clip(staging, "Staged", tags=(("network", "Nickelodeon"),))
    vocabulary = Vocabulary(path=str(tmp_path / "vocabulary.json"))
    vocabulary.record({"network": "Nickelodeon"})
    vocabulary.save()

    result = sync_vocabulary(str(library), vocabulary, pending_root=str(staging))

    assert result.values_removed == ()
    assert vocabulary.values("network") == ("Cartoon Network", "Nickelodeon")


def test_a_staged_record_stops_counting_once_it_is_gone(tmp_path):
    """The other half: the spare is not permanent.

    Skipping a clip deletes its record, and the value it was the only user of has
    to go with it, or a typo made once in a folder of eight hundred would be
    offered forever.
    """
    library = tmp_path / "export"
    staging = tmp_path / "import"
    write_clip(library, "Kept", tags=(("network", "Cartoon Network"),))
    vocabulary = Vocabulary(path=str(tmp_path / "vocabulary.json"))
    vocabulary.record({"network": "Nickelodeon"})
    vocabulary.save()

    result = sync_vocabulary(str(library), vocabulary, pending_root=str(staging))

    assert result.values_removed == (("network", "Nickelodeon"),)
    assert vocabulary.values("network") == ("Cartoon Network",)


def test_pending_record_tags_reads_records_with_or_without_their_video(tmp_path):
    """A settled answer is the user's whether or not the file is still there.

    Every `.cnfo` counts, deliberately wider than `build_catalog`'s clip rule:
    pruning a value because the folder is mid-import is the churn this exists to
    stop, and an unreadable record contributes nothing rather than being reported
    — the sync's own walk is what reports those.
    """
    staging = tmp_path / "import"
    directory = staging / "Rips"
    directory.mkdir(parents=True)
    write_record(str(directory / "With video.cnfo"),
                 make_record(tags=(("network", "Cartoon Network"),)))
    write_record(str(directory / "No video.cnfo"),
                 make_record(tags=(("block", "Toonami"),)))
    broken = write_record(str(directory / "Broken.cnfo"), make_record())
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("not xml at all")

    found = pending_record_tags(str(staging))

    assert found == {"network": ["Cartoon Network"], "block": ["Toonami"]}


def test_pending_record_tags_of_a_folder_that_is_not_there_is_empty(tmp_path):
    """Same rule as the catalog: no folder is an empty answer, not a fault. A
    fresh install has no `import/` yet."""
    assert pending_record_tags(str(tmp_path / "never-existed")) == {}


def test_a_sync_keeps_the_shipped_defaults_a_library_does_not_use(tmp_path):
    """The thin-library case, and the reason the defaults are protected.

    One clip is not evidence that the other ten filler types are unused -- it is
    evidence that one clip has been exported. Pruning on that basis is what
    would collapse a new user's dropdowns on their first sync, and the guard is in
    `prune_to` rather than here so the next caller of it cannot reintroduce it.
    """
    write_clip(tmp_path, "Only Bumper", tags=(("filler_type", "Bumper"),))
    vocabulary = Vocabulary.defaults(str(tmp_path / "vocabulary.json"))

    result = sync_vocabulary(str(tmp_path), vocabulary)

    assert result.clips_found == 1
    assert result.values_removed == ()
    assert vocabulary.values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"]))
    assert Vocabulary.load(str(tmp_path / "vocabulary.json")).values(
        "filler_type") == tuple(sorted(DEFAULT_VALUES["filler_type"]))


def test_a_sync_still_removes_a_stale_value_beside_the_protected_defaults(
    tmp_path,
):
    """The paired case. Without it, a fix that simply stopped `prune_to`
    removing anything would pass every other test in this file."""
    write_clip(tmp_path, "Only Bumper", tags=(("filler_type", "Bumper"),))
    vocabulary = Vocabulary.defaults(str(tmp_path / "vocabulary.json"))
    vocabulary.record({"block": "Toonami"})
    vocabulary.save()

    result = sync_vocabulary(str(tmp_path), vocabulary)

    assert result.values_removed == (("block", "Toonami"),)
    assert vocabulary.values("block") == ()
    assert len(vocabulary.values("filler_type")) == len(
        DEFAULT_VALUES["filler_type"])


def test_an_empty_library_does_not_prune_the_shipped_defaults(tmp_path):
    """The rule that keeps a fresh install's dropdowns populated.

    `export/` is empty on a fresh install by design, and an unconditional prune
    would delete the seeded `filler_type` list -- leaving every dropdown on its
    "Populate this list by staging tags" placeholder on a brand-new install. An
    empty library is not evidence that every value is unused; it is evidence
    there is no library.
    """
    vocabulary = Vocabulary.defaults(str(tmp_path / "vocabulary.json"))

    result = sync_vocabulary(str(tmp_path), vocabulary)

    assert result.clips_found == 0
    assert result.skipped_prune is True
    assert result.values_removed == ()
    assert vocabulary.values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"]))


def test_a_missing_root_is_an_empty_library_and_also_does_not_prune(tmp_path):
    vocabulary = Vocabulary.defaults(str(tmp_path / "vocabulary.json"))

    result = sync_vocabulary(str(tmp_path / "never-existed"), vocabulary)

    assert result.skipped_prune is True
    assert vocabulary.values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"]))


def test_a_cancelled_sync_writes_nothing(tmp_path):
    """The rule that keeps a destructive operation survivable.

    The catalog is built in full before anything is mutated and a cancelled one
    is thrown away, because pruning against half a library would delete every
    value the other half is using -- with nothing in the result to tell that
    apart from a correct prune.
    """
    for stem in ("First", "Second", "Third"):
        write_clip(tmp_path, stem, tags=(("network", "Cartoon Network"),))
    vocabulary = Vocabulary(path=str(tmp_path / "vocabulary.json"))
    vocabulary.record({"block": "Toonami"})
    vocabulary.save()
    reached = []

    result = sync_vocabulary(
        str(tmp_path),
        vocabulary,
        on_progress=lambda _found, path: reached.append(path),
        should_cancel=lambda: bool(reached),
    )

    assert result.cancelled is True
    assert result.values_removed == ()
    assert result.values_added == 0
    assert result.skipped_prune is True
    assert vocabulary.values("block") == ("Toonami",)
    assert vocabulary.values("network") == ()
    assert Vocabulary.load(str(tmp_path / "vocabulary.json")).values("block") == (
        "Toonami",)


def test_a_sync_of_a_library_it_has_already_read_changes_nothing(tmp_path):
    """Idempotent, which is what makes the button safe to press twice. The
    second run has nothing to add, nothing to remove, and nothing to write."""
    write_clip(tmp_path, "First", tags=(
        ("network", "Cartoon Network"),
        ("filler_type", "Promo"),
    ))
    path = str(tmp_path / "vocabulary.json")
    vocabulary = Vocabulary(path=path)

    first = sync_vocabulary(str(tmp_path), vocabulary)
    vocabulary.dirty = False
    written = os.path.getmtime(path)
    second = sync_vocabulary(str(tmp_path), vocabulary)

    assert first.values_added == 2
    assert second.values_added == 0
    assert second.values_removed == ()
    assert vocabulary.dirty is False
    assert os.path.getmtime(path) == written


def test_a_sync_names_the_records_it_could_not_read(tmp_path):
    """A silently skipped record is a clip the user could never find again,
    which is the failure the export summary screen exists to stop."""
    write_clip(tmp_path, "First", tags=(("network", "Cartoon Network"),))
    broken = write_clip(tmp_path, "Second")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("not xml at all")
    vocabulary = Vocabulary(path=str(tmp_path / "vocabulary.json"))

    result = sync_vocabulary(str(tmp_path), vocabulary)

    assert result.clips_found == 1
    assert len(result.problems) == 1
    assert result.problems[0].path == "Cartoon Network/Promo/Second.cnfo"
    assert "Second" in result.problems[0].path


def test_a_sync_counts_a_value_added_and_removed_in_the_same_namespace(tmp_path):
    """Measured between the union and the prune rather than after both.

    The two land on the same namespace here: one clip's value arrives while
    another's leaves, and the file ends the run one value longer than it began.
    A size taken after the prune would report that net `+1` and call the
    addition nothing.
    """
    write_clip(tmp_path, "Kept", tags=(("network", "Cartoon Network"),))
    write_clip(tmp_path, "Added", tags=(("network", "Nickelodeon"),))
    vocabulary = Vocabulary(path=str(tmp_path / "vocabulary.json"))
    vocabulary.record({"network": "Cartoon Cartoon Network"})
    vocabulary.save()

    result = sync_vocabulary(str(tmp_path), vocabulary)

    assert result.values_added == 2
    assert result.values_removed == (("network", "Cartoon Cartoon Network"),)
    assert vocabulary.values("network") == ("Cartoon Network", "Nickelodeon")


def test_a_sync_mutates_the_instance_it_was_given_rather_than_a_fresh_copy(
    tmp_path, monkeypatch
):
    """The caller hands over `get_vocabulary()` on purpose, and this is the
    reason that matters: a fresh `Vocabulary.load()` would write a correct file
    and leave the cache stale, so an editor opened later in the same session
    would offer the old dropdowns.

    `load` is made to raise so that a caller which reached for its own copy
    fails loudly instead of quietly producing a file nobody re-reads.
    """
    write_clip(tmp_path, "First", tags=(("network", "Cartoon Network"),))
    path = str(tmp_path / "vocabulary.json")
    cached = get_vocabulary(path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the sync loaded its own vocabulary")

    monkeypatch.setattr("shared.catalog.Vocabulary", refuse, raising=False)
    sync_vocabulary(str(tmp_path), cached)

    assert cached.values("network") == ("Cartoon Network",)
    assert get_vocabulary(path) is cached
    assert Vocabulary.load(path).values("network") == ("Cartoon Network",)


def test_a_sync_with_no_vocabulary_reports_instead_of_raising(tmp_path):
    write_clip(tmp_path, "First", tags=(("network", "Cartoon Network"),))

    result = sync_vocabulary(str(tmp_path), None)

    assert result.clips_found == 1
    assert result.values_added == 0
    assert result.skipped_prune is True