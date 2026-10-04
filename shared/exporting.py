"""Named-export settings, planning, and preflight policy for commcut."""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from numbers import Real

from shared import environment
from shared.naming import (
    DEFAULT_FILE_NAMING_SCHEME,
    FilenameSchemeError,
    compile_filename_scheme,
    record_filename,
    render_compiled_filename,
    sanitize_filename_stem,
)
from shared.paths import (
    DEFAULT_FOLDER_SCHEME,
    FolderSchemeError,
    MAX_FOLDER_RELATIVE_PATH_BYTES,
    compile_folder_scheme,
    normalized_validation_key,
    render_folder_components,
    sanitize_path_component,
)
from shared.records import RECORD_EXTENSION
from shared.segments import SegmentModel
from shared.scheme import canonical_tag_name

FILE_NAMING_SCHEME_KEY = "file_naming_scheme"
FOLDER_ORGANIZATION_SCHEME_KEY = "folder_organization_scheme"

#: Where the user chooses to put named clips, when they have chosen. Absent or
#: blank means the default below.
EXPORT_FOLDER_KEY = "export_folder"

#: Default folder under the install root that named clips are written to.
EXPORT_FOLDER_NAME = "export"

REQUIRED_EXPORT_TAG_NAMES: frozenset[str] = frozenset({
    "title",
    "network",
    "filler_type",
    "time_period",
})
OUTPUT_EXTENSION = ".mp4"


class ExportSettingsError(ValueError):
    """Export settings are missing, malformed, or invalid."""


class ExportPlanError(ValueError):
    """The requested batch cannot be exported safely."""


def default_export_folder() -> str:
    """The export root when the user has not chosen one: `export/` beside the app."""
    return os.path.join(environment.install_root(), EXPORT_FOLDER_NAME)


def _overlaps(candidate: str, other: str) -> bool:
    """True when either path contains the other.

    Case-folded on **every** platform, which is the whole point of spelling it
    out rather than reaching for `os.path.normcase` -- that is a no-op on POSIX.
    macOS ships a case-*insensitive* filesystem by default, so on a Mac
    `.../IMPORT` and `.../import` are one folder, and a normcase-based check
    would pass the first as a perfectly good export root and hand it straight to
    the importer it is meant to protect. A rule that only works on the platform
    its author develops on is not a rule.

    Folding unconditionally is the safe direction to be wrong in *here*, and
    that is a property of this rule rather than of paths in general. Being wrong
    this way means refusing an export folder that really is a distinct directory
    on a case-sensitive volume -- one clear message, and a rename fixes it.
    The opposite mistake would let the export root be the folder the importer
    moves and deletes from. (The same fold applied to a *traversal* decision
    would be the dangerous direction instead, which is why this comparison is not
    shared with anything that decides what may be opened.)

    A `ValueError` from `commonpath` -- different drives, or a relative path
    mixed with an absolute one -- answers False. Two paths on different drives
    cannot contain each other, which is the answer being asked for.
    """
    left = os.path.abspath(candidate).casefold()
    right = os.path.abspath(other).casefold()
    try:
        shared = os.path.commonpath((left, right))
    except ValueError:
        return False
    return shared in (left, right)


def export_folder_setting_error(value: str) -> str | None:
    """Why a typed export folder cannot be used, or None when it can.

    The one implementation of the rules, so the Settings window's save and this
    module's resolver cannot disagree about which paths are acceptable -- the
    same reason the scheme fields validate through `compile_filename_scheme`
    rather than a second copy of the grammar.

    Three rules, in order, each a message written for a person:

    1. absolute -- a relative path would resolve against whatever the working
       directory happens to be when the export runs, which is not a folder
       anybody chose
    2. not `import/`, not inside it, and not containing it
    3. not an existing file

    A blank value is **not** an error: it is how the user asks for the default,
    and `export_folder()` reads it that way.

    Writability is deliberately not checked. `_validate_export_root` already
    accepts a root that does not exist yet as long as its nearest existing
    ancestor is a directory, and `shared/ffmpeg.py` creates the tree when it
    writes -- so a chosen folder that has not been created yet is a normal
    state, and probing it here would refuse something that works. A folder that
    goes away *after* this returns is caught by `_validate_export_root`, which
    names the path it could not use.
    """
    candidate = value.strip()
    if not candidate:
        return None

    if not os.path.isabs(candidate):
        return (
            "The export folder has to be a full path, not a relative one:\n"
            f"{candidate}"
        )

    candidate = os.path.abspath(candidate)
    import_root = environment.import_folder()
    if _overlaps(candidate, import_root):
        return (
            f"The export folder cannot be the import folder or share it:\n"
            f"  export: {candidate}\n"
            f"  import: {import_root}\n\n"
            f"commcut moves and deletes finished clips out of the import "
            f"folder, so an export root that overlaps it would write into the "
            f"one folder the importer is allowed to empty."
        )

    if os.path.lexists(candidate) and not os.path.isdir(candidate):
        return (
            "That is a file, not a folder:\n"
            f"{candidate}"
        )

    return None


