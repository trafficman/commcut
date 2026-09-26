"""Shared environment setup for the project's entry-point scripts.

Scripts may live at the project root (main.py, mainwindow.py) or in a
subdirectory (editor/, scanner/, settings/). Either way they need the same two
things before anything else:

  1. The project root on sys.path so `shared.*` imports resolve when the
     script is run directly (e.g. `python editor/editor.py`).
  2. The bundled binaries folder discoverable so mpv, ffprobe, and ffmpeg
     resolve to the versions under bin/<os>/ rather than whatever happens to
     be on the system.

The bin folder is selected per-OS via ``get_binary_path`` / ``_bin_dir``
(the cross-platform binary resolution formerly living in ``core.py``).

Two roots, and the difference matters once the app is frozen
------------------------------------------------------------------------------

Running from source there is only one root and the distinction is academic.
Frozen there are two, and conflating them is the single most common way a
PyInstaller build of this app breaks:

  ``resource_root()``  read-only data bundled inside the executable's payload
                       (the four .ui files). Resolves to ``sys._MEIPASS``.
  ``install_root()``   writable user data that must survive a restart
                       (settings.json, import/, export/, temp/) plus the
                       bundled bin/<os>/ folder. Resolves to the folder holding
                       the executable, never the extraction folder -- the
                       latter is wiped when the process exits.

The PyInstaller spec pins ``contents_directory="."`` so that, in the shipped
onedir layout, ``resource_root() == install_root()`` and the two collapse into
a single path. They are still separate functions because that is a property of
the build, not of the code: a onefile build would put ``resource_root()`` in a
temporary directory and ``install_root()`` beside the .exe.

Windows DLL loading
-----------------------------------------------------------------------------

``ctypes.util.find_library`` -- which python-mpv calls at *import* time to
locate libmpv -- scans ``%PATH%`` for each candidate name and returns the
absolute path of the first hit. It then loads that absolute path with
``LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR``. So prepending ``bin/<os>`` to ``%PATH%``
is the load-bearing step, and it has to happen *before* ``import mpv`` runs.
python-mpv's import is deferred inside ``create_mpv_player`` precisely so that
this ordering can be guaranteed.

``os.add_dll_directory`` is also registered for good measure: it is the
modern mechanism and it keeps working if the loader flags python-mpv uses ever
change. The returned handle is stored in a module global, because
``add_dll_directory`` unregisters the directory as soon as its handle is
garbage collected -- a dropped handle shows up as a bare ``OSError`` much later,
at player construction, with nothing pointing at the cause.
"""

import os
import platform
import sys


# Presence of this file inside a `shared/` folder is what identifies a folder
# as the project root, so a script that already lives there (main.py) is not
# treated as if it were one level below the root.
_SHARED_MARKER = os.path.join('shared', 'environment.py')

# Per-OS bundled-binary subfolder. Only Windows is populated; the Linux and
# macOS folders ship empty, so get_binary_path() raises there rather than
# silently resolving to a system-installed ffmpeg.
_OS_BIN_FOLDERS = {
    'windows': 'win',
    'linux': 'linux',
    'darwin': 'mac',  # macOS
}

# mpv's video output driver. 'direct3d' is the Windows embedded-rendering path
# and is the reason the WA_NativeWindow attribute is set on the video frame;
# the other platforms use mpv's generic GPU output. Kept here rather than
# hardcoded at the call site so the next port does not have to rediscover it.
MPV_VIDEO_OUTPUT = {
    'windows': 'direct3d',
    'linux': 'gpu',
    'darwin': 'gpu',
}

# Handles returned by os.add_dll_directory(). Module-level so they outlive the
# call that created them -- see the module docstring.
_dll_directory_handles = []

# The child windows this app can open, and the script that implements each one
# when running from source. When frozen, the scripts do not exist on disk, so
# the same binary is re-executed with '--window <name>' instead; see
# main.py's dispatcher and launch_command() below.
_WINDOW_SCRIPTS = {
    'scanner': os.path.join('scanner', 'scanner.py'),
    'editor': os.path.join('editor', 'editor.py'),
    'settings': os.path.join('settings', 'settings.py'),
}

