"""Tests for front-end enforcement of the required base record fields.

A keep segment must carry Title, Network, Filler Type, and Time Period before
it can be staged or written to the .cmct sidecar. Ignored segments are exempt,
matching the export preflight, which excludes them from the requirement.
"""

import pytest

from editor_stub import EditorStub, ensure_qapp
from shared.exporting import missing_required_tags
from shared.segments import sidecar_path


# ---------------------------------------------------------------------------
# The shared rule
# ---------------------------------------------------------------------------

def test_missing_required_tags_reports_absent_and_blank():
    assert missing_required_tags({}) == (
        "filler_type", "network", "time_period", "title",
    )
    assert missing_required_tags({
        "title": "A", "network": "CN", "filler_type": "Promo",
        "time_period": "Morning",
    }) == ()


def test_missing_required_tags_treats_whitespace_only_as_missing():
    tags = {
        "title": "A", "network": "  ", "filler_type": "Promo",
        "time_period": "Morning",
    }
    assert missing_required_tags(tags) == ("network",)


def test_missing_required_tags_ignores_optional_tags():
    assert missing_required_tags({
        "title": "A", "network": "CN", "filler_type": "Promo",
        "time_period": "Morning", "year": "", "block": "Toonami",
    }) == ()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def warnings(monkeypatch):
    """Capture QMessageBox.warning instead of showing a modal dialog."""
    calls = []

    def record(parent, title, text, *args, **kwargs):
        calls.append((title, text))

    monkeypatch.setattr("editor.editor.QMessageBox.warning", staticmethod(record))
    return calls


@pytest.fixture
def information(monkeypatch):
    """Capture QMessageBox.information instead of showing a modal dialog."""
    calls = []

    def record(parent, title, text, *args, **kwargs):
        calls.append((title, text))

    monkeypatch.setattr(
        "editor.editor.QMessageBox.information", staticmethod(record))
    return calls


def _editor(qapp, tmp_path, **segment):
    base = {"start": 0.0, "ignored": False, "tags": {}}
    base.update(segment)
    return EditorStub(
        [base, {"start": 30.0, "ignored": False, "tags": {}}],
        media_path=str(tmp_path / "compilation.mp4"),
    )


# ---------------------------------------------------------------------------
# Which tags the form is still missing
# ---------------------------------------------------------------------------

def test_missing_labels_lists_all_four_in_form_order(qapp, tmp_path):
    editor = _editor(qapp, tmp_path)
    assert editor._missing_required_labels() == [
        "Title", "Network", "Type", "Time Period",
    ]


def test_missing_labels_omits_filled_fields(qapp, tmp_path):
    editor = _editor(qapp, tmp_path)
    editor.set_tag("title", "Some Title")
    editor.set_tag("network", "Cartoon Network")
    assert editor._missing_required_labels() == ["Type", "Time Period"]


def test_missing_labels_reads_the_form_not_the_model(qapp, tmp_path):
    editor = _editor(qapp, tmp_path, tags={
        "title": "A", "network": "CN", "filler_type": "Promo",
        "time_period": "Morning",
    })
    editor._write_tags_to_form(
        editor.segment_model.segments[0]["tags"])
    editor.set_tag("network", "")
    assert editor._missing_required_labels() == ["Network"]


def test_ignored_segment_is_exempt(qapp, tmp_path):
    editor = _editor(qapp, tmp_path)
    editor.set_ignored(True)
    assert editor._missing_required_labels() == []


# ---------------------------------------------------------------------------
# The required-field outline
# ---------------------------------------------------------------------------

def test_outline_covers_exactly_the_missing_fields(qapp, tmp_path):
    editor = _editor(qapp, tmp_path)
    editor.set_tag("title", "Some Title")
    assert editor.outlined_required_fields() == [
        "filler_type", "network", "time_period",
    ]


