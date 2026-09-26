"""Pytest configuration — make the project root importable for tests."""
import os
import sys
import tempfile

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# shared.diagnostics.log() is reached from code paths the suite exercises on
# purpose (ffmpeg failures, mpv with no media). Keep those writes out of the
# source tree so a test run never leaves a commcut.log behind.
os.environ.setdefault(
    "COMMCUT_LOG", os.path.join(tempfile.gettempdir(), "commcut-tests.log"))