def export_folder() -> str:
    """Absolute path of the folder named clips are written to.

    The one owner of that path. It was written out in three places before this
    existed -- `editor/editor.py` joined it onto `PROJECT_ROOT`, and
    `shared/ffmpeg.py` kept a private `_export_dir()` -- and a fourth copy was
    about to appear for the library walk. They happened to agree, because
    `shared/environment.py:setup_environment` returns `install_root()` as its
    `project_root`, but three spellings of one path is three places for the
    configurable version to be missed in.

    The user's choice lives in `settings.json` under `EXPORT_FOLDER_KEY`. An
    **absent** key, or one holding an empty string, is `default_export_folder()`.

    **An unusable stored value raises rather than falling back.** Settings
    refuses to save one, so this is only reachable by hand-editing a file the
    readers explicitly support editing -- and silently exporting somewhere the
    user did not choose is worse than refusing: the Importer reads the same
    folder as its library, so a bad value would have it walking the wrong tree
    too, with nothing on screen to say so. The message names the key and where
    to fix it, which is all three of the windows that call this need.

    A key present but not a string is refused rather than read as the default,
    JSON `null` included: an absent key is how the default is spelled, and a
    null is a different claim that no writer here ever makes. Reading it as
    "unset" would make the same file mean two things.

    Not cached. `settings.json` is a few hundred bytes and every caller asks
    once per window, and a cached answer would go stale the moment the user
    changed the setting in the window they are about to replace.

    The `environment` module is imported rather than its three functions bound
    individually, because all three read `install_root()` and a caller -- or a
    test -- that redirected one binding but not the others would resolve the
    default export folder against one root and the settings file against
    another, which is the half-applied answer this function exists to prevent.

    `plan_export` still takes the root as an argument: a caller that wants to
    plan a batch somewhere other than the configured one should say so. This is
    the default they get when they do not.
    """
    path = environment.settings_path()
    settings = _read_settings(path)

    if EXPORT_FOLDER_KEY not in settings:
        return default_export_folder()

    stored = settings[EXPORT_FOLDER_KEY]
    if not isinstance(stored, str):
        raise ExportSettingsError(
            f"{EXPORT_FOLDER_KEY} must be a string, not "
            f"{type(stored).__name__}.\n\n"
            f"Open Settings and choose an export folder, or delete the "
            f"{EXPORT_FOLDER_KEY} line from {path}."
        )

    error = export_folder_setting_error(stored)
    if error is not None:
        raise ExportSettingsError(
            f"{error}\n\nThe {EXPORT_FOLDER_KEY} setting in "
            f"{path} is not usable. Open Settings and choose an export "
            f"folder, or delete that line to go back to the default."
        )

    if not stored.strip():
        return default_export_folder()
    return os.path.abspath(stored.strip())


@dataclass(frozen=True)
class ExportSchemes:
    file_scheme: str
    folder_scheme: str


@dataclass(frozen=True)
class PlannedExportClip:
    segment_index: int
    start: float
    duration: float
    relative_components: tuple[str, ...]
    #: The clip's canonical tags, sorted. The record written beside the video
    #: is rendered from exactly these, so the file a user reads and the name
    #: the user sees cannot come from two different dicts.
    tags: tuple[tuple[str, str], ...]
    #: Where the clip record goes: the video's components with the last one
    #: replaced. Resolved here rather than at write time so a stem the record
    #: cannot name refuses the batch before anything encodes.
    record_relative_components: tuple[str, ...]

    @property
    def filename(self) -> str:
        return self.relative_components[-1]

    @property
    def relative_path(self) -> str:
        return "/".join(self.relative_components)

    @property
    def record_relative_path(self) -> str:
        return "/".join(self.record_relative_components)


