"""Named-export settings, planning, and preflight policy for commcut."""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real

from shared.naming import (
    DEFAULT_FILE_NAMING_SCHEME,
    FilenameSchemeError,
    compile_filename_scheme,
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
from shared.segments import SegmentModel
from shared.scheme import canonical_tag_name

FILE_NAMING_SCHEME_KEY = "file_naming_scheme"
FOLDER_ORGANIZATION_SCHEME_KEY = "folder_organization_scheme"
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

    @property
    def filename(self) -> str:
        return self.relative_components[-1]

    @property
    def relative_path(self) -> str:
        return "/".join(self.relative_components)


@dataclass(frozen=True)
class ExportPlan:
    clips: tuple[PlannedExportClip, ...]
    export_root: str


# ---------------------------------------------------------------------------
# Settings snapshot
# ---------------------------------------------------------------------------

def load_export_schemes(settings_path: str) -> ExportSchemes:
    """Read and validate one immutable scheme snapshot for a complete export."""
    try:
        # utf-8-sig for the same reason as SettingsWindow._read_settings: a
        # byte-order mark on a hand-edited settings.json is not an error the
        # user should be told about, and both readers have to agree.
        with open(settings_path, encoding="utf-8-sig") as settings_file:
            settings = json.load(settings_file)
    except FileNotFoundError:
        settings = {}
    except RecursionError as error:
        raise ExportSettingsError("Settings nesting is too deep") from error
    except (OSError, ValueError) as error:
        raise ExportSettingsError(f"Settings could not be loaded: {error}") from error

    if not isinstance(settings, dict):
        raise ExportSettingsError("settings.json must contain a JSON object")

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


def _canonical_tags(tags: Mapping[str, str], segment_index: int) -> dict[str, str]:
    canonical_tags: dict[str, str] = {}
    for name, value in tags.items():
        canonical = canonical_tag_name(name)
        if canonical is None:
            raise ExportPlanError(
                f"Segment {segment_index + 1} contains unknown tag {name!r}"
            )
        if not isinstance(value, str):
            raise ExportPlanError(
                f"Segment {segment_index + 1} tag {canonical!r} must be a string"
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


def _validate_required_tags(tags: Mapping[str, str], segment_index: int) -> None:
    missing = missing_required_tags(tags)
    if missing:
        raise ExportPlanError(
            f"Segment {segment_index + 1} is missing required tags: "
            + ", ".join(missing)
        )


def _is_link_or_reparse_point(path: str) -> bool:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
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

        components = clip.relative_components
        if not isinstance(components, tuple) or not components:
            errors.append(f"Plan entry {position} has invalid relative components")
            continue
        safe_components: list[str] = []
        for component in components:
            if not isinstance(component, str) or not component:
                errors.append(f"Plan entry {position} has an empty path component")
                continue
            if os.path.isabs(component) or os.path.splitdrive(component)[0]:
                errors.append(
                    f"Plan entry {position} has an absolute path component"
                )
                continue
            try:
                sanitized = sanitize_path_component(component)
            except (TypeError, ValueError) as error:
                errors.append(f"Plan entry {position} has an unsafe component: {error}")
                continue
            if sanitized != component:
                errors.append(
                    f"Plan entry {position} has an unsanitized path component"
                )
                continue
            safe_components.append(component)
        if len(safe_components) != len(components):
            continue
        if not components[-1].endswith(OUTPUT_EXTENSION):
            errors.append(
                f"Plan entry {position} does not end in {OUTPUT_EXTENSION}"
            )

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
) -> ExportPlan:
    """Resolve every keep-segment and preflight the complete batch."""
    if not isinstance(model, SegmentModel):
        raise TypeError("model must be a SegmentModel")
    if not isinstance(schemes, ExportSchemes):
        raise TypeError("schemes must be an ExportSchemes snapshot")

    validate_segment_model(model)
    root = _validate_export_root(export_root)
    filename_scheme = compile_filename_scheme(schemes.file_scheme)
    folder_scheme = compile_folder_scheme(schemes.folder_scheme)

    planned: list[PlannedExportClip] = []
    errors: list[str] = []
    owners: dict[tuple[str, ...], int] = {}

    for segment_index, segment in enumerate(model.segments):
        if segment.get("ignored"):
            continue

        start = model.start(segment_index)
        duration = model.end(segment_index) - start
        if not math.isfinite(start) or not math.isfinite(duration) or duration <= 0:
            errors.append(
                f"Segment {segment_index + 1} has an invalid duration"
            )
            continue

        try:
            tags = _canonical_tags(segment.get("tags", {}), segment_index)
            _validate_required_tags(tags, segment_index)
            folder_components = render_folder_components(folder_scheme, tags)
            stem = render_compiled_filename(filename_scheme, tags)
            filename = sanitize_filename_stem(stem, OUTPUT_EXTENSION)
        except (ExportPlanError, FilenameSchemeError, FolderSchemeError, ValueError) as error:
            errors.append(str(error))
            continue

        relative_components = (*folder_components, filename)
        relative_text = "/".join(relative_components)
        if len(relative_text.encode("utf-8")) > MAX_FOLDER_RELATIVE_PATH_BYTES:
            errors.append(
                f"Segment {segment_index + 1} relative destination exceeds "
                f"{MAX_FOLDER_RELATIVE_PATH_BYTES} UTF-8 bytes"
            )
            continue
        key = _normalized_relative_key(relative_components)
        if key in owners:
            errors.append(
                f"Segments {owners[key] + 1} and {segment_index + 1} resolve to the "
                f"same destination: {'/'.join(relative_components)}"
            )
            continue
        owners[key] = segment_index
        planned.append(
            PlannedExportClip(
                segment_index=segment_index,
                start=float(start),
                duration=float(duration),
                relative_components=relative_components,
            )
        )

    if not model.segments:
        errors.append("The segment model is empty")
    elif not planned and not errors:
        errors.append("The segment model has no non-ignored clips")
    if errors:
        raise ExportPlanError("Export preflight failed:\n- " + "\n- ".join(errors))

    plan = ExportPlan(clips=tuple(planned), export_root=root)
    preflight_export_plan(plan)
    return plan
