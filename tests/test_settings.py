"""Tests for file and folder scheme integration in Settings."""

import json
import os
import re
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QThread
from PySide6.QtWidgets import QApplication

import settings.settings as settings_module
from shared import environment, exporting
from shared.paths import DEFAULT_FOLDER_SCHEME
from shared.records import ClipRecord, write_record
from shared.vocabulary import (
    DEFAULT_VALUES,
    Vocabulary,
    get_vocabulary,
)

VALID_FILE_SCHEME = settings_module.DEFAULT_FILE_SCHEME
file_scheme_error = settings_module.file_scheme_error
vocabulary_sync_summary = settings_module.vocabulary_sync_summary

#: Captured before any fixture patches `settings_module.SyncWorker`, so
#: `FakeSyncWorker` can build a real one rather than another fake.
RealSyncWorker = settings_module.SyncWorker


# ---------------------------------------------------------------------------
# Qt and settings fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    """Create one offscreen QApplication for all Settings-window tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def window_factory(qapp, monkeypatch, tmp_path):
    """Create isolated Settings windows backed by temporary settings files.

    Both readers of settings.json are pointed at one temporary file. The window
    resolves it through its own `settings_path`, and `export_folder()` resolves
    it through `environment.settings_path()`, which joins the same patched
    `install_root` -- so redirecting that one root is enough, and there is no
    way for the window to save to one file while the resolver reads another.
    """
    windows = []

    def create(settings=None, encoding="utf-8"):
        path = tmp_path / "settings.json"
        if settings is not None:
            path.write_text(
                json.dumps(settings, ensure_ascii=False),
                encoding=encoding,
            )
        monkeypatch.setattr(environment, "install_root", lambda: str(tmp_path))
        monkeypatch.setattr(settings_module, "settings_path", lambda: str(path))
        window = settings_module.SettingsWindow()
        windows.append(window)
        qapp.processEvents()
        return window

    yield create

    for window in windows:
        window.close()
        window.deleteLater()
    qapp.processEvents()


def base_settings(**extra):
    """Return valid baseline settings with optional overrides."""
    return {
        settings_module.FILE_NAMING_SCHEME_KEY: VALID_FILE_SCHEME,
        **extra,
    }


def test_settings_written_with_a_byte_order_mark_still_load(window_factory):
    """A BOM is not the user's mistake to report back to them.

    settings.json is hand-editable user data, and plenty of things write UTF-8
    with a leading byte-order mark: PowerShell 5.1's Set-Content/Out-File,
    Notepad, older .NET tooling. Reading it as plain utf-8 raises
    JSONDecodeError on the BOM, which the window reports as a corrupt file.
    """
    window = window_factory(base_settings(), encoding="utf-8-sig")

    assert window.ui.lineEditFileScheme.text() == VALID_FILE_SCHEME
    assert window.ui.lineEditFileScheme.isEnabled()


def test_export_snapshot_agrees_about_a_byte_order_mark(tmp_path):
    """The Settings window and the export planner read the same file, so they
    have to agree on what is readable."""
    from shared.exporting import load_export_schemes

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({settings_module.FILE_NAMING_SCHEME_KEY: VALID_FILE_SCHEME}),
        encoding="utf-8-sig",
    )

    schemes = load_export_schemes(str(path))

    assert schemes.file_scheme == VALID_FILE_SCHEME


