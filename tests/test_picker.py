"""Tests for the source picker window.

The picker is the front door of the Editing Wizard, so what matters is that it
only offers videos from the import folder, that it says something useful when
that folder is empty (the first-run state of a packaged build), and that the
selection reaches the scanner as an argument rather than through some shared
piece of state.
"""

import os

import pytest

from editor_stub import ensure_qapp


def touch(path, size=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    return path


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def picker_factory(qapp, monkeypatch, tmp_path):
    """Build PickerWindows against a temporary import folder.

    The window resolves its folder through shared.sources, so that is what gets
    redirected rather than a path argument: the window takes none, and the
    test should not have to invent one.
    """
    import picker.picker as picker_module
    from shared import sources

    folder = tmp_path / "import"
    folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sources, "import_folder", lambda: str(folder))
    monkeypatch.setattr(picker_module, "import_folder", lambda: str(folder))

    # The picker hands the chosen video to the scanner by asking the shell to
    # open it, so the recording is of window requests rather than of processes.
    # open_safely is the only method offered, on purpose: reaching for
    # shell().open() instead is an AttributeError here rather than an
    # exception escaping into Qt's event loop.
    opened = []

    class RecordingShell:
        def open_safely(self, name, **kwargs):
            opened.append((name, kwargs))
            return object()

    monkeypatch.setattr(picker_module, "shell", RecordingShell)

    created = []

    def make():
        window = picker_module.PickerWindow()
        created.append(window)
        return window

    yield make, folder, opened, []

    for window in created:
        window.close()
        window.deleteLater()
    qapp.processEvents()


def listed_names(window):
    return [
        window.ui.listVideos.item(row).text().split("    ")[0]
        for row in range(window.ui.listVideos.count())
    ]


class TestListing:
    def test_lists_the_videos_in_the_folder(self, picker_factory):
        make, folder, _, _ = picker_factory
        touch(str(folder / "compilation.mp4"), 1024)
        touch(str(folder / "second.mkv"), 2048)

        assert listed_names(make()) == ["compilation.mp4", "second.mkv"]

    def test_the_shipped_readme_is_not_listed(self, picker_factory):
        """import/ ships with a README.txt so the folder survives a zip."""
        make, folder, _, _ = picker_factory
        touch(str(folder / "README.txt"))

        assert listed_names(make()) == []

    def test_an_empty_folder_explains_itself(self, picker_factory):
        """import/ ships empty, so this is the first thing a tester sees."""
        make, folder, _, _ = picker_factory

        status = make().ui.labelStatus.text()

        assert str(folder) in status
        assert "Refresh" in status

    def test_the_folder_is_shown_so_they_know_where_to_put_it(self, picker_factory):
        make, folder, _, _ = picker_factory

        assert str(folder) in make().ui.labelFolder.text()

    def test_refresh_picks_up_a_video_added_while_open(self, picker_factory):
        make, folder, _, _ = picker_factory
        window = make()
        assert listed_names(window) == []

        touch(str(folder / "late.mp4"))
        window.ui.refreshButton.click()

        assert listed_names(window) == ["late.mp4"]

    def test_refresh_drops_a_video_that_was_removed(self, picker_factory):
        make, folder, _, _ = picker_factory
        path = touch(str(folder / "temporary.mp4"))
        window = make()

        os.remove(path)
        window.ui.refreshButton.click()

        assert listed_names(window) == []

    def test_already_scanned_videos_are_labelled(self, picker_factory):
        """A video with a .cmct goes straight to the editor, which should not
        be a surprise."""
        make, folder, _, _ = picker_factory
        touch(str(folder / "done.mp4"))
        touch(str(folder / "done.cmct"))

        assert "already scanned" in make().ui.listVideos.item(0).text()

    def test_size_is_shown_alongside_the_name(self, picker_factory):
        make, folder, _, _ = picker_factory
        touch(str(folder / "compilation.mp4"), 2 * 1024 * 1024)

        assert "2.0 MB" in make().ui.listVideos.item(0).text()

    def test_the_count_is_reported(self, picker_factory):
        make, folder, _, _ = picker_factory
        touch(str(folder / "a.mp4"))
        touch(str(folder / "b.mp4"))

        assert "2 video" in make().ui.labelStatus.text()