@dataclass(frozen=True)
class ExportPlan:
    clips: tuple[PlannedExportClip, ...]
    export_root: str
    #: Clips left out of this run because a cancelled run in the same editing
    #: session already wrote them. Reported, never silently dropped.
    skipped: tuple[PlannedExportClip, ...] = ()


# ---------------------------------------------------------------------------
# Settings snapshot
# ---------------------------------------------------------------------------

def _read_settings(settings_path: str) -> dict:
    """Read settings.json into a dict, or {} when there is no file yet.

    The one reader behind both `load_export_schemes` and `export_folder`, so the
    two cannot disagree about what a file means -- and so making the export root
    configurable does not add a *third* reader to a file that already has two
    independent ones (the other is `SettingsWindow._read_settings`, which has
    its own wording because its messages are logged and shown).

    `utf-8-sig` for the same reason as that reader: a byte-order mark on a
    hand-edited settings.json is not an error the user should be told about.
    """
    try:
        with open(settings_path, encoding="utf-8-sig") as settings_file:
            settings = json.load(settings_file)
    except FileNotFoundError:
        return {}
    except RecursionError as error:
        raise ExportSettingsError("Settings nesting is too deep") from error
    except (OSError, ValueError) as error:
        raise ExportSettingsError(f"Settings could not be loaded: {error}") from error

    if not isinstance(settings, dict):
        raise ExportSettingsError("settings.json must contain a JSON object")
    return settings


def load_export_schemes(settings_path: str) -> ExportSchemes:
    """Read and validate one immutable scheme snapshot for a complete export."""
    settings = _read_settings(settings_path)

    file_scheme = settings.get(FILE_NAMING_SCHEME_KEY, DEFAULT_FILE_NAMING_SCHEME)
    folder_scheme = settings.get(FOLDER_ORGANIZATION_SCHEME_KEY, DEFAULT_FOLDER_SCHEME)
    if not isinstance(file_scheme, str):
        raise ExportSettingsError(f"{FILE_NAMING_SCHEME_KEY} must be a string")
    if not isinstance(folder_scheme, str):
        raise ExportSettingsError(f"{FOLDER_ORGANIZATION_SCHEME_KEY} must be a string")

    try:
        compile_filename_scheme(file_scheme)
        compile_folder_scheme(folder_scheme)
    except (FilenameSchemeError, FolderSchemeError) as error:
        raise ExportSettingsError(str(error)) from error
    return ExportSchemes(file_scheme=file_scheme, folder_scheme=folder_scheme)


# ---------------------------------------------------------------------------
# In-memory model snapshot
# ---------------------------------------------------------------------------

def model_with_tag_locks(
    model: SegmentModel,
    tag_locks: Mapping[str, str],
) -> SegmentModel:
    """Copy a model and materialize session locks only on wholly unedited segments."""
    segments: list[dict] = []
    for segment in model.segments:
        copied = dict(segment)
        tags = dict(segment.get("tags", {}))
        if not any(tags.values()):
            for key, value in tag_locks.items():
                if value and not tags.get(key):
                    tags[key] = value
        copied["tags"] = tags
        segments.append(copied)
    return SegmentModel(
        source=model.source,
        duration=model.duration,
        segments=segments,
    )


# ---------------------------------------------------------------------------
# Pure destination planning
# ---------------------------------------------------------------------------

def _normalized_relative_key(relative_components: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        normalized_validation_key(component)
        for component in relative_components
    )


def normalized_destination_key(relative_path: str) -> tuple[str, ...]:
    """A destination path in the key space collisions are decided in.

    Public because a second planner has to make the same decision about the same
    string: `shared/importing.py` compares an incoming clip against what the
    destination library already holds, and comparing those two with `==` would let
    `cartoon network/A Clip.mp4` and `Cartoon Network/A Clip.mp4` overwrite each
    other on a case-insensitive volume. Takes the posix text both planners render,
    so neither has to re-split it.
    """
    return _normalized_relative_key(relative_path.split("/"))


