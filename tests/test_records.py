"""Tests for the clip record: its format, its reader, and how it is published."""

import os

import pytest

from shared.records import (
    RECORD_EXTENSION,
    RECORD_SCHEMA_VERSION,
    REASON_INVALID,
    REASON_NOT_A_RECORD,
    REASON_NOT_WELL_FORMED,
    REASON_UNKNOWN_ELEMENT,
    REASON_UNKNOWN_TAG_KEY,
    REASON_UNSUPPORTED_VERSION,
    ClipRecord,
    RecordError,
    load_record,
    parse_record_xml,
    record_error_reason,
    render_record_xml,
    write_record,
)


def make_record(**overrides):
    values = {
        "source": "Cartoon Network - April Fools 2000.mp4",
        "segment_index": 7,
        "start": 314.2,
        "duration": 29.9,
        "tags": (
            ("block", "Toonami"),
            ("network", "Cartoon Network"),
            ("title", "Worlds Finest"),
        ),
    }
    values.update(overrides)
    return ClipRecord(**values)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_a_record_survives_itself():
    """The cheapest possible proof the writer is right: read back what it wrote."""
    record = make_record()

    assert parse_record_xml(render_record_xml(record)) == record


@pytest.mark.parametrize("value", [
    "Ampersand & Co",
    "Less <than> & greater >",
    'Quotes " and apostrophes \'',
    "Mixed ünïcödé — and 中文",
    "A very long descriptive title that goes on for a while",
    "  leading and trailing  ",
    "Smiley \U0001F4E7",
])
def test_a_tag_value_with_awkward_characters_round_trips(value):
    record = make_record(tags=(("title", value),))

    assert parse_record_xml(render_record_xml(record)) == record


def test_the_rendered_document_is_utf8_without_a_byte_order_mark():
    document = render_record_xml(make_record())

    assert document.startswith('<?xml version="1.0" encoding="utf-8"?>')
    assert not document.startswith("\ufeff")
    assert "&amp;" in document or "&" not in document


def test_the_root_carries_the_schema_version():
    document = render_record_xml(make_record())

    assert f'<commcut-clip version="{RECORD_SCHEMA_VERSION}">' in document
    assert "<segment " in document
    assert '<tag key="title">Worlds Finest</tag>' in document


def test_provenance_is_preserved_as_written():
    record = make_record(start=0.0, duration=1234.5, segment_index=0)

    parsed = parse_record_xml(render_record_xml(record))

    assert parsed.source == record.source
    assert parsed.segment_index == 0
    assert parsed.start == 0.0
    assert parsed.duration == 1234.5


def test_the_record_stores_the_source_name_and_nothing_about_the_user():
    """A record travels with the clip, so the folder layout is not part of it."""
    record = make_record(source="compilation.mp4")

    assert "\\\\" not in render_record_xml(record)
    assert parse_record_xml(render_record_xml(record)).source == "compilation.mp4"


def test_tag_order_in_the_source_does_not_change_the_document():
    """Sorted output is what makes two records of one clip diff cleanly."""
    first = make_record(tags=(("title", "A"), ("network", "N")))
    second = make_record(tags=(("network", "N"), ("title", "A")))

    assert render_record_xml(first) == render_record_xml(second)


def test_empty_values_are_omitted_because_a_missing_tag_is_an_empty_one():
    document = render_record_xml(
        make_record(tags=(("title", "Kept"), ("block", "")))
    )

    assert 'key="title"' in document
    assert 'key="block"' not in document


def test_a_record_with_no_tags_is_still_a_record():
    record = make_record(tags=())

    assert parse_record_xml(render_record_xml(record)) == record


# ---------------------------------------------------------------------------
# Values XML cannot hold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("character", [
    "\x00", "\x0b", "\x0c", "\x1f", "￾", "￿", "\r",
])
def test_a_value_xml_cannot_represent_is_refused_by_name(character):
    """ElementTree emits these raw and its own parser then refuses the result.

    Carriage return is in the list despite being legal XML: line-end
    normalization would quietly turn it into a line feed, so the tag would read
    back as something other than what was written.
    """
    with pytest.raises(RecordError, match="title"):
        render_record_xml(make_record(tags=(("title", f"Bad{character}Value"),)))


@pytest.mark.parametrize("character", ["\t", "\n", " ", ""])
def test_the_characters_xml_does_allow_still_round_trip(character):
    record = make_record(tags=(("information", f"edge{character}case"),))

    assert parse_record_xml(render_record_xml(record)) == record


def test_a_refused_value_names_the_offending_key_and_not_another():
    with pytest.raises(RecordError) as error:
        render_record_xml(
            make_record(tags=(("title", "Fine"), ("block", "Bad\x0bValue")))
        )

    assert "'block'" in str(error.value)
    assert "title" not in str(error.value)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_a_record_with_no_version_attribute_reads_as_the_first_schema():
    document = render_record_xml(make_record()).replace(
        f' version="{RECORD_SCHEMA_VERSION}"', "", 1
    )

    assert parse_record_xml(document) == make_record()


