"""Tests for the tag vocabulary: the file, its defaults, and its dedup rule."""

import json
import os

import pytest

from shared.environment import install_root
from shared.vocabulary import (
    DEFAULT_VALUES,
    VOCABULARY_FILENAME,
    VOCABULARY_SCHEMA_VERSION,
    Vocabulary,
    forget_cached_vocabulary,
    get_vocabulary,
    record_use,
    vocabulary_path,
)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / VOCABULARY_FILENAME)


@pytest.fixture(autouse=True)
def no_shared_cache():
    """The module caches per path; a test that redirects the path must not see
    another test's values, and nothing here should leak into the next."""
    yield
    forget_cached_vocabulary()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_a_missing_file_is_the_fresh_install_case(path):
    """Deleting the vocabulary has to be a safe troubleshooting step, so a file
    that is not there is not an error -- it is the first run."""
    vocabulary = Vocabulary.load(path)

    assert vocabulary.values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"])
    )


def test_the_defaults_come_from_code_not_from_a_shipped_file():
    """A shipped vocabulary.json would make deletion lossy; a constant cannot."""
    assert DEFAULT_VALUES, "the app must ship some starting values"
    assert all(isinstance(namespace, str) for namespace in DEFAULT_VALUES)
    assert all(
        isinstance(value, str) and value
        for values in DEFAULT_VALUES.values() for value in values
    )


def test_the_defaults_cover_filler_type_and_nothing_worth_arguing_about():
    """Only filler_type is seeded. Which values are a judgement call about the
    project, not a structural one, so this pins the shape and not the list --
    asserting the exact contents here would just fail every time they are edited.

    *Structure*: one namespace, non-empty strings, no duplicates within it, and
    nothing that dedupes to the same key.
    """
    assert list(DEFAULT_VALUES) == ["filler_type"]
    seeded = DEFAULT_VALUES["filler_type"]
    assert seeded, "a shipped default list cannot be empty"
    assert all(isinstance(value, str) and value.strip() for value in seeded)
    assert len(set(seeded)) == len(seeded)

    vocabulary = Vocabulary()
    assert vocabulary.record({"filler_type": seeded}) is False, (
        "two of the shipped defaults dedupe to the same value"
    )


@pytest.mark.parametrize("contents", [
    "{ this is not json",
    '{"version": 1}',
    '{"tags": {}}',
    '{"version": 1, "tags": []}',
    '[]',
    '"a string"',
    '{"version": 99, "tags": {"network": ["X"]}}',
    '{"version": null, "tags": {}}',
])
def test_anything_unusable_rebuilds_from_the_defaults(path, contents):
    """A wrong shape, an unreadable file, or a newer schema all mean the same
    thing as a fresh install. Refusing would make the one action that fixes a
    bad file the thing that fails because of it."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(contents)

    vocabulary = Vocabulary.load(path)

    assert vocabulary.values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"])
    )


def test_a_byte_order_mark_does_not_cost_the_user_their_vocabulary(path):
    with open(path, "w", encoding="utf-8-sig") as handle:
        json.dump({"version": VOCABULARY_SCHEMA_VERSION,
                   "tags": {"network": ["Cartoon Network"]}}, handle)

    assert Vocabulary.load(path).values("network") == ("Cartoon Network",)


def test_an_unreadable_namespace_is_skipped_and_the_rest_kept(path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": VOCABULARY_SCHEMA_VERSION, "tags": {
            "network": ["Cartoon Network"],
            "colour": ["Red"],
            "filler_type": "not a list",
            "block": ["Toonami", 7, None],
        }}, handle)

    vocabulary = Vocabulary.load(path)

    assert vocabulary.values("network") == ("Cartoon Network",)
    assert vocabulary.values("block") == ("Toonami",)
    assert vocabulary.values("colour") == ()
    assert vocabulary.values("filler_type") == ()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_recording_reports_whether_anything_changed():
    vocabulary = Vocabulary()

    assert vocabulary.record({"network": "Cartoon Network"}) is True
    assert vocabulary.record({"network": "Cartoon Network"}) is False


def test_a_case_variant_is_not_a_second_entry_and_the_first_casing_wins():
    vocabulary = Vocabulary()

    vocabulary.record({"network": "Cartoon Network"})

    assert vocabulary.record({"network": "cartoon network"}) is False
    assert vocabulary.values("network") == ("Cartoon Network",)


def test_a_tag_locked_in_the_wrong_case_is_kept_wrong_and_not_duplicated():
    """Deliberate: the list offers what was typed. Fixing the casing is the
    user's call, and a vocabulary that corrected it would be editing data."""
    vocabulary = Vocabulary()

    vocabulary.record({"filler_type": "promo"})

    assert vocabulary.values("filler_type") == ("promo",)
    assert vocabulary.record({"filler_type": "Promo"}) is False


