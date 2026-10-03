"""Tests for the Library Mesh Wizard's model.

The wizard's whole safety property is that a folder name becomes a tag only because
a person said so, and that is enforced by there being no code path that fills the
table without an explicit `assign`. These tests drive `MeshSession` directly, which
is what the window does too — so they are the design, and the window tests are
about rendering it.

`tests/test_importing.py` owns `match_value`, which supplies the evidence this
module ranks; these test what it does with that evidence.
"""

import os

import pytest

from shared.catalog import Catalog, CatalogClip
from shared.mesh import (
    ALIAS_TABLE_VERSION,
    COLOURS,
    LEARNED_FOLDER,
    LEARNED_RULE,
    MESHED,
    REJECTED,
    UNMESHED,
    AliasTable,
    MeshConflict,
    MeshSession,
    folder_chain,
    namespace_choices,
    unmeshed_names,
)
from shared.records import ClipRecord
from shared.vocabulary import Vocabulary


# ---------------------------------------------------------------------------
# Harnesses
# ---------------------------------------------------------------------------

class FakeVideo:
    """A `FoundVideo` without the filesystem behind it.

    The session reads only `relative_path`, and building real files for a test
    about which question comes next would be arranging more than is being tested.
    """

    def __init__(self, relative_path, has_record=False):
        self.path = os.path.normpath(os.path.join("C:/library", relative_path))
        self.relative_path = relative_path
        self.has_record = has_record


def videos(*relative_paths):
    return tuple(FakeVideo(path) for path in relative_paths)


def session(paths, library=None, vocabulary=None, root="C:/library"):
    return MeshSession(root, videos(*paths), library=library,
                       vocabulary=vocabulary)


def catalog_with(*pairs):
    """A destination library holding one clip per `(namespace, value)`."""
    clips = tuple(
        CatalogClip(
            path=f"/library/{namespace}/{index}.mp4",
            relative_path=f"{namespace}/{index}.mp4",
            record_path=f"/library/{namespace}/{index}.cnfo",
            tags=((namespace, value),),
            record=ClipRecord(source="theirs.mp4", segment_index=0, start=0.0,
                              duration=1.0, tags=((namespace, value),)),
        )
        for index, (namespace, value) in enumerate(pairs)
    )
    return Catalog(clips=clips)


# ---------------------------------------------------------------------------
# Folder names are the input, and nothing else is read
# ---------------------------------------------------------------------------

def test_the_folder_chain_is_the_names_without_the_file():
    assert folder_chain("CN/2000s/Promo/A Clip.mp4") == ("CN", "2000s", "Promo")
    assert folder_chain("A Clip.mp4") == ()
    assert folder_chain("") == ()


def test_the_chain_does_not_take_the_file_name_as_a_tag():
    """The narrowing to folder names, and the reason it is safe: a filename
    contributes nothing, so the wizard cannot be argued into the reverse parser."""
    assert folder_chain("CN/2000s/Promo/Worlds Finest.mp4")[-1] == "Promo"


def test_unmeshed_names_is_one_entry_per_distinct_folder_name():
    assert unmeshed_names(videos(
        "CN/2000s/A.mp4", "CN/2000s/B.mp4", "Nickelodeon/1990s/C.mp4",
    )) == ("1990s", "2000s", "CN", "Nickelodeon")


def test_a_video_at_the_top_of_the_folder_contributes_no_name():
    loose = session(["A Clip.mp4", "CN/B Clip.mp4"])

    assert loose.pending() == ("CN",)


def test_the_entry_count_is_the_path_count_of_each_name():
    loose = session(["CN/A.mp4", "CN/B.mp4", "CN/2000s/C.mp4"])

    assert loose.entry("CN").paths == 3
    assert loose.entry("2000s").paths == 1


# ---------------------------------------------------------------------------
# The rule the whole wizard exists for
# ---------------------------------------------------------------------------

def test_a_fresh_session_is_empty_even_when_every_name_matches_exactly():
    """The never-inferred rule.

    Four folder names, all of which the library already uses, all in one
    unambiguous namespace. A defaulting shortcut anywhere in the constructor would
    fill this table, and then "import every clip with the tags it must have" would
    be deciding tags rather than asking for them.
    """
    loose = session(
        ["Cartoon Network/2000s/Promo/A.mp4",
         "Cartoon Network/2000s/Promo/B.mp4"],
        library=catalog_with(("network", "Cartoon Network"),
                             ("time_period", "2000s"),
                             ("filler_type", "Promo")),
    )

    assert loose.pending() == ("2000s", "Cartoon Network", "Promo")
    assert loose.entries() and all(
        entry.state == UNMESHED for entry in loose.entries())
    assert loose.aliases() == AliasTable(())


