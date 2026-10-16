"""Re-tagging a written clip: the Library Manager's backend.

Applies to: `shared/retagging.py`, `library/manager.py`.

A clip in the export library is a video and a `<stem>.cnfo` beside
it, and its filename and folder are a projection of the tags in that
record (invariant 12). Re-tagging a clip therefore means re-rendering
its destination under the two schemes and moving the pair there — the
same question `plan_clip_destination` answers for a segment about to
be exported, asked about a clip that already exists.

The plan is pure: `plan_retag` resolves the destination and refuses
what cannot be saved (a missing required tag, a render error, a
destination another clip already holds). The execution is a transaction
ordered so invariant 12's guarantee — a video present implies its
record present — holds at every instant, including the instants a crash
could leave behind:

1. the destination directory is created
2. the record is built, preserving the clip's ID (a v1 record, which
   has none, is assigned one here — the lazy migration)
3. the record is published in the destination directory
4. the video is moved onto its new path
5. the old record is removed

The record is published *before* the video moves, which is the import
executor's ordering mirrored for a move: a crash between 3 and 4 leaves
an orphaned record at the destination (the catalog reports it, and a
retry is self-healing because the destination video is free), and a
crash between 4 and 5 leaves the new pair complete with the old record
orphaned and identifiable by its clip ID for a future sweep. The
forbidden state — a video with no record — is unreachable: the only
step that could produce it (removing the old record) runs last, and is
skipped entirely when the old and new record are one file, which is
what a case-only change is on a case-insensitive volume.

No ffmpeg runs: a re-tag never touches a clip's content.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from shared.catalog import Catalog, CatalogClip
from shared.exporting import (
    ClipDestination,
    DestinationIndex,
    ExportPlanError,
    plan_clip_destination,
)
from shared.records import ClipRecord, new_clip_id, write_record


def retag_is_in_place(clip: CatalogClip, destination) -> bool:
    """Whether `destination` is where `clip` already lives.

    A literal component comparison, deliberately not a normalized one: a
    destination that differs only by case is **not** in place. On a
    case-sensitive volume the two paths are different files, and on a
    case-insensitive one the video still has to be renamed to the new
    spelling, so both volumes want the move path — and the collision
    check, which runs in the normalized key space, is what keeps a
    case-varied destination from colliding with a different clip.
    """
    return (tuple(clip.relative_path.split("/"))
            == tuple(destination.relative_components))


def _export_root(clip: CatalogClip) -> str:
    """The export root `clip` was read from.

    Derived from the catalog's own two facts about the clip — its
    absolute path and its path relative to the root — so it cannot
    disagree with the catalog the clip came from. Both `plan_retag` and
    `execute_retag` need the root to turn relative components into
    absolute paths, and neither takes it as an argument: the clip is
    the one thing both are always handed.
    """
    path = os.path.abspath(clip.path)
    for _ in clip.relative_path.split("/"):
        path = os.path.dirname(path)
    return path


def _is_the_same_clip(clip: CatalogClip, other: CatalogClip) -> bool:
    """Whether `other` is `clip`, by any of the three ways a catalog
    can say so.

    Identity is the ordinary answer, and the only one that works for
    a v1 record, which has no clip ID to match on. The clip ID is the
    answer when the catalog was walked again between loading the clip
    and saving it, and the relative path is the answer that holds even
    then for a v1 clip: two catalog entries cannot share a path, so
    the clip's own destination is held by the clip itself.
    """
    if other is clip:
        return True
    if clip.record.clip_id and other.record.clip_id == clip.record.clip_id:
        return True
    return other.relative_path == clip.relative_path


def plan_retag(*, clip: CatalogClip, tags: Mapping[str, str],
               folder_scheme, filename_scheme,
               catalog: Catalog):
    """Resolve a clip's new tags to a destination, or refuse by name.

    The refusals are the export planner's own — a missing required tag,
    a render error (an empty filename, a scheme that renders nothing),
    a relative path over the UTF-8 byte ceiling, all raised by
    `plan_clip_destination` — plus the occupied-destination rule, which
    is the `DestinationIndex` claim the import planner uses, unchanged:
    every other clip in the catalog has already claimed where it lives,
    and a destination one of them holds is refused naming the clip that
    holds it.

    Returns the destination: the canonical tags and both component
    tuples. Whether saving is a move or a record rewrite in place is
    `retag_is_in_place` on the result, which is the one owner of that
    answer because the window asks it on every keystroke.
    """
    label = os.path.basename(clip.path) or clip.path
    destination = plan_clip_destination(
        tags=tags, folder_scheme=folder_scheme,
        filename_scheme=filename_scheme, label=label,
    )

    index = DestinationIndex()
    for other in catalog.clips:
        if _is_the_same_clip(clip, other):
            continue
        index.claim(other.relative_path.split("/"), owner=other)

    claim = index.claim(destination.relative_components, owner=clip)
    if not claim.ok:
        conflict = claim.conflict_with
        raise ExportPlanError(
            f"{label} resolves to the same destination as "
            f"{os.path.basename(conflict.path)} "
            f"({conflict.relative_path}), which is already in the "
            f"library. That clip was left alone."
        )
    return destination


def execute_retag(clip: CatalogClip, destination,
                  tags: Mapping[str, str]) -> ClipRecord:
    """Move a clip's pair to `destination` with its tags rewritten.

    The transaction, in the order the module docstring lays out. Both
    paths are under the one export root — the clip's root, derived
    from the clip — so the video's move is a same-volume rename and
    `os.replace` is atomic.

    `tags` is the clip's new tag set; the record is rendered from
    exactly it, which is the one-way rule's other half: the record
    is the tags, and the path is their projection.

    Returns the record as it now stands on disk, which is the one
    way to learn the clip ID a v1 record was assigned here.
    """
    root = _export_root(clip)
    video_destination = os.path.join(root, *destination.relative_components)
    record_destination = os.path.join(
        root, *destination.record_relative_components)

    os.makedirs(os.path.dirname(video_destination), exist_ok=True)

    record = ClipRecord(
        source=clip.record.source,
        segment_index=clip.record.segment_index,
        start=clip.record.start,
        duration=clip.record.duration,
        tags=tuple(sorted(tags.items())),
        clip_id=clip.record.clip_id or new_clip_id(),
    )

    # Published before the video moves, so a crash leaves either the
    # old pair or the new one, never a video without its record.
    write_record(record_destination, record)

    if retag_is_in_place(clip, destination):
        return record  # a record rewrite; the video never moves

    try:
        os.replace(clip.path, video_destination)
    except BaseException:
        # The video did not move, so the destination record describes
        # nothing. Removing it restores the state the plan found: the
        # old pair untouched, and no orphaned record at the destination.
        _remove_quietly(record_destination)
        raise

    # Last, and only when it is a different file: a case-only change
    # on a case-insensitive volume has one record at both paths, and
    # removing it would delete the record this transaction just wrote.
    if not _same_file(clip.record_path, record_destination):
        _remove_quietly(clip.record_path)

    return record


def _same_file(left: str, right: str) -> bool:
    """Whether two paths are one file, or False when either is gone."""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Library maintenance: scan for, and align, misaligned clips
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MisalignedClip:
    """A clip whose path does not match what its tags render to.

    `destination` is where the clip's tags put it under the current
    schemes; `clip.relative_path` is where it actually lives. The two
    differ, which is the whole point of this type.
    """

    clip: CatalogClip
    destination: ClipDestination


@dataclass(frozen=True)
class ScanResult:
    """What a library maintenance scan found.

    `misaligned` clips can be aligned: their tags render to a
    destination different from their current path, and the tags are
    complete enough to render.

    `errors` are clips whose tags cannot render at all (a missing
    required tag, an unknown key) — aligning them is impossible until
    a person fixes the tags by hand.
    """

    misaligned: tuple[MisalignedClip, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()
    clips_found: int = 0
    cancelled: bool = False


def _ignore_progress(_checked: int, _relative_path: str) -> None:
    pass


def _never_cancel() -> bool:
    return False


def find_misaligned_clips(
    catalog: Catalog,
    folder_scheme,
    filename_scheme,
    on_progress: Callable[[int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ScanResult:
    """Find clips whose path does not match their tags.

    For each clip, renders its tags through the two schemes via
    `plan_clip_destination` — the same function the Library Manager and
    the editor both use to name a clip — and compares the result to the
    clip's actual ``relative_path``. A clip whose tags cannot render
    (a missing required tag, an unknown key) is reported as an error,
    not a mismatch: it cannot be aligned until a person fixes the tags.

    The catalog is consumed read-only. Nothing here writes to disk, so
    this function is the reusable half of the maintenance feature: a
    future "re-scheme an entire library at once" command calls this to
    find what a new scheme would move, and then aligns the same clips.
    """
    if on_progress is None:
        on_progress = _ignore_progress
    if should_cancel is None:
        should_cancel = _never_cancel

    if catalog.cancelled:
        return ScanResult(
            clips_found=len(catalog.clips),
            cancelled=True,
        )

    misaligned: list[MisalignedClip] = []
    errors: list[tuple[str, str]] = []
    clips = catalog.clips
    cancelled = False

    for index, clip in enumerate(clips):
        if should_cancel():
            cancelled = True
            break
        on_progress(index + 1, clip.relative_path)
        label = os.path.basename(clip.path) or clip.path
        try:
            destination = plan_clip_destination(
                tags=clip.tag_dict(),
                folder_scheme=folder_scheme,
                filename_scheme=filename_scheme,
                label=label,
            )
        except ExportPlanError as error:
            errors.append((label, str(error)))
            continue
        if not retag_is_in_place(clip, destination):
            misaligned.append(MisalignedClip(
                clip=clip, destination=destination))

    return ScanResult(
        misaligned=tuple(misaligned),
        errors=tuple(errors),
        clips_found=len(clips),
        cancelled=cancelled,
    )


@dataclass(frozen=True)
class AlignmentResult:
    """What aligning a batch of misaligned clips did.

    `aligned` is the count of clips that were moved. `errors` pairs a
    clip label with a human-readable reason for each one that could not
    be moved (a collision with another clip, an I/O failure).
    """

    aligned: int = 0
    errors: tuple[tuple[str, str], ...] = ()
    cancelled: bool = False


def align_misaligned_clips(
    misaligned: Sequence[MisalignedClip],
    catalog: Catalog,
    on_progress: Callable[[int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> AlignmentResult:
    """Move every misaligned clip to where its tags put it.

    Uses the same `execute_retag` transaction the Library Manager's
    Save button does — the record is published at the destination
    before the video moves, so a crash leaves either the old pair or
    the new one, never a video without its record.

    Collisions are checked through a `DestinationIndex` built from the
    catalog's non-misaligned clips plus the destinations already claimed
    by earlier clips in this run. A clip whose destination another clip
    already holds — current or planned — is refused and reported, not
    moved; the user is told which clip it would have collided with.
    """
    if on_progress is None:
        on_progress = _ignore_progress
    if should_cancel is None:
        should_cancel = _never_cancel

    # Build an index from every clip's current position, excluding the
    # misaligned ones: they are vacating their current positions, so a
    # misaligned clip's old spot must not block its new one. The index
    # then tracks new destinations as they are claimed, so two clips
    # that render to the same place are caught.
    misaligned_clips = {mc.clip for mc in misaligned}
    index = DestinationIndex()
    for clip in catalog.clips:
        if clip in misaligned_clips:
            continue
        index.claim(clip.relative_path.split("/"), owner=clip)

    aligned = 0
    errors: list[tuple[str, str]] = []

    for mc in misaligned:
        if should_cancel():
            break
        clip = mc.clip
        destination = mc.destination
        label = os.path.basename(clip.path) or clip.path
        claim = index.claim(destination.relative_components, owner=clip)
        if not claim.ok:
            conflict = claim.conflict_with
            errors.append((label, (
                f"would collide with "
                f"{os.path.basename(conflict.path)} "
                f"({conflict.relative_path})")))
            continue
        try:
            execute_retag(clip, destination, clip.tag_dict())
            aligned += 1
            on_progress(aligned, clip.relative_path)
        except (OSError, ValueError) as error:
            errors.append((label, str(error)))

    return AlignmentResult(
        aligned=aligned,
        errors=tuple(errors),
        cancelled=should_cancel(),
    )
