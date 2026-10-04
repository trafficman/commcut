"""What a `move` is allowed to delete, and what it is never allowed to.

`Applies to:` `shared/importing.py` (`execute_import`,
`prune_emptied_directories`), `shared/catalog.py`.

`move` is the only transfer that removes the user's media, and it is the only
recursive deletion in the app. The folder cleanup is therefore held to rules that do
not depend on being careful at the call site, and every one of them is pinned here.

The rules, in the order they matter:

1. **Only `os.rmdir`, never `rmtree`.** A file can never be deleted by this code
   path, because there is no code path that deletes a file.
2. **Only directories this run emptied** — the parents of the files it moved. An
   empty folder the user made on purpose is never a candidate.
3. **Never past `import/`**, and never `import/` itself.
4. **Never a link**, so a link pointing outside the tree cannot be followed or
   removed.
5. **A folder that gained something between the move and the cleanup is left
   alone**, which `rmdir` decides atomically rather than this code deciding it.
"""

import os

import pytest

from shared.catalog import build_catalog
from shared.importing import (
    TRANSFER_COPY,
    TRANSFER_LINK,
    TRANSFER_MOVE,
    execute_import,
    plan_import,
    prune_emptied_directories,
)
from shared.records import RECORD_EXTENSION

from test_importing import REQUIRED, candidate, clip_in, schemes  # noqa: F401


# ---------------------------------------------------------------------------
# A move empties the import folder
# ---------------------------------------------------------------------------

def test_a_move_takes_the_records_with_it(schemes, tmp_path):
    """Otherwise `import/` fills with records that describe nothing.

    `build_catalog` pairs a record with a video **only** when they share a stem, so
    the stem-derived path is provably the record that came with this clip.
    """
    video = clip_in(tmp_path, "Worlds Finest")
    record = os.path.splitext(video)[0] + RECORD_EXTENSION
    library = tmp_path / "library"

    execute_import(plan_import([candidate(video)], schemes, str(library),
                               transfer=TRANSFER_MOVE, source_root=str(tmp_path)))

    assert not os.path.exists(video)
    assert not os.path.exists(record), (
        "a record whose video is gone describes nothing, and nothing would ever read "
        "it again")


def test_a_move_prunes_the_folders_it_emptied(schemes, tmp_path):
    video = clip_in(tmp_path, "Worlds Finest", folder="CN/2000s/Promo")
    emptied = os.path.join(str(tmp_path), "CN", "2000s", "Promo")
    library = tmp_path / "library"

    execute_import(plan_import([candidate(video)], schemes, str(library),
                               transfer=TRANSFER_MOVE, source_root=str(tmp_path)))

    assert not os.path.isdir(emptied)
    assert not os.path.isdir(os.path.join(str(tmp_path), "CN", "2000s"))
    assert not os.path.isdir(os.path.join(str(tmp_path), "CN"))
    assert os.path.isdir(str(tmp_path)), "the import folder itself is the user's"


def test_a_move_of_a_whole_import_folder_leaves_it_bare(schemes, tmp_path):
    """The reported symptom, on the shape a real import folder actually has.

    Six clips across three trees, with **two kinds sharing a parent** —
    `CN/2000s/Promo` and `CN/2000s/Bumper`. Whichever is climbed first reaches
    `CN/2000s` while the other is still there, so a single pass leaves `CN/2000s`
    and `CN` standing there empty. Which of the two goes first is a set-iteration
    detail, so this is asserted on the outcome rather than on the order: the whole
    point is that it must not matter.
    """
    source = tmp_path / "import"
    layout = {
        "CN/2000s/Promo/First.mp4": "First",
        "CN/2000s/Promo/Second.mp4": "Second",
        "CN/2000s/Bumper/Third.mp4": "Third",
        "CBS/1990s/Promo/Fourth.mp4": "Fourth",
        "Toonami/Episodes/Fifth.mp4": "Fifth",
        "Toonami/Episodes/Sixth.mp4": "Sixth",
    }
    candidates = []
    for relative, title in layout.items():
        video = clip_in(source, os.path.splitext(os.path.basename(relative))[0],
                        folder=os.path.dirname(relative))
        candidates.append(candidate(video, {
            "title": title, "network": "Cartoon Network",
            "filler_type": "Promo", "time_period": "2000s"}))

    result = execute_import(plan_import(candidates, schemes,
                                        str(tmp_path / "library"),
                                        transfer=TRANSFER_MOVE,
                                        source_root=str(source)))

    assert result.committed == 6
    assert result.failed == ()
    assert os.listdir(str(source)) == [], (
        "every video and every record left, so every folder is empty and every "
        "folder this run emptied is gone")
    assert os.path.isdir(str(source)), "and the import folder itself survives"