def test_outline_clears_as_each_field_is_filled(qapp, tmp_path):
    editor = _editor(qapp, tmp_path)
    editor.set_tag("title", "Some Title")
    editor.set_tag("network", "Cartoon Network")
    editor.set_tag("filler_type", "Promo")
    assert editor.outlined_required_fields() == ["time_period"]

    editor.set_tag("time_period", "Morning")
    assert editor.outlined_required_fields() == []


def test_outline_clears_when_the_segment_is_ignored(qapp, tmp_path):
    editor = _editor(qapp, tmp_path)
    assert editor.outlined_required_fields() == [
        "filler_type", "network", "time_period", "title",
    ]

    editor.set_ignored(True)
    assert editor.outlined_required_fields() == []


def test_outline_returns_when_a_segment_is_ignored_again(qapp, tmp_path):
    editor = _editor(qapp, tmp_path)
    editor.set_ignored(True)
    editor.go_to(1)
    assert editor.outlined_required_fields() == [
        "filler_type", "network", "time_period", "title",
    ]


def test_outline_does_not_mark_optional_fields(qapp, tmp_path):
    editor = _editor(qapp, tmp_path)
    editor.fill_required()
    assert editor.outlined_required_fields() == []


# ---------------------------------------------------------------------------
# Stage refuses to write an incomplete record
# ---------------------------------------------------------------------------

def test_stage_is_refused_when_required_tags_are_missing(qapp, tmp_path, warnings):
    editor = _editor(qapp, tmp_path)
    editor.set_tag("title", "Some Title")

    editor.on_stage()

    assert warnings, "the user must be told what is missing"
    title, text = warnings[0]
    assert title == "Missing required tags"
    assert "Network" in text and "Type" in text and "Time Period" in text
    assert "Title" not in text


def test_refused_stage_writes_nothing_to_the_model(qapp, tmp_path, warnings):
    editor = _editor(qapp, tmp_path)
    editor.set_tag("title", "Some Title")
    editor.dirty = True

    editor.on_stage()

    assert editor.segment_model.segments[0]["tags"] == {"title": "Some Title"}
    assert editor.dirty is True


def test_refused_stage_does_not_create_the_cmct_sidecar(qapp, tmp_path, warnings):
    editor = _editor(qapp, tmp_path)
    editor.set_tag("title", "Some Title")

    editor.on_stage()

    assert not (tmp_path / "compilation.cmct").exists()


def test_refused_stage_does_not_advance(qapp, tmp_path, warnings):
    editor = _editor(qapp, tmp_path)
    editor.set_tag("title", "Some Title")

    editor.on_stage()

    assert editor.current_index == 0


def test_refused_stage_mentions_the_skip_escape_hatch(qapp, tmp_path, warnings):
    editor = _editor(qapp, tmp_path)
    editor.on_stage()
    assert "skipped" in warnings[0][1]


# ---------------------------------------------------------------------------
# Stage succeeds once the record is complete
# ---------------------------------------------------------------------------

def test_stage_persists_a_complete_record_and_advances(qapp, tmp_path, warnings):
    editor = _editor(qapp, tmp_path)
    editor.fill_required()

    editor.on_stage()

    assert not warnings
    assert (tmp_path / "compilation.cmct").exists()
    assert editor.current_index == 1
    assert editor.dirty is False
    # The form snapshot always carries all ten tag keys; the four required
    # ones are what carry values here.
    tags = editor.segment_model.segments[0]["tags"]
    assert missing_required_tags(tags) == ()
    assert tags["title"] == "Some Title"
    assert tags["network"] == "Cartoon Network"
    assert tags["filler_type"] == "Promo"
    assert tags["time_period"] == "Morning"


def test_stage_allows_an_ignored_segment_with_no_tags(qapp, tmp_path, warnings):
    editor = _editor(qapp, tmp_path)
    editor.set_ignored(True)

    editor.on_stage()

    assert not warnings
    assert (tmp_path / "compilation.cmct").exists()
    assert editor.current_index == 1