def _normalized_skip_keys(
    skip_destinations: Collection[str],
) -> set[tuple[str, ...]]:
    """Normalize the caller's destination list into the planner's key space.

    Matching goes through the same normalized keys the conflict check uses, so
    a case-variant on disk can neither miss a skip nor match the wrong file.
    """
    if isinstance(skip_destinations, (str, bytes)):
        raise TypeError("skip_destinations must be a collection of relative paths")
    keys: set[tuple[str, ...]] = set()
    for value in skip_destinations:
        if not isinstance(value, str) or not value:
            raise TypeError("skip_destinations must contain non-empty strings")
        keys.add(_normalized_relative_key(value.split("/")))
    return keys


def _canonical_tags(tags: Mapping[str, str], label: str) -> dict[str, str]:
    canonical_tags: dict[str, str] = {}
    for name, value in tags.items():
        canonical = canonical_tag_name(name)
        if canonical is None:
            raise ExportPlanError(
                f"{label} contains unknown tag {name!r}"
            )
        if not isinstance(value, str):
            raise ExportPlanError(
                f"{label} tag {canonical!r} must be a string"
            )
        canonical_tags[canonical] = value
    return canonical_tags


def missing_required_tags(tags: Mapping[str, str]) -> tuple[str, ...]:
    """Canonical required tag names that are absent or blank, sorted.

    Shared by the export preflight and the editor's front-end form check so
    both agree on exactly which tags are required and what counts as blank.
    """
    missing = []
    for name in sorted(REQUIRED_EXPORT_TAG_NAMES):
        value = tags.get(name)
        if not isinstance(value, str) or not value.strip():
            missing.append(name)
    return tuple(missing)


def _validate_required_tags(tags: Mapping[str, str], label: str) -> None:
    missing = missing_required_tags(tags)
    if missing:
        raise ExportPlanError(
            f"{label} is missing required tags: " + ", ".join(missing)
        )


#: What a destination claim came back with. Both planners need the same three
#: outcomes per clip and must not be able to confuse them.
CLAIM_CLAIMED = "claimed"
CLAIM_SKIPPED = "skipped"


@dataclass(frozen=True)
class DestinationClaim:
    """The result of asking a `DestinationIndex` for one clip's destination."""

    #: False when the destination is `conflict_with` an earlier clip.
    ok: bool
    #: True when the destination was deliberately skipped by the caller.
    skipped: bool = False
    #: Whatever `owner` the earlier clip passed, when this one collided with it.
    conflict_with: object = None


class DestinationIndex:
    """Cross-clip destination bookkeeping: the skip set, and the owner map.

    The normalized-key collision check is the one rule that has to be consistent
    across every clip in a batch *however those clips were produced* — cut from a
    compilation, or read out of somebody else's library. Two planners each keeping
    their own copy of it is how one of them ends up permitting a pair of clips
    that would overwrite each other, so it lives here once.

    The owner map stores whatever token the caller passes rather than a formatted
    name, because the two callers word the conflict differently ("Segments 1 and 3"
    against a filename) and a shared owner that had to be pre-formatted would
    force one of them to parse it back.
    """

    def __init__(self, skip_destinations: Collection[str] = ()) -> None:
        self._skip_keys = _normalized_skip_keys(skip_destinations)
        self._owners: dict[tuple[str, ...], object] = {}

    def claim(
        self,
        relative_components: Sequence[str],
        *,
        owner: object,
    ) -> DestinationClaim:
        """Claim this destination for `owner`, or report why it could not be.

        A skipped destination is *not* claimed, so a later clip with the same tags
        is free to take it — which is the resume behaviour: the clip already
        written by an earlier run is left alone and a duplicate of it is not.
        """
        key = _normalized_relative_key(relative_components)
        if key in self._skip_keys:
            return DestinationClaim(ok=True, skipped=True)
        existing = self._owners.get(key)
        if existing is not None:
            return DestinationClaim(ok=False, conflict_with=existing)
        self._owners[key] = owner
        return DestinationClaim(ok=True)

    def owns(self, relative_components: Sequence[str]) -> bool:
        """Whether some earlier clip has already claimed this destination."""
        return _normalized_relative_key(relative_components) in self._owners


@dataclass(frozen=True)
class ClipDestination:
    """Where one clip's tags put it, and the normalized tags that decided it.

    The tags come back out because both planners need them for the clip's
    `tags` field, and re-canonicalizing a second time to get them would mean two
    dicts that could disagree about what the destination was rendered from — which
    is precisely the split `docs/naming-and-organization.md` refuses to allow.
    """

    relative_components: tuple[str, ...]
    record_relative_components: tuple[str, ...]
    tags: tuple[tuple[str, str], ...]

    @property
    def relative_path(self) -> str:
        """The posix text of where the video goes, as a caller displays it."""
        return "/".join(self.relative_components)

    @property
    def record_relative_path(self) -> str:
        """The posix text of where its record goes."""
        return "/".join(self.record_relative_components)