def test_a_newer_schema_is_refused_by_version_number():
    document = render_record_xml(make_record()).replace(
        f'version="{RECORD_SCHEMA_VERSION}"',
        f'version="{RECORD_SCHEMA_VERSION + 1}"',
        1,
    )

    with pytest.raises(RecordError, match=str(RECORD_SCHEMA_VERSION + 1)):
        parse_record_xml(document)


def test_a_malformed_version_is_refused():
    document = render_record_xml(make_record()).replace(
        f'version="{RECORD_SCHEMA_VERSION}"', 'version="one"', 1
    )

    with pytest.raises(RecordError, match="not an integer"):
        parse_record_xml(document)


@pytest.mark.parametrize("document", [
    "<commcut-clip version='1'><source>a.mp4</source>",
    "not xml at all",
    '<other-clip version="1"><source>a.mp4</source></other-clip>',
    '<commcut-clip version="1"><source>a.mp4</source></commcut-clip>',
    '<commcut-clip version="1"><source>a.mp4</source>'
    '<segment index="0" start="x" duration="1" /></commcut-clip>',
    '<commcut-clip version="1"><source>a.mp4</source>'
    '<tag key="colour">red</tag></commcut-clip>',
    '<commcut-clip version="1"><source>a.mp4</source>'
    '<surprise /></commcut-clip>',
])
def test_a_document_this_module_does_not_understand_is_refused(document):
    with pytest.raises(RecordError):
        parse_record_xml(document)


def test_a_missing_source_or_segment_is_refused():
    with pytest.raises(RecordError, match="<source>"):
        parse_record_xml(
            '<commcut-clip version="1"><segment index="0" start="0" '
            'duration="1" /></commcut-clip>'
        )
    with pytest.raises(RecordError, match="<segment>"):
        parse_record_xml('<commcut-clip version="1"><source>a.mp4</source></commcut-clip>')


def test_a_scheme_alias_in_a_record_reads_as_its_canonical_key():
    """Read-tolerant, write-canonical: a hand-edited record still loads."""
    document = (
        '<commcut-clip version="1"><source>a.mp4</source>'
        '<segment index="0" start="0" duration="1" />'
        '<tag key="type">Promo</tag></commcut-clip>'
    )

    assert parse_record_xml(document).tags == (("filler_type", "Promo"),)


def test_a_duplicate_tag_reads_as_one_entry_or_not_at_all():
    document = (
        '<commcut-clip version="1"><source>a.mp4</source>'
        '<segment index="0" start="0" duration="1" />'
        '<tag key="title">A</tag><tag key="filler_type">Promo</tag>'
        '<tag key="type">Bumper</tag></commcut-clip>'
    )

    with pytest.raises(RecordError, match="twice"):
        parse_record_xml(document)


def test_load_record_reads_what_write_record_published(tmp_path):
    path = tmp_path / ("Clip" + RECORD_EXTENSION)
    record = make_record()

    write_record(str(path), record)

    assert load_record(str(path)) == record


def test_load_reads_a_record_saved_with_a_byte_order_mark(tmp_path):
    """A record is small user data a person may hand-edit; a BOM is not an error."""
    path = tmp_path / ("Clip" + RECORD_EXTENSION)
    path.write_bytes(b"\xef\xbb\xbf" + render_record_xml(make_record()).encode("utf-8"))

    assert load_record(str(path)) == make_record()


def test_a_missing_record_says_so_rather_than_reporting_a_parse_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_record(str(tmp_path / ("Clip" + RECORD_EXTENSION)))


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def test_publishing_replaces_the_record_that_is_already_there(tmp_path):
    """The delete-the-clip-then-re-export case.

    The preflight has already refused any destination whose video exists, so the
    video slot is free. A no-clobber write here would fail the export over the
    orphan a previous failure left behind.
    """
    path = tmp_path / ("Clip" + RECORD_EXTENSION)
    write_record(str(path), make_record(tags=(("title", "First"),)))

    write_record(str(path), make_record(tags=(("title", "Second"),)))

    assert load_record(str(path)).tag_dict["title"] == "Second"


def test_publishing_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / ("Clip" + RECORD_EXTENSION)

    write_record(str(path), make_record())

    assert [name for name in os.listdir(tmp_path) if name != path.name] == []


def test_a_refused_value_publishes_nothing(tmp_path):
    path = tmp_path / ("Clip" + RECORD_EXTENSION)

    with pytest.raises(RecordError):
        write_record(str(path), make_record(tags=(("title", "Bad\x0bValue"),)))

    assert not path.exists()
    assert os.listdir(tmp_path) == []


def test_a_failed_publish_removes_its_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / ("Clip" + RECORD_EXTENSION)
    monkeypatch.setattr(
        "shared.records.os.replace",
        lambda *arguments: (_ for _ in ()).throw(OSError("no space left")),
    )

    with pytest.raises(OSError, match="no space left"):
        write_record(str(path), make_record())

    assert not path.exists()
    assert os.listdir(tmp_path) == []


