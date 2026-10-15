"""Library sharing: a zip of clips from the export library.

Applies to: `shared/sharing.py`, `sharing/sharing.py`,
`sharing/sharingwindow.ui`.

A share is the export library, or a tag-filtered part of it, as a
zip: every clip's video with its `.cnfo` record beside it, under one
root folder named after the zip. A recipient drops the zip's contents
into `import/` and imports them through the existing pipeline, which
reads the tags straight out of the records -- so a share round-trips
through the recipient's own two schemes rather than being re-rendered
here. Nothing parses a filename back into tags, ever (invariant 12),
which is why the record travels with the video.

The filter is the one place its semantics live: within one namespace
the rules are OR'd (a clip matching any value counts), across
namespaces they are AND'd (a clip has to match every namespace's
rule), and an exclusion drops a clip that matched the inclusions.
Matching happens in `value_dedup_key` space, so `cartoon network`
and `Cartoon Network` are one value -- the same identity rule the
vocabulary and the destination planner use, so a value the user types
into a filter row and a folder the library rendered agree.

This module holds no Qt and spawns no child process: the filter runs
on the GUI thread on every row change, and the zip runs on a worker
thread in the window. `zipfile` is stdlib, so the frozen-mode sweep
finds nothing to guard here.
"""

from __future__ import annotations

import itertools
import os
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterable, Sequence

from shared.importing import _format_bytes
from shared.records import RECORD_EXTENSION
from shared.paths import sanitize_path_component
from shared.scheme import canonical_tag_name
from shared.vocabulary import value_dedup_key

#: Appended to the zip's stem, before the extension, so a share is
#: recognizable as one this app wrote. Deduplicated rather than
#: stacked: a name that already carries it is left alone.
SHARE_SUFFIX = " (Created with CommCut)"

#: The share's extension.
ZIP_EXTENSION = ".zip"

#: The stem used when a typed name sanitizes to nothing.
DEFAULT_SHARE_STEM = "CommCut Share"

#: A share is written beside its final name and renamed into place, so
#: an interrupted write leaves a half-file that nothing globs as a zip.
PART_EXTENSION = ".part"

#: A safety cap on the number of entries accepted in an imported zip.
#: Eight hundred clips at two files each is 1,600 entries, so 50,000
#: is well above anything legitimate yet still catches a zip bomb.
MAX_ZIP_ENTRIES = 50000


class _Cancelled(Exception):
    """Raised internally when `should_cancel` fires mid-write."""


def _namespace_key(namespace: str) -> str:
    """The key two namespace spellings must share to be the same field.

    The namespace combo is editable, so a user can type `Network`,
    `network`, or the `type` alias and mean the same field. The
    alias table and the canonical keys are lowercase, so the
    spelling is casefolded *before* it is looked up -- otherwise
    `Type` would fall through the alias table and never reach
    `filler_type`. A clip's own keys are already canonical, so
    canonicalizing both sides is what makes the comparison a hit.
    """
    canonical = canonical_tag_name(namespace.casefold())
    return (canonical or namespace).casefold()


def _clip_tag_keys(clip) -> dict[str, str]:
    """One clip's tags in the filter's key space.

    Namespace key -> value dedup key. A clip holds one value per
    namespace, so this is a map rather than a multimap.
    """
    keys: dict[str, str] = {}
    for namespace, value in clip.tags:
        if not isinstance(value, str) or not value.strip():
            continue
        keys[_namespace_key(namespace)] = value_dedup_key(value)
    return keys


def _group_rules(rules: Iterable) -> dict[str, set[str]]:
    """Group `(namespace, value)` rules by namespace, in dedup-key space.

    Empty namespaces and values are dropped rather than refused: a row
    the user has not finished typing is not an error, it is a rule
    that matches nothing yet.
    """
    grouped: dict[str, set[str]] = {}
    for rule in rules:
        namespace, value = rule
        if not isinstance(namespace, str) or not namespace.strip():
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        grouped.setdefault(
            _namespace_key(namespace), set()).add(value_dedup_key(value))
    return grouped


