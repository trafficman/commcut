"""Tests for the import folder: what can be opened, and what is offered.

The alpha deliberately refuses to open arbitrary paths, so these cover both
halves of that rule: the picker may only *offer* videos in the import folder,
and the scanner/editor may only *accept* one. The second half is the one that
matters, because the value arrives as a command-line argument anyone can edit.
"""

import os

import pytest

from shared.sources import (
    DEFAULT_SOURCE_NAME, SourceVideo, import_folder, is_video_file,
    list_source_videos, require_source_video, resolve_import_video,
)


def touch(path, size=0):
    """Create a file with a given size, making parents as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    return path


@pytest.fixture
def imports(tmp_path):
    """An isolated import folder for one test."""
    return tmp_path / "import"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

class TestListing:
    def test_lists_only_videos(self, imports):
        touch(str(imports / "compilation.mp4"), 10)
        touch(str(imports / "notes.txt"))
        touch(str(imports / "README.txt"))

        assert [video.name for video in list_source_videos(str(imports))] == [
            "compilation.mp4"]

    def test_the_shipped_readme_placeholder_is_not_offered(self, imports):
        """import/ ships with a README.txt so the folder survives a zip; it
        must never appear as a selectable video."""
        touch(str(imports / "README.txt"))

        assert list_source_videos(str(imports)) == []

    def test_ignores_subfolders(self, imports):
        touch(str(imports / "compilation.mp4"))
        os.makedirs(str(imports / "clips.mov"))

        assert [video.name for video in list_source_videos(str(imports))] == [
            "compilation.mp4"]

    def test_is_case_insensitive_on_extensions(self, imports):
        touch(str(imports / "RIP.MP4"))
        touch(str(imports / "other.MkV"))

        names = [video.name for video in list_source_videos(str(imports))]
        assert sorted(names) == ["RIP.MP4", "other.MkV"]

    def test_sorted_case_insensitively(self, imports):
        for name in ("beta.mp4", "Alpha.mp4", "gamma.mp4"):
            touch(str(imports / name))

        assert [video.name for video in list_source_videos(str(imports))] == [
            "Alpha.mp4", "beta.mp4", "gamma.mp4"]

    def test_reports_the_size(self, imports):
        touch(str(imports / "compilation.mp4"), 2048)

        assert list_source_videos(str(imports))[0].size_bytes == 2048

    def test_flags_a_video_that_was_scanned_before(self, imports):
        touch(str(imports / "compilation.mp4"))
        touch(str(imports / "compilation.cmct"))

        assert list_source_videos(str(imports))[0].has_sidecar is True

    def test_a_video_without_a_sidecar_is_not_flagged(self, imports):
        touch(str(imports / "compilation.mp4"))

        assert list_source_videos(str(imports))[0].has_sidecar is False

    def test_a_missing_folder_is_empty_not_an_error(self, tmp_path):
        assert list_source_videos(str(tmp_path / "nope")) == []

    def test_returns_real_entries(self, imports):
        touch(str(imports / "compilation.mp4"), 3)

        video = list_source_videos(str(imports))[0]
        assert isinstance(video, SourceVideo)
        assert video.path == os.path.join(str(imports), "compilation.mp4")


def test_is_video_file():
    assert is_video_file("a.mp4")
    assert is_video_file("a.MKV")
    assert not is_video_file("a.txt")
    assert not is_video_file("a.mp4.txt")
    assert not is_video_file("a")


def test_import_folder_is_under_the_install_root():
    assert import_folder().endswith(os.path.join("", "import"))


# ---------------------------------------------------------------------------
# Accepting a path
# ---------------------------------------------------------------------------

class TestResolve:
    def test_accepts_a_video_in_the_folder(self, imports):
        path = touch(str(imports / "compilation.mp4"))

        assert resolve_import_video(path, folder=str(imports)) == path

    def test_rejects_a_file_outside_the_folder(self, imports, tmp_path):
        outside = touch(str(tmp_path / "elsewhere" / "sneaky.mp4"))

        with pytest.raises(ValueError) as error:
            resolve_import_video(outside, folder=str(imports))

        assert str(imports) in str(error.value)

    def test_rejects_the_folder_itself(self, imports):
        with pytest.raises(ValueError):
            resolve_import_video(str(imports), folder=str(imports))

    def test_rejects_a_traversal_out_of_the_folder(self, imports):
        """A relative-looking path must not escape by way of '..'."""
        traversal = os.path.join(str(imports), os.pardir, "outside.mp4")

        with pytest.raises(ValueError):
            resolve_import_video(traversal, folder=str(imports))

    def test_rejects_a_sibling_folder_with_a_shared_prefix(self, imports, tmp_path):
        """`import_backup` must not pass as inside `import`."""
        sibling = tmp_path / "import_backup"
        path = touch(str(sibling / "compilation.mp4"))

        with pytest.raises(ValueError):
            resolve_import_video(path, folder=str(imports))

    def test_does_not_require_the_file_to_exist(self, imports):
        """Existence is require_source_video's business, so it can explain a
        stale selection in the words that fit."""
        assert resolve_import_video(
            str(imports / "gone.mp4"), folder=str(imports)).endswith("gone.mp4")

    def test_rejects_a_non_video_file(self, imports):
        path = touch(str(imports / "notes.txt"))

        with pytest.raises(ValueError) as error:
            resolve_import_video(path, folder=str(imports))

        assert ".mp4" in str(error.value)

    def test_no_value_falls_back_to_the_legacy_default(self, imports):
        assert resolve_import_video(None, folder=str(imports)) == os.path.join(
            str(imports), DEFAULT_SOURCE_NAME)

    def test_a_relative_value_is_made_absolute(self, imports, monkeypatch):
        """A relative argument means what it always means: relative to the
        process's working directory."""
        touch(str(imports / "compilation.mp4"))
        monkeypatch.chdir(str(imports))

        assert resolve_import_video(
            "compilation.mp4", folder=str(imports)) == os.path.join(
                str(imports), "compilation.mp4")


class TestRequireSourceVideo:
    def test_returns_a_validated_path(self, imports):
        path = touch(str(imports / "compilation.mp4"))

        assert require_source_video(path, folder=str(imports)) == path

    def test_refuses_a_path_outside_the_folder(self, tmp_path, imports):
        outside = touch(str(tmp_path / "elsewhere.mp4"))

        with pytest.raises(ValueError):
            require_source_video(outside, folder=str(imports))

    def test_missing_video_in_an_empty_folder_names_the_folder(self, imports):
        """The first-run state of a packaged build: import/ ships empty."""
        with pytest.raises(FileNotFoundError) as error:
            require_source_video(None, folder=str(imports))

        message = str(error.value)
        assert str(imports) in message
        assert "Editor" in message

    def test_missing_video_with_others_present_lists_them(self, imports):
        touch(str(imports / "one.mp4"))
        touch(str(imports / "two.mp4"))

        with pytest.raises(FileNotFoundError) as error:
            require_source_video(
                str(imports / "gone.mp4"), folder=str(imports))

        assert "one.mp4" in str(error.value)
        assert "two.mp4" in str(error.value)

    def test_the_legacy_default_still_works(self, imports):
        """A source run of scanner.py with no arguments has to keep working."""
        path = touch(str(imports / DEFAULT_SOURCE_NAME))

        assert require_source_video(None, folder=str(imports)) == path