def read_saved_settings(tmp_path):
    """Read the temporary settings file after a save operation."""
    return json.loads((Path(tmp_path) / "settings.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Pure folder validation
# ---------------------------------------------------------------------------

def test_folder_scheme_error_uses_production_compiler():
    assert settings_module.folder_scheme_error(DEFAULT_FOLDER_SCHEME) is None

    error = settings_module.folder_scheme_error("{network}/{type}")

    assert error is not None
    assert "missing required top-level tags" in error


# ---------------------------------------------------------------------------
# Loading and preview behavior
# ---------------------------------------------------------------------------

def test_fresh_install_loads_and_saves_both_defaults(window_factory, tmp_path):
    window = window_factory()

    assert window.ui.lineEditFileScheme.text() == VALID_FILE_SCHEME
    assert window.ui.lineEditFolderScheme.text() == DEFAULT_FOLDER_SCHEME
    assert window.save_settings()

    saved = read_saved_settings(tmp_path)
    assert saved[settings_module.FILE_NAMING_SCHEME_KEY] == VALID_FILE_SCHEME
    assert saved[settings_module.FOLDER_ORGANIZATION_SCHEME_KEY] == DEFAULT_FOLDER_SCHEME


def test_missing_folder_key_uses_default_and_renders_preview(window_factory):
    window = window_factory(base_settings())

    assert window.ui.lineEditFolderScheme.text() == DEFAULT_FOLDER_SCHEME
    assert window.ui.lineEditFolderPreview.text() == (
        "Cartoon Network/Blocks/Toonami/Promo/2000s/Kids/"
    )


def test_folder_preview_uses_production_errors_and_success_state(window_factory):
    window = window_factory(base_settings())
    preview = window.ui.lineEditFolderPreview

    window.ui.lineEditFolderScheme.setText("{network}/{type}")
    assert "missing required top-level tags" in preview.text()
    assert settings_module.PREVIEW_ERROR_STYLE in preview.styleSheet()

    window.ui.lineEditFolderScheme.setText(DEFAULT_FOLDER_SCHEME)
    assert preview.text().endswith("Kids/")
    assert preview.styleSheet() == ""


def test_folder_help_matches_the_shared_tag_set_and_restrictions(window_factory):
    window = window_factory(base_settings())
    help_text = window.ui.textBrowserFolderScheme.toPlainText()

    assert "{title}" in help_text
    assert "{year}" in help_text
    assert "{info}" in help_text
    assert "nested groups" in help_text
    assert "OR groups" in help_text


def test_file_help_documents_the_syntax_and_the_title_requirement(window_factory):
    """The file scheme help is the in-app documentation, so it must name every
    construct the parser actually supports and the rule it enforces.

    Assertions match on the construct, not on one exact notation, so rewording
    the help does not fail the suite but dropping a construct does.
    """
    window = window_factory(base_settings())
    help_text = window.ui.textBrowserFileScheme.toPlainText()

    # The shared tag set.
    assert "{title}" in help_text
    assert "{year}" in help_text
    assert "{info}" in help_text
    # Fallback, optional AND group, OR group, escaping, and nesting.
    assert "{a,b}" in help_text
    assert "[{block} - ]" in help_text
    assert "AND" in help_text
    assert "OR group" in help_text
    # Some bracketed pipe expression demonstrates the OR group, whichever
    # placeholder names it happens to use.
    assert re.search(r"\[[^\]]*\|[^\]]*\]", help_text), "no OR group is shown"
    assert "nest" in help_text
    assert "backslash" in help_text
    # The rule the strict profile enforces.
    assert "outside any brackets" in help_text


def test_file_help_examples_behave_as_documented(window_factory, monkeypatch):
    """Every construct the help names must actually do what the help says."""
    window = window_factory(base_settings())
    preview = window.ui.lineEditPreview
    base_tags = dict(settings_module.PREVIEW_TAGS)

    def rendered(scheme, **overrides):
        monkeypatch.setattr(
            settings_module, "PREVIEW_TAGS", {**base_tags, **overrides})
        window.ui.lineEditFileScheme.setText(scheme)
        # setText on an unchanged value does not re-emit textChanged, so drive
        # the real preview path explicitly to keep every case independent.
        window._update_file_preview()
        assert preview.styleSheet() == "", f"{scheme} was rejected by the preview"
        return preview.text()

    # {a,b} takes the first tag that is set, then the next one.
    assert rendered("{title} [{year,time_period}]").endswith("2000")
    assert rendered("{title} [{year,time_period}]", year="").endswith("2000s")
    assert rendered("{title} [{year,time_period}]", year="", time_period="") \
        == base_tags["title"]
    # An optional section drops, separator and all, when its tag is empty.
    assert rendered("{title} [{block} - ]").endswith("Toonami -")
    assert rendered("{title} [{block} - ]", block="") == base_tags["title"]
    # [a|b] joins the tags that are set, and drops the group when none are.
    assert rendered(
        "{title} [{length}|{information}]",
    ).endswith("30 Seconds Remastered")
    assert rendered(
        "{title} [{length}|{information}]", information="",
    ).endswith("30 Seconds")
    assert rendered(
        "{title} [{length}|{information}]", length="", information="",
    ) == base_tags["title"]
    # A backslash writes the next character literally.
    assert rendered(r"{title} \[x\]").endswith("[x]")


def test_file_help_states_the_title_rule_the_compiler_enforces(window_factory):
    """The help says {title} must be top-level and alone; prove that is so."""
    window = window_factory(base_settings())
    help_text = window.ui.textBrowserFileScheme.toPlainText()
    assert "outside any brackets" in help_text

    assert file_scheme_error("{title}") is None

    # Neither a bracketed title nor a fallback that merely contains one counts.
    assert file_scheme_error("[{title}]") is not None
    assert file_scheme_error("{year,title}") is not None


def test_help_panels_lay_out_and_can_scroll(window_factory):
    """The help panels are the in-app documentation, so each must lay out at
    the window's minimum width and stay readable when its text is taller than
    the panel. The window is intentionally compact, so scrolling is expected;
    this guards against a panel collapsing instead."""
    window = window_factory(base_settings())
    window.show()
    window.resize(window.minimumSize())
    QApplication.instance().processEvents()

    for name in ("textBrowserFileScheme", "textBrowserFolderScheme"):
        browser = getattr(window.ui, name)
        document_height = browser.document().size().height()
        assert document_height > 0, f"{name} did not lay out"
        assert browser.viewport().height() > 0, f"{name} has no visible area"
        assert browser.minimumHeight() >= 100, f"{name} is too small to read"
        if document_height > browser.viewport().height():
            # QTextBrowser must be able to reach the rest of its own text.
            assert browser.verticalScrollBar().maximum() > 0, (
                f"{name} overflows by "
                f"{document_height - browser.viewport().height():.0f}px "
                "but cannot scroll"
            )


def test_malformed_stored_folder_scheme_disables_only_folder_editor(
    window_factory,
    monkeypatch,
):
    messages = warning_recorder(monkeypatch)
    window = window_factory(base_settings(
        **{settings_module.FOLDER_ORGANIZATION_SCHEME_KEY: "{network}/{type}"}
    ))

    assert window.ui.lineEditFileScheme.isEnabled()
    assert not window.ui.lineEditFolderScheme.isEnabled()
    assert window.ui.lineEditFolderPreview.text() == ""
    assert messages


# ---------------------------------------------------------------------------
# Saving and cancel behavior
# ---------------------------------------------------------------------------

def test_save_persists_default_folder_key_when_missing(window_factory, tmp_path):
    window = window_factory(base_settings())

    assert window.save_settings()

    saved = read_saved_settings(tmp_path)
    assert saved[settings_module.FILE_NAMING_SCHEME_KEY] == VALID_FILE_SCHEME
    assert saved[settings_module.FOLDER_ORGANIZATION_SCHEME_KEY] == DEFAULT_FOLDER_SCHEME


def test_save_updates_both_schemes_and_preserves_unrelated_settings(
    window_factory,
    tmp_path,
):
    window = window_factory(base_settings(unrelated="keep me"))
    custom_file_scheme = "{title} - {network}"
    custom_folder_scheme = "{network}/{type}/{time_period}"

    window.ui.lineEditFileScheme.setText(custom_file_scheme)
    window.ui.lineEditFolderScheme.setText(custom_folder_scheme)

    assert window.save_settings()

    saved = read_saved_settings(tmp_path)
    assert saved[settings_module.FILE_NAMING_SCHEME_KEY] == custom_file_scheme
    assert saved[settings_module.FOLDER_ORGANIZATION_SCHEME_KEY] == custom_folder_scheme
    assert saved["unrelated"] == "keep me"


def test_unchanged_invalid_legacy_file_value_does_not_block_folder_save(
    window_factory,
    tmp_path,
):
    stored_file_scheme = "invalid legacy scheme"
    window = window_factory({
        settings_module.FILE_NAMING_SCHEME_KEY: stored_file_scheme,
        settings_module.FOLDER_ORGANIZATION_SCHEME_KEY: DEFAULT_FOLDER_SCHEME,
    })
    custom_folder_scheme = "{network}/{type}/{time_period}"

    window.ui.lineEditFolderScheme.setText(custom_folder_scheme)

    assert window.save_settings()

    saved = read_saved_settings(tmp_path)
    assert saved[settings_module.FILE_NAMING_SCHEME_KEY] == stored_file_scheme
    assert saved[settings_module.FOLDER_ORGANIZATION_SCHEME_KEY] == custom_folder_scheme


def test_invalid_folder_scheme_blocks_the_atomic_save(window_factory, tmp_path, monkeypatch):
    messages = warning_recorder(monkeypatch)
    window = window_factory(base_settings())

    window.ui.lineEditFileScheme.setText("{title} changed")
    window.ui.lineEditFolderScheme.setText("{network}/{type}")

    assert not window.save_settings()

    saved = read_saved_settings(tmp_path)
    assert saved[settings_module.FILE_NAMING_SCHEME_KEY] == VALID_FILE_SCHEME
    assert settings_module.FOLDER_ORGANIZATION_SCHEME_KEY not in saved
    assert messages[0][0] == "Invalid folder organization scheme"


def test_cancel_restores_both_saved_schemes(window_factory):
    window = window_factory(base_settings())
    custom_folder_scheme = "{network}/{type}/{time_period}"

    window.ui.lineEditFileScheme.setText("{title} changed")
    window.ui.lineEditFolderScheme.setText(custom_folder_scheme)
    window.reject_changes()

    assert window.ui.lineEditFileScheme.text() == VALID_FILE_SCHEME
    assert window.ui.lineEditFolderScheme.text() == DEFAULT_FOLDER_SCHEME


def test_window_manager_close_restores_unsaved_schemes(window_factory):
    window = window_factory(base_settings())
    custom_folder_scheme = "{network}/{type}/{time_period}"

    window.ui.lineEditFileScheme.setText("{title} changed")
    window.ui.lineEditFolderScheme.setText(custom_folder_scheme)
    window.close()

    assert window.ui.lineEditFileScheme.text() == VALID_FILE_SCHEME
    assert window.ui.lineEditFolderScheme.text() == DEFAULT_FOLDER_SCHEME


# ---------------------------------------------------------------------------
# The export folder
# ---------------------------------------------------------------------------

def warning_recorder(monkeypatch):
    """Replace QMessageBox with one that records what the window shows.

    Returns the list of `(title, message)` pairs. A class with a staticmethod
    rather than an instance, because the window calls `QMessageBox.warning(...)`
    on whatever the name refers to.
    """
    recorded = []

    class Recorder:
        @staticmethod
        def warning(parent, title, message):
            recorded.append((title, message))

    monkeypatch.setattr(settings_module, "QMessageBox", Recorder)
    return recorded


def test_the_import_folder_has_no_row_and_the_export_folder_does(window_factory):
    """The import folder is not configurable at all -- commcut moves and deletes
    from it, so it stays inside the program root -- and the export folder is.

    Both halves are asserted together because the mistake is symmetric: leaving
    a disabled import row in place reads as "coming soon", and a re-enabled
    export row in the .ui alone would imply a setting that nothing saves.
    """
    window = window_factory(base_settings())

    for gone in ("lineEditImport", "fileBrowseImport", "labelImport"):
        assert not hasattr(window.ui, gone), gone
    assert window.ui.labelExport.text() == "Export Folder"
    assert window.ui.lineEditExport.isEnabled()
    assert not window.ui.lineEditExport.isReadOnly()
    assert window.ui.fileBrowseExport.isEnabled()


def test_the_picker_is_a_native_folder_dialog(qapp, monkeypatch, tmp_path):
    """Native for the same reason as the source-video picker: the OS dialog is
    better and it remembers the folder the user was last in. And a *folder*
    picker -- `ShowDirsOnly` is what stops it offering files as answers, which
    would then have to be refused by the save for no reason.

    Recorded in `exec`, not `__init`: everything the caller configures happens
    between the two, so reading it at construction would assert the defaults.
    """
    from PySide6.QtWidgets import QFileDialog

    recorded = {}

    class RecordingDialog(QFileDialog):
        def exec(self):
            recorded["options"] = self.options()
            recorded["file_mode"] = self.fileMode()
            return 0

    monkeypatch.setattr(settings_module, "QFileDialog", RecordingDialog)

    assert settings_module.choose_export_folder() is None
    assert recorded["file_mode"] == QFileDialog.Directory
    assert recorded["options"] & QFileDialog.ShowDirsOnly
    assert not recorded["options"] & QFileDialog.DontUseNativeDialog


def test_the_picker_opens_in_the_folder_already_chosen(qapp, monkeypatch, tmp_path):
    """Not a global state store -- it is the value being edited, handed in by
    the caller -- so opening where the user already is costs nothing.

    Recorded as the call rather than read back off the widget: `setDirectory`
    is the contract, and the resolved path a QFileDialog reports before it is
    shown depends on the platform dialog behind it.
    """
    from PySide6.QtWidgets import QFileDialog

    chosen = tmp_path / "clips"
    chosen.mkdir()
    recorded = {}

    class RecordingDialog(QFileDialog):
        def setDirectory(self, path):
            recorded["start_at"] = path

        def exec(self):
            return 0

    monkeypatch.setattr(settings_module, "QFileDialog", RecordingDialog)

    settings_module.choose_export_folder(start_at=str(chosen))

    assert recorded["start_at"] == str(chosen)


def test_a_start_folder_that_is_not_there_is_ignored(qapp, monkeypatch, tmp_path):
    """The export folder may name a drive that is not mounted, or a folder the
    user has since deleted. Passing that to the dialog would open it on the
    wrong folder, which is worse than opening on the platform's own idea of
    where it was."""
    from PySide6.QtWidgets import QFileDialog

    recorded = {}

    class RecordingDialog(QFileDialog):
        def setDirectory(self, path):
            recorded["start_at"] = path

        def exec(self):
            return 0

    monkeypatch.setattr(settings_module, "QFileDialog", RecordingDialog)

    assert settings_module.choose_export_folder(
        start_at=str(tmp_path / "unplugged")) is None
    assert "start_at" not in recorded


def test_a_cancelled_picker_changes_nothing(window_factory, monkeypatch, tmp_path):
    window = window_factory(base_settings(export_folder=str(tmp_path / "clips")))
    monkeypatch.setattr(
        settings_module, "choose_export_folder", lambda *a, **k: None)

    window.browse_for_export_folder()

    assert window.ui.lineEditExport.text() == str(tmp_path / "clips")


def test_browse_puts_the_chosen_folder_in_the_field(window_factory, monkeypatch,
                                                    tmp_path):
    chosen = tmp_path / "clips"
    chosen.mkdir()
    window = window_factory(base_settings())
    monkeypatch.setattr(
        settings_module, "choose_export_folder", lambda *a, **k: str(chosen))

    window.browse_for_export_folder()

    assert window.ui.lineEditExport.text() == str(chosen)


def test_the_chosen_folder_is_saved_and_the_resolver_agrees(window_factory,
                                                            tmp_path):
    """The window's save and `export_folder()` are separate code paths reading
    the same file, so the assertion is that they land on one answer rather than
    that the window wrote something."""
    chosen = tmp_path / "clips"
    chosen.mkdir()
    window = window_factory(base_settings())

    window.ui.lineEditExport.setText(str(chosen))
    assert window.save_settings()

    saved = read_saved_settings(tmp_path)
    assert saved[settings_module.EXPORT_FOLDER_KEY] == str(chosen)
    assert exporting.export_folder() == str(chosen)


def test_the_stored_path_is_canonicalized_on_save(window_factory, tmp_path):
    """One spelling in the file, so two settings files cannot claim different
    things about the same folder."""
    chosen = tmp_path / "clips"
    chosen.mkdir()
    window = window_factory(base_settings())

    window.ui.lineEditExport.setText(str(chosen) + os.sep + "." + os.sep)
    assert window.save_settings()

    stored = read_saved_settings(tmp_path)[settings_module.EXPORT_FOLDER_KEY]
    assert stored == str(chosen)


def test_clearing_the_field_removes_the_key(window_factory, tmp_path):
    """Stored as an absent key rather than an empty string, so the file reads
    the same way to a hand editor as it does to this app: nothing set is the
    default."""
    chosen = tmp_path / "clips"
    chosen.mkdir()
    window = window_factory(base_settings(export_folder=str(chosen)))

    window.ui.lineEditExport.setText("")
    assert window.save_settings()

    assert settings_module.EXPORT_FOLDER_KEY not in read_saved_settings(tmp_path)
    assert exporting.export_folder() == exporting.default_export_folder()


def test_an_absent_key_shows_as_an_empty_field(window_factory):
    window = window_factory(base_settings())

    assert window.ui.lineEditExport.text() == ""


@pytest.mark.parametrize("stored", ["relative/clips", "clips"])
def test_a_relative_path_blocks_the_atomic_save(window_factory, tmp_path,
                                                monkeypatch, stored):
    messages = warning_recorder(monkeypatch)
    window = window_factory(base_settings())
    window.ui.lineEditExport.setText(stored)
    window.ui.lineEditFileScheme.setText("{title} changed")

    assert not window.save_settings()

    saved = read_saved_settings(tmp_path)
    assert settings_module.EXPORT_FOLDER_KEY not in saved
    assert saved[settings_module.FILE_NAMING_SCHEME_KEY] == VALID_FILE_SCHEME
    assert messages[0][0] == "Invalid export folder"


def test_the_export_folder_cannot_be_the_import_folder(window_factory, tmp_path,
                                                       monkeypatch):
    """The one rule that protects the user's files: commcut deletes from
    `import/`, so an export root overlapping it would write into the one folder
    the importer is allowed to empty."""
    messages = warning_recorder(monkeypatch)
    window = window_factory(base_settings())
    window.ui.lineEditExport.setText(str(tmp_path / "import"))

    assert not window.save_settings()
    assert settings_module.EXPORT_FOLDER_KEY not in read_saved_settings(tmp_path)
    assert "import folder" in messages[0][1]


def test_a_hand_edited_bad_value_is_shown_rather_than_hidden(window_factory):
    """The user has to be able to see what a hand edit did and fix it here,
    rather than being told to go and open a text editor again."""
    window = window_factory(base_settings(export_folder="clips"))

    assert window.ui.lineEditExport.text() == "clips"
    assert window.ui.lineEditExport.isEnabled()


def test_an_untouched_bad_value_does_not_block_the_scheme_save(window_factory,
                                                                tmp_path,
                                                                monkeypatch):
    """The rule the scheme fields already follow: only a *user-edited* field
    blocks the save, and an unchanged value is preserved even when validation
    would now reject it. A folder left over from a stricter past must not make
    the naming schemes unsavable -- which is the only way out of it for a user
    who has never heard of this setting."""
    warning_recorder(monkeypatch)
    window = window_factory(base_settings(export_folder="clips"))

    window.ui.lineEditFileScheme.setText("{title} changed")
    assert window.save_settings()

    saved = read_saved_settings(tmp_path)
    assert saved[settings_module.FILE_NAMING_SCHEME_KEY] == "{title} changed"
    assert saved[settings_module.EXPORT_FOLDER_KEY] == "clips"


def test_editing_away_from_a_bad_value_saves_it(window_factory, tmp_path,
                                                monkeypatch):
    """Once the field is touched, the invalid value is the user's problem to
    fix rather than legacy to preserve -- which is what makes the previous test
    a dead end rather than a trap."""
    warning_recorder(monkeypatch)
    chosen = tmp_path / "clips"
    chosen.mkdir()
    window = window_factory(base_settings(export_folder="clips"))

    window.ui.lineEditExport.setText(str(chosen))
    assert window.save_settings()

    assert read_saved_settings(tmp_path)[
        settings_module.EXPORT_FOLDER_KEY] == str(chosen)
    assert exporting.export_folder() == str(chosen)


def test_editing_a_bad_value_to_something_else_invalid_blocks_the_save(
        window_factory, tmp_path, monkeypatch):
    messages = warning_recorder(monkeypatch)
    window = window_factory(base_settings(export_folder="clips"))

    window.ui.lineEditExport.setText("somewhere/else")

    assert not window.save_settings()
    assert [title for title, _ in messages] == ["Invalid export folder"]
    assert read_saved_settings(tmp_path)[
        settings_module.EXPORT_FOLDER_KEY] == "clips"


def test_a_value_that_is_not_a_string_disables_the_field(window_factory, tmp_path,
                                                          monkeypatch):
    """There is nothing to put in a text box, so this is the scheme fields'
    treatment rather than the previous test's -- and the stored value is left
    alone for the user to repair by hand."""
    warnings = warning_recorder(monkeypatch)
    window = window_factory(base_settings(export_folder=42))

    assert not window.ui.lineEditExport.isEnabled()
    assert not window.ui.fileBrowseExport.isEnabled()
    assert warnings[0][1] == f"{settings_module.EXPORT_FOLDER_KEY} must be a string"
    assert read_saved_settings(tmp_path)[settings_module.EXPORT_FOLDER_KEY] == 42


def test_cancel_restores_the_export_folder(window_factory, tmp_path):
    chosen = tmp_path / "clips"
    chosen.mkdir()
    window = window_factory(base_settings(export_folder=str(chosen)))

    window.ui.lineEditExport.setText(str(tmp_path / "somewhere else"))
    window.reject_changes()

    assert window.ui.lineEditExport.text() == str(chosen)


def test_window_manager_close_restores_the_export_folder(window_factory, tmp_path):
    chosen = tmp_path / "clips"
    chosen.mkdir()
    window = window_factory(base_settings(export_folder=str(chosen)))

    window.ui.lineEditExport.setText(str(tmp_path / "somewhere else"))
    window.close()

    assert window.ui.lineEditExport.text() == str(chosen)


def test_an_unusable_export_folder_is_reported_rather_than_raised(window_factory,
                                                                 monkeypatch):
    """The sync button is a click handler with nothing above it catching, so a
    hand-edited value that the editor refuses has to be reported here too rather
    than taken into the event loop.

    The window is built with the bad value already stored, which is also how it
    happens in the wild -- and it means the field comes up disabled, so this is
    asserting the button is independent of the field rather than of the window."""
    warnings = warning_recorder(monkeypatch)
    window = window_factory(base_settings(export_folder=42))

    assert window.start_vocabulary_sync() is None
    reported = [w for w in warnings if w[0] == "Export folder is not usable"]
    assert len(reported) == 1
    assert settings_module.EXPORT_FOLDER_KEY in reported[0][1]


# ---------------------------------------------------------------------------
# The tag vocabulary sync
# ---------------------------------------------------------------------------

def make_library(root, clips):
    """A temporary export library. `clips` maps a relative stem to its tags."""
    for stem, tags in clips.items():
        directory = Path(root) / "Cartoon Network" / "Promo"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.mp4").write_bytes(b"video")
        write_record(str(directory / f"{stem}.cnfo"), ClipRecord(
            source="compilation.mp4", segment_index=0, start=0.0, duration=1.0,
            tags=tuple(tags),
        ))
    return str(root)


def library_clip(tags=("network", "Cartoon Network"), stem="Worlds Finest"):
    return {stem: (tags,)}


class FakeSignal:
    """The one thing the shipped wiring needs of a dialog's `canceled`."""

    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def fire(self):
        for slot in self._slots:
            slot()


class FakeProgressDialog:
    """The QProgressDialog surface `_on_sync_advanced` and the teardown drive."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.title = ""
        self.label = ""
        self.parent = kwargs.get("parent")
        self.modal = None
        self.auto_close = None
        self.auto_reset = None
        self.minimum_duration = None
        self.shown = False
        self.deleted = False
        self.canceled = FakeSignal()
        FakeProgressDialog.instances.append(self)

    def setWindowTitle(self, title):
        self.title = title

    def setLabelText(self, text):
        self.label = text

    def setParent(self, parent):
        self.parent = parent

    def setModal(self, modal):
        self.modal = modal

    def setMinimumDuration(self, duration):
        self.minimum_duration = duration

    def setAutoClose(self, auto):
        self.auto_close = auto

    def setAutoReset(self, auto):
        self.auto_reset = auto

    def show(self):
        self.shown = True

    def deleteLater(self):
        self.deleted = True


class FakeThread(QThread):
    """A real QThread that never actually starts, so the wiring is exercised in
    order without waiting on an event loop.

    A subclass rather than a stand-in because the shipped code hands one to
    `QObject.moveToThread`, which type-checks it -- the same reason
    `tests/editor_stub.py:FakeThread` is shaped this way. `start()` is overridden
    so no thread is created and `finished` is emitted by hand, in the order a real
    one would. Parked by default, so a test can inspect the window while a sync is
    nominally in flight; `start_sync(..., run=True)` drives it to completion.
    """

    instances = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.quit_calls = 0
        self.deleted = False
        self.start_calls = 0
        self.auto_run = False
        FakeThread.instances.append(self)

    def start(self):
        self.start_calls += 1
        if not self.auto_run:
            return
        self.started.emit()
        # What the real thread does once its event loop ends.
        self.finished.emit()

    def quit(self):
        self.quit_calls += 1

    def deleteLater(self):
        self.deleted = True


class FakeSyncWorker(QObject):
    """The real `SyncWorker`, with the one thing a fake thread cannot give it.

    `start_vocabulary_sync` calls `moveToThread` on the worker, and Qt refuses a
    move onto a QThread that has no running event loop -- which leaves the worker
    bound to the fake thread's, so every signal it emits is *queued* to an event
    loop that never runs and the window's slots never fire. `tests/editor_stub.py`
    hits the same thing and solves it the same way: keep the real worker and its
    real signals, and stub only the affinity.
    """

    instances = []

    def __init__(self, root, vocabulary, cancel_event=None, pending_root=None):
        super().__init__()
        self._real = RealSyncWorker(
            root, vocabulary, cancel_event=cancel_event,
            pending_root=pending_root)
        self.cancel_event = self._real.cancel_event
        self.advanced = self._real.advanced
        self.finished = self._real.finished
        self.deleted = False
        FakeSyncWorker.instances.append(self)

    def moveToThread(self, thread):
        self.thread = thread

    def run(self):
        self._real.run()

    def deleteLater(self):
        self.deleted = True


class RecordedDialog:
    """Stands in for `VocabularySyncDialog` so `exec()` does not block."""

    instances = []

    def __init__(self, result, parent=None):
        self.result = result
        self.executed = False
        self.deleted = False
        RecordedDialog.instances.append(self)

    def exec(self):
        self.executed = True

    def deleteLater(self):
        self.deleted = True

    def summary_text(self):
        if isinstance(self.result, Exception):
            return f"{type(self.result).__name__}: {self.result}"
        return vocabulary_sync_summary(self.result)


@pytest.fixture
def sync_harness(qapp, window_factory, monkeypatch, tmp_path):
    """A Settings window whose sync runs against a temporary library and a
    temporary vocabulary file, with the thread parked so a test chooses when the
    run happens.

    `export_folder()` resolves through the install root rather than
    `PROJECT_ROOT`, so the library is handed to `start_vocabulary_sync` explicitly
    rather than redirected -- which is why it takes a root argument.

    `QMessageBox` is answered rather than shown. A parked run leaves a live
    thread, so `window_factory`'s teardown close reaches `closeEvent`'s
    "a sync is running" question, and a real modal box under the offscreen
    platform blocks forever. Answered No by default; the close tests re-patch it
    with their own answer.
    """
    library = tmp_path / "library"
    vocabulary_file = tmp_path / "vocabulary.json"
    monkeypatch.setattr(settings_module, "vocabulary_path",
                        lambda: str(vocabulary_file))
    monkeypatch.setattr(settings_module, "export_folder",
                        lambda: str(library))
    monkeypatch.setattr(settings_module, "import_folder",
                        lambda: str(tmp_path / "import"))
    monkeypatch.setattr(settings_module, "QThread", FakeThread)
    monkeypatch.setattr(settings_module, "SyncWorker", FakeSyncWorker)
    monkeypatch.setattr(settings_module, "QProgressDialog", FakeProgressDialog)
    monkeypatch.setattr(settings_module, "VocabularySyncDialog", RecordedDialog)

    class MessageBox:
        Yes = 1
        No = 2

        @staticmethod
        def question(*_args, **_kwargs):
            return MessageBox.No

        @staticmethod
        def warning(*_args, **_kwargs):
            return MessageBox.No

    monkeypatch.setattr(settings_module, "QMessageBox", MessageBox)
    FakeProgressDialog.instances = []
    FakeThread.instances = []
    FakeSyncWorker.instances = []
    RecordedDialog.instances = []
    window = window_factory(base_settings())
    yield window, library, vocabulary_file

    # Drain any run a test left parked, so teardown closes a window that is not
    # mid-sync. This goes through the real teardown rather than forcing the
    # state, so a test that broke it would fail here too.
    if window._sync_thread is not None:
        window._sync_thread.auto_run = True
        window._sync_thread.start()
        qapp.processEvents()


def start_sync(window, root, run=False):
    """Press the button and optionally drive the parked run to completion."""
    worker = window.start_vocabulary_sync(root)
    thread = window._sync_thread
    if run:
        thread.auto_run = True
        thread.start()
    QApplication.instance().processEvents()
    return worker


def last_dialog():
    return RecordedDialog.instances[-1]


def make_library(root, clips):
    """A temporary export library. `clips` maps a relative stem to its tags."""
    for stem, tags in clips.items():
        directory = Path(root) / "Cartoon Network" / "Promo"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.mp4").write_bytes(b"video")
        write_record(str(directory / f"{stem}.cnfo"), ClipRecord(
            source="compilation.mp4", segment_index=0, start=0.0, duration=1.0,
            tags=tuple(tags),
        ))
    return str(root)


def library_clip(tags=("network", "Cartoon Network"), stem="Worlds Finest"):
    return {stem: (tags,)}


def test_the_sync_button_reads_the_library_and_reconciles_the_vocabulary(
    sync_harness,
):
    window, library, vocabulary_file = sync_harness
    make_library(library, library_clip(stem="Kept"))

    start_sync(window, library, run=True)

    result = last_dialog().result
    assert result.clips_found == 1
    assert Vocabulary.load(str(vocabulary_file)).values("network") == (
        "Cartoon Network",)
    assert "Read 1 clip(s)" in last_dialog().summary_text()


def test_the_sync_drops_a_value_no_clip_uses_and_says_which(sync_harness):
    window, library, vocabulary_file = sync_harness
    make_library(library, library_clip())
    # Seeded rather than defaulted: a library holding one clip prunes the twelve
    # shipped `filler_type` values along with the stale one, which is its own
    # test and would drown this one.
    seeded = Vocabulary(path=str(vocabulary_file))
    seeded.record({"network": "Cartoon Network", "block": "Toonami"})
    seeded.save()

    start_sync(window, library, run=True)

    body = last_dialog().summary_text()
    assert "Removed 1 value(s) no clip uses" in body
    assert "block: Toonami" in body
    assert Vocabulary.load(str(vocabulary_file)).values("block") == ()


def test_the_sync_keeps_the_shipped_defaults_a_library_does_not_use(sync_harness):
    """A library of one clip is not evidence that the other ten filler types are
    unused. This is the button's own first-run experience, so it is asserted
    through the window rather than only on the library function."""
    window, library, vocabulary_file = sync_harness
    make_library(library, {
        "Only Bumper": (("filler_type", "Bumper"),),
    })

    start_sync(window, library, run=True)

    body = last_dialog().summary_text()
    assert "No values were removed." in body
    # Said anyway, because "no values were removed" alone reads as the sync
    # having found nothing to do, when the truth is that the prune was stopped.
    assert "were kept anyway, because they are shipped defaults" in body
    assert "filler_type: Promo" in body
    assert Vocabulary.load(str(vocabulary_file)).values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"]))


def test_the_sync_still_removes_a_stale_value_and_keeps_the_defaults(sync_harness):
    window, library, vocabulary_file = sync_harness
    make_library(library, {
        "Only Bumper": (("filler_type", "Bumper"),),
    })
    seeded = Vocabulary(path=str(vocabulary_file))
    seeded.record({"filler_type": "Bumper", "block": "Toonami"})
    seeded.save()

    start_sync(window, library, run=True)

    body = last_dialog().summary_text()
    assert "Removed 1 value(s) no clip uses" in body
    assert "block: Toonami" in body
    reloaded = Vocabulary.load(str(vocabulary_file))
    assert reloaded.values("block") == ()
    assert reloaded.values("filler_type") == ("Bumper",)


def test_the_sync_reaches_the_process_wide_cache_a_later_editor_reads(
    sync_harness,
):
    """The dropdowns come from `get_vocabulary()`, cached per path for the whole
    process. Syncing into a private copy would write a correct file and leave the
    cache stale, so an editor opened later in the same session would offer the
    old values."""
    window, library, vocabulary_file = sync_harness
    make_library(library, library_clip())

    start_sync(window, library, run=True)

    assert get_vocabulary(str(vocabulary_file)).values("network") == (
        "Cartoon Network",)


def test_the_sync_names_every_record_it_could_not_read(sync_harness):
    """A silently skipped record is a clip the user could never find again."""
    window, library, _ = sync_harness
    make_library(library, {
        "Good": (("network", "Cartoon Network"),),
        "Bad": (("network", "Nickelodeon"),),
    })
    (Path(library) / "Cartoon Network" / "Promo" / "Bad.cnfo").write_text(
        "not xml", encoding="utf-8")

    start_sync(window, library, run=True)

    body = last_dialog().summary_text()
    assert "1 record(s) could not be read" in body
    assert "Bad.cnfo" in body


def test_a_failed_sync_is_reported_rather_than_raised(sync_harness, monkeypatch):
    """An exception escaping a QThread slot reaches PySide6's abort path, so the
    worker turns it into a result and the window reports it."""
    window, library, _ = sync_harness

    def explode(*_args, **_kwargs):
        raise OSError("the export root is on a share that went away")

    monkeypatch.setattr(settings_module, "sync_vocabulary", explode)

    start_sync(window, library, run=True)

    assert isinstance(last_dialog().result, OSError)
    assert "on a share that went away" in last_dialog().summary_text()
    assert window.isEnabled()


def test_a_cancelled_sync_writes_nothing_and_says_so(sync_harness):
    """End to end through the worker: the cancel event is the one the dialog's
    Cancel button sets, and `sync_vocabulary` writes nothing when the walk is
    cancelled."""
    window, library, vocabulary_file = sync_harness
    make_library(library, library_clip())
    kept = get_vocabulary(str(vocabulary_file))
    kept.record({"block": "Toonami"})
    kept.save()

    worker = start_sync(window, library)
    worker.cancel_event.set()
    window._sync_thread.auto_run = True
    window._sync_thread.start()
    QApplication.instance().processEvents()

    body = last_dialog().summary_text()
    assert "Cancelled" in body
    assert "Nothing was written" in body
    assert Vocabulary.load(str(vocabulary_file)).values("block") == ("Toonami",)


def test_an_empty_library_leaves_the_shipped_defaults_alone(sync_harness):
    """A fresh install's `export/` is empty by design. Pruning it would delete
    the seeded filler_type list and leave every dropdown on its
    "Populate this list by staging tags" placeholder."""
    window, library, vocabulary_file = sync_harness

    start_sync(window, library, run=True)

    assert "No clips were found" in last_dialog().summary_text()
    assert Vocabulary.load(str(vocabulary_file)).values("filler_type") == tuple(
        sorted(DEFAULT_VALUES["filler_type"]))


def test_the_window_is_disabled_while_a_sync_is_in_flight(sync_harness):
    """The worker holds the live cached vocabulary, so an edit made mid-run would
    silently not reach the file."""
    window, library, _ = sync_harness
    make_library(library, library_clip())

    start_sync(window, library)

    assert not window.isEnabled()


def test_the_window_is_enabled_again_once_the_run_is_over(sync_harness):
    window, library, _ = sync_harness
    make_library(library, library_clip())

    start_sync(window, library, run=True)

    assert window.isEnabled()


def test_the_run_is_torn_down_and_the_dialog_released(sync_harness):
    window, library, _ = sync_harness
    make_library(library, library_clip())

    start_sync(window, library, run=True)

    assert window._sync_thread is None
    assert window._sync_worker is None
    assert window._sync_dialog is None
    assert FakeThread.instances[-1].deleted is True
    assert FakeProgressDialog.instances[-1].deleted is True
    assert last_dialog().deleted is True


def test_pressing_the_button_twice_does_not_start_two_runs(sync_harness):
    window, library, _ = sync_harness
    make_library(library, library_clip())

    start_sync(window, library)

    assert window.start_vocabulary_sync(library) is None
    assert len(FakeThread.instances) == 1


def test_the_progress_dialog_is_parentless_and_does_not_auto_close(sync_harness):
    """Parentless so `setEnabled(False)` on the window does not disable the
    Cancel button, non-modal so the window's own close button still reaches
    closeEvent, and autoClose off because an indeterminate dialog that reached its
    maximum would read as a cancel of a sync with nothing to cancel."""
    window, library, _ = sync_harness
    make_library(library, library_clip())

    start_sync(window, library)

    dialog = FakeProgressDialog.instances[-1]
    assert dialog.parent is None
    assert dialog.modal is False
    assert dialog.auto_close is False
    assert dialog.auto_reset is False
    assert dialog.shown is True


def test_progress_is_shown_without_a_total(sync_harness):
    window, library, _ = sync_harness
    make_library(library, {
        "First": (("network", "Cartoon Network"),),
        "Second": (("network", "Nickelodeon"),),
    })

    start_sync(window, library, run=True)

    dialog = FakeProgressDialog.instances[-1]
    assert "Read 1 clip(s)" in dialog.label
    assert "Second.cnfo" in dialog.label


def test_closing_mid_run_asks_first_and_defers_the_close(sync_harness, monkeypatch):
    """Destroying a QThread that is still running aborts the process, so there is
    no path that lets this window go first: the close is refused, the walk is
    cancelled, and the window closes from the thread's own `finished`."""
    window, library, _ = sync_harness
    make_library(library, library_clip())
    questions = []

    class MessageBox:
        Yes = 1
        No = 2

        @staticmethod
        def question(_parent, title, message, *_buttons, default=0):
            questions.append((title, message))
            return MessageBox.Yes

    monkeypatch.setattr(settings_module, "QMessageBox", MessageBox)

    start_sync(window, library)
    window.close()

    assert questions and "running" in questions[0][0]
    assert window._sync_cancel.is_set()
    assert window._sync_close_after is True
    assert window._sync_thread is not None

    window._sync_thread.auto_run = True
    window._sync_thread.start()
    QApplication.instance().processEvents()

    assert window._sync_thread is None
    assert window._sync_close_after is False


def test_a_close_refused_mid_run_leaves_the_run_alone(sync_harness, monkeypatch):
    window, library, _ = sync_harness
    make_library(library, library_clip())

    class MessageBox:
        Yes = 1
        No = 2

        @staticmethod
        def question(*_args, **_kwargs):
            return MessageBox.No

    monkeypatch.setattr(settings_module, "QMessageBox", MessageBox)

    start_sync(window, library)
    window.close()

    assert not window._sync_cancel.is_set()
    assert window._sync_close_after is False


def test_closing_again_while_cancelling_does_not_start_another_question(
    sync_harness, monkeypatch,
):
    window, library, _ = sync_harness
    make_library(library, library_clip())
    questions = []

    class MessageBox:
        Yes = 1
        No = 2

        @staticmethod
        def question(_parent, title, message, *_buttons, default=0):
            questions.append(title)
            return MessageBox.Yes

    monkeypatch.setattr(settings_module, "QMessageBox", MessageBox)

    start_sync(window, library)
    window.close()
    window.close()

    assert len(questions) == 1


def test_removed_values_are_listed_until_there_are_too_many():
    assert settings_module.format_removed(()) == ""

    few = [("block", f"Block {index}") for index in range(3)]
    assert settings_module.format_removed(few).count("\n") == 2

    many = [("block", f"Block {index}") for index in range(20)]
    body = settings_module.format_removed(many)
    assert body.endswith("and 8 more")
    assert body.count("\n") == 12


def test_a_failed_sync_says_that_nothing_was_written(qapp):
    dialog = settings_module.VocabularySyncDialog(
        OSError("the share went away"))
    try:
        assert "Nothing was written" in dialog.summary_text()
        assert "the share went away" in dialog.summary_text()
    finally:
        dialog.deleteLater()
