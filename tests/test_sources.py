"""Tests for what this app will open, and what it insists about it.

A source video can be picked from anywhere, so these are not about which folder
a path is in. They are about the four things that still have to hold wherever the
video lives: it is named, it is there, it is a video, and its folder can be
written — because the `.cmct` sidecar goes beside it and the editor rewrites that
on every Stage.

The folder-containment rule this file used to cover is gone with the picker, and
its tests went with it rather than being converted: `tests/test_sources.py` used
to pin a traversal defence, and with every path outside `import/` now legal there
was no rule left for it to defend.

`import/` itself moved out of here too. It used to be answered by a dead second
copy of `shared/environment.py:import_folder`, which nothing but a test called;
the roots it depends on are asserted in `tests/test_frozen_mode.py`.
"""

import os

import pytest

import shared.sources as sources
from shared.segments import sidecar_path
from shared.sources import (
    VIDEO_EXTENSIONS,
    is_video_file,
    validate_source_video,
)


def touch(path, size=0):
    """Create a file with a given size, making parents as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    return path


# ---------------------------------------------------------------------------
# What counts as a video
# ---------------------------------------------------------------------------

def test_is_video_file():
    assert is_video_file("a.mp4")
    assert is_video_file("a.MKV")
    assert not is_video_file("a.txt")
    assert not is_video_file("a.mp4.txt")
    assert not is_video_file("a")


def test_the_extension_list_is_generous_on_purpose():
    """ffmpeg reads all of these, and an alpha tester should not have to care
    which container their compilation rip happens to be in."""
    for expected in (".mp4", ".mkv", ".mov", ".avi", ".m2ts", ".ts", ".vob"):
        assert expected in VIDEO_EXTENSIONS


# ---------------------------------------------------------------------------
# Validating a chosen path
# ---------------------------------------------------------------------------

class TestValidateSourceVideo:
    """The rule a file dialog's answer has to pass, from any folder."""

    def test_accepts_a_video_anywhere_and_returns_it_absolute(self, tmp_path):
        path = touch(str(tmp_path / "deep" / "elsewhere" / "compilation.mp4"))

        assert validate_source_video(path) == path

    def test_makes_a_relative_path_absolute(self, tmp_path, monkeypatch):
        """A relative argument means what it always means: relative to the
        process's working directory."""
        touch(str(tmp_path / "compilation.mp4"))
        monkeypatch.chdir(str(tmp_path))

        assert validate_source_video("compilation.mp4") == os.path.join(
            str(tmp_path), "compilation.mp4")

    def test_accepts_any_supported_container(self, tmp_path):
        """The dialog's filter lists them all, and this is the rule behind it —
        one owner, so a new container cannot be added to one and missed in the
        other."""
        for name in ("a.mp4", "b.mkv", "c.MOV", "d.webm", "e.m2ts"):
            path = touch(str(tmp_path / name))
            assert validate_source_video(path) == path

    def test_refuses_an_empty_value(self):
        with pytest.raises(ValueError) as error:
            validate_source_video("")

        assert "No source video" in str(error.value)

    def test_refuses_a_file_that_is_not_there(self, tmp_path):
        with pytest.raises(FileNotFoundError) as error:
            validate_source_video(str(tmp_path / "gone.mp4"))

        assert "gone.mp4" in str(error.value)

    def test_refuses_a_folder_and_says_so(self, tmp_path):
        """Picking the folder by mistake is a real case now that the dialog
        starts wherever the user last was."""
        folder = tmp_path / "Season 1"
        folder.mkdir()

        with pytest.raises(ValueError) as error:
            validate_source_video(str(folder))

        message = str(error.value)
        assert "folder" in message
        assert str(folder) in message

    def test_refuses_a_non_video_and_lists_what_is_supported(self, tmp_path):
        """The dialog's filter is a convenience, not this rule — "All files" is
        one click away."""
        path = touch(str(tmp_path / "notes.txt"))

        with pytest.raises(ValueError) as error:
            validate_source_video(path)

        message = str(error.value)
        assert "notes.txt" in message
        assert ".mkv" in message

    def test_a_dangling_symlink_is_reported_as_missing_not_as_a_video(self, tmp_path):
        """`os.path.exists` follows the link, so a broken one is simply not
        there — which is the truth, and a better message than a decode failure
        two windows later."""
        try:
            os.symlink(str(tmp_path / "missing.mp4"), str(tmp_path / "gone.mp4"))
        except (OSError, NotImplementedError):
            pytest.skip("this platform or user cannot create symlinks")

        with pytest.raises(FileNotFoundError):
            validate_source_video(str(tmp_path / "gone.mp4"))


# ---------------------------------------------------------------------------
# The writable folder rule
# ---------------------------------------------------------------------------

