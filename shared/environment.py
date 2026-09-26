"""Shared environment setup for the project's entry-point scripts.

Scripts may live at the project root (main.py, mainwindow.py) or in a
subdirectory (editor/, scanner/, settings/). Either way they need the same two
things before anything else:

  1. The project root on sys.path so `shared.*` imports resolve when the
     script is run directly (e.g. `python editor/editor.py`).
  2. The bundled binaries folder prepended to PATH so mpv, ffprobe, and
     ffmpeg resolve to the versions under bin/<os>/ rather than whatever
     happens to be on the system.

The bin folder is selected per-OS via ``get_binary_path`` / ``_bin_dir``
(the cross-platform binary resolution formerly living in ``core.py``).
"""

import os
import platform
import sys


# Presence of this file inside a `shared/` folder is what identifies a folder
# as the project root, so a script that already lives there (main.py) is not
# treated as if it were one level below the root.
_SHARED_MARKER = os.path.join('shared', 'environment.py')


def _bin_dir():
    """Return the absolute path to the bundled binaries folder for this OS.

    Mirrors ``core.py``'s directory mapping: ``bin/win`` on Windows,
    ``bin/linux`` on Linux, ``bin/mac`` on macOS.
    """
    system = platform.system().lower()
    if system == 'windows':
        folder = os.path.join('bin', 'win')
    elif system == 'linux':
        folder = os.path.join('bin', 'linux')
    elif system == 'darwin':  # macOS
        folder = os.path.join('bin', 'mac')
    else:
        raise EnvironmentError(f"Unsupported operating system: {system}")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, folder)


def get_binary_path(binary_name):
    """Dynamically resolve the path to a bundled binary based on OS.

    Looks under ``bin/<os>/`` for the named binary, appending the platform's
    executable extension (``.exe`` on Windows). Raises ``FileNotFoundError``
    if the binary is not present at the expected location.
    """
    ext = '.exe' if platform.system().lower() == 'windows' else ''
    binary_path = os.path.join(_bin_dir(), f"{binary_name}{ext}")

    if not os.path.exists(binary_path):
        raise FileNotFoundError(f"Could not find {binary_name} at expected path: {binary_path}")

    return binary_path


def setup_environment(script_path):
    """Prepare sys.path and PATH for a script in a subdirectory of the project.

    Call at the very top of the script (before any imports of `shared` or
    mpv):

        from shared.environment import setup_environment
        SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

    Returns (script_dir, project_root) so the caller can build paths
    relative to the script (for its own .ui file) or to the project root
    (for assets, input videos, etc.).
    """
    script_dir = os.path.dirname(os.path.abspath(script_path))

    # Writable resource root: the folder next to the bundled executable when
    # frozen, or the source tree root when run from source. When frozen,
    # __file__ lives under a temporary _MEIPASS dir (wiped on exit) — never the
    # place for settings.json or user data — so we root user-facing folders at
    # dirname(sys.executable) (the binary dir) instead. This is identical in
    # spirit across Windows, macOS, and Linux: PyInstaller sets sys.frozen and
    # points sys.executable at the platform binary in all three cases.
    if getattr(sys, 'frozen', False):
        project_root = os.path.dirname(os.path.abspath(sys.executable))
    elif os.path.exists(os.path.join(script_dir, _SHARED_MARKER)):
        # The script already sits at the project root (main.py, mainwindow.py),
        # so stepping up a level would escape the tree entirely.
        project_root = script_dir
    else:
        project_root = os.path.abspath(os.path.join(script_dir, '..'))

    # 1. Make the project root importable so 'shared' resolves.
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # 2. Prepend the bundled binaries folder to PATH, resolved per-OS.
    bin_dir = _bin_dir()
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

    return script_dir, project_root