def test_an_exact_match_is_a_suggestion_not_a_decision():
    loose = session(["Toonami/A.mp4"], library=catalog_with(("block", "Toonami")))

    prompt = loose.next_prompt()

    assert prompt.suggested_namespace == "block"
    assert loose.entry("Toonami").state == UNMESHED
    assert loose.is_complete() is False


def test_nothing_but_assign_and_reject_changes_a_decision():
    loose = session(["CN/A.mp4"])

    loose.next_prompt()

    assert loose.entry("CN").state == UNMESHED


# ---------------------------------------------------------------------------
# Sequencing
# ---------------------------------------------------------------------------

def test_the_prompt_takes_the_path_with_the_most_unmeshed_names():
    """Not depth-first: a path with five open questions teaches the most, and a
    path whose names are all decided teaches nothing."""
    loose = session([
        "one/A.mp4",                       # one unmeshed
        "two/three/four/five/B.mp4",       # four
    ])

    prompt = loose.next_prompt()

    assert prompt.name == "two"
    assert [segment.name for segment in prompt.segments] == [
        "two", "three", "four", "five"]
    assert prompt.segments[0].is_current is True
    assert prompt.unmeshed_paths == 4


def test_within_a_path_the_leftmost_unmeshed_name_comes_first():
    loose = session(["CN/Cartoon Network/2000s/Promo/A.mp4"])

    assert loose.next_prompt().name == "CN"


def test_an_already_answered_name_is_skipped_along_the_same_path():
    loose = session(["CN/Cartoon Network/2000s/Promo/A.mp4"])
    loose.assign("CN", "network", "Cartoon Network")

    assert loose.next_prompt().name == "Cartoon Network"


def test_a_most_first_path_beats_a_shallower_one_once_the_shallow_one_is_done():
    loose = session(["CN/2000s/A.mp4", "one/two/three/B.mp4"])
    loose.assign("CN", "network", "CN")

    prompt = loose.next_prompt()

    assert prompt.name == "one", "the untouched path now has the most open"


def test_ties_break_on_the_sorted_path_so_a_session_is_reproducible():
    paths = ("zebra/one/A.mp4", "alpha/one/B.mp4")

    assert (session(paths).next_prompt().name
            == session(paths).next_prompt().name == "alpha")


def test_the_next_prompt_is_none_exactly_when_every_name_is_dealt_with():
    loose = session(["CN/2000s/A.mp4"])

    assert loose.next_prompt() is not None
    loose.assign("CN", "network", "CN")
    loose.assign("2000s", "time_period", "2000s")

    assert loose.is_complete() is True
    assert loose.next_prompt() is None


def test_a_rejected_name_is_never_offered_again():
    loose = session(["CN/2000s/A.mp4"])
    loose.reject("CN")

    assert loose.next_prompt().name == "2000s"
    assert loose.entry("CN").state == REJECTED


def test_a_name_is_answered_once_for_every_path_containing_it():
    """One entry per name, not per path: forty `CN` folders is one answer, and it
    is what makes the alias idea work at all."""
    loose = session([f"CN/{index}.mp4" for index in range(40)])

    loose.assign("CN", "network", "Cartoon Network")

    assert loose.entry("CN").paths == 40
    assert loose.next_prompt() is None


# ---------------------------------------------------------------------------
# Assigning
# ---------------------------------------------------------------------------

def test_assigning_records_the_namespace_and_the_value():
    loose = session(["CN/A.mp4"])

    loose.assign("CN", "network", "Cartoon Network")

    entry = loose.entry("CN")
    assert (entry.state, entry.namespace, entry.value) == (
        MESHED, "network", "Cartoon Network")


def test_a_blank_value_is_refused_because_a_folder_name_must_become_something():
    loose = session(["CN/A.mp4"])

    with pytest.raises(ValueError, match="needs a tag value"):
        loose.assign("CN", "network", "   ")


def test_an_unknown_namespace_is_refused():
    loose = session(["CN/A.mp4"])

    with pytest.raises(ValueError, match="not a tag commcut knows"):
        loose.assign("CN", "colour", "Red")


def test_a_second_answer_for_one_name_is_refused():
    """A decision applies to every path containing the name, so changing it
    halfway would mean two different answers for the same folder."""
    loose = session(["CN/A.mp4"])
    loose.assign("CN", "network", "Cartoon Network")

    with pytest.raises(ValueError, match="already been meshed"):
        loose.assign("CN", "network", "Nickelodeon")