#: The child-window names launch_command() accepts, sorted for stable display.
WINDOW_NAMES = tuple(sorted(_WINDOW_SCRIPTS))


def is_frozen():
    """True when running from a PyInstaller-built executable."""
    return bool(getattr(sys, 'frozen', False))


def _os_key():
    return platform.system().lower()


def _source_root():
    """The project root as it exists in the source tree.

    Walks up from this file to the folder containing shared/environment.py, so
    it is correct no matter which entry point imported us. Not meaningful when
    frozen, where __file__ points into the extraction directory.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_root():
    """Read-only root for data bundled inside the executable payload.

    This is where the four .ui files live once frozen. Resolve read-only
    resources through resource_path(), never through install_root(): the two
    point at the same folder in the shipped onedir build but not in general.
    """
    if is_frozen():
        return os.path.abspath(getattr(sys, '_MEIPASS', _source_root()))
    return _source_root()


def install_root():
    """Writable root for user data and for the bundled bin/<os> folder.

    Holds settings.json, import/, export/, and temp/. When frozen this is the
    folder containing the executable -- explicitly *not* the extraction
    directory, which is deleted when the process exits, and not necessarily
    writable (a build dropped in Program Files would fail every write).
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return _source_root()


def resource_path(*parts):
    """Absolute path to a bundled read-only resource.

    Pass the resource's path *relative to the project root*, subfolders
    included: resource_path("settings", "settingswindow.ui"). The PyInstaller
    spec mirrors the source tree's subfolder layout into the payload so that
    one expression is correct both unfrozen and frozen -- flattening the four
    .ui files into the payload root would make resource_path() right only in a
    packaged build and wrong when running from source.
    """
    return os.path.join(resource_root(), *parts)


def _bin_dir():
    """Return the absolute path to the bundled binaries folder for this OS.

    bin/win on Windows, bin/linux on Linux, bin/mac on macOS. When frozen this
    is resolved against install_root() rather than against __file__, so the
    binaries sit beside the executable in the shipped onedir layout instead of
    inside a temporary extraction directory.
    """
    folder = _OS_BIN_FOLDERS.get(_os_key())
    if folder is None:
        raise EnvironmentError(
            f"Unsupported operating system: {platform.system()}")

    return os.path.join(install_root(), 'bin', folder)


def bin_dir():
    """Public accessor for the bundled binaries folder, whether or not it exists."""
    return _bin_dir()


def get_binary_path(binary_name):
    """Dynamically resolve the path to a bundled binary based on OS.

    Looks under ``bin/<os>/`` for the named binary, appending the platform's
    executable extension (``.exe`` on Windows). Raises ``FileNotFoundError``
    if the binary is not present at the expected location.

    Every ffmpeg/ffprobe call site should use this rather than a bare binary
    name. A bare name resolves through the mutated ``%PATH%`` and silently
    picks up whatever ffmpeg the machine happens to have if the prepend ever
    fails -- which is a very confusing bug to chase.
    """
    ext = '.exe' if _os_key() == 'windows' else ''
    binary_path = os.path.join(_bin_dir(), f"{binary_name}{ext}")

    if not os.path.exists(binary_path):
        raise FileNotFoundError(
            f"Could not find {binary_name}{ext} in the bundled binaries "
            f"folder: {binary_path}. It is shipped for Windows only in this "
            f"build; other platforms are not supported yet."
        )

    return binary_path


def video_output():
    """The mpv video output driver to use on this platform."""
    key = _os_key()
    if key not in MPV_VIDEO_OUTPUT:
        raise EnvironmentError(
            f"Unsupported operating system: {platform.system()}")
    return MPV_VIDEO_OUTPUT[key]


