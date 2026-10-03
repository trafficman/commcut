"""The tag form, and the only implementation of it in the app.

Applies to: `shared/tag_form.py`, `shared/tagform.ui`, `editor/editor.py`,
`importer/queue.py`.

Two windows show tags to a person: the Editing Wizard and the Library Mesh Tag
Editor. They are the same ten fields with the same behaviour, so they are one
widget here rather than two that drift.

Drift is not a hypothetical. `docs/tag-vocabulary.md` records why a dropdown's
configuration matters — the insert policy, the completer's case sensitivity, its
filter mode, its completion mode — because each of those decides what a typed
value becomes. Two forms means two of every one of those decisions, and the one
that gets missed is the one nobody was looking at.

So this module owns:

- the field layout, in `shared/tagform.ui`, as a promoted widget
- which fields exist, which suggest from the vocabulary, which are required, and
  which are lockable
- the vocabulary binding, the most-recently-used ordering, and the note-taking that
  feeds both
- the required-field outline

What it deliberately does **not** own: what a tag *means*. Nothing here decides a
value. The editor's values come from its segments, the queue's from the Mesh
Wizard's alias table, and both arrive already settled.

`Title` is absent from the suggested and lockable sets in both, and for one
reason: it is unique per clip, so there is nothing to suggest from — a list of
every title ever typed is a list with one use each — and nothing to carry forward.
It stays a plain line edit.
"""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QFile, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QWidget,
)

from shared.environment import resource_path
from shared.exporting import missing_required_tags
from shared.ui_loader import UiLoader
from shared.vocabulary import get_vocabulary, vocabulary_path

#: Tag key → attribute name for the fields that suggest from the vocabulary.
SUGGESTED_TAG_FIELDS = {
    "network":     "lineEditNetwork",
    "block":       "lineEditBlock",
    "filler_type": "lineEditType",
    "year":        "lineEditYear",
    "time_period": "lineEditTimePeriod",
    "show":        "lineEditShow",
    "special":     "lineEditSpecial",
    "length":      "lineEditLength",
    "information": "lineEditInfo",
}

#: Every tag field, in form order. Title is the odd one out, so the split is
#: expressed once above rather than as two parallel lists to keep in step: a new
#: tag goes in SUGGESTED_TAG_FIELDS and lands here automatically.
TAG_FIELDS = {
    "title": "lineEditTitle",
    **SUGGESTED_TAG_FIELDS,
}

#: Tag key → attribute name for the corresponding lock toggle. Title is
#: excluded for the same reason it is absent from the suggested fields.
LOCK_BUTTONS = {
    "filler_type": "lockType",
    "network":     "lockNetwork",
    "year":        "lockYear",
    "time_period": "lockTimePeriod",
    "block":       "lockBlock",
    "show":        "lockShow",
    "special":     "lockSpecial",
    "length":      "lockLength",
    "information": "lockInfo",
}

#: The base record fields every exported clip needs, in form order, mapped to the
#: label the user sees. Enforced here as well as in `shared.exporting`.
REQUIRED_TAG_LABELS = {
    "title": "Title",
    "network": "Network",
    "filler_type": "Type",
    "time_period": "Time Period",
}

#: Every attribute `shared/tagform.ui` provides, so the form can adopt its own
#: children and a caller can write `form.lineEditTitle` exactly as the editor
#: wrote `self.ui.lineEditTitle`. Explicit rather than swept out of the loaded
#: object's namespace, so a renamed widget fails at load rather than as an
#: AttributeError three methods later.
FORM_WIDGETS = (
    tuple(TAG_FIELDS.values())
    + tuple(LOCK_BUTTONS.values())
    + ("labelTitle", "labelType", "labelNetwork", "labelTimePeriod",
       "labelYear", "labelBlock", "labelShow", "labelSpecial", "labelLength",
       "labelInfo")
)

#: The border on a required field that still needs a value. Built into a selector
#: from the field's own class at use time, so changing a field's widget class
#: cannot leave this selecting nothing -- which would remove the warning
#: silently, since a non-matching QSS rule is not an error.
REQUIRED_FIELD_BORDER = "1px solid #c0392b"

#: Shown in a dropdown that has nothing in it yet, so an empty list reads as an
#: instruction rather than as a broken control.
EMPTY_VOCABULARY_HINT = "Populate this list by staging tags"


def field_text(field) -> str:
    """The text a tag field currently holds.

    One accessor for one reason: the tag fields are two different widget types,
    and they disagree about both their accessors (`QLineEdit.text()`,
    `QComboBox.currentText()`) and their change signals. Every reader goes through
    here rather than each call site choosing one, so a widget type changing breaks
    one function instead of the ones nobody looked at. The editable combo's
    `currentText()` is the line edit's text when nothing in the list matches,
    which is what the form means -- a typed value, not a selection.
    """
    if isinstance(field, QComboBox):
        return field.currentText()
    return field.text()