class TestTheWritableFolderRule:
    """Rule 4, and the reason it exists.

    The `.cmct` is written beside the video and rewritten by the editor on every
    Stage, so the folder has to take a write for the whole session. Nothing
    checked this while every source was in `import/`, which is writable by
    construction; with a source from anywhere it stops being true, and the
    failure arrives late — a bare `PermissionError` out of the scanner's Finished
    handler or the editor's Stage, neither of which names the folder.
    """

    def test_a_writable_folder_is_accepted(self, tmp_path):
        path = touch(str(tmp_path / "compilation.mp4"))

        assert validate_source_video(path) == path

    def test_an_unwritable_folder_is_refused_and_the_message_names_it(
        self, tmp_path, monkeypatch
    ):
        folder = tmp_path / "read-only"
        folder.mkdir()
        path = touch(str(folder / "compilation.mp4"))

        monkeypatch.setattr(
            sources, "_writability_problem",
            lambda _folder, _name: "[Errno 13] Permission denied")

        with pytest.raises(ValueError) as error:
            validate_source_video(path)

        message = str(error.value)
        assert str(folder) in message
        assert "Permission denied" in message
        assert ".cmct" in message

    def test_the_check_asks_the_folder_not_the_file(self, tmp_path, monkeypatch):
        """The sidecar may not exist yet, and the editor rewrites an existing
        one, so it is the folder's writability that matters either way."""
        path = touch(str(tmp_path / "compilation.mp4"))
        asked = []

        monkeypatch.setattr(
            sources, "_writability_problem",
            lambda folder, _name: asked.append(folder) or None)

        validate_source_video(path)

        assert asked == [str(tmp_path)]

    def test_the_folder_is_checked_after_the_cheap_rules(self, tmp_path,
                                                          monkeypatch):
        """Not worth writing a probe file to learn that a path is a folder."""
        calls = []
        monkeypatch.setattr(
            sources, "_writability_problem",
            lambda folder, _name: calls.append(folder) or None)

        with pytest.raises(FileNotFoundError):
            validate_source_video(str(tmp_path / "missing.mp4"))
        with pytest.raises(ValueError):
            validate_source_video(str(tmp_path))

        assert calls == []

    def test_the_probe_name_is_never_longer_than_the_sidecars(self, tmp_path,
                                                               monkeypatch):
        """The probe shares the folder with the sidecar, so a longer probe name
        would refuse a folder the real write would have fitted in -- and report
        a length problem as a permission one."""
        path = touch(str(tmp_path / "compilation.mp4"))
        asked = []

        monkeypatch.setattr(
            sources, "_writability_problem",
            lambda folder, name: asked.append(name) or None)

        validate_source_video(path)

        sidecar_name = os.path.basename(sidecar_path(path))
        assert len(asked[0]) <= len(sidecar_name)

    def test_a_sidecar_the_platform_cannot_address_is_refused_by_name(
        self, tmp_path, monkeypatch
    ):
        """A source in a folder deep enough that the sidecar cannot be
        addressed is refused here, by name and by length, rather than failing
        later as a file that is not missing."""
        from shared.paths import absolute_path_limit

        folder = tmp_path
        path = touch(str(folder / "compilation.mp4"))
        limit = absolute_path_limit()

        # A folder long enough that the sidecar cannot be addressed.
        monkeypatch.setattr(
            sources, "absolute_path_error",
            lambda *_args, **_kwargs: f"too long: the limit is {limit}")

        with pytest.raises(ValueError) as error:
            validate_source_video(path)

        assert "too long" in str(error.value)


class TestTheWritabilityProbe:
    """The probe itself, since `os.access` is not an acceptable substitute."""

    def test_a_writable_folder_reports_no_problem(self, tmp_path):
        assert sources._writability_problem(str(tmp_path), ".probe") is None

    def test_the_probe_leaves_nothing_behind_on_success(self, tmp_path):
        sources._writability_problem(str(tmp_path), ".probe")

        assert os.listdir(str(tmp_path)) == []

    def test_the_probe_file_is_dot_prefixed(self, tmp_path, monkeypatch):
        """Hidden in a POSIX listing, and recognisable as commcut's own."""
        removed = []
        monkeypatch.setattr(sources.os, "remove",
                            lambda path: removed.append(path))

        sources._writability_problem(str(tmp_path), ".probe")

        assert removed == [os.path.join(str(tmp_path), ".probe")]

    def test_the_probe_never_truncates_a_file_that_is_already_there(
        self, tmp_path
    ):
        """Created exclusively, so a name that is taken is left alone -- and
        its being there is itself proof the folder is writable."""
        existing = tmp_path / ".probe"
        existing.write_bytes(b"the user's file")

        assert sources._writability_problem(str(tmp_path), ".probe") is None
        assert existing.read_bytes() == b"the user's file"

    def test_a_folder_that_refuses_creation_reports_the_reason(self, tmp_path,
                                                               monkeypatch):
        """Reported, not raised: the caller turns it into a message, and a probe
        that raised would have to be caught in both windows."""
        def refuse(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied", str(tmp_path))

        monkeypatch.setattr("builtins.open", refuse)

        problem = sources._writability_problem(str(tmp_path), ".probe")

        assert "Permission denied" in problem

    def test_a_failed_removal_does_not_raise(self, tmp_path, monkeypatch):
        """The `finally` runs when the create failed too, so it has to tolerate
        the probe never having existed rather than masking the original error
        with a second one.

        The stray file this leaves is the documented trade: one empty,
        dot-prefixed file, and only if removing it failed.
        """
        real_remove = os.remove

        def refuse(path):
            if path.endswith(".probe"):
                raise FileNotFoundError(path)
            return real_remove(path)

        monkeypatch.setattr(sources.os, "remove", refuse)

        assert sources._writability_problem(str(tmp_path), ".probe") is None
        assert [name for name in os.listdir(str(tmp_path))
                if name.endswith(".probe")]