@pytest.mark.parametrize("first,second", [
    ("A B", "A  B"),
    ("A/B", "A-B"),
    ("A/B", "A:B"),
    ("A<>B", "A-B"),
    ("A?B", "A-B"),
    ("Toonami", "  Toonami  "),
])
def test_two_values_that_would_render_identically_are_one_entry(first, second):
    """The whole reason the dedup key runs through the folder sanitation: the
    export preflight treats these as the same directory, so offering both is
    offering a collision."""
    vocabulary = Vocabulary()

    assert vocabulary.record({"title": first}) is True
    assert vocabulary.record({"title": second}) is False
    assert vocabulary.values("title") == (first.strip(),)


def test_the_stored_value_is_what_the_user_typed_not_what_it_renders_as():
    vocabulary = Vocabulary()

    vocabulary.record({"title": "A/B"})

    assert vocabulary.values("title") == ("A/B",)


@pytest.mark.parametrize("value", ["", "   ", "\t\n "])
def test_an_empty_value_is_not_a_tag(value):
    vocabulary = Vocabulary()

    assert vocabulary.record({"network": value}) is False
    assert vocabulary.values("network") == ()


def test_surrounding_whitespace_is_trimmed_but_the_inside_is_theirs():
    vocabulary = Vocabulary()

    vocabulary.record({"network": "  Cartoon Network  "})

    assert vocabulary.values("network") == ("Cartoon Network",)


def test_a_scheme_alias_is_recorded_under_its_canonical_key():
    vocabulary = Vocabulary()

    vocabulary.record({"type": "Promo"})

    assert vocabulary.values("filler_type") == ("Promo",)
    assert vocabulary.values("type") == ("Promo",)


def test_an_unknown_tag_name_is_ignored_rather_than_invented():
    vocabulary = Vocabulary()

    assert vocabulary.record({"colour": "Red", "network": "CN"}) is True
    assert vocabulary.values("colour") == ()
    assert vocabulary.values("network") == ("CN",)


def test_title_is_just_another_value_once_recorded():
    """The module stores whatever it is told and has no opinion on which tags
    are worth remembering -- the editor decides that, since the editor owns the
    dropdowns. It stays permissive so a caller with a different set of
    namespaces needs no special case."""
    vocabulary = Vocabulary()

    vocabulary.record({"title": "Worlds Finest"})

    assert vocabulary.values("title") == ("Worlds Finest",)


def test_recording_an_empty_mapping_changes_nothing():
    vocabulary = Vocabulary()

    assert vocabulary.record({}) is False
    assert vocabulary.namespaces() == ()


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def test_a_prune_removes_what_the_caller_did_not_name_and_reports_it():
    vocabulary = Vocabulary()
    vocabulary.record({"network": "Cartoon Network"})
    vocabulary.record({"network": "Nickelodeon"})
    vocabulary.dirty = False

    result = vocabulary.prune_to({"network": ["Nickelodeon"]})

    assert result.removed == (("network", "Cartoon Network"),)
    assert result.protected == ()
    assert vocabulary.values("network") == ("Nickelodeon",)
    assert vocabulary.dirty is True


def test_a_prune_removes_and_never_adds():
    """Adding is `record()`'s job. A prune handed a value the file does not
    have stores nothing, so the two operations cannot be confused at a call
    site -- and `sync_vocabulary` has to union before it prunes, or it would
    prune away everything it had just added."""
    vocabulary = Vocabulary()

    assert vocabulary.prune_to({"network": ["Nickelodeon"]}).removed == ()
    assert vocabulary.namespaces() == ()


def test_a_prune_matches_in_the_dedup_key_space_rather_than_on_raw_strings():
    """The whole reason removal goes through `_dedup_key` internally: a prune
    handed `Cartoon Network` has to take `Cartoon/Network` and `cartoon network`
    with it, because `record()` would never have stored them as three entries
    and the export preflight would render them as one folder."""
    vocabulary = Vocabulary()
    vocabulary.record({"network": "Cartoon Network"})
    vocabulary.record({"network": "cartoon network"})

    assert vocabulary.values("network") == ("Cartoon Network",)
    assert vocabulary.prune_to({"network": ["Cartoon Network"]}).removed == ()