def test_a_copy_and_a_link_leave_the_import_folder_exactly_as_it_was(schemes,
                                                                   tmp_path):
    """`link` is the one that is easy to get wrong: it leaves the video in place, so
    removing the record would be deleting metadata for a clip that still exists."""
    for transfer in (TRANSFER_COPY, TRANSFER_LINK):
        source = tmp_path / transfer
        video = clip_in(source, "Worlds Finest", folder="CN/2000s")
        record = os.path.splitext(video)[0] + RECORD_EXTENSION
        emptied = os.path.join(str(source), "CN", "2000s")

        execute_import(plan_import([candidate(video)], schemes,
                                   str(tmp_path / f"library-{transfer}"),
                                   transfer=transfer, source_root=str(source)))

        assert os.path.exists(video), f"{transfer} must leave the video"
        assert os.path.exists(record), (
            f"{transfer} leaves the video in place, so its record goes with it")
        assert os.path.isdir(emptied), (
            f"{transfer} emptied nothing, so no folder is removed")


def test_a_partly_filled_folder_is_left_alone_and_the_ones_above_it_too(schemes,
                                                                       tmp_path):
    """The rule that makes this safe to run at all: a folder this run did not empty
    is not a folder this run removes.

    `Still Here` is deliberately **not** in the plan — it is a clip the user has not
    imported yet, sitting in the same folder as one they have. That is the ordinary
    state of a folder being filled by hand, and it is exactly the case where a
    cleanup that reached one level too far would destroy un-imported media.
    """
    kept = clip_in(tmp_path, "Still Here", folder="CN/2000s")
    moved = clip_in(tmp_path, "Goes", folder="CN/2000s")
    folder = os.path.join(str(tmp_path), "CN", "2000s")
    moving = candidate(moved, {"title": "Goes", "network": "Cartoon Network",
                               "filler_type": "Promo", "time_period": "2000s"})

    result = execute_import(plan_import([moving], schemes, str(tmp_path / "library"),
                                        transfer=TRANSFER_MOVE,
                                        source_root=str(tmp_path)))

    assert result.committed == 1
    assert os.path.isdir(folder), "a folder with a clip left in it stays"
    assert os.path.exists(kept), "and so does the clip in it"
    assert not os.path.exists(moved)
    assert os.path.isdir(os.path.join(str(tmp_path), "CN"))


def test_a_cancelled_move_still_prunes_what_it_emptied(schemes, tmp_path):
    """Otherwise a cancelled run leaves exactly the litter it was supposed to avoid,
    and the user is told the run was cancelled and left with an untidy folder.

    Two clips so the cancel lands on the second: `should_cancel` is read at the top
    of each iteration, so a single-clip run has no second iteration to stop in.
    """
    first = clip_in(tmp_path, "One", folder="CN/First")
    second = clip_in(tmp_path, "Two", folder="CN/Second")
    plans = [
        candidate(first, {"title": "One", "network": "Cartoon Network",
                          "filler_type": "Promo", "time_period": "2000s"}),
        candidate(second, {"title": "Two", "network": "Cartoon Network",
                           "filler_type": "Promo", "time_period": "1990s"}),
    ]
    plan = plan_import(plans, schemes, str(tmp_path / "library"),
                       transfer=TRANSFER_MOVE, source_root=str(tmp_path))

    calls = {"n": 0}

    def on_progress(_done, _path):
        calls["n"] += 1

    result = execute_import(plan, on_progress=on_progress,
                            should_cancel=lambda: calls["n"] >= 1)

    assert result.cancelled is True
    assert result.committed == 1, "the clip it did write stays written"
    assert not os.path.isdir(os.path.join(str(tmp_path), "CN", "First")), (
        "the folder it did empty is still cleaned up")
    assert os.path.isdir(os.path.join(str(tmp_path), "CN", "Second")), (
        "and the one it never reached is untouched")