def test_a_name_that_is_not_in_the_tree_is_refused():
    loose = session(["CN/A.mp4"])

    with pytest.raises(KeyError):
        loose.assign("Nickelodeon", "network", "Nickelodeon")


def test_the_namespace_choices_never_include_title():
    """A folder is a shared label; title is unique per clip. Meshing a folder onto
    title would give every clip under it the same name."""
    assert "title" not in namespace_choices()
    assert len(namespace_choices()) == 9


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def test_two_names_in_one_path_claiming_one_namespace_conflict():
    """The case that is real: one clip would be handed two values for one tag.

    `Up Next` and `Promo` sit in the *same* folder path, so the clip under them
    has nowhere to put two `filler_type` values and a record holds one.
    """
    loose = session(["CN/Up Next/Promo/A.mp4"])
    loose.assign("CN", "network", "Cartoon Network")
    loose.assign("Up Next", "filler_type", "Up Next")

    conflict = loose.assign("Promo", "filler_type", "Promo")

    assert isinstance(conflict, MeshConflict)
    assert conflict.namespace == "filler_type"
    assert {conflict.existing_name, conflict.incoming_name} == {"Up Next", "Promo"}
    assert "CN/Up Next/Promo" in conflict.example_path
    assert "need editing by hand" in conflict.describe()


def test_two_folders_meaning_the_same_namespace_on_separate_paths_do_not_conflict():
    """The rule, stated in the negative, and the test whose absence let the
    original bug through.

    A library with a dozen folders all meaning `filler_type` is the ordinary shape
    of any real library. No clip ever sees two of them at once, so there is
    nothing to warn about -- and a warning that fires on the normal case is
    dismissed reflexively, which is the worst possible fate for the one case that
    matters.
    """
    loose = session([
        "CN/2000s/Promo/A.mp4",
        "CN/2000s/Bumper/B.mp4",
        "CN/2000s/Cartoon/C.mp4",
        "CN/2000s/PSA/D.mp4",
        "CN/2000s/Billboard/E.mp4",
    ])
    loose.assign("CN", "network", "Cartoon Network")

    assert loose.assign("Promo", "filler_type", "Promo") is None
    assert loose.assign("Bumper", "filler_type", "Bumper") is None
    assert loose.assign("Cartoon", "filler_type", "Cartoon") is None
    assert loose.assign("PSA", "filler_type", "PSA") is None
    assert loose.assign("Billboard", "filler_type", "Billboard") is None
    assert loose.conflicts() == ()
    assert loose.affected_paths() == ()


def test_two_networks_in_separate_folders_do_not_conflict_either():
    """The same thing for the namespace where it is least likely: a library can
    hold a `CN` folder and a `Nickelodeon` folder with nothing to reconcile."""
    loose = session(["CN/A.mp4", "Nickelodeon/B.mp4"])

    loose.assign("CN", "network", "Cartoon Network")

    assert loose.assign("Nickelodeon", "network", "Nickelodeon") is None


def test_a_conflict_is_found_however_the_two_names_were_answered():
    """Which answer is "incoming" depends on the order they were asked in, so what
    has to hold is that a conflict is found, on the right namespace, between the
    same two names -- not that it reads identically either way round."""
    paths = ["CN/Up Next/Promo/A.mp4"]
    one = session(paths)
    one.assign("Up Next", "filler_type", "Up Next")
    one.assign("Promo", "filler_type", "Promo")

    other = session(paths)
    other.assign("Promo", "filler_type", "Promo")
    other.assign("Up Next", "filler_type", "Up Next")

    for loose in (one, other):
        conflicts = loose.conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].namespace == "filler_type"
        assert {conflicts[0].existing_name,
                conflicts[0].incoming_name} == {"Up Next", "Promo"}


def test_two_names_claiming_one_namespace_with_the_same_value_do_not_conflict():
    """Two spellings of one thing is the ordinary case and must not nag."""
    loose = session(["CN/Cartoon Network/A.mp4"])
    loose.assign("CN", "network", "Cartoon Network")

    assert loose.assign("Cartoon Network", "network", "Cartoon Network") is None


def test_the_same_value_differing_only_in_case_does_not_conflict():
    loose = session(["CN/C.N/A.mp4"])
    loose.assign("CN", "network", "Cartoon Network")

    assert loose.assign("C.N", "network", "cartoon network") is None


# ---------------------------------------------------------------------------
# Derived tags
# ---------------------------------------------------------------------------