def test_publishing_into_a_directory_that_does_not_exist_fails_loudly(tmp_path):
    """The export owns directory creation; this does not second-guess it."""
    with pytest.raises(OSError):
        write_record(str(tmp_path / "missing" / ("Clip" + RECORD_EXTENSION)),
                     make_record())


# ---------------------------------------------------------------------------
# The record is a value, not a mutable dict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tags", [
    [("title", "A")],
    (("type", "Promo"),),
    (("title", "A"), ("title", "B")),
])
def test_a_record_that_is_not_canonical_is_refused(tags):
    """`key=` aliases and duplicate keys are a writer's error, not a reader's."""
    with pytest.raises(RecordError):
        ClipRecord(source="a.mp4", segment_index=0, start=0.0, duration=1.0,
                   tags=tags)


@pytest.mark.parametrize("overrides", [
    {"source": ""},
    {"segment_index": -1},
    {"segment_index": True},
    {"start": "0"},
    {"duration": None},
])
def test_a_record_with_impossible_provenance_is_refused(overrides):
    with pytest.raises(RecordError):
        make_record(**overrides)


def test_the_tag_dict_view_is_a_copy():
    record = make_record()
    view = record.tag_dict

    view["title"] = "changed"

    assert record.tag_dict["title"] == "Worlds Finest"


# ---------------------------------------------------------------------------
# Why a record was refused, as a code
# ---------------------------------------------------------------------------

def reason_for(document):
    with pytest.raises(RecordError) as error:
        parse_record_xml(document)
    return record_error_reason(error.value)


def test_a_reason_is_produced_for_every_kind_of_refusal():
    """One code per cause, so a screen can group a library's problems instead of
    showing the user eighty sentences that all say something is wrong.

    The messages are asserted alongside the codes because the classification reads
    them: a reworded message must fail a test here rather than silently change
    what a caller is told.
    """
    assert reason_for("<commcut-clip><unclosed>") == REASON_NOT_WELL_FORMED
    assert reason_for(
        '<commcut-clip version="1"><source>a.mp4</source>'
        '<segment index="0" start="0" duration="1" />'
        '<tag key="colour">Red</tag></commcut-clip>'
    ) == REASON_UNKNOWN_TAG_KEY
    assert reason_for(
        '<commcut-clip version="1"><source>a.mp4</source>'
        '<segment index="0" start="0" duration="1" />'
        "<mood>grumpy</mood></commcut-clip>"
    ) == REASON_UNKNOWN_ELEMENT
    assert reason_for(
        '<commcut-clip version="99"><source>a.mp4</source>'
        '<segment index="0" start="0" duration="1" /></commcut-clip>'
    ) == REASON_UNSUPPORTED_VERSION
    assert reason_for(
        '<commcut-clip version="zero"><source>a.mp4</source></commcut-clip>'
    ) == REASON_UNSUPPORTED_VERSION
    assert reason_for(
        '<commcut-clip version="0"><source>a.mp4</source></commcut-clip>'
    ) == REASON_UNSUPPORTED_VERSION
    assert reason_for("<playlist><entry/></playlist>") == REASON_NOT_A_RECORD
    assert reason_for(
        '<commcut-clip version="1">'
        '<segment index="0" start="0" duration="1" /></commcut-clip>'
    ) == REASON_INVALID
    assert reason_for(
        '<commcut-clip version="1"><source>a.mp4</source></commcut-clip>'
    ) == REASON_INVALID
    assert reason_for(
        '<commcut-clip version="1"><source>a.mp4</source>'
        '<segment index="0" start="zero" duration="1" /></commcut-clip>'
    ) == REASON_INVALID


def test_the_reason_codes_do_not_collide():
    """They are only useful for grouping if no two causes share a bucket, so this
    fails the moment one starts."""
    codes = {
        REASON_NOT_WELL_FORMED, REASON_NOT_A_RECORD, REASON_UNSUPPORTED_VERSION,
        REASON_UNKNOWN_TAG_KEY, REASON_UNKNOWN_ELEMENT, REASON_INVALID,
    }

    assert len(codes) == 6


def test_a_record_that_parsed_but_cannot_be_held_still_gets_a_reason():
    """`parse_record_xml` sorts tags, so a duplicate key arrives as a canonical
    key twice and is caught by `ClipRecord.__post_init__` rather than by the XML
    walk. The classification has to survive that step, not just the parse."""
    document = (
        '<commcut-clip version="1">'
        "<source>a.mp4</source>"
        '<segment index="0" start="0" duration="1" />'
        '<tag key="network">CN</tag>'
        '<tag key="network">Nick</tag>'
        "</commcut-clip>"
    )

    with pytest.raises(RecordError) as error:
        parse_record_xml(document)

    assert record_error_reason(error.value) == REASON_INVALID
    assert "network" in str(error.value)