class TestSelection:
    def test_open_is_disabled_with_nothing_selected(self, picker_factory):
        make, folder, _, _ = picker_factory
        touch(str(folder / "compilation.mp4"))
        window = make()

        window.ui.listVideos.setCurrentRow(-1)

        assert not window.ui.openButton.isEnabled()

    def test_the_first_video_is_selected_and_openable(self, picker_factory):
        make, folder, _, _ = picker_factory
        touch(str(folder / "compilation.mp4"))
        window = make()

        assert window.ui.listVideos.currentRow() == 0
        assert window.ui.openButton.isEnabled()

    def test_opening_asks_for_the_scanner_with_that_video(self, picker_factory):
        make, folder, opened, _ = picker_factory
        path = touch(str(folder / "compilation.mp4"))
        window = make()

        window.ui.openButton.click()

        assert opened == [("scanner", {"source": path})]

    def test_opening_the_right_one_when_several_are_listed(self, picker_factory):
        make, folder, opened, _ = picker_factory
        touch(str(folder / "a.mp4"))
        second = touch(str(folder / "b.mp4"))
        window = make()

        window.ui.listVideos.setCurrentRow(1)
        window.ui.openButton.click()

        assert opened[0][1]["source"] == second

    def test_double_clicking_opens_too(self, picker_factory):
        make, folder, opened, _ = picker_factory
        touch(str(folder / "compilation.mp4"))
        window = make()

        window.ui.listVideos.itemDoubleClicked.emit(window.ui.listVideos.item(0))

        assert len(opened) == 1

    def test_opening_leaves_the_window_in_the_shells_hands(self, picker_factory, qapp):
        """The picker no longer closes itself.

        Under one-window navigation the shell builds the scanner and then takes
        down whatever was on screen. The picker used to open the scanner and
        then close itself, which is what forced the shell to remove windows by
        identity to cope with a window that closed after its successor was
        already up. Doing it in both places is the bug this removes, so the
        picker is asserted *not* to close here.
        """
        make, folder, opened, _ = picker_factory
        touch(str(folder / "compilation.mp4"))
        window = make()
        window.show()
        qapp.processEvents()

        window.ui.openButton.click()
        qapp.processEvents()

        assert opened == [("scanner", {"source": str(folder / "compilation.mp4")})]
        assert window.isVisible(), "taking the screen down is the shell's job"

    def test_cancelling_opens_nothing(self, picker_factory):
        make, folder, opened, _ = picker_factory
        touch(str(folder / "compilation.mp4"))
        window = make()

        window.ui.cancelButton.click()

        assert opened == []


def test_the_picker_stays_open_when_the_scanner_cannot_be_opened(
        picker_factory, qapp, monkeypatch):
    """The shell reports a window that will not open and returns None. The
    picker must not close itself in that case, or the user would be left with
    neither the picker nor a scanner and no way to pick a different video."""
    import picker.picker as picker_module

    make, folder, opened, _ = picker_factory
    touch(str(folder / "compilation.mp4"))
    window = make()
    window.show()
    qapp.processEvents()

    class FailingShell:
        def open_safely(self, name, **kwargs):
            opened.append((name, kwargs))
            return None

    monkeypatch.setattr(picker_module, "shell", FailingShell)

    window.ui.openButton.click()
    qapp.processEvents()

    assert opened == [("scanner", {"source": str(folder / "compilation.mp4")})]
    assert window.isVisible(), "the picker must stay up so a different video can be picked"


@pytest.mark.parametrize("size,expected", [
    (0, "0 B"),
    (900, "900 B"),
    (1024, "1.0 KB"),
    (2 * 1024 * 1024, "2.0 MB"),
])
def test_format_size(size, expected):
    from picker.picker import format_size

    assert format_size(size) == expected