def test_a_prune_resolves_a_scheme_alias_to_its_canonical_namespace():
    vocabulary = Vocabulary()
    vocabulary.record({"type": "Promo"})

    assert vocabulary.prune_to({"filler_type": ["Promo"]}).removed == ()
    assert vocabulary.values("filler_type") == ("Promo",)


def test_a_prune_leaves_a_namespace_it_was_not_asked_about():
    """Naming a namespace is how a caller asks for it to be reconciled, so a
    namespace missing from `in_use` is not touched. `shared/catalog.py` lists
    every namespace the library uses, including ones with nothing in them, so a
    namespace the library never mentions at all keeps whatever the file had."""
    vocabulary = Vocabulary()
    vocabulary.record({"network": "Cartoon Network", "block": "Toonami"})

    vocabulary.prune_to({"network": ["Cartoon Network"]})

    assert vocabulary.values("block") == ("Toonami",)


def test_a_prune_of_an_empty_namespace_empties_it():
    vocabulary = Vocabulary()
    vocabulary.record({"network": "Cartoon Network", "block": "Toonami"})

    assert vocabulary.prune_to({"network": []}).removed == (
        ("network", "Cartoon Network"),)
    assert vocabulary.values("network") == ()
    assert vocabulary.namespaces() == ("block", "network")


def test_a_prune_of_nothing_at_all_removes_nothing():
    vocabulary = Vocabulary()
    vocabulary.record({"network": "Cartoon Network"})
    vocabulary.dirty = False

    assert vocabulary.prune_to({}).removed == ()
    assert vocabulary.values("network") == ("Cartoon Network",)
    assert vocabulary.dirty is False


def test_a_prune_that_removes_nothing_leaves_the_file_alone():
    vocabulary = Vocabulary()
    vocabulary.record({"network": "Cartoon Network"})
    vocabulary.dirty = False

    assert vocabulary.prune_to({"network": ["Cartoon Network"]}).removed == ()
    assert vocabulary.dirty is False


def test_a_prune_of_a_namespace_the_file_does_not_have_is_not_an_error():
    vocabulary = Vocabulary()
    vocabulary.record({"network": "Cartoon Network"})

    assert vocabulary.prune_to(
        {"special": ["Kids"], "network": ["Cartoon Network"]},
    ).removed == ()


def test_a_prune_ignores_blank_and_non_string_entries_rather_than_keeping_them():
    """`in_use` holds raw values from records, and an empty one in a list is a
    value nobody uses, not a reason to keep anything."""
    vocabulary = Vocabulary()
    vocabulary.record({"network": "Cartoon Network"})

    assert vocabulary.prune_to({"network": ["  ", None, 3]}).removed == (
        ("network", "Cartoon Network"),)
    assert vocabulary.values("network") == ()


def test_a_prune_survives_the_round_trip_to_the_file(path):
    vocabulary = Vocabulary(path=path)
    vocabulary.record({"network": "Cartoon Network"})
    vocabulary.record({"block": "Toonami"})
    vocabulary.record({"special": "Kids"})
    vocabulary.prune_to({
        "network": ["Cartoon Network"], "special": ["Kids"], "block": [],
    })
    vocabulary.save()

    reloaded = Vocabulary.load(path)
    assert reloaded.values("block") == ()
    assert reloaded.values("network") == ("Cartoon Network",)
    assert reloaded.values("special") == ("Kids",)


# ---------------------------------------------------------------------------
# Defaults are not library residue
# ---------------------------------------------------------------------------

def test_a_prune_never_removes_a_shipped_default():
    """A default is the project's starter vocabulary, not something the library
    stopped using. A user who has exported one clip has a `filler_type` list
    that says nothing about the other ten defaults, and pruning on that basis is
    what would collapse a new user's dropdowns on their first sync."""
    vocabulary = Vocabulary.defaults()

    result = vocabulary.prune_to({"filler_type": ["Bumper"]})

    assert result.removed == ()
    # Bumper is the one value in use, so the other ten are the ones spared.
    assert len(result.protected) == len(DEFAULT_VALUES["filler_type"]) - 1
    assert vocabulary.values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"]))