def filter_clips(
    clips: Sequence,
    includes: Iterable = (),
    excludes: Iterable = (),
) -> tuple:
    """The clips a share of `clips` would hold, under `includes`/`excludes`.

    Both arguments are sequences of `(namespace, value)` pairs. Within
    one namespace the pairs are OR'd -- a clip matching any of that
    namespace's values counts -- and across namespaces they are AND'd:
    a clip has to match every namespace's rule to be included. An
    empty `includes` is every clip. A clip matching any pair in
    `excludes` is dropped, and the exclusion is applied after the
    inclusion, so an excluded value only ever removes a clip the
    inclusions would otherwise have kept.

    Returns a tuple in the input's order, so the tree the window builds
    from it is stable.
    """
    include_rules = _group_rules(includes)
    exclude_rules = _group_rules(excludes)

    def included(keys: dict[str, str]) -> bool:
        if not include_rules:
            return True
        for namespace, values in include_rules.items():
            if keys.get(namespace) not in values:
                return False
        return True

    def excluded(keys: dict[str, str]) -> bool:
        for namespace, values in exclude_rules.items():
            if keys.get(namespace) in values:
                return True
        return False

    filtered = []
    for clip in clips:
        keys = _clip_tag_keys(clip)
        if included(keys) and not excluded(keys):
            filtered.append(clip)
    return tuple(filtered)


def share_plan(clips: Sequence) -> tuple[int, int, int]:
    """What a share of `clips` would hold: folders, videos, bytes.

    Folders are the distinct folders that directly contain a clip, so a
    library two levels deep counts its leaf folders. Size is the sum of
    the videos' sizes on disk; a video that cannot be stat'd counts as
    zero rather than refusing the plan, because the plan is a preview
    of what the share dialog shows and a missing file is skipped at
    write time anyway.
    """
    folders: set[str] = set()
    size = 0
    for clip in clips:
        components = clip.relative_path.split("/")
        if len(components) > 1:
            folders.add("/".join(components[:-1]))
        try:
            size += os.path.getsize(clip.path)
        except OSError:
            continue
    return len(folders), len(clips), size


def share_zip_name(user_input: str) -> str:
    """The zip's filename for a name the user typed.

    The suffix is applied before the extension and deduplicated, so
    `My Share` and `My Share (Created with CommCut)` both name
    `My Share (Created with CommCut).zip`. A name that sanitizes to
    nothing -- empty, or only unsafe characters -- falls back to
    `DEFAULT_SHARE_STEM` rather than refusing, because the dialog's OK
    button is the moment the name is committed and a default is easier
    to rename than an error.
    """
    text = (user_input or "").strip()
    if text.casefold().endswith(ZIP_EXTENSION):
        text = text[: -len(ZIP_EXTENSION)]
    try:
        stem = sanitize_path_component(text)
    except (TypeError, ValueError):
        stem = DEFAULT_SHARE_STEM
    if not stem.endswith(SHARE_SUFFIX):
        stem = f"{stem}{SHARE_SUFFIX}"
    return f"{stem}{ZIP_EXTENSION}"


