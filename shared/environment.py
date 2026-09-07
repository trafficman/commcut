"""Shared environment setup for editor/ and scanner/ scripts.

Both scripts live in a subdirectory of the project root (e.g. editor/ or
scanner/) and need the same two things before anything else:

  1. The project root on sys.path so `shared.*` imports resolve when the
     script is run directly (e.g. `python editor/editor.py`).
  2. The bundled binaries folder prepended to PATH so mpv, ffprobe, and
     ffmpeg resolve to the versions under bin/<os>/ rather than whatever
     happens to be on the system.

Cross-platform note: the bundled-binaries folder is currently `bin/win`
(Windows-only). When the project goes cross-platform, extend this to
select bin/linux, bin/mac, etc. based on platform.system() — see AGENTS.md
"Gaps" and core.py:get_binary_path for the proper approach.
"""

import os
import sys


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
    project_root = os.path.abspath(os.path.join(script_dir, '..'))

    # 1. Make the project root importable so 'shared' resolves.
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # 2. Prepend the bundled binaries folder to PATH.
    bin_dir = os.path.join(project_root, 'bin', 'win')
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

    return script_dir, project_root
