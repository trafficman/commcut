"""Pytest configuration — make the project root importable for tests."""
import os
import sys
import tempfile

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# shared.diagnostics.log() is reached from code paths the suite exercises on
# purpose (ffmpeg failures, mpv with no media). Keep those writes out of the
# source tree so a test run never leaves a commcut.log behind.
os.environ.setdefault(
    "COMMCUT_LOG", os.path.join(tempfile.gettempdir(), "commcut-tests.log"))


#: The absolute-path length rule is Windows's, for now.
#:
#: Windows' ``MAX_PATH`` is 260 characters for a whole absolute path, and that
#: is a real problem for a library of named clips.  macOS and Linux allow far
#: more -- 1024 and 4096 bytes -- and their limits are not settled: the rule is
#: in `shared/paths.py` for all three platforms, but it is *exercised* only
#: where it bites, so the tests that depend on the Windows limit are skipped
#: elsewhere rather than rewritten for a number nobody has decided on.
#:
#: The portable rules are not affected: a component is limited by ``NAME_MAX``
#: everywhere, and that is tested on every platform.
windows_path_length_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the path-length rule is Windows's; macOS and Linux are out of scope",
)
