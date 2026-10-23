"""Tests for the shared tag-lock model and the order tag sources are consulted in.

Pure, with no Qt. `shared/tag_locks.py` holds the rule that both windows which
carry a value forward obey — the Editing Wizard and the Library Mesh Tag Editor
— and `resolve_tag_sources` is the order a form is filled in when more than one
source has something to say about a tag.

The two questions here are "what is a lock" and "which source wins", and both
have one answer for the whole app, so both are tested here rather than twice.
"""

import pytest

from shared.tag_locks import TagLocks, resolve_tag_sources


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------

def test_a_pin_is_the_value_it_was_given():
    locks = TagLocks()
    locks.pin("network", "Cartoon Network")

    assert locks.pinned("network") == "Cartoon Network"
    assert "network" in locks


def test_an_empty_pin_carries_nothing():
    """Locking an empty field is allowed, and there is nothing in it to carry."""
    locks = TagLocks()
    locks.pin("network", "")

    assert locks.pinned("network") == ""
    assert locks.inherited() == {}
    assert locks.as_dict() == {"network": ""}


def test_a_lock_is_engaged_only_while_the_field_holds_the_pinned_value():
    """The derivation both windows render, and the reason a lock is a value and
    not a live mirror of the field."""
    locks = TagLocks()
    locks.pin("network", "Cartoon Network")

    assert locks.is_engaged("network", "Cartoon Network")
    assert not locks.is_engaged("network", "Nickelodeon")
    assert not locks.is_engaged("network", "")


def test_a_lock_with_no_pin_is_never_engaged():
    assert not TagLocks().is_engaged("network", "Cartoon Network")


def test_releasing_a_pin_that_was_never_set_is_fine():
    locks = TagLocks()
    locks.release("network")

    assert locks.pinned("network") is None


def test_a_title_cannot_be_pinned():
    """A title is unique per clip, so there is nothing for it to carry."""
    with pytest.raises(ValueError, match="title"):
        TagLocks().pin("title", "Toonami Bumper")


def test_a_tag_commcut_does_not_know_cannot_be_pinned():
    with pytest.raises(ValueError, match="not a tag commcut knows"):
        TagLocks().pin("colour", "Blue")


# ---------------------------------------------------------------------------
# The order the sources are consulted in
# ---------------------------------------------------------------------------

def test_entered_beats_every_other_source():
    """It is the only opinion nobody else has."""
    locks = TagLocks({"network": "Cartoon Network"})
    answered = resolve_tag_sources(
        entered={"network": "Nickelodeon"},
        record={"network": "Cartoon Network"},
        autofill={"network": "Cartoon Network"},
        locks=locks,
    )

    assert answered["network"] == "Nickelodeon"


def test_the_record_beats_the_autofill():
    """A clip's tags live in its record."""
    answered = resolve_tag_sources(
        record={"network": "Cartoon Network"},
        autofill={"network": "Nickelodeon"},
    )

    assert answered["network"] == "Cartoon Network"


def test_a_record_suppresses_the_autofill_entirely():
    """The folder answers must not resurrect a tag the Tagged Library Mesh
    removed, so a record that exists is the whole of what is known."""
    answered = resolve_tag_sources(
        record={"title": "A Title"},
        autofill={"network": "Cartoon Network"},
    )

    assert answered == {"title": "A Title"}


def test_the_autofill_beats_the_lock():
    """The folder's answer is about this clip; a lock is about its neighbour."""
    locks = TagLocks({"network": "Cartoon Network"})
    answered = resolve_tag_sources(
        autofill={"network": "Nickelodeon"},
        locks=locks,
    )

    assert answered["network"] == "Nickelodeon"


def test_the_lock_fills_a_field_nothing_else_answered():
    locks = TagLocks({"network": "Cartoon Network"})
    answered = resolve_tag_sources(
        autofill={"block": "Toonami"},
        locks=locks,
    )

    assert answered == {"block": "Toonami", "network": "Cartoon Network"}


def test_the_lock_fills_a_record_s_gap():
    """A record is authoritative about what it holds, and a lock answers the
    rest: the residue path, where a clip came back missing a tag."""
    locks = TagLocks({"filler_type": "Promo"})
    answered = resolve_tag_sources(
        record={"title": "A Title", "network": "Cartoon Network"},
        autofill={"filler_type": "Bumper"},
        locks=locks,
    )

    assert answered == {
        "title": "A Title",
        "network": "Cartoon Network",
        "filler_type": "Promo",
    }


def test_the_lock_does_not_fill_a_contested_namespace():
    """Two folder names claiming one namespace is the mesh's deliberate refusal
    to guess. A carried value would close that question without the user
    deciding it."""
    locks = TagLocks({"filler_type": "Promo"})
    answered = resolve_tag_sources(
        contested=["filler_type"],
        locks=locks,
    )

    assert answered == {}


def test_an_empty_value_is_not_an_answer():
    """A field nobody filled in is a question, and a question is answered by
    the next source rather than by the empty string."""
    locks = TagLocks({"network": "Cartoon Network"})
    answered = resolve_tag_sources(
        record={"network": ""},
        locks=locks,
    )

    assert answered["network"] == "Cartoon Network"


def test_a_namespace_nobody_answered_is_left_out():
    """The form renders an absent namespace as empty."""
    answered = resolve_tag_sources(
        entered={"title": "A Title"},
        autofill={},
        locks=TagLocks(),
    )

    assert answered == {"title": "A Title"}