def test_tags_come_from_every_meshed_folder_name_on_the_path():
    loose = session(["Cartoon Network/2000s/Promo/A.mp4"])
    loose.assign("Cartoon Network", "network", "Cartoon Network")
    loose.assign("2000s", "time_period", "2000s")
    loose.assign("Promo", "filler_type", "Promo")

    result = loose.tags_for("Cartoon Network/2000s/Promo/A.mp4")

    assert result.tags == {"network": "Cartoon Network",
                           "time_period": "2000s", "filler_type": "Promo"}
    assert result.resolved is True
    assert result.needs_manual_edit is False


def test_a_rejected_or_unmeshed_folder_name_contributes_nothing():
    """Its videos still import; they simply get no tag from that name. A rejected
    one is a decision, and an unmeshed one is a hole, and neither may guess."""
    loose = session(["CN/2000s/A.mp4"])
    loose.assign("2000s", "time_period", "2000s")
    loose.reject("CN")

    result = loose.tags_for("CN/2000s/A.mp4")

    assert result.tags == {"time_period": "2000s"}
    assert result.resolved is True


def test_a_path_where_two_names_claim_one_namespace_gets_neither_value():
    """Option 3: nobody guesses.

    Shallowest-wins and deepest-wins both write an arbitrary choice into the same
    record a deliberate tag would go in, where nothing later can tell them apart.
    So the contested namespace is left out entirely and the clip is marked as
    needing a person.
    """
    loose = session(["Up Next/Promo/A.mp4"])
    loose.assign("Up Next", "filler_type", "Up Next")
    loose.assign("Promo", "filler_type", "Promo")

    result = loose.tags_for("Up Next/Promo/A.mp4")

    assert "filler_type" not in result.tags, (
        "neither value may be chosen for the clip")
    assert result.resolved is False
    assert result.needs_manual_edit is True


def test_an_unresolved_clip_keeps_the_tags_that_were_decided():
    """The edit screen can prefill everything that is settled and ask about only
    the one that is not, which is the point of withholding just the namespace."""
    loose = session(["CN/2000s/Up Next/Promo/A.mp4"])
    loose.assign("CN", "network", "Cartoon Network")
    loose.assign("2000s", "time_period", "2000s")
    loose.assign("Up Next", "filler_type", "Up Next")
    loose.assign("Promo", "filler_type", "Promo")

    result = loose.tags_for("CN/2000s/Up Next/Promo/A.mp4")

    assert result.tags == {"network": "Cartoon Network",
                           "time_period": "2000s"}
    assert result.needs_manual_edit is True


def test_a_third_folder_cannot_reclaim_a_contested_namespace():
    """Otherwise the middle folder removes the tag and the last one quietly puts
    a value back, which is the guess option 3 exists to refuse."""
    loose = session(["Up Next/Animated/Promo/A.mp4"])
    loose.assign("Up Next", "filler_type", "Up Next")
    loose.assign("Promo", "filler_type", "Promo")
    loose.assign("Animated", "filler_type", "Animated")

    result = loose.tags_for("Up Next/Animated/Promo/A.mp4")

    assert "filler_type" not in result.tags


def test_one_bad_path_does_not_affect_its_neighbours():
    """The point of checking per path: a collision is local."""
    loose = session(["Up Next/Promo/A.mp4", "Promo/B.mp4", "Bumper/C.mp4"])
    loose.assign("Up Next", "filler_type", "Up Next")
    loose.assign("Promo", "filler_type", "Promo")
    loose.assign("Bumper", "filler_type", "Bumper")

    assert loose.tags_for("Up Next/Promo/A.mp4").needs_manual_edit is True
    assert loose.tags_for("Promo/B.mp4").tags == {"filler_type": "Promo"}
    assert loose.tags_for("Bumper/C.mp4").tags == {"filler_type": "Bumper"}


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def test_the_prompt_marks_the_current_name_and_colours_each_segment():
    loose = session(["CN/2000s/Promo/A.mp4"])

    segments = loose.next_prompt().segments

    assert [segment.is_current for segment in segments] == [True, False, False]
    assert len({segment.colour for segment in segments}) == 3, (
        "two names on one path must be tellable apart")


def test_colours_are_stable_for_a_name_within_a_session():
    """First appearance in sorted order, not `hash()`: Python salts str hashes
    per process, so a hash-derived colour would change between runs for no reason
    the user could see."""
    loose = session(["CN/2000s/A.mp4", "CN/Promo/B.mp4"])

    first = loose.next_prompt().segments
    loose.assign("CN", "network", "CN")
    second = loose.next_prompt().segments

    assert [s.colour for s in first if s.name == "CN"] == [
        s.colour for s in second if s.name == "CN"]