def test_a_prune_keeps_the_defaults_while_still_removing_everything_else():
    """The paired case, and the one that matters most: without it, a fix that
    simply stopped `prune_to` removing anything would pass every other test
    here."""
    vocabulary = Vocabulary.defaults()
    vocabulary.record({"block": "Toonami"})
    vocabulary.record({"network": "Cartoon Network"})

    result = vocabulary.prune_to({
        "filler_type": ["Bumper"], "block": [], "network": ["Cartoon Network"],
    })

    assert result.removed == (("block", "Toonami"),)
    assert len(vocabulary.values("filler_type")) == len(
        DEFAULT_VALUES["filler_type"])
    assert vocabulary.values("network") == ("Cartoon Network",)


def test_a_default_is_recognised_through_the_dedup_key_not_the_raw_string():
    """The stored value here is `PROMO`, which is not literally in
    `DEFAULT_VALUES` -- but its key is the default `Promo`'s, and `record()`
    would never have filed them as two entries. So this is the default, whatever
    casing it is under, and it is protected."""
    vocabulary = Vocabulary()
    vocabulary.record({"filler_type": "PROMO"})

    assert vocabulary.values("filler_type") == ("PROMO",)
    assert vocabulary.prune_to({"filler_type": ["Bumper"]}).removed == ()
    assert vocabulary.values("filler_type") == ("PROMO",)


def test_protecting_the_defaults_does_not_extend_to_another_namespace():
    """`filler_type` is the only seeded namespace. A value that happens to share
    a default's spelling in `block` is an ordinary value and goes like any
    other."""
    vocabulary = Vocabulary.defaults()
    vocabulary.record({"block": "Bumper"})

    result = vocabulary.prune_to({
        "filler_type": ["Bumper"], "block": [],
    })

    assert result.removed == (("block", "Bumper"),)


def test_a_prune_of_nothing_removes_nothing_and_writes_nothing(path):
    """`prune_to({})` touches no namespace at all, so it must not even mark the
    vocabulary dirty -- otherwise a sync whose prune had nothing to do would
    rewrite the file and a test that watched the mtime would see it."""
    vocabulary = Vocabulary.defaults(path)
    vocabulary.record({"block": "Toonami"})
    vocabulary.save()
    vocabulary.dirty = False

    assert vocabulary.prune_to({}).removed == ()
    assert vocabulary.dirty is False

    reloaded = Vocabulary.load(path)
    assert reloaded.values("block") == ("Toonami",)
    assert reloaded.values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"]))


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

def test_the_written_file_is_the_documented_shape(path):
    vocabulary = Vocabulary(path=path)
    vocabulary.record({"network": "Cartoon Network"})
    vocabulary.record({"information": "Remastered"})

    vocabulary.save()

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data == {
        "version": VOCABULARY_SCHEMA_VERSION,
        "tags": {"information": ["Remastered"], "network": ["Cartoon Network"]},
    }