def launch_command(window_name):
    """Return the argv that opens `window_name` in its own process.

    Each window is a separate process by design, not by accident: constructing
    an mpv player (direct3d) while another top-level window is foreground
    deadlocks on Windows, so the main menu must not open the scanner in-process.

    From source that is the interpreter plus the window's script. Frozen, the
    scripts do not exist on disk and ``sys.executable`` is the application
    binary itself, so the same binary is re-executed with a ``--window`` flag
    that main.py dispatches on.

    Raises ValueError for an unknown window, FileNotFoundError when running
    from source and the script is missing.
    """
    try:
        relative = _WINDOW_SCRIPTS[window_name]
    except KeyError:
        raise ValueError(
            f"Unknown window {window_name!r}; expected one of "
            f"{', '.join(WINDOW_NAMES)}"
        ) from None

    if is_frozen():
        return [sys.executable, '--window', window_name]

    script = os.path.join(install_root(), relative)
    if not os.path.exists(script):
        raise FileNotFoundError(
            f"Could not find {window_name} script at {script}")
    return [sys.executable, script]


#: Writable folders the app expects to exist next to the executable.
#:
#: export/ and temp/ are created lazily by shared.ffmpeg when something is
#: actually written, so they only need to exist for discoverability. import/
#: is never created by anything else, and an installed build has no source
#: tree to inherit it from -- the user drops a source video in by hand.
APP_FOLDERS = ('import', 'export', 'temp')


def ensure_app_folders():
    """Create the app's writable folders under install_root().

    Returns the list of folders that now exist. Raises OSError if the install
    root is not writable, which is a legitimate thing to hit (a build dropped
    into Program Files) and is worth reporting rather than swallowing: every
    later save, scan, and export would fail the same way with a much less
    obvious message.
    """
    root = install_root()
    for name in APP_FOLDERS:
        os.makedirs(os.path.join(root, name), exist_ok=True)
    return [os.path.join(root, name) for name in APP_FOLDERS]


def _register_bin_dir():
    """Make the bundled binaries discoverable. Returns the folder, or None.

    Two mechanisms, both needed for different reasons. The %PATH% prepend is
    what ctypes.util.find_library -- and therefore python-mpv's import-time DLL
    lookup -- actually searches, so it is load-bearing. os.add_dll_directory
    registers the folder the modern way and keeps working regardless of the
    loader flags python-mpv passes to CDLL.
    """
    bin_dir = _bin_dir()
    if not os.path.isdir(bin_dir):
        return None

    if _os_key() == 'windows' and hasattr(os, 'add_dll_directory'):
        try:
            handle = os.add_dll_directory(bin_dir)
        except OSError:
            handle = None
        if handle is not None:
            # Held at module scope on purpose. Losing the handle unregisters
            # the directory, and the failure surfaces much later as a bare
            # OSError from ctypes when the player is constructed.
            _dll_directory_handles.append(handle)

    # ctypes.util.find_library subscripts os.environ['PATH'] directly, so PATH
    # has to exist, not merely be preferred.
    current = os.environ.get('PATH', '')
    entries = current.split(os.pathsep) if current else []
    if bin_dir not in entries:
        os.environ['PATH'] = (
            bin_dir + os.pathsep + current if current else bin_dir)

    return bin_dir


def setup_environment(script_path):
    """Prepare sys.path and the binaries folder for an entry-point script.

    Call at the very top of the script (before any imports of `shared` or
    mpv):

        from shared.environment import setup_environment
        SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

    Returns (script_dir, project_root) so the caller can build paths relative
    to the script or to the install root. `project_root` is install_root():
    the source tree when unfrozen, the executable's folder when frozen.

    `script_dir` is the script's own folder, which is only meaningful when
    running from source -- when frozen it points into the payload directory.
    Use resource_path() instead of it for anything that needs to exist in a
    packaged build; that distinction is why this function is no longer the way
    .ui files are located.
    """
    script_dir = os.path.dirname(os.path.abspath(script_path))
    project_root = install_root()

    # 1. Make the project root importable so 'shared' resolves. A frozen build
    #    already has an importer covering every bundled module, and its
    #    install root holds no .py files, so this is a no-op there.
    if not is_frozen() and project_root not in sys.path:
        sys.path.insert(0, project_root)

    # 2. Make the bundled binaries discoverable by mpv, ffprobe, and ffmpeg.
    _register_bin_dir()

    return script_dir, project_root
