"""Bringing somebody else's finished clips into this library.

Applies to: `shared/importing.py`, `shared/exporting.py`, `shared/catalog.py`,
`shared/records.py`, `shared/sources.py`, `shared/paths.py`.

A finished clip is not a segment of a compilation, so it cannot arrive through
`shared/exporting.py:plan_export`, which plans segments. What it *is* is already
somebody else's answer to the same question — where does a clip with these tags go
— so the answer is read from their `.cnfo` and the planning is done through the
identical rules. `plan_clip_destination` and `DestinationIndex` are shared with the
export planner rather than forked, because the one thing an importer must not get
wrong is putting a foreign clip somewhere an exported one would not go.

Two modes, and the difference is where the tags came from:

- **Tagged.** `candidates_from_catalog(build_catalog(root))` turns records into
  candidates. The foreign library did the tagging; this module only decides where
  the results land. No ffmpeg runs: `ClipRecord` carries `duration`, so the new
  record is rendered from the old one.
- **Untagged.** There is no record, so somebody has to supply the tags. That
  somebody is a person, and this module's job is to make it easy for them —
  `find_videos` finds the clips and `propose_tags_from_path` suggests candidates.
  A proposal is never a value: nothing here writes one anywhere, and `plan_import`
  only ever receives tags a caller has already settled.

Three rules that are decisions rather than mechanics:

- **A destination that is already occupied is skipped if the record already there
  carries the same tags, and refused if it does not.** Export refuses outright,
  which is right for a clip the user is looking at and wrong for a re-run: the
  second run of an import has to be a no-op rather than a failure, or the
  operation cannot be retried after a partial cancellation.
- **A bad clip does not take the batch down with it.** One record missing Time
  Period should cost one clip out of eight hundred. `preflight_export_plan` refuses
  everything, and that is right for a Stage or an Export; an import of a third
  party's library is a different operation. So every skip and refusal is carried in
  the result by name, and partial success is never silent.
- **The file in `import/` is copied by default, not moved.** A cancelled run, a
  wrong tag, or a wrong destination must not destroy the user's media. `move` is
  available and is the caller's choice to make, never the default.

Nothing here holds a Qt type, and no child process runs: copying, linking and
moving are all filesystem operations, so `no_console_kwargs` has no work to do.
The transfer is a parameter rather than a radio button so this module is complete
for all three behaviours before there is a screen to choose between them.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from shared.catalog import Catalog
from shared.environment import install_root
from shared.exporting import (
    DestinationIndex,
    ExportPlanError,
    ExportSchemes,
    PlannedExportClip,
    normalized_destination_key,
    plan_clip_destination,
    validate_export_parent,
)
from shared.naming import compile_filename_scheme
from shared.paths import compile_folder_scheme, normalized_validation_key
from shared.records import RECORD_EXTENSION, ClipRecord, render_record_xml
from shared.scheme import CANONICAL_TAG_KEYS
from shared.sources import is_video_file

#: What to do with the file in `import/` once its clip has been written.
#:
#: `copy` is the default and the only one safe to assume. `move` destroys the
#: user's media if the run is cancelled or a tag is wrong, and `link` is not always
#: available: exFAT and FAT32 have no hard links, and a link cannot cross a volume,
#: so an import folder on a different drive from the library would fail every
#: single clip. `link` therefore falls back to a copy per clip rather than
#: refusing the batch -- one unlinkable file is a note, not a failure.
TRANSFER_COPY = "copy"
TRANSFER_LINK = "link"
TRANSFER_MOVE = "move"
TRANSFERS = (TRANSFER_COPY, TRANSFER_LINK, TRANSFER_MOVE)


def _check_transfer(transfer: str) -> str:
    if transfer not in TRANSFERS:
        raise ValueError(
            f"transfer must be one of {', '.join(TRANSFERS)}, not {transfer!r}")
    return transfer


# ---------------------------------------------------------------------------
# What is being imported
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportCandidate:
    """One finished clip, with its tags settled.

    `source_path` is the video wherever it is now; `provenance` is what the new
    record's `<source>` will hold, and `duration` what the new record's duration
    will be. For a tagged import all three come from the foreign record -- which is
    why a tagged import needs no ffmpeg -- and for an untagged one the tags come
    from a person and the duration has to be probed.
    """

    source_path: str
    #: Canonical tag pairs. Validated by the planner rather than here, so a missing
    #: required tag is reported by the same rule the export planner uses.
    tags: tuple[tuple[str, str], ...]
    #: Becomes the new record's `<source>`. For a clip cut from a compilation that
    #: is the compilation; for an imported one it is the file it arrived as. One
    #: field, two honest values -- nothing in the app reads it for logic.
    provenance: str
    duration: float


def candidates_from_catalog(catalog: Catalog) -> tuple[ImportCandidate, ...]:
    """The clips in `catalog` as import candidates.

    The tagged path in one step: a library walked, its clips read, and each handed
    over with the tags, duration and provenance its own record already carries.
    """
    return tuple(
        ImportCandidate(
            source_path=clip.path,
            tags=clip.tags,
            provenance=clip.record.source,
            duration=clip.record.duration,
        )
        for clip in catalog.clips
    )


def import_folder() -> str:
    """Where finished clips are put to be imported.

    The importer's own folder, and deliberately not a source-video folder any more:
    a source video is picked with a file dialog from anywhere, which is what freed
    this one up. Resolved here rather than reaching into `shared/sources.py` so the
    two names -- the app's `import/` and the caller's `--from` -- cannot be
    confused at a call site.
    """
    return os.path.join(install_root(), "import")


# ---------------------------------------------------------------------------
# Why a clip was left out
# ---------------------------------------------------------------------------

#: A code rather than a sentence, so a caller can group an eight-hundred-clip
#: report; `ImportSkip.reason_text` is what the person reads.
REASON_MISSING_TAGS = "missing-required-tags"
REASON_UNKNOWN_TAG = "unknown-tag"
REASON_INVALID_DURATION = "invalid-duration"
REASON_NO_DURATION = "no-duration"
REASON_PATH_TOO_LONG = "path-too-long"
REASON_UNSAFE_PARENT = "unsafe-parent"
REASON_UNREADABLE_SOURCE = "unreadable-source"
REASON_DESTINATION_TAKEN = "destination-taken"
REASON_DUPLICATE = "duplicate-destination"
REASON_ALREADY_PRESENT = "already-present"
REASON_TRANSFER_FAILED = "transfer-failed"
REASON_RECORD_FAILED = "record-failed"
REASON_OUT_OF_SPACE = "out-of-space"


@dataclass(frozen=True)
class ImportSkip:
    """One clip that is not being imported, and why.

    Carried by name on purpose. A silent skip is a clip the user cannot find
    afterwards, which is the failure `Catalog.problems` exists for and the one the
    export summary screen was written to stop.
    """

    source_path: str
    #: Where it would have gone, or "" when it never got that far.
    relative_path: str
    reason: str
    reason_text: str

    def __str__(self) -> str:
        target = f" -> {self.relative_path}" if self.relative_path else ""
        return f"{self.source_path}{target}: {self.reason_text}"


def _reason_for_plan_error(error: ExportPlanError) -> str:
    """Map a shared planner's complaint onto an import skip reason.

    `plan_clip_destination` is shared, so its messages are worded for whichever
    clip the caller named. Classifying them here keeps the export's vocabulary out
    of the import's, at the cost of reading prose -- the same trade
    `shared.records.record_error_reason` makes, and pins with tests for the same
    reason.
    """
    message = str(error)
    if "missing required tags" in message:
        return REASON_MISSING_TAGS
    if "unknown tag" in message:
        return REASON_UNKNOWN_TAG
    return REASON_PATH_TOO_LONG


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedImportClip:
    """One clip resolved to a destination, ready to be written.

    Wraps `PlannedExportClip` rather than repeating its fields: that type already
    answers "where does a clip with these tags go", and reusing it is what keeps the
    import and the export from developing separate opinions. Its `segment_index`
    and `start` carry no meaning here and are left at their defaults rather than
    given invented values.
    """

    destination: PlannedExportClip
    #: The video to copy, and the `<source>` and duration for its new record.
    source_path: str
    provenance: str
    duration: float
    #: Size of the file, measured at plan time so a copying run cannot begin and
    #: then discover it has nowhere to put the bytes.
    size_bytes: int

    @property
    def relative_path(self) -> str:
        return self.destination.relative_path

    @property
    def record_relative_path(self) -> str:
        return self.destination.record_relative_path


@dataclass(frozen=True)
class ImportPlan:
    """What an import will do, resolved and checked before anything is written."""

    clips: tuple[PlannedImportClip, ...] = ()
    #: Clips left out, each with its reason. Never silently empty.
    skipped: tuple[ImportSkip, ...] = ()
    export_root: str = ""
    transfer: str = TRANSFER_COPY
    #: Bytes a copying transfer has to write.
    total_bytes: int = 0

    @property
    def already_present(self) -> tuple[ImportSkip, ...]:
        """The skips that mean "you already have this one"."""
        return tuple(skip for skip in self.skipped
                     if skip.reason == REASON_ALREADY_PRESENT)

    @property
    def refusals(self) -> tuple[ImportSkip, ...]:
        """The skips that are a real conflict rather than a no-op."""
        return tuple(skip for skip in self.skipped
                     if skip.reason != REASON_ALREADY_PRESENT)

    def record_for(self, clip: PlannedImportClip) -> ClipRecord:
        """The record this clip will be published with.

        `segment_index` 0 and `start` 0.0 are not claims about the clip, they are
        the only values an imported clip has: it was not cut from a segment, so
        there is no index and it begins at the beginning. Written so that
        invariant 12's guarantee -- `video present` implies `record present` --
        holds for an imported clip exactly as it does for an exported one.
        """
        return ClipRecord(
            source=clip.provenance,
            segment_index=0,
            start=0.0,
            duration=clip.duration,
            tags=clip.destination.tags,
        )


def _library_tags(existing: Catalog | None) -> dict[tuple[str, ...], dict[str, str]]:
    """What the destination library already holds, keyed in the collision key
    space.

    `normalized_destination_key` rather than `==`: on a case-insensitive volume
    `cartoon network/A Clip.mp4` and `Cartoon Network/A Clip.mp4` are one file, and
    comparing the strings would let the incoming clip overwrite the existing one.
    That is the single outcome worse than refusing.
    """
    if existing is None:
        return {}
    return {
        normalized_destination_key(clip.relative_path): clip.tag_dict()
        for clip in existing.clips
    }


def _duration_skip(candidate: ImportCandidate, label: str) -> ImportSkip | None:
    """A clip with no usable duration, or None.

    `ClipRecord.__post_init__` requires only that a duration be *numeric*, so a
    foreign record holding `0` reads perfectly and gets here. Refusing is right:
    the record would claim a clip of no length, and invariant 12 makes that record
    the thing a reader trusts.
    """
    duration = candidate.duration
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return ImportSkip(
            source_path=candidate.source_path, relative_path="",
            reason=REASON_NO_DURATION,
            reason_text=f"{label} has no usable duration, so a record for it "
                        f"could not be written",
        )
    if not math.isfinite(duration) or duration <= 0:
        return ImportSkip(
            source_path=candidate.source_path, relative_path="",
            reason=REASON_INVALID_DURATION,
            reason_text=f"{label} has a duration of {duration}, which is not a "
                        f"clip any length",
        )
    return None


def plan_import(
    candidates: Iterable[ImportCandidate],
    schemes: ExportSchemes,
    export_root: str,
    *,
    transfer: str = TRANSFER_COPY,
    existing: Catalog | None = None,
) -> ImportPlan:
    """Resolve every candidate to a destination, and decide what to do about each.

    Per-clip rather than all-or-nothing, which is the one place this deliberately
    parts company with `plan_export`. A Stage or an Export is one clip the user is
    looking at, so refusing the whole batch on one bad segment is right there; an
    import of somebody else's eight hundred clips is a different operation, and one
    record missing Time Period should cost one clip rather than all of them. The
    price of that is that a partial import has to be *reported* rather than raised,
    which is what `ImportSkip` is.

    `existing` is the destination library -- `build_catalog(export_folder())`. It is
    what makes an occupied destination decidable rather than a blanket refusal:

    - taken, and the record already there carries the same tags -> **skipped** as
      already present. This is what makes a re-run a no-op instead of a failure.
    - taken, anything else -> **refused by name**, naming the tags that differ. No
      clobber for a real conflict, as the export preflight has it.

    `transfer` is recorded in the plan rather than acted on; planning writes
    nothing.
    """
    if not isinstance(schemes, ExportSchemes):
        raise TypeError("schemes must be an ExportSchemes snapshot")
    _check_transfer(transfer)

    root = os.path.abspath(os.fspath(export_root))
    filename_scheme = compile_filename_scheme(schemes.file_scheme)
    folder_scheme = compile_folder_scheme(schemes.folder_scheme)
    index = DestinationIndex()
    library = _library_tags(existing)

    clips: list[PlannedImportClip] = []
    skipped: list[ImportSkip] = []
    total_bytes = 0

    for candidate in candidates:
        label = os.path.basename(candidate.source_path) or candidate.source_path

        duration_problem = _duration_skip(candidate, label)
        if duration_problem is not None:
            skipped.append(duration_problem)
            continue

        try:
            destination = plan_clip_destination(
                tags=dict(candidate.tags),
                folder_scheme=folder_scheme,
                filename_scheme=filename_scheme,
                label=label,
            )
        except ExportPlanError as error:
            skipped.append(ImportSkip(
                source_path=candidate.source_path, relative_path="",
                reason=_reason_for_plan_error(error), reason_text=str(error),
            ))
            continue

        relative_path = destination.relative_path
        claim = index.claim(destination.relative_components,
                            owner=candidate.source_path)
        if not claim.ok:
            skipped.append(ImportSkip(
                source_path=candidate.source_path,
                relative_path=relative_path,
                reason=REASON_DUPLICATE,
                reason_text=(
                    f"{label} resolves to the same destination as "
                    f"{os.path.basename(str(claim.conflict_with))}, and that "
                    f"clip comes first"
                ),
            ))
            continue

        held = library.get(normalized_destination_key(relative_path))
        if held is not None:
            # Symmetric, not one-directional. Comparing only the incoming tags
            # would call an existing clip carrying a tag this one lacks "already
            # present" -- and quietly keep the poorer copy of the pair.
            incoming = dict(destination.tags)
            if held == incoming:
                skipped.append(ImportSkip(
                    source_path=candidate.source_path,
                    relative_path=relative_path,
                    reason=REASON_ALREADY_PRESENT,
                    reason_text=(
                        f"{label} is already in the library with these tags, so "
                        f"there was nothing to import"
                    ),
                ))
                continue
            differing = sorted(
                key for key in set(held) | set(incoming)
                if held.get(key) != incoming.get(key)
            )
            skipped.append(ImportSkip(
                source_path=candidate.source_path,
                relative_path=relative_path,
                reason=REASON_DESTINATION_TAKEN,
                reason_text=(
                    f"{label} would land on a clip that is already there with "
                    f"different {', '.join(differing)}. The clip already in the "
                    f"library was left alone."
                ),
            ))
            continue

        try:
            validate_export_parent(root, destination.relative_components)
        except ExportPlanError as error:
            skipped.append(ImportSkip(
                source_path=candidate.source_path, relative_path="",
                reason=REASON_UNSAFE_PARENT, reason_text=str(error),
            ))
            continue

        try:
            size = os.path.getsize(candidate.source_path)
        except OSError as error:
            skipped.append(ImportSkip(
                source_path=candidate.source_path, relative_path="",
                reason=REASON_UNREADABLE_SOURCE,
                reason_text=f"{label} could not be read: "
                            f"{error.strerror or error}",
            ))
            continue

        clips.append(PlannedImportClip(
            destination=PlannedExportClip(
                segment_index=0,
                start=0.0,
                duration=float(candidate.duration),
                relative_components=destination.relative_components,
                tags=destination.tags,
                record_relative_components=destination.record_relative_components,
            ),
            source_path=candidate.source_path,
            provenance=candidate.provenance,
            duration=float(candidate.duration),
            size_bytes=size,
        ))
        total_bytes += size

    return ImportPlan(
        clips=tuple(clips),
        skipped=tuple(skipped),
        export_root=root,
        transfer=transfer,
        total_bytes=total_bytes,
    )


# ---------------------------------------------------------------------------
# Free space
# ---------------------------------------------------------------------------

class ImportSpaceError(ValueError):
    """The destination cannot hold what a copying import has to write."""


def check_free_space(plan: ImportPlan) -> None:
    """Refuse a copying import that does not fit, once, before anything is written.

    No precedent in the app for a space check: `preflight_export_plan` measures the
    *path length* in UTF-8 bytes, not free space, and ffmpeg is told to overwrite
    what it has already written. This is new, and it is here rather than in the UI
    because a run of eight hundred copies that dies on clip four hundred leaves the
    user to work out which half arrived.

    Failing once up front is the shape `check_video_encoder` established; what is
    new is measuring a volume instead of a codec.

    `link` and `move` need nothing: a hard link writes a directory entry, and a move
    relocates.
    """
    if plan.transfer != TRANSFER_COPY or not plan.clips:
        return
    free = shutil.disk_usage(_nearest_existing(plan.export_root)).free
    if plan.total_bytes <= free:
        return
    shortfall = plan.total_bytes - free
    raise ImportSpaceError(
        f"Importing {len(plan.clips)} clip(s) needs "
        f"{_format_bytes(plan.total_bytes)}, but there is only "
        f"{_format_bytes(free)} free in:\n{plan.export_root}\n\n"
        f"That is {_format_bytes(shortfall)} short. Free some space, or import "
        f"in smaller batches, or import without copying the videos."
    )


def _nearest_existing(path: str) -> str:
    """The first directory at or above `path` that exists.

    An import into a library folder that is not there yet is ordinary -- the app
    creates it -- and `disk_usage` on a missing path raises rather than answering.
    The volume is the same either way, so the walk up is free.
    """
    candidate = os.path.abspath(path)
    while not os.path.isdir(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return candidate
        candidate = parent
    return candidate


def _format_bytes(count: int) -> str:
    """A size a person can read. Deliberately coarse -- this is a refusal, and a
    refusal wants to be understood rather than measured."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportResult:
    """What an import actually did.

    Three lists and a flag, because "imported 812 clips" is the number a bug
    produces. Every clip this run committed stays committed, and every one it did
    not is named: the same accounting `_export_summary` gives the export screen,
    for the same reason.
    """

    #: Destinations written, relative, in the order they were committed.
    written: tuple[str, ...] = ()
    #: Clips that were already in the library with the same tags.
    already_present: tuple[ImportSkip, ...] = ()
    #: Clips that failed, each with its reason.
    failed: tuple[ImportSkip, ...] = ()
    cancelled: bool = False

    @property
    def committed(self) -> int:
        return len(self.written)