def test_two_sessions_over_the_same_tree_agree_on_colours():
    paths = ["zebra/one/A.mp4", "alpha/two/B.mp4"]

    def colours_for(sess):
        prompt = sess.next_prompt()
        return {segment.name: segment.colour for segment in prompt.segments}

    assert colours_for(session(paths)) == colours_for(session(paths))


def test_colours_come_from_the_palette():
    loose = session(["CN/A.mp4"])

    assert loose.next_prompt().segments[0].colour in COLOURS


def test_an_already_answered_segment_shows_its_state_on_the_path_bar():
    loose = session(["CN/2000s/A.mp4"])
    loose.assign("CN", "network", "Cartoon Network")

    segments = loose.next_prompt().segments

    assert [segment.state for segment in segments] == [MESHED, UNMESHED]


def test_the_prompt_suggests_a_namespace_only_when_there_is_exactly_one_match():
    """Two candidates and the wizard picks one is the guess it exists to prevent."""
    ambiguous = session(["Saturday/A.mp4"], library=catalog_with(
        ("block", "Saturday"), ("special", "Saturday")))
    clear = session(["Saturday/A.mp4"], library=catalog_with(
        ("block", "Saturday")))

    assert ambiguous.next_prompt().suggested_namespace is None
    assert clear.next_prompt().suggested_namespace == "block"


def test_the_prompt_ranks_the_candidate_values_with_their_evidence():
    loose = session(["Saturday/A.mp4"], library=catalog_with(
        ("block", "Saturday"), ("special", "Saturday"), ("special", "Saturday")))

    matches = loose.next_prompt().suggested_values

    assert [m.namespace for m in matches] == ["special", "block"]
    assert [m.clip_count for m in matches] == [2, 1]


def test_a_name_the_library_never_heard_of_has_no_suggestions():
    loose = session(["Brand New/A.mp4"],
                    library=catalog_with(("block", "Toonami")))

    prompt = loose.next_prompt()

    assert prompt.suggested_namespace is None
    assert prompt.suggested_values == ()


def test_the_prompt_carries_the_running_counts():
    loose = session(["CN/2000s/A.mp4", "CN/Nickelodeon/B.mp4"])
    loose.assign("CN", "network", "CN")
    loose.reject("2000s")

    prompt = loose.next_prompt()

    assert (prompt.meshed, prompt.rejected) == (1, 1)
    assert prompt.name == "Nickelodeon"


def test_a_vocabulary_hit_is_suggested_but_never_accepted():
    """`vocabulary.json` is a cache downstream of the library. A value there is
    weaker evidence, and still not a decision."""
    vocabulary = Vocabulary()
    vocabulary.record({"block": "Ghost"})
    loose = session(["Ghost/A.mp4"], vocabulary=vocabulary)

    prompt = loose.next_prompt()

    assert prompt.suggested_namespace == "block"
    assert loose.entry("Ghost").state == UNMESHED


# ---------------------------------------------------------------------------
# The output
# ---------------------------------------------------------------------------

def test_the_alias_table_holds_every_decision_and_no_open_ones():
    loose = session(["CN/2000s/A.mp4", "CN/Nickelodeon/B.mp4"])
    loose.assign("CN", "network", "CN")
    loose.reject("2000s")

    table = loose.aliases()

    assert [(e.name, e.state) for e in table] == [
        ("2000s", REJECTED), ("CN", MESHED)]


def test_the_alias_table_round_trips():
    loose = session(["CN/2000s/A.mp4"])
    loose.assign("CN", "network", "CN")
    loose.reject("2000s")

    table = loose.aliases()
    restored = AliasTable.from_dict(table.to_dict())

    # Compared on the decisions, not field-for-field: `paths` is deliberately not
    # serialised, and the next test says why.
    assert [(e.name, e.state, e.namespace, e.value) for e in restored] == [
        (e.name, e.state, e.namespace, e.value) for e in table]


def test_the_path_count_is_not_persisted_because_it_is_not_a_decision():
    """`paths` is how much a decision affected in *this* run, recomputed from the
    tree every time. Persisting it would freeze a number that goes stale the moment
    a folder moves, and the round trip would then disagree with the session that
    produced it."""
    loose = session(["CN/A.mp4", "CN/B.mp4"])
    loose.assign("CN", "network", "CN")

    restored = AliasTable.from_dict(loose.aliases().to_dict())

    assert restored.entries[0].paths == 0
    assert loose.entry("CN").paths == 2
    assert [(e.name, e.namespace, e.value, e.state) for e in restored] == [
        (e.name, e.namespace, e.value, e.state) for e in loose.aliases()]


