"""Tests for the Tagged Library Mesh's model.

`shared/values.py` is the design; `tests/test_values_window.py` is about rendering it.
So the questions asked here are the ones about *what a session does*:

- that nothing is ever inferred — a value matching the library exactly is still a
  question, not an answer
- that a value is one entry however many clips carry it, and that the next question
  is the one that moves the most clips
- that a translation can only ever become another value in the same namespace, or
  stop existing
- that a plan only touches records, and only the ones an answer changed

The fixtures write real files and real records, because the behaviour under test is
partly about which files exist afterwards. A fake catalog would agree with a bug in
`plan_translation` about the paths it invents.
"""

import os

import pytest

from shared.catalog import build_catalog
from shared.records import ClipRecord, write_record
from shared.values import (
    DELETED,
    KEPT,
    TRANSLATED,
    UNTRANSLATED,
    VALUE_TABLE_VERSION,
    ValueSession,
    ValueTable,
    execute_translation,
    plan_translation,
)

#: The four tags an export requires, which is not the same four a value may be asked
#: about — that asymmetry is several tests' whole point.
REQUIRED = {
    "title": "Some Title",
    "network": "Cartoon Network",
    "filler_type": "Promo",
    "time_period": "2000s",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def put_clip(root, relative):
    """A real video file, so `build_catalog` sees a record with a sibling."""
    path = os.path.join(str(root), *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * 16)
    return path


def write_sidecar(root, relative, tags):
    """A record beside a clip. Every field but the tags is the foreign library's."""
    path = put_clip(root, relative)
    write_record(
        os.path.splitext(path)[0] + ".cnfo",
        ClipRecord(source=relative, segment_index=0, start=0.0, duration=30.0,
                   tags=tuple(sorted(tags.items()))),
    )


@pytest.fixture
def foreign(tmp_path):
    """Three clips from somebody else's library, in two folders.

    `CN` is on two of them and `CBS` on one, both under `network`; `Promo` and `Ad`
    are both `filler_type`; `Toonami` is the folder name *and* the block value, which
    is the case the two tables must not confuse.
    """
    root = tmp_path / "import"
    write_sidecar(root, "Toonami/A.mp4", {**REQUIRED, "network": "CN",
                                          "filler_type": "Promo",
                                          "block": "Toonami"})
    write_sidecar(root, "Toonami/B.mp4", {"title": "Second", "network": "CN",
                                          "filler_type": "Ad"})
    write_sidecar(root, "Other/C.mp4", {"title": "Third", "network": "CBS",
                                        "filler_type": "Promo"})
    return root


@pytest.fixture
def session(foreign):
    return ValueSession(str(foreign), build_catalog(str(foreign)))


def answer_everything(session, answers=None):
    """Give every value an answer, and hand the caller each one as it comes.

    The answers default to "keep", which is a real answer and not a no-op: it is what
    takes a value out of the pending set, and a session left pending must refuse to
    plan.
    """
    given = []
    answers = dict(answers or {})
    while session.pending():
        prompt = session.next_prompt()
        entry = prompt.entry
        key = (entry.namespace, entry.value)
        action = answers.pop(key, None)
        if action is None:
            session.keep(*key)
            given.append(key)
        elif len(action) == 1:
            session.delete_tag(*key)
            given.append(key)
        else:
            session.translate(entry.namespace, entry.value, action[0])
            given.append(key)
    return given


# ---------------------------------------------------------------------------
# Nothing is inferred
# ---------------------------------------------------------------------------

def test_a_fresh_session_asks_about_every_value_even_when_the_library_says_it_exactly(
        foreign, tmp_path):
    """The guard this whole window rests on.

    `network: Cartoon Network` is what the user's own library uses for every clip in
    it, and the folder is about to be tagged with exactly that value. The session
    still has to ask, because the only thing that makes an answer an answer is that a
    person gave it. There is no code path from "the evidence agrees" to "answered".
    """
    library_root = tmp_path / "export"
    write_sidecar(library_root, "Cartoon Network/Mine.mp4", REQUIRED)

    mesh = ValueSession(str(foreign), build_catalog(str(foreign)),
                        library=build_catalog(str(library_root)))

    assert mesh.pending(), (
        "a value that matches the library exactly must still be a question. "
        "Inferring it here would put a tag on eight hundred clips on the strength "
        "of a string comparison.")
    assert all(entry.state == UNTRANSLATED for entry in mesh.entries())


def test_nothing_is_written_while_questions_are_open(session):
    """`resolved_value` has to leave an unanswered value as it is.

    It does — but the *reason* it matters is that a half-answered session would
    otherwise write the folder's own vocabulary straight back into its records, which
    is the one outcome this window exists to prevent.
    """
    prompt = session.next_prompt()
    assert prompt.entry.resolved_value() == prompt.entry.value


# ---------------------------------------------------------------------------
# What the folder holds
# ---------------------------------------------------------------------------

def test_one_entry_per_distinct_value_however_many_clips_carry_it(session):
    """The asymmetry with the title, and the reason this window is a table.

    Two clips carrying `network: CN` is **one** question. The same clips' titles are
    two questions, which is what the Library Mesh Tag Editor is for.
    """
    entries = {(e.namespace, e.value): e for e in session.entries()}

    assert entries[("network", "CN")].clip_count == 2
    assert entries[("filler_type", "Promo")].clip_count == 2
    assert entries[("network", "CBS")].clip_count == 1
    assert entries[("block", "Toonami")].clip_count == 1

    assert len(session.entries()) == 6, (
        "six distinct values across three clips — CN, CBS, Promo, Ad, Toonami, "
        "2000s — and none of them a title. Not one entry per clip, and not one per "
        "tag either: `network` and `filler_type` each hold more than one.")


def test_a_title_is_never_asked_about(session):
    """A title is unique per clip, so there is no shared value to translate.

    Translating `Toonami Ep 12` would rename every clip with that title, which is a
    bulk title edit and the Rename Wizard's job. This is the same exclusion
    `namespace_choices` and `learn_rule` already make.
    """
    assert [e for e in session.entries() if e.namespace == "title"] == []
    assert ("title", "Some Title") not in session.pending()


def test_the_next_question_is_the_value_on_the_most_clips(session):
    """Most-first, for the reason `MeshSession._best_chain` is.

    The value that decides the most is the one worth showing, and the rest get cheap
    once it is answered — because the answer applies to all of them. `network: CN` and
    `filler_type: Promo` both decide two clips here, and the tie is broken on the
    sorted key so the order is the same run twice rather than whichever the dict
    happened to hold first.
    """
    first = session.next_prompt()

    assert first.entry.clip_count == 2
    assert (first.entry.namespace, first.entry.value) == ("filler_type", "Promo")


def test_the_order_of_questions_is_reproducible(foreign):
    """Ties break on the sorted key, so a run is the same run twice.

    Without this a session's question order would depend on dict insertion order and
    a screenshot of the screen would not be reproducible.
    """
    orders = []
    for _ in range(3):
        mesh = ValueSession(str(foreign), build_catalog(str(foreign)))
        order = []
        while mesh.pending():
            entry = mesh.next_prompt().entry
            order.append((entry.namespace, entry.value))
            mesh.keep(entry.namespace, entry.value)
        orders.append(order)

    assert orders[0] == orders[1] == orders[2]
    assert orders[0] == sorted(
        orders[0], key=lambda key: (-_clip_counts(foreign)[key], key))


def _clip_counts(root):
    mesh = ValueSession(str(root), build_catalog(str(root)))
    return {(e.namespace, e.value): e.clip_count for e in mesh.entries()}


def test_two_spellings_that_render_to_one_folder_are_one_entry(tmp_path):
    """`value_dedup_key`, so the folder is not asked the same thing twice.

    `CN` and `cn` render to one folder, so filing them under two different answers
    is how a library ends up with two spellings of one network. The key has one
    owner — `shared.vocabulary.value_dedup_key` — and this is the test that the
    value mesh uses it rather than a second normalization of its own.
    """
    root = tmp_path / "import"
    write_sidecar(root, "A.mp4", {**REQUIRED, "network": "CN"})
    write_sidecar(root, "B.mp4", {"title": "Second", "network": "cn"})

    mesh = ValueSession(str(root), build_catalog(str(root)))

    networks = [e for e in mesh.entries() if e.namespace == "network"]
    assert len(networks) == 1
    assert networks[0].clip_count == 2


def test_an_empty_folder_has_nothing_to_ask(tmp_path):
    root = tmp_path / "import"
    os.makedirs(str(root), exist_ok=True)

    mesh = ValueSession(str(root), build_catalog(str(root)))

    assert mesh.entries() == ()
    assert mesh.next_prompt() is None
    assert mesh.is_complete()


# ---------------------------------------------------------------------------
# What an answer may be
# ---------------------------------------------------------------------------

def test_a_value_becomes_another_value_in_the_same_namespace(session):
    session.translate("network", "CN", "Cartoon Network")

    assert session.entry("network", "CN").state == TRANSLATED
    assert session.entry("network", "CN").resolved_value() == "Cartoon Network"
    assert session.find("network", "Cartoon Network") is None, (
        "translating to a value this folder does not hold does not create an entry "
        "for it. The new value belongs to the library, not to the folder.")


def test_a_value_can_stop_existing(session):
    """A foreign library routinely carries a tag this one has no word for, and
    "this tag should not be on the clip at all" is a different answer from "call it
    something else" — so it is a different answer, not a special case."""
    session.delete_tag("filler_type", "Ad")

    assert session.entry("filler_type", "Ad").state == DELETED
    assert session.entry("filler_type", "Ad").resolved_value() == ""


def test_no_call_can_move_a_value_between_namespaces(session):
    """The structural guarantee, not a convention.

    `translate` takes the value and no namespace to move it to; the namespace is the
    entry's. There is nothing to check downstream because there is nothing to write.
    A translation that could change namespaces would be a different operation with a
    different set of conflicts, which is exactly why it is not this one.
    """
    entry = session.entry("network", "CN")

    session.translate("network", "CN", "Cartoon Network")

    assert session.entry("network", "CN").namespace == "network"
    assert session.find("filler_type", "Cartoon Network") is None


def test_an_empty_translation_is_refused_rather_than_deleting_the_tag(session):
    """Two different answers that look identical on screen must not be one call.

    An empty box is a mistyped value; `delete_tag` is the deliberate act. Accepting
    both would make "Remove This Tag" unreachable and would let a stray backspace
    quietly strip a tag off two hundred clips.
    """
    for empty in ("", "   "):
        with pytest.raises(ValueError, match="needs somewhere to go"):
            session.translate("network", "CN", empty)
        assert session.entry("network", "CN").state == UNTRANSLATED


def test_a_value_a_record_could_never_hold_is_refused_at_the_question(session):
    """Found out here, not once per clip at the end of a run.

    A **carriage return** is the case worth pinning rather than an angle bracket.
    `ElementTree` escapes `<`, so it stores and hands back unchanged; a CR it does
    not, because XML line-end normalization turns every one into a newline on the way
    in — a tag that read `Info\\rMore` would come back as `Info\\nMore`, which is the
    silent drift the record format exists to prevent. And the check here is the real
    one, a render through `shared.records.render_record_xml`, because that is the
    function that will refuse the write. A hand-written character check would be a
    second copy of a rule that already has an owner.
    """
    with pytest.raises(ValueError, match="could not be stored"):
        session.translate("network", "CN", "Cartoon\rNetwork")
    assert session.entry("network", "CN").state == UNTRANSLATED

    # And the bracket, which is *legal*, so the refusal is not over-eager.
    session.translate("network", "CN", "Cartoon <Network>")
    assert session.entry("network", "CN").resolved_value() == "Cartoon <Network>"


def test_an_answer_is_given_once_and_the_second_one_says_so(session):
    """The answer is the decision; re-deciding it silently would lose the first."""
    session.keep("network", "CN")

    with pytest.raises(ValueError, match="has already been"):
        session.keep("network", "CN")
    with pytest.raises(ValueError, match="has already been"):
        session.translate("network", "CN", "Cartoon Network")
    with pytest.raises(ValueError, match="has already been"):
        session.delete_tag("network", "CN")


def test_a_value_that_is_not_in_the_folder_is_refused_rather_than_invented(session):
    with pytest.raises(KeyError, match="not a value in"):
        session.translate("network", "Nickelodeon", "Cartoon Network")


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------

def test_the_library_spellings_offered_are_from_this_namespace(session, tmp_path):
    """`match_value` answers across every namespace, and that is the right question
    for the folder wizard — "which of my tags does this word mean?" — and the wrong
    one here, where the tag is settled and only the spelling is in question.

    A `block` called `Promo` says nothing about what a `filler_type` called `Promo`
    should become, so offering it here would be the same confusion the folder pass
    once had, where a warning fired on the ordinary shape of a real library.
    """
    library_root = tmp_path / "export"
    write_sidecar(library_root, "A.mp4", {**REQUIRED, "block": "Promo"})
    write_sidecar(library_root, "B.mp4", {**REQUIRED, "network": "Cartoon Network"})

    mesh = ValueSession(str(foreign), build_catalog(str(foreign)),
                        library=build_catalog(str(library_root)))

    for namespace in ("filler_type", "block", "network"):
        for match in mesh.matches_for(namespace, "Promo") + mesh.matches_for(
                namespace, "CN"):
            assert match.namespace == namespace


def test_a_value_the_library_has_never_seen_offers_nothing_and_says_so(session):
    """Empty is not permission to guess: it means new here, and the user has to ask."""
    assert session.matches_for("network", "CN") == ()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_a_session_with_questions_open_is_never_planned(session):
    """The refusal that matters most.

    An unanswered value resolves to itself, so planning a half-answered session
    would write the folder's own vocabulary back into its records and report success.
    """
    session.keep("network", "CN")
    assert not session.is_complete()

    with pytest.raises(ValueError, match="have no answer yet"):
        plan_translation(session)


def test_a_translation_rewrites_every_record_carrying_the_value_and_only_those(
        foreign, session):
    session.translate("network", "CN", "Cartoon Network")
    session.keep("network", "CBS")
    session.keep("filler_type", "Promo")
    session.delete_tag("filler_type", "Ad")
    session.keep("block", "Toonami")
    session.keep("time_period", "2000s")

    plan = plan_translation(session)

    assert [c.relative_path for c in plan.clips] == ["Toonami/A.mp4", "Toonami/B.mp4"], (
        "A and B carry `network: CN`. C does not, and `time_period: 2000s` is on A "
        "alone but was kept, so nothing else moves.")


def test_the_video_is_never_touched_only_its_record_is_replaced(foreign, session):
    """Invariant 12, checked rather than asserted.

    `write_record` publishes atomically in the record's own directory and *replaces*,
    so there is no instant at which a reader sees a half-written record or a record
    without its video. That is a property of the writer; what this checks is that
    nothing here bypasses it by touching the video path at all.
    """
    before = {name: os.path.getsize(os.path.join(str(foreign), *name.split("/")))
              for name in ("Toonami/A.mp4", "Toonami/B.mp4", "Other/C.mp4")}

    session.translate("network", "CN", "Cartoon Network")
    answer_everything(session)
    result = execute_translation(plan_translation(session))

    assert result.written, "this fixture must actually rewrite something"
    for name, size in before.items():
        assert os.path.isfile(os.path.join(str(foreign), *name.split("/")))
        assert os.path.getsize(os.path.join(str(foreign), *name.split("/"))) == size


def test_the_replaced_record_is_still_a_valid_record(foreign, session):
    """A translation is checked by rendering, so the record it writes is one that
    can be read back. `build_catalog` is the reader."""
    session.translate("filler_type", "Ad", "Commercial")
    answer_everything(session)
    execute_translation(plan_translation(session))

    tags = {clip.relative_path: clip.tag_dict()
            for clip in build_catalog(str(foreign)).clips}

    assert tags["Toonami/B.mp4"]["filler_type"] == "Commercial"
    assert tags["Toonami/A.mp4"]["filler_type"] == "Promo", (
        "only the clips carrying `Ad` change")


def test_a_deleted_tag_is_gone_from_the_record_and_the_record_still_reads(
        foreign, session):
    session.delete_tag("filler_type", "Ad")
    answer_everything(session)
    execute_translation(plan_translation(session))

    tags = {clip.relative_path: clip.tag_dict()
            for clip in build_catalog(str(foreign)).clips}

    assert "filler_type" not in tags["Toonami/B.mp4"]
    assert tags["Toonami/B.mp4"]["title"] == "Second", (
        "everything else about the record survives: a translation changes what the "
        "clip is called, not where it came from")


def test_keeping_everything_writes_nothing(foreign, session):
    """A user who answered "keep" forty times has decided the folder is fine, and
    rewriting forty records with identical content is not a harmless thing to do to
    a folder of eight hundred."""
    answer_everything(session)

    plan = plan_translation(session)

    assert plan.clips == ()
    assert execute_translation(plan).written == ()


def test_two_values_merging_is_reported_and_permitted(session):
    """Legal, and often the point — but it is the one outcome that makes two clips
    indistinguishable, so it is computed from the answers and shown rather than
    discovered later in an import summary."""
    session.translate("network", "CN", "Cartoon Network")
    session.translate("network", "CBS", "Cartoon Network")
    session.keep("filler_type", "Promo")
    session.delete_tag("filler_type", "Ad")
    session.keep("block", "Toonami")

    merges = session.merges()

    assert len(merges) == 1
    assert merges[0].namespace == "network"
    assert merges[0].value == "Cartoon Network"
    assert merges[0].sources == ("CBS", "CN")
    assert "indistinguishable" in session.report()


def test_two_names_for_one_thing_in_the_library_merge_with_no_action(session):
    """The case that is not the user's doing: the folder already held both."""
    session.keep("network", "CN")
    session.keep("network", "CBS")

    merges = session.merges()
    assert merges == (), (
        "nothing was translated, so nothing merged. Two values only become one when "
        "an answer makes them the same value.")


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------

def test_a_run_that_does_not_finish_keeps_what_it_committed(foreign, session):
    """The same rule the export and the import follow, for the same reason: a
    re-run has to be able to finish the job, and a half-applied batch that had to be
    undone would be worse than either."""
    session.translate("network", "CN", "Cartoon Network")
    answer_everything(session)
    plan = plan_translation(session)

    committed = []

    def on_progress(done, path):
        committed.append(path)

    result = execute_translation(
        plan, on_progress=on_progress,
        should_cancel=lambda: len(committed) >= 1)

    assert result.cancelled is True
    assert len(result.written) == 1
    assert result.failed == ()
    assert build_catalog(str(foreign)).clips, "the folder is intact"
    assert build_catalog(str(foreign)).tag_index["network"] == (
        "CBS", "CN", "Cartoon Network"), (
        "the one record it did write stays written, and the two it did not are still "
        "what they were. Re-running finishes the rest, because `plan_translation` is "
        "a pure function of the records as they now stand.")


def test_a_record_that_cannot_be_written_costs_one_clip_and_is_named(
        foreign, session, monkeypatch):
    """Per-record, like `plan_import`, for the same reason: one bad record should
    not cost the batch, and a clip the run dropped must be findable afterwards."""
    session.translate("network", "CN", "Cartoon Network")
    answer_everything(session)
    plan = plan_translation(session)

    import shared.values as values_module

    real = values_module.write_record

    def flaky(path, record):
        # The record path, not the video's: `execute_translation` is handed the
        # `.cnfo` and never learns the clip's name.
        if os.path.basename(path) == "B.cnfo":
            raise OSError("file is locked")
        real(path, record)

    monkeypatch.setattr(values_module, "write_record", flaky)
    result = execute_translation(plan)

    assert result.written == ("Toonami/A.mp4",)
    assert len(result.failed) == 1
    assert "Toonami/B.mp4" in str(result.failed[0])
    assert "locked" in str(result.failed[0])


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def test_the_report_says_what_the_folder_held_and_what_became_of_it(session):
    session.translate("network", "CN", "Cartoon Network")
    session.delete_tag("filler_type", "Ad")
    session.keep("network", "CBS")
    session.keep("filler_type", "Promo")
    session.keep("block", "Toonami")
    session.keep("time_period", "2000s")

    report = session.report()

    assert "6 tag value(s)" in report
    assert "across 3 clip(s)" in report
    assert "1 translated" in report
    assert "1 tag(s) removed" in report
    assert "network: CN  ->  Cartoon Network" in report
    assert "0 left to go" in report


def test_the_report_names_the_values_still_waiting(session):
    session.keep("network", "CN")

    assert "left to go" in session.report()
    assert "have no answer" in session.report()


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def test_the_table_holds_every_answer_and_no_open_ones(session):
    session.translate("network", "CN", "Cartoon Network")
    session.delete_tag("filler_type", "Ad")

    table = session.table()

    assert [(e.namespace, e.value, e.state) for e in table] == [
        ("filler_type", "Ad", DELETED),
        ("network", "CN", TRANSLATED),
    ], (
        "`block: Toonami` was never answered, so it is not in the output. A table "
        "that carried the open ones would look like a decision was made about it.")


def test_the_table_round_trips(session):
    session.translate("network", "CN", "Cartoon Network")
    session.delete_tag("filler_type", "Ad")
    session.keep("network", "CBS")

    restored = ValueTable.from_dict(session.table().to_dict())

    assert [(e.namespace, e.value, e.state, e.translated_to) for e in restored] == [
        (e.namespace, e.value, e.state, e.translated_to)
        for e in session.table()]


def test_the_clip_count_is_not_persisted_because_it_is_not_a_decision(session):
    """Same reasoning as `AliasEntry.paths`: it is how much a decision affected *in
    this run*, recomputed from the tree every time. Persisting it would freeze a
    number that goes stale the moment a folder moves."""
    session.keep("network", "CN")

    restored = ValueTable.from_dict(session.table().to_dict())

    assert all(entry.clip_count == 0 for entry in restored)
    assert session.entry("network", "CN").clip_count == 2


def test_a_table_from_a_newer_build_is_refused_rather_than_half_read(session):
    session.keep("network", "CN")
    data = session.table().to_dict()

    data["version"] = 99

    with pytest.raises(ValueError, match="value table version"):
        ValueTable.from_dict(data)


def test_a_value_map_for_a_tag_this_build_does_not_know_is_refused(session):
    """Not a wrong translation applied — a half-read map is how a tag gets applied
    wrongly, and the records would then carry a key nothing can render."""
    data = {"version": VALUE_TABLE_VERSION,
            "entries": [{"namespace": "colour", "value": "CN",
                         "state": TRANSLATED, "translated_to": "x"}]}

    with pytest.raises(ValueError, match="not a tag a value can be translated on"):
        ValueTable.from_dict(data)


def test_an_unknown_state_costs_the_answer_not_the_table(session):
    session.translate("network", "CN", "Cartoon Network")
    data = session.table().to_dict()
    data["entries"][0]["state"] = "translated-ish"

    table = ValueTable.from_dict(data)

    assert table.entries[0].state == UNTRANSLATED
