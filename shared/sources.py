"""Which videos this app can open, and what it insists about them.

A source video is named in two ways: the main menu's **Editor** button asks for
one with a native file dialog, and one can be dropped on the main menu itself.
Neither is where the source lives — there is no folder it has to be in, which is
what freed ``import/`` to be the Library Importer's staging folder instead of a
source-video drop.

What survives that is validation rather than containment.
:func:`validate_source_video` is the one supported way to turn a selection into a
source path, and the menu, the scanner and the editor all call it, so none of them
can disagree about what may be opened — including which of two entry points does.
It checks that the path is a real video file and — the rule that only became
reachable once sources could come from anywhere — that its folder can be written,
because the ``.cmct`` sidecar is written *beside* the video and the editor rewrites
it on every Stage.

:func:`is_video_file` is the one definition of "a video this app can open". The
file dialog builds its filter from the same list, so a container added to one
cannot be missing from the other.

``import/`` still exists and still resolves through :func:`import_folder`, because
the importer will read it; nothing here reads it any more.

The sidecar rule comes from :mod:`shared.segments`, which knows the ``.cmct``
format. The dependency runs one way only: this module answers *which file*,
segments answers *what is in it*, and segments never imports this module.
"""

import os

from shared.environment import install_root


#: Folder under the install root that the Library Importer reads from. Not a
#: source-video folder: a compilation can be picked from anywhere.
IMPORT_FOLDER_NAME = "import"

#: Extensions offered in the file dialog and accepted by `is_video_file`.
#: Deliberately generous: ffmpeg reads all of these, and an alpha tester should
#: not have to care which container their compilation rip happens to be in.
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2v", ".m2ts", ".ts", ".vob", ".ogv", ".3gp",
    ".divx", ".asf", ".rm", ".rmvb", ".mxf", ".f4v",
})


def import_folder():
    """Absolute path of the folder the Library Importer reads from.

    Not a source-video folder any more. A compilation can be picked from
    anywhere, which is what freed this one for importing finished clips.
    """
    return os.path.join(install_root(), IMPORT_FOLDER_NAME)


def is_video_file(path):
    """True if `path` looks like a video file this app is willing to open."""
    return (os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS)


def _writability_problem(folder):
    """None when a file can be created in `folder`, else why one cannot.

    A real probe rather than `os.access(folder, os.W_OK)`. That call is advisory
    and routinely disagrees with what actually happens: on POSIX it succeeds for
    root whatever the mode bits say, and on Windows it is a coarse ACL guess that
    a full disk or a share mounted read-only will happily pass. Creating and
    removing a file is the only test that cannot be wrong in the direction that
    matters, which is the direction where the check exists.

    The name is dot-prefixed so it is hidden in a POSIX listing and unlikely to
    collide with anything, and it is removed on both paths -- a process that dies
    between the two leaves one empty file, which is the right size of mess for a
    check.
    """
    probe = os.path.join(folder, f".commcut-write-test-{os.getpid()}")
    try:
        with open(probe, "wb"):
            pass
    except OSError as error:
        return str(error)
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return None


def validate_source_video(path):
    """Absolute path of a source video, or raise naming what is wrong with it.

    The one supported way to turn a selection into a source path, called from the
    scanner and the editor so the two cannot disagree about what may be opened.

    Four rules, each refused with a message written for a person:

    1. not empty
    2. exists, and is a file rather than a folder
    3. has a video extension -- the file dialog's filter is a convenience, not
       this rule, because "All files" is one click away
    4. its folder is writable, because the `.cmct` sidecar is written *beside*
       the video

    Rule 4 is the one that is not about the file. `shared/segments.py:sidecar_path`
    puts the sidecar next to the source, and the editor rewrites it on every
    Stage and again before export, so the folder has to be writable for the whole
    session -- not merely at the moment of scanning. Without the check a
    read-only source folder fails three different ways depending on when it is
    reached: a bare `PermissionError` out of the scanner's Finished handler, the
    same out of the editor's Stage with the user's tags unsaved, and "The editor
    window could not start" if the placeholder is the first write. None of them
    named the folder.

    This is a pre-flight and not a guarantee -- a folder can become read-only
    mid-session, and a network share can go away -- so the editor's own write
    failure still has to report clearly. That is the backstop; this is the
    refusal that arrives before any work is done.
    """
    if not path:
        raise ValueError("No source video was chosen.")

    resolved = os.path.abspath(os.fspath(path))
    if not os.path.exists(resolved):
        raise FileNotFoundError(f"That file is no longer there:\n{resolved}")
    if not os.path.isfile(resolved):
        raise ValueError(
            f"That is a folder, not a video:\n{resolved}\n\n"
            f"Choose a video file inside it."
        )
    if not is_video_file(resolved):
        raise ValueError(
            f"{os.path.basename(resolved)} is not a video this app can open.\n\n"
            f"Supported: {', '.join(sorted(VIDEO_EXTENSIONS))}"
        )

    folder = os.path.dirname(resolved)
    problem = _writability_problem(folder)
    if problem is not None:
        raise ValueError(
            f"commcut has to write a .cmct file beside the video, and it "
            f"cannot write in:\n{folder}\n\n{problem}\n\n"
            f"Choose a video in a folder you can write to."
        )
    return resolved