def field_change_signal(field):
    """The signal a tag field emits when the user changes its text.

    `QLineEdit` has no `editTextChanged` and `QComboBox` has no `textChanged`,
    so the choice lives here. Either way it fires on typing *and*, for the combo,
    on picking from the popup -- and the blockSignals pair around
    `write_tags` keeps the programmatic write out of it.
    """
    if isinstance(field, QComboBox):
        return field.editTextChanged
    return field.textChanged


def set_field_text(field, value: str) -> None:
    """Write a tag field's text, whichever of the two widget types it is.

    The counterpart to `field_text`, for the same reason: `setEditText` exists
    only on the combo, and setting one field with the other's setter is an
    `AttributeError` at runtime rather than a mistake a reader can see.
    """
    if isinstance(field, QComboBox):
        field.setEditText(value)
    else:
        field.setText(value)


class TagForm(QWidget):
    """The ten tag fields, wired to a vocabulary file.

    A promoted widget rather than a plain object so there is exactly one layout
    in the app. `editorwindow.ui` and the queue's own `.ui` each host one of
    these; neither draws its own grid, so neither can be subtly different.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loaded = UiLoader().load(
            QFile(resource_path("shared", "tagform.ui")), self)
        self.setLayout(self._loaded.layout())
        for name in FORM_WIDGETS:
            setattr(self, name, getattr(self._loaded, name))
        self._vocabulary = None
        self._vocabulary_path = None
        self._recent_tag_values: dict[str, list[str]] = {}
        self._required_base_styles: dict[str, str] = {}

    # -- the fields ------------------------------------------------------

    def field(self, namespace: str):
        """The widget for one tag key."""
        return getattr(self, TAG_FIELDS[namespace])

    def lock_button(self, namespace: str):
        return getattr(self, LOCK_BUTTONS[namespace])

    def read_tags(self) -> dict[str, str]:
        """Snapshot the current form values into a tags dict."""
        return {key: field_text(self.field(key)) for key in TAG_FIELDS}

    def write_tags(self, tags: dict[str, str], overrides: Mapping | None = None) -> None:
        """Populate the form from a tags dict, with signals blocked.

        `overrides` wins over `tags` for the keys it names. The editor passes its
        lock values that way: a lock is the only source of input for an unedited
        segment, and it overrides the stored empty value.

        Blocking the signals is why writing the form cannot mark a segment
        edited.
        """
        source = dict(tags)
        source.update(overrides or {})
        for key in TAG_FIELDS:
            field = self.field(key)
            field.blockSignals(True)
            set_field_text(field, source.get(key, ""))
            field.blockSignals(False)

    def missing_required_labels(self) -> list[str]:
        """Display names of the required tags the form is still missing.

        Read from the form rather than from a model, so the answer reflects what
        the user is looking at.
        """
        missing = set(missing_required_tags(self.read_tags()))
        return [label for key, label in REQUIRED_TAG_LABELS.items()
                if key in missing]

    def refresh_required_fields(self, exempt: bool = False) -> None:
        """Outline the required fields the form is still missing.

        Recomputed on every keystroke, so the outline always states what a save
        or an import will demand right now.

        `exempt` clears it entirely. The editor passes True for a segment marked
        skipped: such a segment is excluded from export, so the backend's
        required-tag rule does not apply to it either, and outlining its fields
        would demand something nothing will ask for.
        """
        missing = set() if exempt else set(self.missing_required_labels())
        for key, label in REQUIRED_TAG_LABELS.items():
            field = self.field(key)
            field.setStyleSheet(
                f"{type(field).__name__} {{ border: {REQUIRED_FIELD_BORDER}; }}"
                if label in missing else self._required_base_styles.get(key, "")
            )

    def set_locks_visible(self, visible: bool) -> None:
        """Show or hide every lock toggle.

        Hidden by the Library Mesh Tag Editor, which shows the same form for
        independent clips: a lock means "carry this value forward to the next
        segment", and a queue has no next segment to carry it to. The widgets
        still exist so there is one definition of the form rather than two.
        """
        for namespace in LOCK_BUTTONS:
            self.lock_button(namespace).setVisible(visible)

    # -- the vocabulary --------------------------------------------------

    def init_vocabulary(self, path: str | None = None) -> None:
        """Bind the dropdowns to a vocabulary file.

        `path` exists for the widget tests, which point it at a temp folder so a
        suite run cannot write to the real install root. Production leaves it
        alone and gets the file beside the executable.

        The most-recently-used order is per form, not per process: one editing
        session is one source video, and opening the next one should not inherit
        the last one's habits at the top of every list.

        Configuring the combos lives here rather than in a separate call so there
        is one place that makes a tag field a dropdown at all -- a field that is
        populated but not configured is a closed list, which looks fine right up
        until someone tries to type.
        """
        for namespace in SUGGESTED_TAG_FIELDS:
            _configure_tag_combo(self.field(namespace))
        for key in SUGGESTED_TAG_FIELDS:
            self._required_base_styles.setdefault(key, self.field(key).styleSheet())
        self._vocabulary_path = path or vocabulary_path()
        self._vocabulary = get_vocabulary(self._vocabulary_path)
        self._recent_tag_values = {key: [] for key in SUGGESTED_TAG_FIELDS}
        self.refresh_combos()

    @property
    def vocabulary_path(self) -> str | None:
        return self._vocabulary_path

    def ordered_tag_values(self, namespace: str) -> list[str]:
        """The values to offer for a namespace: most recently used first.

        "Recently" means this form's session, which is why the order lives on the
        form rather than in the vocabulary file. The remainder is alphabetical so
        the list is deterministic and a value the user has not touched is still
        findable.
        """
        recent = [value for value in self._recent_tag_values.get(namespace, [])
                  if value]
        available = (self._vocabulary.values(namespace)
                     if self._vocabulary is not None else ())
        ordered = list(dict.fromkeys(recent))
        seen = set(ordered)
        ordered.extend(value for value in available if value not in seen)
        return ordered

    def refresh_combos(self) -> None:
        """Repopulate every dropdown from the vocabulary, most recent first.

        `QComboBox.clear()` empties the line edit as well as the item list and
        emits change signals, so each field's text is saved and restored around
        the refill. Without that, saving one clip would erase what the user is in
        the middle of typing into every *other* field.

        Called when a set of tags is committed, never on navigation: repopulating
        while someone is mid-entry would eat the entry.
        """
        for namespace in SUGGESTED_TAG_FIELDS:
            combo = self.field(namespace)
            values = self.ordered_tag_values(namespace)
            if not values and combo.count() == 1 and combo.itemText(0) == (
                    EMPTY_VOCABULARY_HINT):
                continue
            # Saved and restored through the same accessor the form is read
            # with, or a refresh would hand back a different value than
            # `read_tags` reports.
            typed = field_text(combo)
            combo.blockSignals(True)
            combo.clear()
            for value in values:
                combo.addItem(value)
            if not values:
                combo.addItem(EMPTY_VOCABULARY_HINT)
                # Disabled so it cannot be chosen as a tag by clicking it; the
                # line edit is still free text, so a typed value is unaffected.
                item = combo.model().item(0)
                if item is not None:
                    item.setEnabled(False)
            combo.setEditText(typed)
            combo.blockSignals(False)

    def note_recent_tags(self, tags: dict[str, str]) -> bool:
        """Move the values just committed to the front of their dropdowns.

        Returns whether any ordering changed. Using a value already in the
        vocabulary changes no file, but it does change what the dropdown should
        lead with -- so the refresh cannot hang off the vocabulary's own "changed"
        answer, or re-using a value would leave the list showing whatever the last
        *new* value put there.
        """
        moved = False
        for namespace, value in tags.items():
            value = (value or "").strip()
            if not value:
                continue
            recent = self._recent_tag_values.setdefault(namespace, [])
            if recent[:1] == [value]:
                continue
            if value in recent:
                recent.remove(value)
            recent.insert(0, value)
            moved = True
        return moved


def _configure_tag_combo(combo) -> None:
    """Make one tag field an editable combo that suggests from the vocabulary.

    The combo owns its own item list and refuses to grow one from typing: the
    default insert policy turns every half-remembered value into a permanent
    entry, which would quietly duplicate what the vocabulary file is for and
    leave the user scrolling through their own typos.

    The completer is what makes this more than a list to scroll: it narrows as
    you type, case-insensitively and by substring, so `toon` finds `Toonami`.
    `UnfilteredPopupCompletion` is the part that matters for correctness -- the
    default inline mode rewrites what you typed to match a completion, so
    pressing Enter commits `Toonami Kids` when you wrote `Toonami`.

    The popup is opened by calling the completer on each edit rather than by
    `QComboBox.setCompleterPopupVisible(True)`, which PySide6 does not expose.
    Signals are already blocked around every programmatic write, so this fires
    for typing and for picking, and not for the form being repopulated.
    """
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    combo.setCompleter(combo.completer())
    completer = combo.completer()
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setFilterMode(Qt.MatchContains)
    completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
    combo.editTextChanged.connect(
        lambda text: completer.complete() if text and combo.count() else None
    )