#: Past tense for the transfer, for the message a failure produces. Not
#: derivable -- "copy" does not become "copyed".
_TRANSFER_PAST = {
    TRANSFER_COPY: "copied",
    TRANSFER_LINK: "linked",
    TRANSFER_MOVE: "moved",
}


def _transfer_file(source: str, destination: str, transfer: str) -> str:
    """Put `source` at `destination`. Returns the transfer that actually happened.

    `link` is a request, not a demand: exFAT and FAT32 have no hard links and a
    link cannot cross a volume, so an unlinkable file is copied and reported rather
    than failing the batch. A caller that needs to know gets `link` back for the
    ones that worked and `copy` for the ones that did not, which is more honest
    than pretending the link happened.
    """
    if transfer == TRANSFER_MOVE:
        shutil.move(source, destination)
        return TRANSFER_MOVE
    if transfer == TRANSFER_LINK:
        try:
            os.link(source, destination)
            return TRANSFER_LINK
        except OSError:
            shutil.copy2(source, destination)
            return TRANSFER_COPY
    shutil.copy2(source, destination)
    return TRANSFER_COPY


def execute_import(
    plan: ImportPlan,
    on_progress: Callable[[int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ImportResult:
    """Write every clip in `plan`, and report exactly what happened.

    A cancelled run stops and says so, and **keeps everything it committed** —
    the same rule the export follows, and the reason `already_present` is part of
    `plan_import` rather than something a caller has to arrange. Re-planning the
    same folder after a cancellation therefore skips what landed and finishes the
    rest.

    The record is published *before* the video is committed, not after, and both
    go through a temporary file in the destination directory first. That ordering
    is what keeps invariant 12's guarantee -- `video present` implies `record
    present` -- true at every instant a reader could observe, including the one
    where the process is killed mid-run. It is the reverse of the export's order and
    for the same reason: there, the encode is the long part and the record
    identifies it; here, the file copy is.

    One failing clip does not stop the run. Its destination is left clean and it
    goes in `failed` with its reason.
    """
    if on_progress is None:
        on_progress = lambda _done, _path: None  # noqa: E731
    if should_cancel is None:
        should_cancel = lambda: False  # noqa: E731

    check_free_space(plan)

    written: list[str] = []
    failed: list[ImportSkip] = []
    root = plan.export_root

    for position, clip in enumerate(plan.clips):
        relative = clip.relative_path
        if should_cancel():
            return ImportResult(
                written=tuple(written),
                already_present=plan.already_present,
                failed=tuple(failed),
                cancelled=True,
            )
        on_progress(position, relative)

        video_path = os.path.join(root, *clip.destination.relative_components)
        record_path = os.path.join(root, *clip.destination.record_relative_components)
        try:
            os.makedirs(os.path.dirname(video_path), exist_ok=True)
            _transfer_file(clip.source_path, video_path, plan.transfer)
        except OSError as error:
            failed.append(ImportSkip(
                source_path=clip.source_path, relative_path=relative,
                reason=REASON_TRANSFER_FAILED,
                reason_text=f"{os.path.basename(clip.source_path)} could not be "
                            f"{_TRANSFER_PAST[plan.transfer]}: "
                            f"{error.strerror or error}",
            ))
            continue

        try:
            record = plan.record_for(clip)
            with open(_sibling_temp(record_path), "w", encoding="utf-8",
                      newline="\n") as handle:
                handle.write(render_record_xml(record))
            os.replace(_sibling_temp(record_path), record_path)
        except (OSError, ValueError) as error:
            # The video landed and its record did not, which invariant 12 forbids.
            # Removing the video is the only way back to a state the guarantee
            # holds for: a clip with no record reads as a stray file, and a
            # dangling record reads as a clip that exists.
            _remove_quietly(video_path)
            failed.append(ImportSkip(
                source_path=clip.source_path, relative_path=relative,
                reason=REASON_RECORD_FAILED,
                reason_text=f"{os.path.basename(clip.source_path)} was copied but "
                            f"its record could not be written, so it was removed "
                            f"again: {error}",
            ))
            continue

        written.append(relative)

    on_progress(len(plan.clips), "")
    return ImportResult(
        written=tuple(written),
        already_present=plan.already_present,
        failed=tuple(failed),
    )


def _sibling_temp(path: str) -> str:
    """A staging name beside `path`, so publishing is a rename on one volume.

    Beside rather than in `temp/`: `os.replace` is only atomic within a volume, so
    a staged file elsewhere would turn every record into a copy followed by a
    rename, and a crash between the two would leave a record in a folder nothing
    reads.
    """
    return f"{path}.commcut-import.tmp"


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Untagged discovery
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FoundVideo:
    """A video in a tree, whether or not anything says what it is."""

    path: str
    relative_path: str
    #: True when a `.cnfo` sits beside it. The untagged path still lists those, so
    #: the caller can hand them to the tagged one instead of making the user
    #: decide which half they are in.
    has_record: bool


def find_videos(
    root: str,
    on_progress: Callable[[int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[FoundVideo, ...]:
    """Every video under `root`, records or not.

    The deliberate inverse of `shared/catalog.py:build_catalog`, which yields only
    clips a record backs. That rule is right for a library this app maintains and
    useless for a folder of somebody's rips, where the absence of a record is the
    normal case rather than a sign of corruption.

    Traversal is identical to the catalog's -- `os.walk`, `followlinks=False`, a
    missing root is empty rather than an error, `on_progress` carrying no total --
    because one traversal rule in this app is worth more than two that differ. The
    one deliberate difference: a directory that cannot be read is simply not
    descended into, where the catalog reports it. Here a half-readable folder is
    the normal state of a folder someone has been filling by hand for years, and a
    walk that refused to return anything at all because of one bad subfolder would
    be useless.
    """
    if on_progress is None:
        on_progress = lambda _found, _path: None  # noqa: E731
    if should_cancel is None:
        should_cancel = lambda: False  # noqa: E731

    base = os.path.abspath(os.fspath(root))
    if not os.path.isdir(base):
        return ()

    found: list[FoundVideo] = []
    for current_root, _dirs, file_names in os.walk(
        base, topdown=True, followlinks=False,
    ):
        if should_cancel():
            break
        directory = _relative_dir(base, current_root)
        video_names = sorted(
            (name for name in file_names if is_video_file(name)), key=str.casefold)
        if not video_names:
            continue
        stems_with_records = {
            os.path.splitext(name)[0]
            for name in file_names if name.lower().endswith(RECORD_EXTENSION)
        }
        for name in video_names:
            if should_cancel():
                break
            path = os.path.join(current_root, name)
            relative = f"{directory}/{name}" if directory else name
            on_progress(len(found), relative)
            if not os.path.isfile(path):
                continue
            found.append(FoundVideo(
                path=path,
                relative_path=relative,
                has_record=os.path.splitext(name)[0] in stems_with_records,
            ))
    return tuple(found)


def _relative_dir(root: str, path: str) -> str:
    relative = os.path.relpath(path, root)
    return "" if relative == os.curdir else relative.replace(os.sep, "/")


# ---------------------------------------------------------------------------
# Proposing tags from a path
# ---------------------------------------------------------------------------

#: How much to trust a proposal. Derived, never asserted, and `weak` means "do not
#: offer this as an answer" rather than "offer it with a shrug".
CONFIDENCE_EXACT = "exact"
CONFIDENCE_CANDIDATE = "candidate"
CONFIDENCE_WEAK = "weak"


def _clip_count(library: Catalog, namespace: str, value: str) -> int:
    """How many of the library's clips actually use this value.

    Counted from `clips`, not from `Catalog.tag_index`: the index holds *unique
    values per namespace*, so counting there answers "how many spellings of this
    value exist" (always one) rather than "how many clips use it". The difference
    is the whole point of showing a count -- fourteen clips against one is what
    makes a namespace choice obvious, and one against one says nothing.
    """
    needle = normalized_validation_key(value)
    return sum(
        1 for clip in library.clips
        for key, held in clip.tags
        if key == namespace and normalized_validation_key(held) == needle
    )


@dataclass(frozen=True)
class TagProposal:
    """A suggestion for one tag, with the reason it was suggested.

    The type exists to keep a guess from becoming a value. Nothing in the app
    writes a `TagProposal` anywhere, and `plan_import` only accepts tags a caller
    has already settled — so the boundary between "a person said this" and "a
    folder name looked like this" is a type boundary rather than a convention
    somebody has to remember.

    `evidence` is the whole point of the class. `docs/naming-and-organization.md`
    refuses the reverse parser outright, and that refusal is about trust: a scheme
    renders lossily, so a name cannot be read back. A proposal does not claim the
    name says what it means; it claims a person might agree, and shows why.
    """

    namespace: str
    value: str
    confidence: str
    evidence: str


def propose_tags_from_path(
    relative_path: str,
    *,
    library: Catalog | None = None,
    vocabulary=None,
) -> tuple[TagProposal, ...]:
    """Suggest tags for a video from where it sits, and from nothing else.

    A heuristic, and its docstring should not pretend otherwise. Folder names and
    `-`/`(...)` tokens in a filename are what a person's naming scheme tends to
    produce, so they are worth offering — as questions.

    Confidence is earned, not guessed:

    - `exact` -- the token matches a value the existing library uses in exactly one
      namespace, by the same normalization the collision rules use.
    - `candidate` -- it matches in several namespaces (all listed), or only in the
      advisory `vocabulary.json`, which is a cache downstream of the library and
      drifts, so a hit there is weaker evidence than a hit in the library itself.
    - `weak` -- only a substring. Offered so a person can recognize it, never as an
      answer.

    Nothing is proposed for `title`: it is unique per clip, and a folder or a `-`
    token in a filename is a *shared* label, which is what every other tag means.
    Guessing the one field nothing can be wrong about twice is not a kindness.
    """
    tokens = _path_tokens(relative_path)
    if not tokens:
        return ()

    index = library.tag_index if library is not None else {}
    proposals: list[TagProposal] = []
    seen: set[tuple[str, str]] = set()

    for token in tokens:
        for namespace in sorted(_suggestible_namespaces()):
            matches = _namespaces_holding(index, vocabulary, token)
            confidence, evidence = _rank(matches, namespace, token, library,
                                         vocabulary)
            if confidence is None:
                continue
            key = (namespace, token)
            if key in seen:
                continue
            seen.add(key)
            proposals.append(TagProposal(
                namespace=namespace, value=token, confidence=confidence,
                evidence=evidence,
            ))
    return tuple(proposals)


def _suggestible_namespaces() -> tuple[str, ...]:
    """Namespaces a proposal may name: the canonical ones that are not `title`.

    The same nine the vocabulary file keeps. A proposal cannot invent a namespace,
    because `canonical_tag_name` would refuse it and the mesh wizard would have
    nothing to map it onto.
    """
    return tuple(sorted(name for name in CANONICAL_TAG_KEYS if name != "title"))


#: ` - ` in a filename, and anything in round brackets. The two shapes a rendered
#: commcut name actually produces, so the two worth splitting on.
_PARENTHETICAL = re.compile(r"\(([^)]*)\)")
_DASH_SEPARATOR = re.compile(r"\s+-\s+")


def _path_tokens(relative_path: str) -> tuple[str, ...]:
    """The candidate strings in a relative path: folder names, `-`-separated parts
    of the filename, and anything the filename put in round brackets.

    Deliberately crude, and that is the point: this is the one place in the app
    that looks at a path for meaning, and it may only ever produce something a
    person confirms. A parenthetical is extracted *before* the brackets are
    stripped, because `(Toonami)` inside `Worlds Finest (Toonami)` is one tag and
    the words around it are another -- flattening first would yield the single
    string `Worlds Finest Toonami` and find nothing.
    """
    parts = [part for part in relative_path.split("/") if part]
    if not parts:
        return ()

    stem = os.path.splitext(parts[-1])[0]
    tokens: list[str] = [part.replace("_", " ").strip() for part in parts[:-1]]
    tokens.extend(match.strip() for match in _PARENTHETICAL.findall(stem))

    flattened = _PARENTHETICAL.sub(" ", stem).replace("[", " ").replace("]", " ")
    for chunk in _DASH_SEPARATOR.split(flattened):
        cleaned = " ".join(chunk.split())
        if cleaned:
            tokens.append(cleaned)

    unique: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token not in unique:
            unique.append(token)
    return tuple(unique)


def _namespaces_holding(index, vocabulary, token: str) -> dict[str, str]:
    """Where this token is already in use, as `{namespace: "library"|"vocabulary"}`.

    The library is asked first and the vocabulary only where the library is silent,
    because the two are not equally good evidence: the library is what this user
    actually has, and `vocabulary.json` is a hint that survives a clip being
    deleted.
    """
    needle = normalized_validation_key(token)
    found: dict[str, str] = {}
    for namespace, values in index.items():
        if any(normalized_validation_key(value) == needle for value in values):
            found[namespace] = "library"
    if vocabulary is None:
        return found
    for namespace in _suggestible_namespaces():
        if namespace in found:
            continue
        for value in vocabulary.values(namespace):
            if normalized_validation_key(value) == needle:
                found[namespace] = "vocabulary"
                break
    return found


def _rank(matches, namespace: str, token: str, library, vocabulary):
    """The confidence and the sentence a proposal carries with it.

    `None` confidence means "do not propose this in this namespace at all", which
    is the answer for every token that is not already a value somewhere. A proposal
    for a namespace the token has never been seen in is a suggestion to *create*
    one, and that is a decision the mesh wizard exists to make, not this function.
    """
    kind = matches.get(namespace)
    if kind is None:
        return None, ""

    if len(matches) == 1:
        if kind == "library":
            count = _clip_count(library, namespace, token)
            return (CONFIDENCE_EXACT,
                    f"your library uses {token!r} for {namespace}, in "
                    f"{count} folder name(s)")
        return (CONFIDENCE_CANDIDATE,
                f"{token!r} appears in your tag history under {namespace}, but "
                f"no clip uses it now")

    others = sorted(name for name in matches if name != namespace)
    where = "your library" if kind == "library" else "your tag history"
    return (CONFIDENCE_CANDIDATE,
            f"{token!r} appears in {where} under "
            f"{', '.join([namespace] + others)} -- pick the right one")


# ---------------------------------------------------------------------------
# Evidence for the value question
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValueMatch:
    """One value the imported library uses, and what is known about it here.

    The counts are the point. Choosing silently between two equally plausible
    namespaces is how a library ends up with fourteen clips under `block` and one
    under `special`; showing "block (14), special (2)" makes it a click.
    """

    value: str
    namespace: str
    #: Clips in the *existing library* using it. Zero when the hit came from the
    #: advisory vocabulary file, which is exactly the distinction worth showing.
    clip_count: int
    in_library: bool
    in_vocabulary: bool


def match_value(
    value: str,
    library: Catalog | None = None,
    vocabulary=None,
) -> tuple[ValueMatch, ...]:
    """Where this imported value is already in use here, best evidence first.

    Ranked rather than chosen. An empty result is not permission to guess -- it
    means the value is new to this library, and the caller has to ask.

    `in_library` and `in_vocabulary` are separate columns because they are
    different kinds of knowing: the library is what the user has, the vocabulary
    file is what they once typed.
    """
    needle = normalized_validation_key(value)
    index = library.tag_index if library is not None else {}
    matches: list[ValueMatch] = []

    for namespace, values in index.items():
        for held in values:
            if normalized_validation_key(held) != needle:
                continue
            matches.append(ValueMatch(
                value=held,
                namespace=namespace,
                clip_count=_clip_count(library, namespace, held),
                in_library=True,
                in_vocabulary=bool(vocabulary is not None and any(
                    normalized_validation_key(candidate) == needle
                    for candidate in vocabulary.values(namespace))),
            ))
            break

    if vocabulary is not None:
        for namespace in _suggestible_namespaces():
            if any(match.namespace == namespace for match in matches):
                continue
            for held in vocabulary.values(namespace):
                if normalized_validation_key(held) == needle:
                    matches.append(ValueMatch(
                        value=held, namespace=namespace, clip_count=0,
                        in_library=False, in_vocabulary=True,
                    ))
                    break

    return tuple(sorted(
        matches,
        key=lambda match: (not match.in_library, -match.clip_count,
                           match.namespace),
    ))