def test_the_serialised_table_says_which_version_it_is():
    loose = session(["CN/A.mp4"])
    loose.assign("CN", "network", "CN")

    assert loose.aliases().to_dict()["version"] == ALIAS_TABLE_VERSION


def test_a_table_from_a_newer_build_is_refused_rather_than_half_read():
    loose = session(["CN/A.mp4"])
    loose.assign("CN", "network", "CN")
    data = loose.aliases().to_dict()

    data["version"] = 99

    with pytest.raises(ValueError, match="alias table version"):
        AliasTable.from_dict(data)


def test_an_unknown_entry_state_reads_back_as_unmeshed():
    """A file written by a newer build should cost the user that name's decision,
    not the whole table — a half-read alias table is how a tag gets applied
    wrongly."""
    loose = session(["CN/A.mp4"])
    data = loose.aliases().to_dict()
    data["entries"] = [{"name": "CN", "namespace": "network", "value": "CN",
                        "state": "meshed-ish"}]

    table = AliasTable.from_dict(data)

    assert table.entries[0].state == UNMESHED


# ---------------------------------------------------------------------------
# Learning a literal from a file name
# ---------------------------------------------------------------------------
#
# A folder name and an autofill rule are one kind of thing -- a string, and what
# a person decided it means -- and these are the rules that come from putting
# them in one table. The point of the unification is that there is exactly one
# answer to "what does this literal mean", so the tests below are mostly about
# the duplicates and the title.

def test_a_rule_is_learned_and_shows_up_in_the_rules_list():
    loose = session(["CN/2000s/A.mp4"])

    outcome = loose.learn_rule("30 Sec", "length", "30 Sec")

    assert outcome.created is True
    assert outcome.conflict is None
    assert [entry.name for entry in loose.rules()] == ["30 Sec"]
    assert loose.rules()[0].learned == LEARNED_RULE


def test_a_rule_remembers_the_file_name_it_was_taught_on():
    loose = session(["CN/2000s/A.mp4"])

    loose.learn_rule("Toonami", "block", "Toonami",
                     filename="Toonami - 30 Sec.mkv")

    assert loose.rules()[0].example_path == "Toonami - 30 Sec.mkv"


def test_a_folder_name_and_a_rule_are_one_table():
    """One literal, one meaning, always."""
    loose = session(["30 Sec/2000s/A.mp4"])

    outcome = loose.learn_rule("30 Sec", "length", "Short")

    assert outcome.created is False, (
        "the folder already claimed this literal, so nothing was added")
    assert outcome.entry.learned == LEARNED_FOLDER
    assert outcome.entry.value is None, "and the folder is still unmeshed"
    assert loose.rules() == ()


def test_a_folder_name_meshed_first_cannot_be_redefined_by_a_rule():
    """The failure the unification exists to prevent: two systems comparing the
    same string, silently disagreeing, with neither knowing."""
    loose = session(["30 Sec/2000s/A.mp4"])
    loose.assign("30 Sec", "length", "Short")

    outcome = loose.learn_rule("30 Sec", "length", "30 Sec")

    assert outcome.created is False
    assert loose.entry("30 Sec").value == "Short", (
        "the folder's answer stands; the rules modal offers to edit it instead")


def test_editing_changes_what_every_clip_touching_that_literal_sees():
    """Two clips under the folder, one edit: both see the new meaning, because
    the table is keyed by literal and not by any one path."""
    loose = session(["30 Sec/2000s/A.mp4", "30 Sec/B.mp4"])
    loose.assign("30 Sec", "length", "Short")
    loose.assign("2000s", "time_period", "2000s")

    loose.edit_rule("30 Sec", "length", "30 Sec")

    assert loose.tags_for("30 Sec/2000s/A.mp4").tags == {
        "length": "30 Sec", "time_period": "2000s"}
    assert loose.tags_for("30 Sec/B.mp4").tags == {"length": "30 Sec"}


def test_editing_something_the_table_does_not_have_is_refused():
    loose = session(["CN/A.mp4"])

    with pytest.raises(KeyError):
        loose.edit_rule("Never Seen", "block", "Toonami")


def test_a_rule_may_not_target_the_title():
    """A rule is a standing instruction; a title is unique per clip. Letting a
    rule fill it re-introduces the automation the folder scan exists to avoid,
    and a stale rule would rename clips in a finished library."""
    loose = session(["CN/2000s/A.mp4"])

    with pytest.raises(ValueError) as error:
        loose.learn_rule("30 Sec", "title", "30 Sec")

    assert "standing" in str(error.value)
    assert loose.rules() == ()