def test_a_move_with_no_source_root_prunes_nothing(schemes, tmp_path):
    """A caller that has not said where the boundary is gets no deletion at all.

    The safest reading of silence, and the reason `source_root` is a parameter
    rather than something inferred from the candidates.
    """
    video = clip_in(tmp_path, "Worlds Finest", folder="CN/Promo")
    emptied = os.path.join(str(tmp_path), "CN", "Promo")

    execute_import(plan_import([candidate(video)], schemes,
                               str(tmp_path / "library"), transfer=TRANSFER_MOVE))

    assert os.path.isdir(emptied)


def test_a_moved_folder_ends_up_reported_as_no_orphans_at_all(schemes, tmp_path):
    """The end-to-end statement: after a move, the import folder holds neither the
    clips nor the records, so the next scan has nothing to complain about."""
    source = tmp_path / "import"
    clip_in(source, "Worlds Finest", folder="CN/Promo")
    # Read the path *after* the tree exists: the plan resolves it now, and a stale
    # one would silently import nothing while every assertion below still passed
    # against a folder that had never been touched.
    video = os.path.join(str(source), "CN", "Promo", "Worlds Finest.mp4")

    execute_import(plan_import([candidate(video)], schemes,
                               str(tmp_path / "library"), transfer=TRANSFER_MOVE,
                               source_root=str(source)))

    catalog = build_catalog(str(source))
    assert catalog.clips == ()
    assert catalog.problems == (), (
        "no orphans, and nothing else either: a folder emptied by a move leaves "
        "nothing for the next scan to complain about")


# ---------------------------------------------------------------------------
# The pruning rules, on their own
# ---------------------------------------------------------------------------

def _tree(root):
    """`root/a/b/c` with one file in it, and the paths that matter."""
    deep = os.path.join(str(root), "a", "b", "c")
    os.makedirs(deep, exist_ok=True)
    target = os.path.join(deep, "clip.mp4")
    with open(target, "wb") as handle:
        handle.write(b"\0")
    return deep, target


def test_a_folder_that_still_holds_a_file_is_never_removed(tmp_path):
    """The rule `rmdir` enforces for us, tested rather than assumed.

    A file arriving between the move and the cleanup — from another commcut, a sync
    client, the user — must cost nothing and delete nothing.
    """
    deep, _ = _tree(tmp_path)
    stranger = os.path.join(deep, "somebody-elses.mp4")
    with open(stranger, "wb") as handle:
        handle.write(b"\0")

    removed = prune_emptied_directories([deep], str(tmp_path))

    assert removed == ()
    assert os.path.isdir(deep)
    assert os.path.exists(stranger), "the whole point: the file is untouched"


def test_only_the_named_directories_are_candidates(tmp_path):
    """An empty folder the user made by hand is not in the set, so it is not
    touched — however empty it is, and however many there are."""
    deep, _ = _tree(tmp_path)
    made_by_hand = os.path.join(str(tmp_path), "Empty On Purpose")
    os.makedirs(made_by_hand, exist_ok=True)
    os.remove(os.path.join(deep, "clip.mp4"))

    prune_emptied_directories([deep], str(tmp_path))

    assert not os.path.isdir(deep)
    assert os.path.isdir(made_by_hand), (
        "this one was not emptied by an import, so it is not an import's to remove")


def test_the_climb_stops_at_the_import_folder_and_never_removes_it(tmp_path):
    """The boundary, from both sides.

    The folder itself is the user's — they chose it and they will drop the next
    batch in it — so it is never removed. And the pruning never looks above it, so
    a folder that merely *contains* the import folder is out of reach too.
    """
    outer = tmp_path / "outside"
    source = outer / "import"
    deep = str(source / "a" / "b")
    os.makedirs(deep, exist_ok=True)
    with open(os.path.join(deep, "clip.mp4"), "wb") as handle:
        handle.write(b"\0")
    sibling = str(outer / "not-import")
    os.makedirs(sibling, exist_ok=True)

    removed = prune_emptied_directories([deep], str(source))

    assert os.path.isdir(str(source)), "the import folder is the user's"
    assert os.path.isdir(sibling), (
        "and a directory whose name merely starts with the import folder's is not "
        "inside it")


def test_a_sibling_folder_with_a_shared_name_prefix_is_outside_the_boundary(tmp_path):
    """`import-old` starts with `import`, and a string-prefix test would treat it as
    a child. This is the case `commonpath` exists for."""
    inside = tmp_path / "import"
    outside = tmp_path / "import-old"
    emptied = str(outside / "a")
    os.makedirs(emptied, exist_ok=True)
    os.makedirs(str(inside), exist_ok=True)

    removed = prune_emptied_directories([emptied], str(inside))

    assert removed == ()
    assert os.path.isdir(emptied)