def _unique_target(target: str) -> str:
    """`target`, or `stem (1).zip` and on, when the name is taken."""
    if not os.path.lexists(target):
        return target
    directory = os.path.dirname(target)
    stem = os.path.splitext(os.path.basename(target))[0]
    for number in itertools.count(1):
        candidate = os.path.join(
            directory, f"{stem} ({number}){ZIP_EXTENSION}")
        if not os.path.lexists(candidate):
            return candidate


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def write_share_zip(
    clips: Sequence,
    target: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str | None:
    """Write a share zip of `clips` to `target`. Returns the path, or
    None when cancelled.

    The zip holds one root folder named after the zip's own stem, with
    each clip's video and its `.cnfo` record at their relative paths
    beneath it. A clip whose video is no longer on disk is skipped --
    and its record with it, since a record with no video describes
    nothing -- rather than refusing the whole share, because one
    deleted clip on a network share should not cost the other eight
    hundred.

    The name is not clobbered: a taken name becomes `stem (1).zip`,
    `stem (2).zip`, and so on. The zip is written to a `.part` beside
    its final name and renamed into place, so an interrupted write
    leaves no half-file where a share is expected. `on_progress` is
    called as `on_progress(done, total, relative_path)` once before
    the first clip and once per clip written, so a progress bar can
    take a real denominator. `should_cancel` is checked before each
    clip; a cancel removes the partial file and returns None.
    """
    final = _unique_target(target)
    os.makedirs(os.path.dirname(final) or ".", exist_ok=True)
    part = final + PART_EXTENSION
    root = os.path.splitext(os.path.basename(final))[0]

    present = [clip for clip in clips if os.path.isfile(clip.path)]
    total = len(present)
    if on_progress is not None:
        on_progress(0, total, "")

    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as archive:
            for done, clip in enumerate(present, start=1):
                if should_cancel is not None and should_cancel():
                    raise _Cancelled()
                relative = clip.relative_path.replace(os.sep, "/")
                record_relative = (
                    os.path.splitext(relative)[0] + RECORD_EXTENSION)
                archive.write(clip.path, f"{root}/{relative}")
                archive.write(clip.record_path, f"{root}/{record_relative}")
                if on_progress is not None:
                    on_progress(done, total, relative)
    except _Cancelled:
        _remove(part)
        return None
    except BaseException:
        _remove(part)
        raise

    os.replace(part, final)
    return final


class ZipImportError(Exception):
    """A zip could not be extracted into `import/`.

    Raised for the things the extractor cannot safely refuse silently: a
    path escape attempt, a symlink, a read error mid-extraction, a zip
    that is too large for the disk or too large in entries.
    """


def has_record_entries(zip_path: str) -> bool:
    """True if `zip_path` holds at least one `.cnfo` record.

    That is the gate for offering zip import at all: a drop that is not
    a library share is answered by the video route, not this one. The
    check is on the name alone -- a corrupt record still counts -- so
    it does not read file contents and stays cheap.
    """
    with zipfile.ZipFile(zip_path, "r") as archive:
        for info in archive.infolist():
            name = info.filename
            if name.lower().endswith(RECORD_EXTENSION):
                return True
    return False


def _unique_extraction_target(dest_root: str, stem: str) -> str:
    """First free `dest_root/stem` path, deduplicated like `_unique_target`.

    The same numbering that keeps two shares from clobbering each other
    keeps two zips with the same root folder apart.
    """
    candidate = os.path.join(dest_root, stem)
    if not os.path.lexists(candidate):
        return candidate
    for number in itertools.count(1):
        candidate = os.path.join(dest_root, f"{stem} ({number})")
        if not os.path.lexists(candidate):
            return candidate


def _check_safe_member(info: zipfile.ZipInfo, dest_root: str) -> str:
    """The extraction path for `info`, or raise `ZipImportError`.

    Defends against zip-slip -- `../escape` -- by refusing any member
    whose path contains a `..` component, and by resolving the final
    path under `dest_root` and refusing anything that climbs above it.
    Each component is normalized individually with `sanitize_path_component`
    so the rendered result is a safe, flat string.
    """
    raw_parts = info.filename.replace("\\", "/").split("/")
    for part in raw_parts:
        if part == "..":
            raise ZipImportError(
                f"A path inside the zip escapes the destination: {info.filename}")
    parts = [
        part for part in raw_parts
        if part and part != "."
    ]
    if not parts:
        return ""
    components = []
    for part in parts[:-1]:
        safe = sanitize_path_component(part)
        if not safe:
            safe = "-"
        components.append(safe)
    components.append(sanitize_path_component(parts[-1]))
    rel = os.path.join(*components)
    target = os.path.join(dest_root, rel)
    resolved = os.path.normpath(os.path.abspath(target))
    try:
        if os.path.commonpath([resolved, dest_root]) != os.path.abspath(dest_root):
            raise ZipImportError(
                f"A path inside the zip escapes the destination: {info.filename}")
    except ValueError:
        raise ZipImportError(
            f"A path inside the zip escapes the destination: {info.filename}")
    return rel


def _check_free_space(required: int, dest_root: str) -> None:
    """Raise `ZipImportError` if `required` bytes won't fit in `dest_root`."""
    try:
        usage = shutil.disk_usage(dest_root)
    except OSError:
        return
    if usage.free < required:
        raise ZipImportError(
            f"The zip needs {_format_bytes(required)} but only "
            f"{_format_bytes(usage.free)} is free in the import folder.")


def _remove_extraction_staging(staging_root: str) -> None:
    """Tidy up a `.part` extraction directory after a failure.

    File by file, then `os.rmdir` only for directories: never `rmtree`,
    matching the import's safety boundary.
    """
    for current_root, dirs, files in os.walk(staging_root, topdown=False):
        for name in files:
            path = os.path.join(current_root, name)
            try:
                os.remove(path)
            except OSError:
                pass
        for name in dirs:
            path = os.path.join(current_root, name)
            try:
                os.rmdir(path)
            except OSError:
                pass
    try:
        os.rmdir(staging_root)
    except OSError:
        pass


def extract_share_zip(
    zip_path: str,
    dest_root: str,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """Extract a share zip into `dest_root`, returning its root folder.

    The zip's own root folder is preserved under `dest_root` -- the one
    folder named after the zip -- so the contents land at
    `dest_root/<root>/...` exactly as `write_share_zip` wrote them, and
    `find_videos` sees them with their records beside them.

    Safety:

    - `ZipImportError` is raised on a path escape, a symlink, or a zip
      that exceeds `MAX_ZIP_ENTRIES` entries.
    - Free space is checked against the compressed-to-be-uncompressed
      total before any file is written.
    - A cancellation or a mid-write error leaves `dest_root` empty of
      the staging folder; partial directories are removed file by file,
      then `os.rmdir` only.

    `on_progress` is called as `on_progress(done, total)` once before
    the first member and once per member, so a progress bar can take a
    real denominator. `should_cancel` is checked before each member.
    """
    if on_progress is None:
        on_progress = lambda _done, _total: None
    if should_cancel is None:
        should_cancel = lambda: False

    os.makedirs(dest_root, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as archive:
        infos = archive.infolist()
        total = len(infos)
        if total > MAX_ZIP_ENTRIES:
            raise ZipImportError(
                f"The zip holds {total} entries, which exceeds the "
                f"{MAX_ZIP_ENTRIES} limit.")

        required = 0
        for info in infos:
            required += info.file_size

        _check_free_space(required, dest_root)

        root_name = os.path.splitext(os.path.basename(zip_path))[0]
        extract_dir = _unique_extraction_target(dest_root, root_name)
        staging = extract_dir + PART_EXTENSION
        os.makedirs(staging, exist_ok=True)

        on_progress(0, total)
        done = 0
        try:
            for info in infos:
                if should_cancel():
                    _remove_extraction_staging(staging)
                    return None
                name = info.filename.replace("\\", "/")
                if name.endswith("/"):
                    continue

                unix_mode = info.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise ZipImportError(
                        f"A symlink inside the zip is not safe to extract: {name}")

                rel = _check_safe_member(info, staging)
                if not rel:
                    continue
                target = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(target) or staging, exist_ok=True)

                with archive.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                done += 1
                on_progress(done, total)

            if should_cancel():
                _remove_extraction_staging(staging)
                return None
        except BaseException:
            _remove_extraction_staging(staging)
            raise

    os.rename(staging, extract_dir)
    return extract_dir
