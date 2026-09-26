"""Tests for file and folder scheme integration in Settings."""

import json
import os
import re
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

import settings.settings as settings_module
from shared.paths import DEFAULT_FOLDER_SCHEME

VALID_FILE_SCHEME = settings_module.DEFAULT_FILE_SCHEME
file_scheme_error = settings_module.file_scheme_error


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
    """Create isolated Settings windows backed by temporary settings files."""
    windows = []

    def create(settings=None, encoding="utf-8"):
        settings_path = tmp_path / "settings.json"
        if settings is not None:
            settings_path.write_text(
                json.dumps(settings, ensure_ascii=False),
                encoding=encoding,
            )
        monkeypatch.setattr(settings_module, "PROJECT_ROOT", str(tmp_path))
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
    assert window.save_schemes()

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
    messages = []

    class MessageBoxRecorder:
        @staticmethod
        def warning(parent, title, message):
            messages.append((title, message))

    monkeypatch.setattr(settings_module, "QMessageBox", MessageBoxRecorder)
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

    assert window.save_schemes()

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

    assert window.save_schemes()

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

    assert window.save_schemes()

    saved = read_saved_settings(tmp_path)
    assert saved[settings_module.FILE_NAMING_SCHEME_KEY] == stored_file_scheme
    assert saved[settings_module.FOLDER_ORGANIZATION_SCHEME_KEY] == custom_folder_scheme


def test_invalid_folder_scheme_blocks_the_atomic_save(window_factory, tmp_path, monkeypatch):
    messages = []

    class MessageBoxRecorder:
        @staticmethod
        def warning(parent, title, message):
            messages.append((title, message))

    monkeypatch.setattr(settings_module, "QMessageBox", MessageBoxRecorder)
    window = window_factory(base_settings())

    window.ui.lineEditFileScheme.setText("{title} changed")
    window.ui.lineEditFolderScheme.setText("{network}/{type}")

    assert not window.save_schemes()

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
