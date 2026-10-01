# The tag vocabulary

Typing `Cartoon Network` into a tag field forty times is the thing this exists
to stop. Every value the user commits lands in
`install_root()/vocabulary.json`, and nine of the ten tag fields in the editor
are editable combos populated from it. The tenth, Title, stays a plain text
box — see [Namespaces](#namespaces).

Applies to: `shared/vocabulary.py`, `editor/editor.py:MediaPlayer`
(`_init_tag_vocabulary`, `_ordered_tag_values`, `_refresh_tag_combos`,
`_note_recent_tags`, `_commit_form_tags_to_model`),
`editor/editorwindow.ui`.

Related: [segment-model.md](segment-model.md#tag-suggestions) (the widgets and
the two commit points), [naming-and-organization.md](naming-and-organization.md#the-clip-record)
(where tags actually live), [testing.md](testing.md).

## The file

```json
{
  "version": 1,
  "tags": {
    "filler_type": ["Back To", "Bumper", "Promo"],
    "network": ["Cartoon Network", "Nickelodeon"]
  }
}
```

A **set of values per namespace**, sorted, and nothing else. No counts, no
ordering, no timestamps — see "Why no counts" below.

### It is a cache, not a record

This is the load-bearing framing. The file is *downstream* of the library and
deliberately imperfect:

- a value typed for a segment that was then skipped never reaches a `.cnfo`;
- nothing reconciles the file against the library;
- a clip deleted from `export/` leaves its values behind forever.

That is acceptable **because nothing depends on the file being complete.** The
vocabulary is *advisory*: nothing validates against it, a value absent from it
is always accepted, and the schemes accept arbitrary strings. A wrong or stale
entry costs a bad suggestion, never a failed export. Any design in which the
file had to be right would be a much larger thing than this one, and the scan
that makes it right is
[not built](status.md#known-gaps-and-planned-work).

### Reading never raises

A missing file, unreadable JSON, a wrong shape, or a `version` this build does
not know — all of them rebuild from `DEFAULT_VALUES` and carry on.

That is what makes **deleting `vocabulary.json` a safe troubleshooting step**,
which is the reason the shipped defaults live in code rather than in a shipped
file: a file commcut has to write is a file commcut can be blamed for. The
cost is that deletion discards the accumulated values, which is worth a line in
the user guide so nobody discovers it the hard way.

### Writing

`Vocabulary.save()` takes no path — an instance knows its own file. That is not
stylistic: a `save(path=None)` fallback is a way to record one vocabulary's
values into another's file, which is exactly how an early version of this wrote
a test's vocabulary into the real install root.

Staged in the file's own directory and moved into place with `os.replace`, so an
interrupted write cannot leave a half-file where the real one goes. **No
`fsync`**: this is a cache, and the durability would be paid on every Stage for
a file whose loss costs nothing.

## The dedup rule

The dedup key is `normalized_validation_key(sanitize_path_component(value))`
— **not** the raw string. That is the one non-obvious choice in the module, and
it is worth stating plainly: the key answers exactly "would these two values
render to the same thing".

Sanitation collapses a run of unsafe characters to a single dash and a run of
whitespace to a single space, so `A/B`, `A:B`, `A<>B` and `A-B` are one entry
rather than four. A vocabulary deduping on raw strings would offer all four,
and every one of them but the first would collide with the first in a plan —
the export preflight already refuses case-varied and normalization-varied
destinations as the same folder. A dropdown that offers a collision is worse
than no dropdown.

Reusing `shared/paths.py:sanitize_path_component` also keeps one owner for the
rendering transform (invariant 9 in `AGENTS.md`) instead of a second
normalization that would drift from it.

What is **stored** is still what the user typed, trimmed. The list offers their
value; the path does the sanitizing.

### Case

Stored as typed, matched case-insensitively, and **the first-seen casing
wins**. A value entered in the wrong case stays wrong and is not duplicated —
correcting it is the user's call, and a module that silently rewrote the
vocabulary would be editing data. The plan is that the wrong casing is caught
where it actually matters, by the export preflight.

### Namespaces

The nine non-title tags, canonical keys, named in `editor/editor.py` as
`_SUGGESTED_TAG_FIELDS`. `shared/scheme.py:canonical_tag_name` resolves an alias
(`type` → `filler_type`) and rejects anything unknown, so a namespace that is not
a tag never enters the file.

**Title has no dropdown.** It is unique per clip, so a list of every title ever
typed is a list with one use each, and there is no second title to suggest from.
It stays a plain `QLineEdit` for the same reason it has no lock toggle: both are
ways of saying "this value belongs to one segment and does not carry".

Two things follow, and both are deliberate:

- The editor writes **all ten** tags to the model and only the **nine** to the
  vocabulary, because the file exists to populate dropdowns and title has no
  dropdown to populate. A namespace nothing reads is dead weight in the file and
  one more thing to reason about during a future sync.
- The module itself has no such rule. `shared/vocabulary.py` stores whatever
  namespaces it is handed, so a caller with a different set needs no special
  case. The filtering lives at the single call site that also decides what to
  offer — `_commit_form_tags_to_model`.

`_TAG_FIELDS` is `{title, **_SUGGESTED_TAG_FIELDS}`, so the split is stated once
and a new tag lands on the suggestable side automatically.

## The shipped defaults

`DEFAULT_VALUES` seeds **`filler_type` only**. The list is a judgement call about
the project's own vocabulary, drawn from the glossary in
[guides/filler_and_you.md](guides/filler_and_you.md), and it is expected to be
edited as that judgement changes — so the tests pin its *shape* (one namespace,
non-empty strings, nothing that dedupes to the same value) rather than its
contents.

The rest is the user's to define. Seeding one namespace on purpose: shaping the
list early is the value, and seeding all nine would be dictating a taxonomy
nobody asked for.

There is no distinction between a seeded value and a used one. A default the
user never wants stays in their list; that is a mild tax, and the alternative —
tracking provenance per value — is a second rule to keep honest in a file two
code paths write.

## Why no counts

Ordering is *not* stored here. The dropdown leads with the most recently used
values, and "recent" means the current editing session — one source video —
which lives on the editor instance and dies with it.

A lifetime counter therefore has no consumer today; the only one named was a
library-info display, which does not exist. Persisting counts would mean
persisting a number nothing reads plus a second rule about how a future sync
adjusts it. The file stays a set.

Adding counts later is a schema bump: a namespace's value becomes
`{value: count}` instead of a bare string. Because the old shape is a set of the
same strings, that is a version-gated read change rather than a migration of
user data.

## The ordering

`most recently used, then the rest alphabetically` — `_ordered_tag_values`.

The most-recently-used part is `self._recent_tag_values`, one list per
namespace, on the editor. **A session is one source video, not one app run**,
so opening the next source starts the ordering fresh while the values persist
in the file. The editor is destroyed and rebuilt on every navigation
(`shared/session.py`), so an instance attribute resets itself exactly when it
should.

The alphabetical remainder keeps the list deterministic and leaves a value the
user has not touched this session findable.

`_note_recent_tags` returns whether any ordering moved, and the refresh hangs
off *that* as well as off the vocabulary's own "changed" answer. Using a value
that is already in the file changes no file, but it does change what the list
should lead with — hanging the refresh off the file alone would leave the list
showing whatever the last *new* value put there.

## The widgets

`MediaPlayer._init_tag_vocabulary()` is the one place a tag field becomes a
dropdown: configure the combo, resolve the file, populate. It iterates
`_SUGGESTED_TAG_FIELDS`, so Title is skipped without needing a guard at any
other call site. Everything below is its consequence, and
[segment-model.md](segment-model.md#tag-suggestions) covers the editor-side
contract in full, including the three helpers that let one codebase hold two
widget types (`_field_text`, `_set_field_text`, `_field_change_signal`).

- **Editable, `NoInsert`.** The default insert policy grows the list from
  everything typed into it, which is the opposite of what the file is for.
- **`UnfilteredPopupCompletion`.** The default inline mode rewrites what you
  typed to match a completion, so typing `Toonami` and pressing Enter would
  commit `Toonami Kids`.
- **`MatchContains`, case-insensitive.** `toon` has to find `Toonami`.
- **The popup is opened by calling `complete()` on each edit**, because PySide6
  does not expose `QComboBox.setCompleterPopupVisible(True)`.
- **`_refresh_tag_combos` saves and restores each field's text.** `clear()`
  empties the line edit as well as the item list, so without that a Stage would
  erase whatever the user is partway through typing in a *different* field.
  It repopulates only after a commit, never on segment navigation, for the same
  reason.

### An empty list explains itself

A namespace with nothing in it gets one item reading **"Populate this list by
staging tags"**, disabled in the model so it cannot be picked as a tag. The line
edit is still free text, so a typed value is unaffected.

An empty dropdown otherwise reads as a broken control rather than as a new one,
and on a fresh install every list *is* empty. It disappears the moment the
namespace has a value in it.

## What counts as used

`_commit_form_tags_to_model()` — **Stage** and **export preparation**. Those two
sites are the complete definition, because the boundary operations pass
`_inherited_tags()` (the lock values) rather than the form.

This matters: the vocabulary is fed at a commit, not from a keystroke. A form
read mid-entry holds `Cartoon N`, which is not a tag anybody means. And both
sites run *after* their own required-tag check, so a stage refused for a
missing required field records nothing.

"Used" means *suggestible and staged*. The same call writes all ten tags to the
model and the nine suggestable ones to the file — see
[Namespaces](#namespaces).

A vocabulary that cannot be written — a full disk, a read-only folder — is
logged and otherwise ignored. Every value in it is also in the `.cmct` and the
records, and the next stage will try again.

## Not built

The **manual sync** and the **library walk** that would reconcile the file with
the `.cnfo` records, the **library scan / catalog**, any **picker** use (it has
no tags yet; when it does, the rule is filename when there is no `.cmct` and
the sidecar when there is one), **counts**, and **seed provenance**.