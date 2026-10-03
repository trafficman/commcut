"""The Manage Autofill Rules dialog.

Applies to: `importer/rules.py`, `importer/queue.py`, `shared/mesh.py`
(`learn_rule`, `edit_rule`, `remove_rule`), `shared/importing.py` (`match_value`).

Where the user teaches commcut that a piece of a file name means a tag, so it
fills that tag in for them from then on.

The rules live in the Mesh Wizard's own table rather than in a list of their own,
which is the reason this dialog looks the way it does: **a literal already in the
table is not added, it is shown and offered for editing.** A folder named `30 Sec`
and a rule for `30 Sec` are one answer to one question, and if two systems held
them separately they would drift without either noticing. So Add reports a
duplicate rather than quietly overwriting, and the dialog's whole job becomes
"what does this string already mean?" and "change it?".

The namespace and value pickers are the same ones the Wizard offers, and the value
list is pre-selected from `match_value` — so typing `Toonami` can propose
`block`, with the evidence attached, exactly as it does in the Wizard. A user
should not have to learn two vocabularies for one table.

Built in code rather than from a `.ui`, like the editor's export summary dialog:
it is a dialog owned by a window, has no layout worth keeping in the packaged
payload, and is built on demand rather than loaded once per process.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from shared.diagnostics import log
from shared.importing import match_value
from shared.mesh import (
    LEARNED_FOLDER,
    MeshConflict,
    MeshSession,
    namespace_choices,
)


def describe_rule(entry) -> str:
    """One line for the rules list."""
    source = "from the file name" if entry.learned != LEARNED_FOLDER else (
        "a folder name in this library")
    if entry.state != "meshed":
        return f"{entry.name}  —  not a tag yet ({source})"
    return f"{entry.name}  →  {entry.namespace}: {entry.value}"


class RulesDialog(QDialog):
    """Add, edit and remove the learned rules for one queue run."""

    def __init__(self, session: MeshSession, filename: str = "", parent=None):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Autofill Rules")
        self.resize(560, 520)

        layout = QVBoxLayout(self)

        caption = QLabel(
            "Teach commcut that a piece of a file name means a tag. It fills "
            "that tag in for you on the clips that follow.",
            self)
        caption.setWordWrap(True)
        layout.addWidget(caption)

        layout.addWidget(QLabel("This clip's file name:", self))
        name_box = QPlainTextEdit(filename, self)
        name_box.setReadOnly(True)
        name_box.setMaximumHeight(48)
        layout.addWidget(name_box)

        form = QWidget(self)
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)

        literal_row = QWidget(form)
        literal_layout = QVBoxLayout(literal_row)
        literal_layout.setContentsMargins(0, 0, 0, 0)
        literal_layout.addWidget(QLabel("The text to look for, exactly as it "
                                        "appears in the name:", self))
        self.editLiteral = QComboBox(form)
        self.editLiteral.setEditable(True)
        self.editLiteral.setInsertPolicy(QComboBox.NoInsert)
        self.editLiteral.currentTextChanged.connect(self._on_literal_changed)
        literal_layout.addWidget(self.editLiteral)
        form_layout.addWidget(literal_row)

        pair = QWidget(form)
        pair_layout = QHBoxLayout(pair)
        pair_layout.setContentsMargins(0, 0, 0, 0)

        namespace_box = QWidget(pair)
        namespace_layout = QVBoxLayout(namespace_box)
        namespace_layout.setContentsMargins(0, 0, 0, 0)
        namespace_layout.addWidget(QLabel("Namespace:", self))
        self.comboNamespace = QComboBox(namespace_box)
        self.comboNamespace.addItems(namespace_choices())
        namespace_layout.addWidget(self.comboNamespace)
        pair_layout.addWidget(namespace_box)

        value_box = QWidget(pair)
        value_layout = QVBoxLayout(value_box)
        value_layout.setContentsMargins(0, 0, 0, 0)
        value_layout.addWidget(QLabel("Tag value, existing or your own:", self))
        self.comboValue = QComboBox(value_box)
        self.comboValue.setEditable(True)
        self.comboValue.setInsertPolicy(QComboBox.NoInsert)
        value_layout.addWidget(self.comboValue)
        pair_layout.addWidget(value_box)

        form_layout.addWidget(pair)

        self.labelEvidence = QLabel(self)
        self.labelEvidence.setWordWrap(True)
        form_layout.addWidget(self.labelEvidence)
        layout.addWidget(form)

        self.buttonAdd = QPushButton("Add", self)
        self.buttonAdd.clicked.connect(self.on_add)
        layout.addWidget(self.buttonAdd)

        self.labelStatus = QLabel(self)
        self.labelStatus.setWordWrap(True)
        layout.addWidget(self.labelStatus)

        listing = QLabel("Rules so far:", self)
        layout.addWidget(listing)

        self.listRules = QListWidget(self)
        layout.addWidget(self.listRules)

        row = QHBoxLayout()
        self.buttonRemove = QPushButton("Remove Selected Rule", self)
        self.buttonRemove.clicked.connect(self.on_remove)
        row.addWidget(self.buttonRemove)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._refresh_list()
        if filename:
            self.editLiteral.setCurrentText(filename)

    # -- the list --------------------------------------------------------

    def _refresh_list(self):
        self.listRules.clear()
        for entry in self.session.rules():
            item = QListWidgetItem(describe_rule(entry))
            item.setData(_ROLE, entry.name)
            self.listRules.addItem(item)

    # -- the literal -----------------------------------------------------

    def _on_literal_changed(self, literal: str):
        """Show what this literal already means, if anything.

        The evidence line is the same ranked, counted information the Wizard
        offers, so "which namespace" is answered with `block (14 clips)` rather
        than a guess.
        """
        entry = self.session.find(literal) if literal else None
        if entry is not None:
            if entry.state == "meshed":
                self.labelEvidence.setText(
                    f"Already in your table: <b>{entry.namespace}: "
                    f"{entry.value}</b>. Adding it again would change nothing, so "
                    f"this offers to edit it instead.")
                self._select_pair(entry.namespace, entry.value)
            else:
                self.labelEvidence.setText(
                    f"That is a folder name in this library, not a rule. A folder "
                    f"name is meshed in the Mesh Wizard; this dialog is for "
                    f"strings found inside file names.")
                self._select_pair("", "")
            return

        matches = match_value(literal, self.session.library,
                              self.session.vocabulary)
        if len(matches) == 1:
            best = matches[0]
            self._select_pair(best.namespace, best.value)
            where = ("your library" if best.in_library else "your tag history")
            self.labelEvidence.setText(
                f"Suggested from {where}: <b>{best.namespace}</b> — "
                f"{best.clip_count} clip(s) use it there.")
        elif len(matches) > 1:
            self._select_pair("", "")
            self.labelEvidence.setText(
                "Appears in more than one namespace: "
                + ", ".join(f"{m.namespace} ({m.clip_count})" for m in matches)
                + ". Pick the right one.")
        else:
            self._select_pair("", "")
            self.labelEvidence.setText(
                "Nothing in your library uses that yet. Type the namespace and "
                "tag you want.")

    def _select_pair(self, namespace: str, value: str):
        if namespace:
            self.comboNamespace.setCurrentText(namespace)
        if value:
            self.comboValue.setCurrentText(value)

    # -- adding and editing ----------------------------------------------

    def on_add(self):
        """Add the rule, or edit the entry the literal already has.

        The refusal is the point: one literal, one meaning, always. A duplicate is
        reported with the existing meaning shown, and a second press edits it —
        which is the answer the Wizard's modal needs and the reason `learn_rule`
        returns the entry rather than raising.
        """
        literal = self.editLiteral.currentText().strip()
        namespace = self.comboNamespace.currentText()
        value = self.comboValue.currentText().strip()

        try:
            outcome = self.session.learn_rule(
                literal, namespace, value, filename=self._filename())
        except ValueError as error:
            self.labelStatus.setText(str(error))
            return

        if not outcome.created:
            self.labelStatus.setText(
                f"“{literal}” is already in your table as "
                f"{outcome.entry.namespace}: {outcome.entry.value}. "
                f"Press Add again to change it.")
            self._select_pair(outcome.entry.namespace, outcome.entry.value)
            self._refresh_list()
            return

        self._note_conflict(outcome.conflict)
        self.labelStatus.setText(f"Added: {describe_rule(outcome.entry)}")
        self.editLiteral.clear()
        self._refresh_list()

    def on_remove(self):
        """Forget the selected rule. A folder name cannot be forgotten this way."""
        item = self.listRules.currentItem()
        if item is None:
            return
        literal = item.data(_ROLE)
        try:
            self.session.remove_rule(literal)
        except ValueError as error:
            self.labelStatus.setText(str(error))
            return
        self.labelStatus.setText(f"Removed the rule for “{literal}”.")
        self._refresh_list()

    def _note_conflict(self, conflict: MeshConflict | None):
        if conflict is None:
            self.labelStatus.setText(self.labelStatus.text())
            return
        log(f"autofill rule raised a conflict: {conflict.describe()}")

    def _filename(self) -> str:
        return self.findChild(QPlainTextEdit).toPlainText().strip()


_ROLE = 32  # Qt.UserRole