def test_the_file_is_utf8_without_a_byte_order_mark(path):
    vocabulary = Vocabulary(path=path)
    vocabulary.record({"network": "Ünïcödé — 中文"})

    vocabulary.save()

    raw = open(path, "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert "Ünïcödé — 中文".encode("utf-8") in raw


def test_every_value_in_a_namespace_comes_back_not_just_the_last(path):
    """A namespace is a list on disk and has to come back as one. Building a
    dict from the list while loading keys on the namespace and keeps only the
    final entry, which loses a user's whole vocabulary the first time they
    relaunch."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": VOCABULARY_SCHEMA_VERSION, "tags": {
            "filler_type": ["Promo", "Bumper", "Commercial"],
        }}, handle)

    assert Vocabulary.load(path).values("filler_type") == (
        "Bumper", "Commercial", "Promo",
    )


def test_values_come_back_the_same_way_they_went_in(path):
    vocabulary = Vocabulary(path=path)
    vocabulary.record({"network": "Cartoon Network"})
    vocabulary.record({"filler_type": "Promo"})

    vocabulary.save()

    reloaded = Vocabulary.load(path)
    assert reloaded.values("network") == ("Cartoon Network",)
    assert reloaded.values("filler_type") == ("Promo",)


def test_writing_clears_the_dirty_flag(path):
    vocabulary = Vocabulary(path=path)
    vocabulary.record({"network": "Cartoon Network"})

    vocabulary.save()

    assert vocabulary.dirty is False


def test_a_failed_write_leaves_no_temporary_file(path, monkeypatch):
    vocabulary = Vocabulary(path=path)
    vocabulary.record({"network": "Cartoon Network"})
    monkeypatch.setattr(
        "shared.vocabulary.os.replace",
        lambda *arguments: (_ for _ in ()).throw(OSError("no space left")),
    )

    with pytest.raises(OSError, match="no space left"):
        vocabulary.save()

    assert not os.path.exists(path)
    assert os.listdir(os.path.dirname(path)) == []


def test_saving_over_an_existing_vocabulary_replaces_it(path):
    first = Vocabulary(path=path)
    first.record({"network": "Cartoon Network"})
    first.save()

    second = Vocabulary.load(path)
    second.record({"network": "Nickelodeon"})
    second.save()

    assert Vocabulary.load(path).values("network") == ("Cartoon Network",
                                                      "Nickelodeon")


def test_a_vocabulary_only_ever_writes_to_its_own_file(tmp_path):
    """save() takes no path, so there is no way to record one vocabulary's
    values into another's file -- which is how a test pointing at a temp folder
    once ended up writing to the real install root."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    vocabulary = Vocabulary(path=str(elsewhere / "vocabulary.json"))
    vocabulary.record({"network": "Cartoon Network"})

    vocabulary.save()

    assert Vocabulary.load(vocabulary.path).values("network") == (
        "Cartoon Network",
    )


def test_the_path_follows_the_install_root(monkeypatch, tmp_path):
    """Not a project root derived from __file__: frozen, that is PyInstaller's
    extraction folder, which is deleted on exit along with the library."""
    monkeypatch.setattr("shared.vocabulary.install_root", lambda: str(tmp_path))

    assert vocabulary_path() == os.path.join(str(tmp_path), VOCABULARY_FILENAME)
    assert vocabulary_path().startswith(install_root.__module__ and str(tmp_path))


def test_the_file_lands_beside_settings_not_inside_it(monkeypatch, tmp_path):
    """load_export_schemes parses all of settings.json and raises on any error,
    so a high-churn cache sharing that file would make a bad vocabulary refuse an
    export for a reason export does not care about."""
    monkeypatch.setattr("shared.vocabulary.install_root", lambda: str(tmp_path))

    assert os.path.dirname(vocabulary_path()) == os.path.dirname(
        os.path.join(str(tmp_path), "settings.json")
    )


# ---------------------------------------------------------------------------
# The process-wide cache
# ---------------------------------------------------------------------------

def test_the_cached_instance_is_reused_for_one_path(monkeypatch, tmp_path):
    monkeypatch.setattr("shared.vocabulary.install_root", lambda: str(tmp_path))

    first = get_vocabulary()
    record_use({"network": "Cartoon Network"})

    assert get_vocabulary() is first
    assert first.values("network") == ("Cartoon Network",)


def test_a_redirected_path_gets_its_own_instance(monkeypatch, tmp_path):
    """Why the cache is keyed on the path: a test that points install_root
    somewhere new must not inherit whatever the last test staged."""
    monkeypatch.setattr("shared.vocabulary.install_root", lambda: str(tmp_path))
    record_use({"network": "Cartoon Network"})

    monkeypatch.setattr("shared.vocabulary.install_root",
                        lambda: str(tmp_path / "other"))

    assert get_vocabulary().values("network") == ()


def test_record_use_persists_and_reports_whether_it_changed(monkeypatch, tmp_path):
    monkeypatch.setattr("shared.vocabulary.install_root", lambda: str(tmp_path))

    assert record_use({"network": "Cartoon Network"}) is True
    assert record_use({"network": "Cartoon Network"}) is False
    assert os.path.exists(vocabulary_path())


def test_a_vocabulary_that_cannot_be_saved_is_not_a_failed_stage(
    monkeypatch, tmp_path
):
    """Every value in the vocabulary is also in the .cmct and the records, and
    the next stage will try again. A full disk must not fail an export."""
    monkeypatch.setattr("shared.vocabulary.install_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        "shared.vocabulary.Vocabulary.save",
        lambda self: (_ for _ in ()).throw(OSError("no space left")),
    )

    assert record_use({"network": "Cartoon Network"}) is True
    assert get_vocabulary().values("network") == ("Cartoon Network",)
    assert get_vocabulary().dirty is False