def test_a_rule_needs_something_to_look_for():
    loose = session(["CN/A.mp4"])

    with pytest.raises(ValueError, match="needs the text to look for"):
        loose.learn_rule("   ", "length", "30 Sec")


def test_a_rule_needs_a_value():
    loose = session(["CN/A.mp4"])

    with pytest.raises(ValueError, match="needs a tag value"):
        loose.learn_rule("30 Sec", "length", "  ")


def test_a_rule_refuses_a_namespace_commcut_does_not_know():
    loose = session(["CN/A.mp4"])

    with pytest.raises(ValueError, match="not a tag commcut knows"):
        loose.learn_rule("30 Sec", "colour", "Red")


def test_only_a_rule_can_be_removed():
    """A folder name is in the table because the library contains it, so
    forgetting it would silently un-mesh every clip under it."""
    loose = session(["CN/2000s/A.mp4"])
    loose.assign("CN", "network", "Cartoon Network")
    loose.learn_rule("30 Sec", "length", "30 Sec")

    with pytest.raises(ValueError, match="not a rule"):
        loose.remove_rule("CN")
    assert loose.entry("CN").state == MESHED

    loose.remove_rule("30 Sec")

    assert loose.rules() == ()
    assert loose.entry("CN").state == MESHED


def test_removing_a_literal_that_is_not_there_is_a_no_op():
    session(["CN/A.mp4"]).remove_rule("Never Seen")


# ---------------------------------------------------------------------------
# Rules applied to a file name
# ---------------------------------------------------------------------------

def rules_session(*paths):
    """A session over `paths`, with nothing meshed yet."""
    return session(paths)


def test_every_matching_rule_contributes_not_only_the_first():
    """A file called `Toonami - 30 Sec` with rules for both is two tags.
    Stopping at the first would make the rules an ordered list rather than a
    set, and which one won would depend on the order they were learned."""
    loose = rules_session("CN/Toonami - 30 Sec.mkv")
    loose.learn_rule("Toonami", "block", "Toonami")
    loose.learn_rule("30 Sec", "length", "30 Sec")

    assert loose.tags_for_filename("Toonami - 30 Sec.mkv").tags == {
        "block": "Toonami", "length": "30 Sec"}


def test_a_rule_matches_a_substring_anywhere_in_the_name():
    loose = rules_session("CN/A.mkv")
    loose.learn_rule("Bumper", "filler_type", "Bumper")

    assert loose.tags_for_filename(
        "Worlds Finest (Bumper 30s).mp4").tags == {"filler_type": "Bumper"}


def test_a_rule_matches_regardless_of_case():
    """A person writing `toonami` in a rule and then seeing `Toonami` in a file
    name means the same token."""
    loose = rules_session("CN/A.mkv")
    loose.learn_rule("toonami", "block", "Toonami")

    assert loose.tags_for_filename("Toonami.mkv").tags == {"block": "Toonami"}


def test_a_name_no_rule_matches_resolves_to_nothing():
    loose = rules_session("CN/A.mkv")
    loose.learn_rule("30 Sec", "length", "30 Sec")

    result = loose.tags_for_filename("Something Else.mkv")

    assert result.tags == {}
    assert result.resolved is True


def test_two_rules_claiming_one_namespace_leave_it_unassigned():
    """The same rule as the folder path, through the same `accumulate`."""
    loose = rules_session("CN/A.mkv")
    loose.learn_rule("Morning", "filler_type", "Promo")
    loose.learn_rule("30s", "filler_type", "Bumper")

    result = loose.tags_for_filename("Morning 30s Promo.mkv")

    assert "filler_type" not in result.tags
    assert result.resolved is False


def test_a_rule_and_a_folder_name_claiming_one_namespace_leave_it_unassigned():
    """Not just two rules against each other: the two sources are combined by the
    same `accumulate` inside `tags_for_clip`, so a folder and a rule cannot
    disagree either. Neither half sees the other's contribution, so the merge has
    to happen in one place -- which is why that function exists."""
    loose = rules_session("Promo/Toonami.mkv")
    loose.assign("Promo", "filler_type", "Promo")
    loose.learn_rule("Toonami", "filler_type", "Bumper")

    assert "filler_type" in loose.tags_for("Promo/Toonami.mkv").tags, (
        "the folder alone is fine")
    assert "filler_type" in loose.tags_for_filename("Toonami.mkv").tags, (
        "the rule alone is fine")

    combined = loose.tags_for_clip("Promo/Toonami.mkv")

    assert "filler_type" not in combined.tags
    assert combined.resolved is False
    assert combined.needs_manual_edit is True