def plan_clip_destination(
    *,
    tags: Mapping[str, str],
    folder_scheme,
    filename_scheme,
    label: str,
) -> ClipDestination:
    """Resolve one clip's tags to a destination, or raise naming this clip.

    Everything between "a clip has these tags" and "this is where it goes": tag
    canonicalization, the required-tag rule, both schemes, sanitation, and the
    UTF-8 byte ceiling on the relative path. Shared with `plan_import`, so the two
    planners cannot come to disagree about where a clip lands — which is the one
    thing an importer most has to get right, since it is putting somebody else's
    library next to this user's.

    `label` is how this clip is named in an error ("Segment 3", or an imported
    filename).
    """
    canonical = _canonical_tags(tags, label)
    _validate_required_tags(canonical, label)

    folder_components = render_folder_components(folder_scheme, canonical)
    stem = render_compiled_filename(filename_scheme, canonical)
    filename = sanitize_filename_stem(stem, OUTPUT_EXTENSION)
    record = record_filename(filename, RECORD_EXTENSION)

    relative_components = (*folder_components, filename)
    record_components = (*folder_components, record)
    if (
        len("/".join(relative_components).encode("utf-8"))
        > MAX_FOLDER_RELATIVE_PATH_BYTES
        or len("/".join(record_components).encode("utf-8"))
        > MAX_FOLDER_RELATIVE_PATH_BYTES
    ):
        raise ExportPlanError(
            f"{label} relative destination exceeds "
            f"{MAX_FOLDER_RELATIVE_PATH_BYTES} UTF-8 bytes"
        )
    return ClipDestination(
        relative_components=relative_components,
        record_relative_components=record_components,
        tags=tuple(sorted(canonical.items())),
    )


def _is_link_or_reparse_point(path: str) -> bool:
    try:
        path_stat = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        # Two different "no", and only one of them used to be caught.
        #
        # `FileNotFoundError` is the whole path being absent, which is the normal
        # case -- most of what this is asked about does not exist yet.
        #
        # `NotADirectoryError` is POSIX's answer when an *ancestor* of the path
        # is a file: `lstat(".../Cartoon Network/Promo")` where `Cartoon Network`
        # is a file gives ENOTDIR. Windows answers `FileNotFoundError` for the
        # same path, so catching only that made the refusal platform-dependent --
        # on macOS and Linux the preflight leaked a raw ENOTDIR out of
        # `plan_export` instead of naming the problem the line below already knows
        # how to name. A path that cannot be reached because something above it is
        # a file is definitionally not a symlink, so answering False is correct
        # here, and the caller's next check reports it by name.
        return False
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _validate_export_root(export_root: str) -> str:
    root = os.path.abspath(os.fspath(export_root))
    nearest_existing: str | None = None
    ancestor = root
    while True:
        if os.path.lexists(ancestor):
            if _is_link_or_reparse_point(ancestor):
                raise ExportPlanError(
                    "Export path must not traverse a symbolic link or reparse "
                    f"point: {ancestor}"
                )
            if nearest_existing is None:
                nearest_existing = ancestor
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent

    if os.path.lexists(root):
        if not os.path.isdir(root):
            raise ExportPlanError(f"Export root is not a directory: {root}")
        return root
    if nearest_existing is None or not os.path.isdir(nearest_existing):
        raise ExportPlanError(
            "Export root cannot be created below a non-directory: "
            f"{nearest_existing or root}"
        )
    return root


def _walk_existing_entries(root: str) -> dict[tuple[str, ...], set[tuple[str, ...]]]:
    existing: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    if not os.path.exists(root):
        return existing

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current_root, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        for name in [*directory_names, *file_names]:
            full_path = os.path.join(current_root, name)
            relative = os.path.relpath(full_path, root)
            components = tuple(relative.split(os.sep))
            existing.setdefault(_normalized_relative_key(components), set()).add(
                components
            )
    return existing


