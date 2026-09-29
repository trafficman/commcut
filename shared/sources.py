"""Which source video this install works on, and what else is available.

This is an alpha: the app deliberately does not let anyone point it at an
arbitrary path. The only videos that can be opened are the ones sitting in the
app's own ``import/`` folder, which the user fills by hand. The main menu's
**Editor** button opens the picker window, which lists this folder and hands the
chosen file to the scanner as an argument.

Two rules make that more than a convention:

* :func:`resolve_import_video` refuses anything that is not a video file inside
  ``import/``. It is the only supported way to turn an argument into a source
  path, and it is called from the scanner and the editor, so a path that did not
  come from the picker cannot open a file the user was never offered.
* :func:`list_source_videos` is the one definition of "what is available", used
  by the picker and by the tests.

``import/`` ships with a ``README.txt`` placeholder, which is why listing
filters on the extension rather than on "everything in the folder".

The sidecar rule comes from :mod:`shared.segments`, which knows the ``.cmct``
format. The dependency runs one way only: this module answers *which file*,
segments answers *what is in it*, and segments never imports this module.
"""

import os
from dataclasses import dataclass

from shared.environment import install_root
from shared.segments import sidecar_path


#: Folder under the install root that source videos are dropped into.
IMPORT_FOLDER_NAME = "import"

#: Filename used when no source was chosen. Kept so a source run of
#: ``python scanner/scanner.py``, and any build predating the picker, still has
#: something to open. The picker always supplies an explicit path.
DEFAULT_SOURCE_NAME = "test.mp4"

#: Extensions the picker offers. Deliberately generous: ffmpeg reads all of
#: these, and an alpha tester should not have to care which container their
#: compilation rip happens to be in.
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2v", ".m2ts", ".ts", ".vob", ".ogv", ".3gp",
    ".divx", ".asf", ".rm", ".rmvb", ".mxf", ".f4v",
})


@dataclass(frozen=True)
class SourceVideo:
    """One video available to work on, as offered by the picker."""

    path: str
    name: str
    size_bytes: int
    #: True when a .cmct sidecar already exists, meaning the video has been
    #: scanned before. The scanner hands those straight to the editor.
    has_sidecar: bool


def import_folder():
    """Absolute path of the folder source videos are picked from."""
    return os.path.join(install_root(), IMPORT_FOLDER_NAME)


def is_video_file(path):
    """True if `path` looks like a video file this app is willing to open."""
    return (os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS)


def list_source_videos(folder=None):
    """Every video in the import folder, sorted by name (case-insensitively).

    A missing folder is an empty list rather than an error: the install root
    may be read-only or the folder may simply not exist yet, and an empty
    picker is the right answer in both cases.
    """
    folder = folder if folder is not None else import_folder()
    try:
        names = sorted(os.listdir(folder), key=str.casefold)
    except OSError:
        return []

    videos = []
    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or not is_video_file(path):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        videos.append(SourceVideo(
            path=path,
            name=name,
            size_bytes=size,
            has_sidecar=os.path.isfile(sidecar_path(path)),
        ))
    return videos


def _volume_is_case_insensitive(folder):
    """True if this filesystem treats differently-cased spellings as one file.

    Asked of the filesystem rather than assumed from the platform: macOS APFS
    and Linux volumes can each be either, and only the filesystem knows which.

    The probe is a full-path casefold compared back against the real path. On
    a case-insensitive volume the two are one file and `samefile` says so. On
    a case-sensitive volume they are different paths -- and if any parent
    directory happens to be genuinely mixed-case, one of them does not exist at
    all and `samefile` raises, which is also the safe answer.
    """
    try:
        return os.path.samefile(folder, folder.casefold())
    except OSError:
        return False


def _is_inside(folder, candidate):
    """True if `candidate` is strictly inside `folder`, symlinks resolved.

    Real paths are compared so a symlink sitting in import/ cannot be used to
    reach a file outside it.

    Two of the checks here are string comparisons and so are case-sensitive,
    while the volume usually is not: NTFS and APFS are case-insensitive by
    default. The same argument would then be accepted on Windows and refused on
    a Mac, for a file the OS would plainly open -- and realpath() cannot paper
    over the difference, because it can only report the filesystem's own casing
    for a path that already exists. So the volume is asked directly, and the
    case-folded retry is gated on its answer.

    The retry is what makes a differently-cased spelling usable. It is also the
    direction that could turn a consistency fix into a traversal hole, so it
    happens only when the filesystem itself says those spellings are one path.
    """
    folder_real = os.path.realpath(folder)
    candidate_real = os.path.realpath(candidate)

    if candidate_real == folder_real:
        return False

    case_insensitive = _volume_is_case_insensitive(folder_real)

    # "Strictly inside" has to survive the fold too: on a case-insensitive
    # volume a differently-cased spelling of the folder is the folder.
    if case_insensitive and candidate_real.casefold() == folder_real.casefold():
        return False

    if _contained_in(folder_real, candidate_real):
        return True

    if not case_insensitive:
        return False

    return _contained_in(folder_real.casefold(), candidate_real.casefold())


def _contained_in(folder_real, candidate_real):
    """The raw containment test, on already-realpath'd, already-normalised paths."""
    try:
        return os.path.commonpath([folder_real, candidate_real]) == folder_real
    except ValueError:
        return False


def resolve_import_video(value=None, folder=None):
    """Validate `value` against the import folder's policy.

    Returns its absolute path. Raises ValueError when the path is empty of
    meaning, outside the folder, or not a video extension -- each with a message
    naming the folder, because this is the check a hand-edited command line
    runs into. Whether the file is still *there* is not decided here; see
    :func:`require_source_video`, which owns that message.

    A relative `value` is resolved against the process's working directory,
    which is what any ordinary path argument means.
    """
    folder = folder if folder is not None else import_folder()
    if not value:
        # No selection: the legacy default. It is validated the same way, so
        # the two paths cannot diverge in what they accept.
        return os.path.join(os.path.abspath(folder), DEFAULT_SOURCE_NAME)

    path = os.path.abspath(value)
    if not _is_inside(folder, path):
        raise ValueError(
            f"{path} is not inside the import folder:\n{folder}\n\n"
            f"Put the video there and pick it from the list."
        )
    if not is_video_file(path):
        raise ValueError(
            f"{os.path.basename(path)} is not a video this app can open.\n\n"
            f"Supported: {', '.join(sorted(VIDEO_EXTENSIONS))}"
        )
    return path


def require_source_video(value=None, folder=None):
    """Return a validated source path, or raise naming what to do about it.

    Without this check a missing source video fails in a way that looks like a
    codec problem: ffprobe returns nothing, a placeholder .cmct is written
    with duration 0.0, and mpv then reports an opaque load failure. An empty
    import/ folder is the expected first-run state of a packaged build, so this
    message is the first thing a tester may well see.
    """
    folder = folder if folder is not None else import_folder()
    path = resolve_import_video(value, folder=folder)
    if os.path.isfile(path):
        return path

    # Two failures worth telling apart: the selection has gone stale (the file
    # was deleted after the picker listed it), or there is nothing to open.
    available = list_source_videos(folder)
    if available:
        listed = "\n".join(f"  - {video.name}" for video in available)
        raise FileNotFoundError(
            f"That video is no longer in the import folder:\n{path}\n\n"
            f"Videos currently in {folder}:\n{listed}"
        )
    raise FileNotFoundError(
        f"No source video found.\n\nPut a compilation video in:\n{folder}\n\n"
        f"then pick it from the list when you press Editor in the main menu. "
        f"The scanner reads that file and writes its .cmct sidecar next to it."
    )