def test_stage_treats_whitespace_only_as_missing(qapp, tmp_path, warnings):
    editor = _editor(qapp, tmp_path)
    editor.fill_required(network="   ")

    editor.on_stage()

    assert warnings
    assert "Network" in warnings[0][1]
    assert not (tmp_path / "compilation.cmct").exists()


def test_staging_the_last_segment_points_at_export(qapp, tmp_path, information):
    """The end-of-editing signal used to be a print().

    A print goes nowhere in a windowed build, so staging the final segment --
    the moment the user finds out they are done -- told them nothing at all.
    The record still has to be written first: this is a stage, not a shortcut.
    """
    editor = _editor(qapp, tmp_path)
    editor.go_to(1)
    editor.fill_required()

    editor.on_stage()

    assert information, "the user has to be told they have finished"
    title, text = information[0]
    assert title == "Every segment is staged"
    assert "Finished - Export" in text
    assert (tmp_path / "compilation.cmct").exists()
    # The last segment is the last one; there is nowhere to advance to.
    assert editor.current_index == 1


def test_staged_record_round_trips_through_the_sidecar(qapp, tmp_path, warnings):
    from shared.segments import SegmentModel

    editor = _editor(qapp, tmp_path)
    editor.fill_required()
    editor.on_stage()

    reloaded = SegmentModel.load(sidecar_path(str(tmp_path / "compilation.mp4")))
    assert reloaded.segments[0]["tags"] == editor.segment_model.segments[0]["tags"]
    assert missing_required_tags(reloaded.segments[0]["tags"]) == ()


# ---------------------------------------------------------------------------
# Locked carry-over cannot substitute for the required fields
# ---------------------------------------------------------------------------

def test_locks_alone_do_not_satisfy_the_requirement(qapp, tmp_path, warnings):
    """A new segment inherits locks, but Title is never lockable."""
    editor = _editor(qapp, tmp_path)
    editor.fill_required()
    editor.click_lock("network")
    editor.on_end_segment()
    editor.go_to(1)

    assert editor.get_tag("network") == "Cartoon Network"
    assert editor._missing_required_labels() == [
        "Title", "Type", "Time Period",
    ]

    editor.on_stage()
    assert warnings


# ---------------------------------------------------------------------------
# The sidecar write is the one that can fail late
# ---------------------------------------------------------------------------

def test_a_sidecar_that_cannot_be_written_names_the_file_and_the_folder(
    qapp, tmp_path, warnings, monkeypatch
):
    """Sources come from anywhere now, so the folder holding the `.cmct` can be
    one the app does not control — a read-only share, a locked drive, a folder
    that filled up. `validate_source_video` checks once, at selection; a folder
    that stops taking writes mid-session still has to be reported in words,
    because the bare `PermissionError` this raised named neither the file nor
    the folder, and it arrived on Stage with the user's tags unsaved.
    """
    editor = _editor(qapp, tmp_path)
    editor.fill_required()

    def refuse(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(editor.segment_model, "save", refuse)

    with pytest.raises(ValueError) as error:
        editor._save_sidecar()

    message = str(error.value)
    assert "compilation.cmct" in message
    assert str(tmp_path) in message
    assert "still here" in message


def test_a_failed_sidecar_write_is_reported_and_leaves_the_editor_dirty(
    qapp, tmp_path, warnings, monkeypatch
):
    """`dirty` stays set on purpose. Cleared, the tags would look staged and the
    close handler would not warn about losing them — which is the one thing the
    user needs to be told."""
    editor = _editor(qapp, tmp_path)
    editor.fill_required()
    editor.dirty = True

    def refuse(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(editor.segment_model, "save", refuse)

    editor.on_stage()

    assert editor.dirty is True
    assert editor.current_index == 0, "must not advance past an unsaved segment"
    assert warnings
    title, text = warnings[-1]
    assert title == "Tags could not be saved"
    assert "compilation.cmct" in text