def _existing_destination_conflicts(
    root: str,
    plan: ExportPlan,
) -> list[str]:
    conflicts: list[str] = []
    existing = _walk_existing_entries(root)

    for clip in plan.clips:
        planned_components = clip.relative_components
        planned_key = _normalized_relative_key(planned_components)
        final_path = os.path.join(root, *planned_components)
        if os.path.lexists(final_path) or planned_key in existing:
            conflicts.append(
                f"{clip.relative_path} already exists (segment "
                f"{clip.segment_index + 1})"
            )
            continue

        for depth in range(1, len(planned_components)):
            planned_prefix = planned_components[:depth]
            actual_prefixes = existing.get(_normalized_relative_key(planned_prefix), set())
            for actual_prefix in actual_prefixes:
                if actual_prefix != planned_prefix:
                    conflicts.append(
                        f"{clip.relative_path} conflicts with case-varied directory "
                        f"{'/'.join(actual_prefix)}"
                    )
                    break
            candidate = os.path.join(root, *planned_prefix)
            if _is_link_or_reparse_point(candidate):
                conflicts.append(
                    f"{clip.relative_path} would pass through symbolic-link "
                    f"directory {'/'.join(planned_prefix)}"
                )
            elif os.path.lexists(candidate) and not os.path.isdir(candidate):
                conflicts.append(
                    f"{clip.relative_path} parent is not a directory: "
                    f"{'/'.join(planned_prefix)}"
                )

    return conflicts


def _checked_relative_components(
    components: tuple[str, ...],
    label: str,
    required_extension: str,
    errors: list[str],
) -> list[str] | None:
    """Validate one relative path, appending every problem found to `errors`.

    Returns the components as a list, or None when the path is too malformed for
    the checks that follow to say anything useful about it. One implementation
    for the video and its record is the point: two copies of the component
    policy would eventually disagree about what a safe destination is.
    """
    if not isinstance(components, tuple) or not components:
        errors.append(f"{label} has invalid relative components")
        return None
    safe_components: list[str] = []
    for component in components:
        if not isinstance(component, str) or not component:
            errors.append(f"{label} has an empty path component")
            continue
        if os.path.isabs(component) or os.path.splitdrive(component)[0]:
            errors.append(f"{label} has an absolute path component")
            continue
        try:
            sanitized = sanitize_path_component(component)
        except (TypeError, ValueError) as error:
            errors.append(f"{label} has an unsafe component: {error}")
            continue
        if sanitized != component:
            errors.append(f"{label} has an unsanitized path component")
            continue
        safe_components.append(component)
    if len(safe_components) != len(components):
        return None
    if not components[-1].endswith(required_extension):
        errors.append(f"{label} does not end in {required_extension}")
    return safe_components


def _validate_record_components(
    clip: PlannedExportClip,
    label: str,
    root: str,
    errors: list[str],
) -> None:
    """Check a clip record's destination as strictly as the video's.

    The record has to sit beside its clip: a catalog entry is a record with a
    sibling video, so a record written anywhere else is one nothing will ever
    find. That makes the shared parent part of the invariant rather than a
    convention, and `plan_export` satisfies it by construction -- this is what
    catches a hand-built plan that does not.
    """
    record_components = clip.record_relative_components
    record_label = f"{label} record"
    if _checked_relative_components(
        record_components, record_label, RECORD_EXTENSION, errors
    ) is None:
        return

    if record_components[:-1] != clip.relative_components[:-1]:
        errors.append(f"{record_label} is not beside its clip")
        return

    destination = os.path.abspath(os.path.join(root, *record_components))
    if os.path.commonpath((root, destination)) != root:
        errors.append(f"{record_label} escapes the export root")
        return
    if len("/".join(record_components).encode("utf-8")) > MAX_FOLDER_RELATIVE_PATH_BYTES:
        errors.append(
            f"{record_label} exceeds {MAX_FOLDER_RELATIVE_PATH_BYTES} UTF-8 bytes"
        )