def test_a_clip_whose_two_sources_agree_resolves_cleanly():
    """The pair of the test above: same namespace, same value, no conflict. Two
    sources naming one thing is the ordinary case and must not nag."""
    loose = rules_session("Promo/Toonami.mkv")
    loose.assign("Promo", "filler_type", "Promo")
    loose.learn_rule("Toonami", "filler_type", "Promo")

    combined = loose.tags_for_clip("Promo/Toonami.mkv")

    assert combined.tags == {"filler_type": "Promo"}
    assert combined.resolved is True


def test_a_rule_does_not_re_map_a_folder_name_it_shares_a_literal_with():
    """A literal is in the table once. Learned as a rule it matches the file
    name; as a folder name it matches its path segment. Nothing is counted twice,
    and a rule cannot quietly re-map a folder."""
    loose = rules_session("Promo/A.mkv")
    loose.assign("Promo", "filler_type", "Promo")

    assert loose.rules() == ()
    assert loose.tags_for("Promo/A.mkv").tags == {"filler_type": "Promo"}


def test_an_empty_file_name_resolves_to_nothing():
    assert rules_session("CN/A.mkv").tags_for_filename("").tags == {}


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def test_the_report_names_every_mesh_with_its_namespace_and_impact():
    loose = session(["CN/2000s/A.mp4", "CN/B.mp4"])
    loose.assign("CN", "network", "Cartoon Network")

    text = loose.report()

    assert "2 folder name(s)" in text
    assert "CN  ->  network: Cartoon Network   (2 video(s))" in text


def test_the_report_names_every_rejection():
    loose = session(["CN/A.mp4"])
    loose.reject("CN")

    assert "Rejected" in loose.report()
    assert "- CN" in loose.report()


def test_the_report_counts_the_videos_it_read():
    loose = session(["CN/A.mp4", "CN/B.mp4", "CN/2000s/C.mp4"])

    assert "Read 3 video(s)" in loose.report()


def test_the_report_says_when_nothing_was_left_over():
    loose = session(["CN/A.mp4"])
    loose.assign("CN", "network", "CN")

    assert "0 left unmeshed" in loose.report()


def test_the_report_names_an_unresolved_tag_rather_than_leaving_it_to_the_clip():
    loose = session(["Up Next/Promo/A.mp4"])
    loose.assign("Up Next", "filler_type", "Up Next")
    loose.assign("Promo", "filler_type", "Promo")

    text = loose.report()

    assert "could not be resolved" in text
    assert "filler_type: 'Up Next' and 'Promo'" in text
    assert "need editing by hand" in text


def test_the_report_says_nothing_about_conflicts_when_there_are_none():
    loose = session(["CN/Promo/A.mp4", "CN/Bumper/B.mp4"])
    loose.assign("CN", "network", "Cartoon Network")
    loose.assign("Promo", "filler_type", "Promo")
    loose.assign("Bumper", "filler_type", "Bumper")

    text = loose.report()

    assert "could not be resolved" not in text
    assert "no clip is affected" not in text


def test_a_collision_reached_by_many_paths_is_reported_once_with_a_count():
    """A library with four hundred affected clips should show the problem once
    with a number, not four hundred times."""
    loose = session([f"Up Next/Promo/{index}.mp4" for index in range(3)])
    loose.assign("Up Next", "filler_type", "Up Next")
    loose.assign("Promo", "filler_type", "Promo")

    text = loose.report()

    assert text.count("need editing by hand") == 1
    assert "3 video(s) need editing" in text


def test_the_report_does_not_claim_a_conflict_the_paths_do_not_have():
    """Raised while meshing, but nothing collides in the tree -- which is what a
    mapping does when the folders using it turn out not to share a path."""
    loose = session(["CN/A.mp4", "Cartoon Network/B.mp4"])
    loose.assign("CN", "network", "Cartoon Network")
    loose.assign("Cartoon Network", "network", "Nickelodeon")

    text = loose.report()

    assert "could not be resolved" not in text
    assert "need editing" not in text


def test_the_report_is_the_same_text_the_tests_and_the_screen_read():
    """One function of the session, so a console print and the completion screen
    cannot disagree about what happened."""
    loose = session(["CN/A.mp4"])
    loose.assign("CN", "network", "CN")

    assert loose.report() == loose.report()