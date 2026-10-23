"""What a tag lock is, and what a lock does to a form.

Applies to: `shared/tag_locks.py`, `shared/tag_form.py`, `editor/editor.py`,
`importer/queue.py`.

Two windows show tags to a person — the Editing Wizard and the Library Mesh Tag
Editor — and both let a value be **locked**: pinned once and carried forward to
whatever comes next. In the editor the next thing is a segment; in the queue it
is the next clip. The idea is the same in both, so the rule is here rather than
written twice.

Three properties, and they are the whole of it:

- **A pin is a value, captured when the toggle is clicked.** It is deliberately
  *not* updated by field edits. A lock is a value that propagates forward, so a
  field that stops matching its pin is a deliberate deviation from it, not a
  reason to repoint the pin.
- **A lock reads engaged only while the field holds the pinned value.** The
  button state is derived, never imperative, so it cannot say a lock is on when
  the field has been changed under it.
- **An empty pin carries nothing.** Locking an empty field is allowed and
  disengages as soon as anything is typed into it.

**A lock may not set a title.** A title is unique per clip, so there is nothing
to carry forward — the same reason the folder mesh and the autofill rules may
not target one, and the reason `shared/tag_form.py` has no title lock button.
The refusal is here rather than only in the UI so that it holds however the pin
was set.

`resolve_tag_sources` is the other half: the order the sources are consulted
in, when a form is filled from more than one of them. It is a function of plain
dicts so it can be reasoned about — and tested — without a window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from shared.scheme import CANONICAL_TAG_KEYS

#: The one tag a lock may not carry. Unique per clip, so there is nothing to
#: carry — see the module docstring.
TITLE_TAG = "title"


def _validate_namespace(namespace: str) -> str:
    """The tag key must be one commcut knows, and must not be a title."""
    if namespace not in CANONICAL_TAG_KEYS:
        raise ValueError(
            f"{namespace!r} is not a tag commcut knows; a lock can only be set on "
            f"one of {', '.join(sorted(CANONICAL_TAG_KEYS))}"
        )
    if namespace == TITLE_TAG:
        raise ValueError(
            "A lock cannot carry the title. A title is unique per clip, so there "
            "is nothing for it to carry forward to."
        )
    return namespace


class TagLocks:
    """The pinned tag values of one session.

    A plain store of `namespace -> pinned value`, plus the two questions a
    window asks about it: *what is pinned* and *is this lock engaged here*.
    """

    def __init__(self, pins: Mapping[str, str] | None = None) -> None:
        self._pins: dict[str, str] = dict(pins or {})

    # -- the pins ---------------------------------------------------------

    def pin(self, namespace: str, value: str) -> None:
        """Pin `value` for `namespace`.

        The value is captured here and not updated afterwards: a lock is a value
        that propagates forward, so a field that stops matching it is a
        deliberate deviation rather than a reason to repoint it.
        """
        _validate_namespace(namespace)
        self._pins[namespace] = value

    def release(self, namespace: str) -> None:
        """Forget the pin for `namespace`. A pin that was never set is fine."""
        self._pins.pop(namespace, None)

    def pinned(self, namespace: str) -> str | None:
        """The pinned value for `namespace`, or None when there is no pin."""
        return self._pins.get(namespace)

    def is_engaged(self, namespace: str, current_value: str) -> bool:
        """Whether the lock for `namespace` is engaged on a field holding
        `current_value`.

        The derivation both windows render: a lock is engaged iff it is pinned
        *and* the field currently holds the pinned value. So the toggle switches
        off the moment the field stops matching it, and back on as soon as it
        matches again.
        """
        pinned = self._pins.get(namespace)
        return pinned is not None and pinned == current_value

    def inherited(self) -> dict[str, str]:
        """The pins that carry a value forward.

        Empty pins are left out: locking an empty field is allowed, but there is
        nothing in it to carry.
        """
        return {key: value for key, value in self._pins.items() if value}

    def as_dict(self) -> dict[str, str]:
        """Every pin, empty ones included."""
        return dict(self._pins)

    # -- the shape of it --------------------------------------------------

    def __len__(self) -> int:
        return len(self._pins)

    def __contains__(self, namespace: object) -> bool:
        return namespace in self._pins

    def __repr__(self) -> str:
        return f"TagLocks({self._pins!r})"


def _first_answer(*candidates: str | None) -> str | None:
    """The first candidate that actually answers, or None.

    An empty value is not an answer. A field nobody filled in is a question,
    and a question is answered by the next source rather than by the empty
    string.
    """
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate
    return None


def resolve_tag_sources(
    *,
    entered: Mapping[str, str] | None = None,
    record: Mapping[str, str] | None = None,
    autofill: Mapping[str, str] | None = None,
    contested: Iterable[str] = (),
    locks: TagLocks | None = None,
) -> dict[str, str]:
    """The one value per namespace that four sources can offer.

    **Entered > Record > Autofill > Locked > Empty**, per namespace:

    - **entered** is what the user typed for this clip during this visit. It
      wins outright: it is the only opinion nobody else has.
    - **record** is the clip's own `.cnfo`. A clip's tags live in its record
      (invariant 12), so once a record exists it is the authority.
    - **autofill** is what the folder structure and the learned rules make of
      the clip's path and file name. It is evidence *about this clip*.
    - **locked** is a value carried forward from an earlier clip. It is evidence
      about a *neighbour*, so it answers only what nothing else did.

    Two rules that are not simply an ordering:

    - **A record suppresses the autofill entirely.** The folder answers must
      not resurrect a tag the Tagged Library Mesh deliberately removed, and a
      record that exists is the whole of what is known about the clip. A lock
      still fills the record's empty fields, because a lock is a live,
      session-scoped value the user has just confirmed rather than a standing
      instruction from an earlier session.
    - **A contested namespace is never answered by a lock.** Two folder names
      claiming one namespace is the mesh's deliberate refusal to guess: the
      namespace is left out and the clip goes to a person. A carried value
      would close that question without the user deciding it.

    Returns the answered namespaces only; a namespace nobody answered is absent,
    which the form renders as empty.
    """
    entered = entered or {}
    record = record or {}
    autofill = autofill or {}
    contested = set(contested or ())
    pinned = locks.inherited() if locks is not None else {}

    answered: dict[str, str] = {}
    namespaces = set(entered) | set(record) | set(autofill) | set(pinned)
    for namespace in sorted(namespaces):
        value = _first_answer(
            entered.get(namespace),
            record.get(namespace),
            None if record else autofill.get(namespace),
            None if namespace in contested else pinned.get(namespace),
        )
        if value is not None:
            answered[namespace] = value
    return answered