def _validate_plan_structure(plan: ExportPlan) -> str:
    errors: list[str] = []
    root = _validate_export_root(plan.export_root)
    if not plan.clips:
        errors.append("The export plan contains no clips")

    segment_indices: set[int] = set()
    destination_owners: dict[tuple[str, ...], int] = {}
    safe_plan_entries: list[tuple[int, tuple[str, ...]]] = []
    for position, clip in enumerate(plan.clips, start=1):
        if not isinstance(clip, PlannedExportClip):
            errors.append(f"Plan entry {position} is not a PlannedExportClip")
            continue
        if (
            not isinstance(clip.segment_index, int)
            or isinstance(clip.segment_index, bool)
            or clip.segment_index < 0
        ):
            errors.append(f"Plan entry {position} has an invalid segment index")
        elif clip.segment_index in segment_indices:
            errors.append(
                f"Segment {clip.segment_index + 1} appears more than once in the plan"
            )
        else:
            segment_indices.add(clip.segment_index)

        numeric_start = _finite_float(clip.start)
        numeric_duration = _finite_float(clip.duration)
        if numeric_start is None:
            errors.append(f"Plan entry {position} has an invalid start value")
        elif numeric_start < 0:
            errors.append(f"Plan entry {position} has a negative start")
        if numeric_duration is None:
            errors.append(f"Plan entry {position} has an invalid duration value")
        elif numeric_duration <= 0:
            errors.append(f"Plan entry {position} has a non-positive duration")

        label = f"Plan entry {position}"
        components = clip.relative_components
        if _checked_relative_components(
            components, label, OUTPUT_EXTENSION, errors
        ) is None:
            continue

        destination = os.path.abspath(os.path.join(root, *components))
        if os.path.commonpath((root, destination)) != root:
            errors.append(f"Plan entry {position} escapes the export root")
            continue
        key = _normalized_relative_key(components)
        if key in destination_owners:
            errors.append(
                f"Plan entries {destination_owners[key] + 1} and {position} have the "
                "same normalized destination"
            )
        else:
            destination_owners[key] = position
        safe_plan_entries.append((position, components))
        if len("/".join(components).encode("utf-8")) > MAX_FOLDER_RELATIVE_PATH_BYTES:
            errors.append(
                f"Plan entry {position} exceeds "
                f"{MAX_FOLDER_RELATIVE_PATH_BYTES} UTF-8 bytes"
            )

        _validate_record_components(clip, label, root, errors)

    directory_owners: dict[tuple[str, ...], tuple[str, ...]] = {}
    for position, components in safe_plan_entries:
        for depth in range(1, len(components)):
            prefix = components[:depth]
            key = _normalized_relative_key(prefix)
            if key in destination_owners:
                errors.append(
                    f"Plan entry {position} uses plan entry "
                    f"{destination_owners[key]} as a parent directory"
                )
            existing_prefix = directory_owners.get(key)
            if existing_prefix is not None and existing_prefix != prefix:
                errors.append(
                    f"Plan entry {position} has a case-varied parent directory"
                )
            else:
                directory_owners[key] = prefix

    if errors:
        raise ExportPlanError("Invalid export plan:\n- " + "\n- ".join(errors))
    return root


def preflight_export_plan(plan: ExportPlan) -> None:
    """Recheck a compiled plan against the current destination filesystem."""
    if not isinstance(plan, ExportPlan):
        raise TypeError("plan must be an ExportPlan")
    root = _validate_plan_structure(plan)
    conflicts = _existing_destination_conflicts(root, plan)
    if conflicts:
        raise ExportPlanError(
            "Export preflight failed:\n- " + "\n- ".join(conflicts)
        )


def _finite_float(value: object) -> float | None:
    if not isinstance(value, Real) or isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return converted if math.isfinite(converted) else None


def validate_segment_model(model: SegmentModel) -> None:
    """Validate the segment timing/schema invariants required by named export."""
    errors: list[str] = []
    if not isinstance(model.segments, list):
        raise ExportPlanError("Segment model segments must be a list")
    duration = _finite_float(model.duration)
    if duration is None or duration < 0:
        errors.append("Segment model duration must be a finite non-negative number")

    previous_start: float | None = None
    for index, segment in enumerate(model.segments):
        label = f"Segment {index + 1}"
        if not isinstance(segment, dict):
            errors.append(f"{label} must be an object")
            continue
        numeric_start = _finite_float(segment.get("start"))
        if numeric_start is None or numeric_start < 0:
            errors.append(f"{label} start must be a finite non-negative number")
        else:
            if index == 0 and numeric_start != 0.0:
                errors.append(f"{label} must start at 0.0")
            if previous_start is not None and numeric_start <= previous_start:
                errors.append(f"{label} start must be greater than the previous start")
            previous_start = numeric_start
        if not isinstance(segment.get("ignored"), bool):
            errors.append(f"{label} ignored value must be boolean")
        if not isinstance(segment.get("tags"), dict):
            errors.append(f"{label} tags must be an object")

    if errors:
        raise ExportPlanError(
            "Invalid segment model:\n- " + "\n- ".join(errors)
        )


