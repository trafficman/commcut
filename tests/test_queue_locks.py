"""Tests for the Library Mesh Tag Editor's tag locks.

A queue does have a next clip, so the editor's lock — a pinned value, carried
forward, engaged only while the field holds it — is offered here too: an
untidy import should not be the same tag retyped for every clip.

These drive the real `QueueWindow` with mpv and the duration probe stubbed, the
way `tests/test_queue.py` does, and the order the sources are consulted in is
the thing under test: **entered > record > autofill > locked > empty**. The
autofill is the folder structure's answer and outranks a lock, because a lock
is a value carried from a *neighbouring* clip while the folder's is about this
one.
"""

import os

import importer.queue as queue_module
from shared.mesh import MeshSession
from shared.records import load_record
from shared.tag_form import set_field_text

from test_queue import (  # noqa: F401  (the queue's own harness)
    FakeVideo,
    harness,
    qapp,
    teaching_dialog,
    write_sidecar,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fill(window, **values):
    """Type values into the queue's form, the way the user does."""
    for namespace, value in values.items():
        set_field_text(window.ui.tagForm.field(namespace), value)


def _lock(window, namespace):
    """Click a lock toggle, the way the user does."""
    window.ui.tagForm.lock_button(namespace).click()


def _lock_is_engaged(window, namespace):
    return window.ui.tagForm.lock_button(namespace).isChecked()


# ---------------------------------------------------------------------------
# A value typed once is carried to the next clip
# ---------------------------------------------------------------------------

def test_a_locked_value_carries_to_the_next_clip(harness):
    """The point of the lock: the next clip does not have to be retyped."""
    open_queue, root, _ = harness
    window = open_queue("A.mp4", "B.mp4")

    try:
        _fill(window, title="First", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "network")
        window.on_next()

        tags = window.ui.tagForm.read_tags()
        assert tags["network"] == "Cartoon Network"
        assert tags["title"] == "", "a title is unique per clip and never carries"
    finally:
        window.close()
        window.deleteLater()


def test_the_carried_value_is_written_into_the_record(harness):
    """A lock is not a suggestion: the value it carried is the clip's tag."""
    open_queue, root, _ = harness
    window = open_queue("A.mp4", "B.mp4")

    try:
        _fill(window, title="First", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "network")
        window.on_next()

        _fill(window, title="Second", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        window.on_next()

        record = load_record(os.path.join(root, "B.cnfo"))
        assert record is not None
        assert record.tag_dict["network"] == "Cartoon Network"
    finally:
        window.close()
        window.deleteLater()


def test_unlocking_stops_the_carry(harness):
    open_queue, root, _ = harness
    window = open_queue("A.mp4", "B.mp4")

    try:
        _fill(window, title="First", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "network")
        _lock(window, "network")
        window.on_next()

        assert window.ui.tagForm.read_tags()["network"] == ""
    finally:
        window.close()
        window.deleteLater()


def test_a_lock_fills_the_gap_in_a_partial_record(harness):
    """The residue path: a clip came back missing a tag, and the record is
    authoritative about the tags it does hold."""
    open_queue, root, _ = harness
    write_sidecar(root, "B.mp4",
                  {"title": "B Title", "network": "Cartoon Network"})

    window = open_queue("A.mp4", "B.mp4")

    try:
        _fill(window, title="A Title", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "filler_type")
        window.on_next()

        tags = window.ui.tagForm.read_tags()
        assert tags["filler_type"] == "Promo", (
            "the record holds two of the four required tags; the lock answers "
            "the one it is missing")
        assert tags["title"] == "B Title", "and the record still holds its own"
        assert tags["network"] == "Cartoon Network"
    finally:
        window.close()
        window.deleteLater()


def test_typing_over_a_locked_field_disengages_it_and_keeps_the_pin(harness):
    """The editor's rule: a lock is a pinned value, and a field that stops
    matching it is a deliberate deviation rather than a reason to repoint it."""
    open_queue, root, _ = harness
    window = open_queue("A.mp4", "B.mp4")

    try:
        _fill(window, title="First", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "network")
        assert _lock_is_engaged(window, "network")

        set_field_text(window.ui.tagForm.field("network"), "Nickelodeon")

        assert not _lock_is_engaged(window, "network")
        assert window.tag_locks.pinned("network") == "Cartoon Network"
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The folder structure outranks the lock
# ---------------------------------------------------------------------------

def _two_folders(harness):
    """A queue over two folders that each answer the network differently."""
    open_queue, root, _ = harness
    session = MeshSession(
        str(root),
        [FakeVideo("CN/A.mp4"), FakeVideo("Nick/B.mp4")],
        vocabulary=None,
    )
    session.assign("CN", "network", "Cartoon Network")
    session.assign("Nick", "network", "Nickelodeon")
    return open_queue("CN/A.mp4", "Nick/B.mp4", session=session)


def test_the_folder_structure_beats_the_lock(harness):
    """The folder's answer is about this clip; the lock was carried from the
    previous one."""
    window = _two_folders(harness)

    try:
        _fill(window, title="First", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "network")
        window.on_next()

        assert window.ui.tagForm.read_tags()["network"] == "Nickelodeon"
        assert not _lock_is_engaged(window, "network"), (
            "and the toggle says so, rather than claiming the lock is in effect")
        assert window.tag_locks.pinned("network") == "Cartoon Network", (
            "the pin is still held for the clips that follow")
    finally:
        window.close()
        window.deleteLater()


def test_the_overridden_lock_is_said_out_loud(harness):
    """A pin that quietly stops applying looks like a broken button, so the
    queue says which of the two answers won."""
    window = _two_folders(harness)

    try:
        _fill(window, title="First", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "network")
        window.on_next()

        label = window.ui.labelConflicts.text()
        assert "Your locked network" in label
        assert "Cartoon Network" in label
        assert "Nickelodeon" in label
        assert "Untagged Library Mesh" in label, (
            "the folder is where the change belongs")
    finally:
        window.close()
        window.deleteLater()


def test_clicking_a_disengaged_lock_repins_the_current_value(harness):
    """One click says "yes, that one, and keep it"."""
    window = _two_folders(harness)

    try:
        _fill(window, title="First", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "network")
        window.on_next()

        _lock(window, "network")

        assert window.tag_locks.pinned("network") == "Nickelodeon"
        assert _lock_is_engaged(window, "network")
    finally:
        window.close()
        window.deleteLater()


def test_a_contested_namespace_is_not_filled_by_the_lock(harness):
    """Two folder names claiming one tag is the mesh's deliberate refusal to
    pick, and a carried value must not close that question either."""
    open_queue, root, _ = harness
    session = MeshSession(
        str(root),
        [FakeVideo("A.mp4"), FakeVideo("Promo/Ad/B.mp4")],
        vocabulary=None,
    )
    session.assign("Promo", "filler_type", "Promo")
    session.assign("Ad", "filler_type", "Advertisement")

    window = open_queue("A.mp4", "Promo/Ad/B.mp4", session=session)

    try:
        _fill(window, title="A Title", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "filler_type")
        window.on_next()

        assert window.ui.tagForm.read_tags()["filler_type"] == "", (
            "the field is left for the user rather than filled with a value "
            "carried from the previous clip")
        assert "could not be filled" in window.ui.labelConflicts.text()
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The reload after the rules dialog
# ---------------------------------------------------------------------------

def test_a_rule_reaches_a_value_a_lock_carried(harness, monkeypatch):
    """The reload has to be able to change what the *app* put in the form.

    A carried value is the app's own answer, not the user's, so a rule taught
    in the dialog has to be able to replace it — otherwise the rule never
    reaches the clip in front of the user, which is the whole point of the
    dialog.
    """
    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("First.mp4"),
                                     FakeVideo("Second - Toonami.mkv")],
                          vocabulary=None)
    window = open_queue("First.mp4", "Second - Toonami.mkv", session=session)

    try:
        _fill(window, title="First", network="Cartoon Network",
              filler_type="Promo", time_period="2000s")
        _lock(window, "network")
        window.on_next()

        assert window.ui.tagForm.read_tags()["network"] == "Cartoon Network"

        def teach(session):
            session.learn_rule("Toonami", "network", "Toonami USA")

        monkeypatch.setattr(queue_module, "RulesDialog", teaching_dialog(teach))
        window.on_manage_rules()

        assert window.ui.tagForm.read_tags()["network"] == "Toonami USA"
    finally:
        window.close()
        window.deleteLater()


def test_the_reload_still_keeps_a_value_the_user_typed(harness, monkeypatch):
    """The other half of the same rule: what the user typed is theirs."""
    open_queue, root, _ = harness
    session = MeshSession(str(root), [FakeVideo("Toonami.mkv")],
                          vocabulary=None)
    window = open_queue("Toonami.mkv", session=session)

    try:
        set_field_text(window.ui.tagForm.field("title"), "Worlds Finest")

        def teach(session):
            session.learn_rule("Toonami", "network", "Toonami USA")

        monkeypatch.setattr(queue_module, "RulesDialog", teaching_dialog(teach))
        window.on_manage_rules()

        tags = window.ui.tagForm.read_tags()
        assert tags["title"] == "Worlds Finest"
        assert tags["network"] == "Toonami USA"
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# The toggles
# ---------------------------------------------------------------------------

def test_the_queue_shows_the_lock_toggles(harness):
    """A queue does have a next clip to carry a value to."""
    open_queue, root, _ = harness
    window = open_queue("A.mp4")

    try:
        assert not window.ui.tagForm.lock_button("network").isHidden()
        assert not window.ui.tagForm.lock_button("filler_type").isHidden()
    finally:
        window.close()
        window.deleteLater()