def test_a_link_is_never_followed_or_removed(tmp_path):
    """A link inside the tree pointing at a directory outside it must not become a
    route for either following or removing."""
    if not hasattr(os, "symlink"):
        pytest.skip("this platform cannot make a link")
    outside = tmp_path / "precious"
    os.makedirs(str(outside), exist_ok=True)
    keep = os.path.join(str(outside), "keep.mp4")
    with open(keep, "wb") as handle:
        handle.write(b"\0")

    source = tmp_path / "import"
    os.makedirs(str(source), exist_ok=True)
    linked = os.path.join(str(source), "shortcut")
    try:
        os.symlink(str(outside), linked, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot make a directory link")

    prune_emptied_directories([linked], str(source))

    assert os.path.isdir(str(outside)), "what is behind the link survives"
    assert os.path.exists(keep), "including its contents"
    assert os.path.islink(linked), "and the link itself is left where it was"


def test_the_link_guard_is_the_only_thing_between_a_link_and_a_deletion(
        tmp_path, monkeypatch):
    """The same rule, on a platform that will not let the test make a real link.

    Making a symlink on Windows needs a privilege most test runs do not have, so the
    rule above can silently skip and the guard go untested. Here it is tested
    directly: when `islink` says yes, nothing happens at all.
    """
    deep, target = _tree(tmp_path)
    os.remove(target)

    monkeypatch.setattr(os.path, "islink", lambda path: path == deep)

    removed = prune_emptied_directories([deep], str(tmp_path))

    assert removed == ()
    assert os.path.isdir(deep), (
        "the guard is a refusal to act, not a sanitised version of the action")


def test_a_parent_shared_by_two_moved_folders_still_goes(tmp_path):
    """The bug this closes, and the shape of almost every real import.

    Two kinds of filler under `import/CN/2000s/` both have to be moved. Whichever
    leaf is climbed first reaches `2000s` while the *other* is still there, so its
    first attempt necessarily fails. A single pass then writes that folder off and
    never comes back — leaving `CN/2000s` and `CN` standing there empty, which is
    precisely the untidy folder the cleanup exists to remove.

    So the climb is retried until nothing more can go, and a folder that was
    non-empty when first tried is not treated as permanently unremovable.
    """
    for name in ("Promo", "Bumper"):
        os.makedirs(os.path.join(str(tmp_path), "CN", "2000s", name), exist_ok=True)
    emptied = [os.path.join(str(tmp_path), "CN", "2000s", name)
               for name in ("Promo", "Bumper")]

    removed = prune_emptied_directories(emptied, str(tmp_path))

    assert not os.path.isdir(os.path.join(str(tmp_path), "CN"))
    assert os.path.isdir(str(tmp_path))
    assert set(removed) == set(emptied) | {
        os.path.join(str(tmp_path), "CN"),
        os.path.join(str(tmp_path), "CN", "2000s"),
    }, "and the shared parents are reported as removed too"


def test_the_climb_is_retried_only_while_it_is_still_earning_something(tmp_path):
    """The loop terminates, and it terminates on the right thing: a directory that
    genuinely still holds something stops being retried, rather than spinning."""
    kept = os.path.join(str(tmp_path), "CN", "Kept", "clip.mp4")
    os.makedirs(os.path.dirname(kept), exist_ok=True)
    with open(kept, "wb") as handle:
        handle.write(b"\0")
    emptied = os.path.join(str(tmp_path), "CN", "Promo")
    os.makedirs(emptied, exist_ok=True)

    removed = prune_emptied_directories([emptied], str(tmp_path))

    assert removed == (emptied,)
    assert os.path.isdir(os.path.dirname(kept)), (
        "and the folder above it is left alone, which is what stops the retry loop "
        "from ever finishing")


def test_pruning_a_folder_that_is_already_gone_is_not_an_error(tmp_path):
    """A second run over a folder the first emptied, or a source on a share another
    process is tidying. Litter is not a failure."""
    deep, target = _tree(tmp_path)
    os.remove(target)

    first = prune_emptied_directories([deep], str(tmp_path))

    assert deep in first, "the folder that held the file is the one removed"
    assert prune_emptied_directories([deep], str(tmp_path)) == ()