def validate_export_parent(
    export_root: str,
    relative_components: Sequence[str],
) -> None:
    """Reject reparse points or files in an export destination's parent path."""
    root = _validate_export_root(export_root)
    candidate = root
    for component in relative_components:
        candidate = os.path.join(candidate, component)
        if _is_link_or_reparse_point(candidate):
            raise ExportPlanError(
                "Export destination parent must not traverse a symbolic link or "
                f"reparse point: {candidate}"
            )
        if os.path.lexists(candidate) and not os.path.isdir(candidate):
            raise ExportPlanError(
                f"Export destination parent is not a directory: {candidate}"
            )
        full = os.path.abspath(candidate)
        if os.path.commonpath((root, full)) != root:
            raise ExportPlanError("Export destination parent escapes the export root")


def plan_export(
    model: SegmentModel,
    schemes: ExportSchemes,
    export_root: str,
    skip_destinations: Collection[str] = (),
) -> ExportPlan:
    """Resolve every keep-segment and preflight the complete batch.

    `skip_destinations` names planned relative paths
    (`PlannedExportClip.relative_path`) to leave out of this run, which is how a
    cancelled export resumes without re-cutting clips it already wrote. The
    skip is a *destination* decision, so it is applied only after the segment's
    tags are canonicalized and required-validated: a skipped clip with an
    incomplete record still refuses the batch, exactly as it would have.

    Skipped clips are excluded from `ExportPlan.clips`, so the preflight's
    existing-destination check never sees them -- that is the point. They are
    returned in `ExportPlan.skipped` so the caller can name them rather than
    silently dropping work.
    """
    if not isinstance(model, SegmentModel):
        raise TypeError("model must be a SegmentModel")
    if not isinstance(schemes, ExportSchemes):
        raise TypeError("schemes must be an ExportSchemes snapshot")
    index = DestinationIndex(skip_destinations)

    validate_segment_model(model)
    root = _validate_export_root(export_root)
    filename_scheme = compile_filename_scheme(schemes.file_scheme)
    folder_scheme = compile_folder_scheme(schemes.folder_scheme)

    planned: list[PlannedExportClip] = []
    skipped: list[PlannedExportClip] = []
    errors: list[str] = []

    for segment_index, segment in enumerate(model.segments):
        if segment.get("ignored"):
            continue

        label = f"Segment {segment_index + 1}"
        start = model.start(segment_index)
        duration = model.end(segment_index) - start
        if not math.isfinite(start) or not math.isfinite(duration) or duration <= 0:
            errors.append(f"{label} has an invalid duration")
            continue

        try:
            destination = plan_clip_destination(
                tags=segment.get("tags", {}),
                folder_scheme=folder_scheme,
                filename_scheme=filename_scheme,
                label=label,
            )
        except (ExportPlanError, FilenameSchemeError, FolderSchemeError, ValueError) as error:
            errors.append(str(error))
            continue

        clip = PlannedExportClip(
            segment_index=segment_index,
            start=float(start),
            duration=float(duration),
            relative_components=destination.relative_components,
            tags=destination.tags,
            record_relative_components=destination.record_relative_components,
        )
        claim = index.claim(destination.relative_components, owner=segment_index)
        if claim.skipped:
            skipped.append(clip)
            continue
        if not claim.ok:
            errors.append(
                f"Segments {claim.conflict_with + 1} and {segment_index + 1} "
                f"resolve to the same destination: "
                f"{'/'.join(destination.relative_components)}"
            )
            continue
        planned.append(clip)

    if not model.segments:
        errors.append("The segment model is empty")
    elif not planned and not errors:
        if skipped:
            errors.append(
                f"Every clip in this batch was already written by an earlier "
                f"export ({len(skipped)} skipped)"
            )
        else:
            errors.append("The segment model has no non-ignored clips")
    if errors:
        raise ExportPlanError("Export preflight failed:\n- " + "\n- ".join(errors))

    plan = ExportPlan(
        clips=tuple(planned),
        export_root=root,
        skipped=tuple(skipped),
    )
    preflight_export_plan(plan)
    return plan
