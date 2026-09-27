"""Tests for the import folder: what can be opened, and what is offered.

The alpha deliberately refuses to open arbitrary paths, so these cover both
halves of that rule: the picker may only *offer* videos in the import folder,
and the scanner/editor may only *accept* one. The second half is the one that
matters, because the value arrives as a command-line argument anyone can edit.
"""

import os

import pytest

import shared.sources as sources
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


class TestContainment:
    """The inside-import-folder test, which is also the traversal defense.

    A realpath() can only report the filesystem's own casing for a path that
    exists, so a candidate that is spelled differently and is not there yet
    keeps whatever case it was typed with. Comparing those strings is
    case-sensitive, while the volume usually is not -- which is why the same
    argument is accepted on Windows and refused on a Mac, with advice to put
    the file somewhere it already is.
    """

    def test_a_case_insensitive_volume_retries_folded(self, imports, monkeypatch):
        """The fix, pinned: a second, case-folded attempt, and it is what
        accepts the spelling a person would type."""
        monkeypatch.setattr(sources, "_volume_is_case_insensitive",
                            lambda folder: True)
        tried = []
        monkeypatch.setattr(sources, "_contained_in",
                            lambda f, c: tried.append((f, c)) or len(tried) > 1)

        differently_cased = str(imports).upper() + "/compilation.mp4"

        assert sources._is_inside(str(imports), differently_cased) is True
        assert len(tried) == 2, tried
        assert tried[1][0] == tried[0][0].casefold()
        assert tried[1][1] == tried[0][1].casefold()

    def test_a_case_sensitive_volume_never_folds_the_case(self, imports, monkeypatch):
        """The direction that must not move.

        Folding the case unconditionally would accept a path that really is
        outside the folder on a case-sensitive filesystem, which turns a
        consistency fix into a traversal hole. This spies on the comparison
        instead of provoking a real refusal, because ``ntpath.commonpath`` folds
        case and ``posixpath.commonpath`` does not -- so the raw comparison's
        case-sensitivity cannot be reproduced on a Windows machine at all.
        """
        monkeypatch.setattr(sources, "_volume_is_case_insensitive",
                            lambda folder: False)
        tried = []
        monkeypatch.setattr(sources, "_contained_in",
                            lambda f, c: tried.append((f, c)) or False)

        differently_cased = str(imports).upper() + "/compilation.mp4"

        assert sources._is_inside(str(imports), differently_cased) is False
        assert len(tried) == 1, tried
        # The candidate kept the case it was typed with, so no fold happened.
        # (realpath normalises separators even for a path that does not exist,
        # which is why this compares against itself rather than the literal.)
        assert tried[0][1] != tried[0][1].casefold()

    def test_a_differently_cased_path_is_accepted_on_this_host(self, imports):
        """The end-to-end version, on a volume that actually behaves that way.

        The candidate deliberately does not exist: realpath() can only report
        the filesystem's own casing for a path that is there, so an existing
        file would be accepted no matter what this function did.
        """
        imports.mkdir(parents=True, exist_ok=True)
        if not sources._volume_is_case_insensitive(str(imports)):
            pytest.skip("this host's filesystem is case-sensitive, so the "
                        "differently-cased spelling really is a different path")

        assert sources._is_inside(
            str(imports), str(imports).upper() + "/not-yet-ripped.mp4") is True

    def test_the_folder_is_never_inside_itself_however_it_is_spelled(
        self, imports, monkeypatch
    ):
        """strictly inside: the folder itself is refused, because that is what
        stops a bare folder being accepted as a source video. The fold must not
        become a way around that."""
        monkeypatch.setattr(sources, "_volume_is_case_insensitive",
                            lambda folder: True)
        monkeypatch.setattr(sources.os.path, "realpath", lambda path: path)

        assert sources._is_inside(str(imports), str(imports).upper()) is False

    def test_a_symlink_out_of_the_folder_is_still_refused(self, imports, tmp_path):
        """The property the case-widening must not disturb: real paths are
        compared, so a symlink in import/ cannot reach a file outside it."""
        outside = touch(str(tmp_path / "elsewhere.mp4"))
        imports.mkdir(parents=True, exist_ok=True)
        try:
            (imports / "sneaky.mp4").symlink_to(outside)
        except OSError as error:
            pytest.skip(f"Symlinks unavailable: {error}")

        assert sources._is_inside(str(imports), str(imports / "sneaky.mp4")) is False

    def test_a_traversal_is_refused(self, imports):
        traversal = os.path.join(str(imports), os.pardir, "outside.mp4")

        assert sources._is_inside(str(imports), traversal) is False

    def test_the_volume_probe_asks_the_filesystem(self, tmp_path):
        """A case-sensitive volume answers "no" -- which is the safe answer, and
        is why the probe compares the casefolded path back against the real one
        rather than assuming anything from the platform."""
        folder = tmp_path / "Import"
        folder.mkdir()

        probe = sources._volume_is_case_insensitive(str(folder))

        assert probe is os.path.samefile(str(folder), str(folder).casefold())


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
