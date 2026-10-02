"""Reading the export library back: the catalog.

Applies to: `shared/catalog.py`, `shared/records.py`, `shared/sources.py`.

A clip's tags live in its ``<stem>.cnfo`` beside the video, and this module is the
one thing that finds those records and reads them. The rule it applies is the one
`docs/naming-and-organization.md` states for the writer: **a record with a
sibling video is a clip, and anything under the export root without a record is
ignored.**

That last half is what makes the rule safe to run over a folder the user owns.
``export/`` is also where a person drops a working folder of half-organized
videos, and a scan that turned those into clips would invent tags from folder
names -- the reverse parse `docs/naming-and-organization.md` refuses outright,
because sanitation, fallback expressions and OR groups all make rendering lossy.
A file with no record therefore yields nothing at all, not a guess.

This module holds no Qt types and never mutates what it reads, so the same walk
serves the vocabulary sync, the Rename Wizard, and whatever the Library Importer
becomes, none of which are the same window.

Two things are deliberately different from the export preflight, which walks the
same tree. ``shared/exporting.py:_walk_existing_entries`` re-raises a walk error
because it is about to *write* into that tree and an unreadable directory must
refuse the batch. A scan has nothing to protect, so a walk error here becomes a
reported `CatalogProblem` and the walk continues: one corrupt record on a network
share should cost that one record, not the other eight hundred.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from shared.records import (
    RECORD_EXTENSION,
    ClipRecord,
    RecordError,
    load_record,
    record_error_reason,
)
from shared.sources import is_video_file
from shared.vocabulary import PruneResult

#: Re-exported so a caller scanning for records does not have to know where the
#: extension is defined. Not a second definition of it.
RECORD_SCAN_EXTENSION = RECORD_EXTENSION

#: `CatalogProblem.reason` when the record file itself could not be opened or
#: read. The record-error reasons come from `shared/records.py`, which is where
#: they are raised.
REASON_UNREADABLE = "unreadable"
REASON_WALK_ERROR = "walk-error"


@dataclass(frozen=True)
class CatalogClip:
    """One clip the library holds, as its record describes it."""

    #: Absolute path of the video.
    path: str
    #: Posix-separated, relative to the root that was walked. For display, and
    #: for the Rename Wizard to name a clip without carrying an absolute path
    #: around. `shared/exporting.py` renders relative paths the same way.
    relative_path: str
    #: Absolute path of the `.cnfo` this was read from. Carried separately from
    #: `path` because the two are different files sharing a stem, and a consumer
    #: that wants to re-read, re-render or report on one needs to name it.
    record_path: str
    #: Raw, canonical-key tag pairs, straight from the record. Never from the
    #: filename -- see the module docstring.
    tags: tuple[tuple[str, str], ...]
    #: The record itself, for a consumer that needs provenance beyond the tags.
    record: ClipRecord

    def tag_dict(self) -> dict[str, str]:
        """The tags as a plain dict."""
        return dict(self.tags)


@dataclass(frozen=True)
class CatalogProblem:
    """Something in the library that could not be read, and why.

    Named rather than counted because a silently skipped record is a clip the
    user cannot find afterwards, which is the same failure the export summary
    screen exists to stop.

    `reason` is the code and `message` is the sentence, because they answer
    different questions. A screen groups by the code — every unknown tag key in
    one place, every corrupt file in another — while the message is what the
    person reads. Matching on the prose instead is what makes this class
    necessary: "this build does not know the tag `colour`" and "this file is
    corrupt" are the same symptom to a walk and completely different things to
    the user. The codes come from `shared/records.py` where they are raised.
    """

    #: Relative to the root that was walked.
    path: str
    message: str
    #: One of the `REASON_*` codes: a record reason from `shared/records.py`, or
    #: `REASON_UNREADABLE` / `REASON_WALK_ERROR` from this module.
    reason: str = REASON_UNREADABLE

    @property
    def is_record_problem(self) -> bool:
        """True when a record was found and refused, rather than unreadable."""
        return self.reason != REASON_UNREADABLE and self.reason != REASON_WALK_ERROR


@dataclass(frozen=True)
class Catalog:
    """Everything one walk of the export root found."""

    clips: tuple[CatalogClip, ...] = ()
    problems: tuple[CatalogProblem, ...] = ()
    #: True when `should_cancel` stopped the walk. A cancelled catalog is a
    #: partial view of the library and must never be treated as a whole one --
    #: see `sync_vocabulary`.
    cancelled: bool = False

    @property
    def tag_index(self) -> dict[str, tuple[str, ...]]:
        """Every tag value in use, per namespace, sorted.

        Derived on demand rather than stored: the counts and the evidence
        ranking the Library Importer will want are also derivable from `clips`,
        and a cache here would be a second place for them to be wrong.
        """
        index: dict[str, set[str]] = {}
        for clip in self.clips:
            for key, value in clip.tags:
                if value:
                    index.setdefault(key, set()).add(value)
        return {namespace: tuple(sorted(values))
                for namespace, values in sorted(index.items())}


def _never_cancel() -> bool:
    return False


def _ignore_progress(_clips_found: int, _relative_path: str) -> None:
    return None


def _relative(root: str, path: str) -> str:
    """`path` relative to `root`, with forward slashes."""
    relative = os.path.relpath(path, root)
    if relative == os.curdir:
        return ""
    return relative.replace(os.sep, "/")


def _video_stems(file_names) -> dict[str, str]:
    """Map each video file's stem to one filename, first match winning.

    Built per directory from the names `os.walk` already handed us rather than
    by stat-ing every extension in `VIDEO_EXTENSIONS` per record: twenty-two
    stat calls a record, on a library of eight hundred, is a walk slow enough to
    notice. A stem with two video siblings resolves to one of them, which is the
    only ambiguity the record format allows and neither choice is wrong.

    `is_video_file` is asked about the whole name, not about a split extension,
    because it splits the name itself -- and `os.path.splitext(".mp4")` answers
    `('.mp4', '')`, since a leading dot makes a hidden file with no extension at
    all. Handing it the extension would find every clip in the library
    unreadable.
    """
    stems: dict[str, str] = {}
    for name in file_names:
        if not is_video_file(name):
            continue
        stem, _ = os.path.splitext(name)
        if stem and stem not in stems:
            stems[stem] = name
    return stems


def _sibling_video(record_path: str, stems: Mapping[str, str]):
    """The video beside `record_path`, or None when there is not one.

    Returns the name rather than a full path because the caller already knows the
    directory. A name from `os.walk`'s file list is checked with `isfile` rather
    than trusted, because a broken symlink is listed as a file and is not one.
    """
    stem, _ = os.path.splitext(os.path.basename(record_path))
    name = stems.get(stem)
    if name is None:
        return None
    candidate = os.path.join(os.path.dirname(record_path), name)
    return candidate if os.path.isfile(candidate) else None


def build_catalog(
    root: str,
    on_progress: Callable[[int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Catalog:
    """Walk `root` and return every clip it holds.

    A missing root is an empty catalog rather than an error, for the same reason
    a fresh install with an empty `export/` is the expected state rather than a
    fault: nothing about the tree is wrong, there is simply nothing in it.

    `on_progress(clips_found, relative_path)` carries **no total**.
    `os.walk` cannot know how many records are ahead of it, and a progress bar
    that invents a denominator is worse than an indeterminate one.

    `should_cancel` is checked per directory and per record. A cancel leaves
    `cancelled` set with whatever had been found, and it is the caller's job not
    to act on a partial view -- see `sync_vocabulary`, which writes nothing at
    all when this returns cancelled.
    """
    if on_progress is None:
        on_progress = _ignore_progress
    if should_cancel is None:
        should_cancel = _never_cancel

    root = os.path.abspath(os.fspath(root))
    if not os.path.isdir(root):
        return Catalog()

    clips: list[CatalogClip] = []
    problems: list[CatalogProblem] = []
    cancelled = False

    def note_walk_error(error: OSError) -> None:
        problems.append(CatalogProblem(
            _relative(root, error.filename) if error.filename else "",
            str(error),
            reason=REASON_WALK_ERROR,
        ))

    for current_root, _directory_names, file_names in os.walk(
        root, topdown=True, onerror=note_walk_error, followlinks=False,
    ):
        if should_cancel():
            cancelled = True
            break

        directory = _relative(root, current_root)
        record_names = sorted(
            (name for name in file_names
             if name.lower().endswith(RECORD_SCAN_EXTENSION)),
            key=str.casefold,
        )
        if not record_names:
            continue

        videos = _video_stems(file_names)
        for name in record_names:
            if should_cancel():
                cancelled = True
                break
            record_path = os.path.join(current_root, name)
            relative = f"{directory}/{name}" if directory else name
            on_progress(len(clips), relative)
            try:
                record = load_record(record_path)
            except (RecordError, OSError) as error:
                problems.append(CatalogProblem(
                    relative, str(error),
                    reason=record_error_reason(error)
                    if isinstance(error, RecordError) else REASON_UNREADABLE,
                ))
                continue
            video_path = _sibling_video(record_path, videos)
            if video_path is None:
                continue
            clips.append(CatalogClip(
                path=video_path,
                relative_path=_relative(root, video_path),
                record_path=record_path,
                tags=record.tags,
                record=record,
            ))

        if cancelled:
            break

    return Catalog(
        clips=tuple(sorted(clips, key=lambda clip: clip.relative_path.casefold())),
        problems=tuple(sorted(problems, key=lambda item: item.path.casefold())),
        cancelled=cancelled,
    )


# ---------------------------------------------------------------------------
# Reconciling the vocabulary against the library
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VocabularySync:
    """What one sync did, in the terms a screen can report.

    The counts are the batch's own accounting rather than a recount: `values_added`
    is measured across the union and `values_removed` is what `prune_to` said it
    took, so what the user is shown cannot disagree with what was written.
    """

    root: str
    clips_found: int = 0
    values_added: int = 0
    #: `(namespace, value)` pairs, raw and display-cased.
    values_removed: tuple[tuple[str, str], ...] = ()
    #: Values no clip uses that were kept anyway, because they are shipped
    #: defaults. Reported because "nothing was removed" is otherwise
    #: indistinguishable from "nothing needed removing", and the second reading
    #: is the wrong one whenever a library does not use every filler type.
    values_kept: tuple[tuple[str, str], ...] = ()
    problems: tuple[CatalogProblem, ...] = ()
    cancelled: bool = False
    #: True when the prune was skipped because the library held no clips. Not an
    #: error and not a dry run -- see `sync_vocabulary`.
    skipped_prune: bool = False


def sync_vocabulary(
    root: str,
    vocabulary,
    on_progress: Callable[[int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> VocabularySync:
    """Reconcile `vocabulary` against the clips the library holds.

    Union first, then prune. Both halves are wanted and neither is enough: the
    union is what puts a rebuilt or hand-edited file back in step with a library
    the user already had, and the prune is what drops a value whose only clip the
    user deleted. The order matters because `prune_to` removes and never adds --
    pruning first would delete everything the union had just contributed.

    Two rules keep a destructive operation from being a surprising one:

    - **A cancelled walk writes nothing.** The catalog is built in full before
      anything is mutated, and a cancelled one is discarded outright. Pruning
      against half a library would delete every value the other half is using,
      and the user would have no way to tell that from a correct prune.
    - **An empty library does not prune.** On a fresh install `export/` is empty
      by design, and an unconditional prune would delete the shipped
      `filler_type` defaults -- leaving every dropdown on its "Populate this list
      by staging tags" placeholder on a brand-new install. An empty library is
      not evidence that every value is unused; it is evidence there is no
      library. The union is a no-op in that case, so nothing is lost by skipping.

    That guard covers the *empty* library. A library holding one clip is a
    different case, and it is handled one level down: `prune_to` never removes a
    shipped default, because a default is not library residue. A user who has
    exported one clip has said nothing about the other ten filler types, and
    sparing the defaults is why `values_kept` exists -- without it the screen
    would report that nothing was unused when the truth is that the prune was
    prevented from acting.

    `vocabulary` is passed in rather than resolved here, and the caller is
    expected to hand over `shared.vocabulary.get_vocabulary()` -- the cached
    instance. Loading a fresh copy would write a correct file and leave the
    cache stale, so an editor opened later in the same session would offer the
    old dropdowns.
    """
    catalog = build_catalog(root, on_progress=on_progress,
                            should_cancel=should_cancel)
    if catalog.cancelled or vocabulary is None:
        return VocabularySync(
            root=os.path.abspath(os.fspath(root)),
            clips_found=len(catalog.clips),
            problems=catalog.problems,
            cancelled=catalog.cancelled,
            skipped_prune=True,
        )

    before = {
        namespace: len(vocabulary.values(namespace))
        for namespace in vocabulary.namespaces()
    }
    for clip in catalog.clips:
        vocabulary.record(clip.tag_dict())

    # Measured between the union and the prune, not after both. The two can land
    # on the same namespace -- one clip's value added, another clip's value whose
    # only clip the user deleted -- and a size taken after the prune would report
    # the net change and call an addition nothing.
    values_added = sum(
        len(vocabulary.values(namespace))
        - before.get(namespace, 0)
        for namespace in vocabulary.namespaces()
    )

    if catalog.clips:
        # Seeded from the vocabulary's own namespaces, because `prune_to` leaves
        # a namespace it was not asked about alone. "The library holds no `block`
        # tag at all" has to empty the block list rather than preserve it, and
        # the namespaces present after the union are the complete set that has
        # to be reconciled.
        in_use: dict[str, list[str]] = {
            namespace: [] for namespace in vocabulary.namespaces()
        }
        for clip in catalog.clips:
            for key, value in clip.tags:
                in_use.setdefault(key, []).append(value)
        removed = vocabulary.prune_to(in_use)
    else:
        removed = PruneResult()

    if vocabulary.dirty:
        vocabulary.save()

    return VocabularySync(
        root=os.path.abspath(os.fspath(root)),
        clips_found=len(catalog.clips),
        values_added=values_added,
        values_removed=removed.removed,
        values_kept=removed.protected,
        problems=catalog.problems,
        skipped_prune=not catalog.clips,
    )