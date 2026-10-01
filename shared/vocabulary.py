"""The tag vocabulary: values the user has already used, offered back to them.

Applies to: `shared/vocabulary.py`, `editor/editor.py`, `editor/editorwindow.ui`.

Typing `Cartoon Network` into a tag field forty times is the thing this exists
to stop. Every value committed through the editor lands in
``install_root()/vocabulary.json``, and each tag field is an editable combo box
populated from it.

The file is a **convenience cache, not a record**. It is downstream of the
library and deliberately imperfect: a value typed for a segment that was then
skipped never reaches a record, and nothing here reconciles the two. That is
acceptable precisely because nothing depends on the file being complete -- the
vocabulary is *advisory*. Nothing validates against it, a value absent from it
is always accepted, and the schemes accept arbitrary strings. A wrong or stale
entry costs a bad suggestion, never a failed export.

Which is also why the shipped defaults live in code rather than in a shipped
file: deleting ``vocabulary.json`` has to be a safe troubleshooting step, so a
missing file is not an error. It is a fresh vocabulary built from
``DEFAULT_VALUES``. A file that cannot be read -- unparseable, wrong shape, or
written by a newer schema -- is handled the same way and for the same reason.

The dedup key is ``normalized_validation_key(sanitize_path_component(value))``
rather than the raw string, and that is the one non-obvious choice here: it is
exactly the question "would these two values render to the same thing".
Sanitation collapses a run of unsafe characters to a single dash and a run of
whitespace to a single space, so ``A/B``, ``A:B``, ``A<>B`` and ``A-B`` are one
entry rather than four -- and offering a folder's worth of spellings that all
become the same folder is how a dropdown starts handing out collisions. Reusing
`shared/paths.py:sanitize_path_component` also keeps one owner for the rendering
transform (invariant 9) rather than a second normalization that would drift
from it. What is *stored* is still what the user typed, trimmed: this list
offers their value, the path does the sanitizing.

Ordering is *not* stored here. The dropdown leads with the most recently used
values, and "recent" means the current editing session -- one source video --
which is per editor window and dies with it. Lifetime counts have no consumer
today and would be a second rule to keep honest, so the file stays a set.

The module stores whatever namespaces it is handed; the editor decides which
of them are worth remembering, because the editor owns the dropdowns. Today that
is every tag except title, which is unique per clip and therefore has nothing to
suggest.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from shared.diagnostics import log
from shared.environment import install_root
from shared.paths import normalized_validation_key, sanitize_path_component
from shared.scheme import CANONICAL_TAG_KEYS, canonical_tag_name

#: Written beside settings.json. Both are in install_root(), which is beside
#: the executable in a packaged build and the clone in a source install.
VOCABULARY_FILENAME = "vocabulary.json"

#: The shape this module writes. A file claiming more is treated as unreadable
#: rather than guessed at -- see the module docstring.
VOCABULARY_SCHEMA_VERSION = 1

#: The values a fresh install starts with, so a new user's filler_type list is
#: already shaped like one instead of ten near-identical spellings by clip ten.
#: The list itself is a judgement call about the project's vocabulary and is
#: expected to be edited; what matters is that it lives here, in code, rather
#: than in a shipped file the user can delete.
DEFAULT_VALUES: Mapping[str, tuple[str, ...]] = {
    "filler_type": (
        "Back To",
        "Be Right Back",
        "Bumper",
        "Commercial",
        "Ending",
        "Interstitial",
        "Intro",
        "Opening",
        "Outro",
        "Promo",
        "Up Next",
    ),
}


@dataclass(frozen=True)
class PruneResult:
    """What a `prune_to` did, split into the two things it can do.

    `protected` exists because "nothing was removed" is ambiguous on its own:
    a caller that cannot tell a spared default from a value still in use will
    report that nothing was unused, which is not true. Both are
    `(namespace, value)` pairs in raw, display-cased form.
    """

    #: Gone from the file.
    removed: tuple[tuple[str, str], ...] = ()
    #: Left in place despite being unused, because they are shipped defaults.
    protected: tuple[tuple[str, str], ...] = ()

    @property
    def kept(self) -> bool:
        """Whether the prune spared anything, protected or in use."""
        return bool(self.protected)


def _is_shipped_default(namespace: str, dedup: str) -> bool:
    """Whether the entry `dedup` names in `namespace` is one of the defaults.

    Compared in the dedup key space, because that is the only way the file can
    express identity: a user who typed `PROMO` has an entry that casefolds to
    the same key as the default `Promo`, and `record()` would not have stored
    theirs alongside it. So an entry whose key matches a default *is* that
    default, whatever casing it happens to be filed under.
    """
    return any(_dedup_key(value) == dedup
               for value in DEFAULT_VALUES.get(namespace, ()))


def vocabulary_path() -> str:
    """The vocabulary file's location, under the install root.

    Not derived from `__file__`: frozen, that is PyInstaller's extraction
    folder, which is deleted on exit along with everything the user has built.
    """
    return os.path.join(install_root(), VOCABULARY_FILENAME)


def _normalize_value(value: str) -> str:
    """The stored form of a tag value: what the user typed, tidied.

    Surrounding whitespace goes, because `sanitize_path_component` strips it and
    two entries differing only by a trailing space are one entry as far as the
    library is concerned. The value is *not* otherwise transformed: this list
    offers what the user typed, and the path does the sanitizing. Only the dedup
    key below looks at the sanitized form.
    """
    return value.strip()


def _dedup_key(value: str) -> str:
    """The key two values must share to be the same vocabulary entry.

    Normalizes to NFC, drops format and control characters, and casefolds, then
    runs the value through the same sanitation a rendered path component gets.
    That last step is what keeps ``A & B`` and ``A-B`` out of the list twice.
    """
    return normalized_validation_key(sanitize_path_component(value))


class Vocabulary:
    """The values offered per namespace, and the one place they are stored.

    An instance knows its own file. `save()` taking an optional path was a way
    to write one vocabulary's values into another vocabulary's file, which is how
    a test pointed at a temp folder and got its file written to the real install
    root instead.
    """

    def __init__(self, values: Mapping[str, Mapping[str, str]] | None = None,
                 path: str | None = None):
        #: namespace -> {dedup key: stored value}. The mapping is what preserves
        #: the first-seen casing while making a case-insensitive check a dict
        #: hit rather than a scan.
        self._values: dict[str, dict[str, str]] = {
            namespace: dict(entries) for namespace, entries in (values or {}).items()
        }
        self.path = path
        self.dirty = False

    # --- reading ---

    def values(self, namespace: str) -> tuple[str, ...]:
        """Every stored value for a namespace, sorted.

        Sorted because the editor re-orders on top of this anyway (most recently
        used first) and wants a deterministic remainder.
        """
        canonical = canonical_tag_name(namespace) or namespace
        return tuple(sorted(self._values.get(canonical, {}).values()))

    def namespaces(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def to_dict(self) -> dict:
        return {
            "version": VOCABULARY_SCHEMA_VERSION,
            "tags": {
                namespace: sorted(entries.values())
                for namespace, entries in sorted(self._values.items())
            },
        }

    # --- writing ---

    def record(self, tags: Mapping[str, str]) -> bool:
        """Add every non-empty value in `tags`. Returns whether anything changed.

        The whole point of this call being at a commit rather than at a
        keystroke: a form read mid-entry holds ``Cartoon N``, which is not a tag
        anybody means.

        A value already present under a different casing is not added again, and
        the existing casing is kept -- the export preflight already treats
        case-varied folder names as the same directory, so offering both would be
        offering a collision.
        """
        changed = False
        for key, value in (tags or {}).items():
            namespace = canonical_tag_name(key)
            if namespace is None:
                continue
            if isinstance(value, str) and self._record_one(namespace, value):
                changed = True
        if changed:
            self.dirty = True
        return changed

    def _record_one(self, namespace: str, value: str) -> bool:
        """Add one value. Returns whether it was new."""
        normalized = _normalize_value(value)
        if not normalized:
            return False
        entries = self._values.setdefault(namespace, {})
        dedup = _dedup_key(normalized)
        if dedup in entries:
            return False
        entries[dedup] = normalized
        return True

    def prune_to(
        self, in_use: Mapping[str, Collection[str]]
    ) -> PruneResult:
        """Remove every unused value except the shipped defaults.

        Returns a `PruneResult`, so a caller can report what it removed *and*
        what it deliberately kept.

        The opposite of `record`, and the only place a value ever leaves this
        module. Three choices make it the safe operation it has to be:

        - `in_use` holds **raw values**, run through `_dedup_key` here rather
          than by the caller. Removal happens in the same key space `record()`
          dedupes in, so `Cartoon/Network` and `cartoon network` cannot survive
          a prune that was given `Cartoon Network`.
        - What comes back is what went away, so a caller can report it. A prune
          that deleted silently would be indistinguishable from one that did
          nothing.
        - **A shipped default is never removed.** `DEFAULT_VALUES` is this
          module's own data, so the rule that protects it belongs here rather
          than at each call site, where the next caller would have to remember
          it. The reasoning is that a default is not library residue: it is the
          project's starter vocabulary, offered before the user has staged a
          single clip, and it says nothing about what their library contains.
          Pruning it on the strength of a thin library is what would make a new
          user's dropdowns collapse the first time they exported one clip.
          Deleting one is still possible by hand-editing the file, which is the
          only way it was possible before the sync existed.

        A namespace absent from `in_use` entirely is left alone, because "this
        library has no `block` tag at all" and "keep whatever is in `block`" are
        different questions and only the caller knows which one it asked. A
        namespace listed with no values is emptied, save for its defaults.

        Sets `dirty` when it removes anything. Sparing a default does not, since
        it changed nothing.
        """
        removed: list[tuple[str, str]] = []
        protected: list[tuple[str, str]] = []
        for namespace, values in (in_use or {}).items():
            canonical = canonical_tag_name(namespace) or namespace
            if canonical not in self._values:
                continue
            keep = {
                _dedup_key(_normalize_value(value))
                for value in values
                if isinstance(value, str) and _normalize_value(value)
            }
            entries = self._values[canonical]
            for dedup in [key for key in entries if key not in keep]:
                if _is_shipped_default(canonical, dedup):
                    protected.append((canonical, entries[dedup]))
                    continue
                removed.append((canonical, entries.pop(dedup)))
        if removed:
            self.dirty = True
        return PruneResult(removed=tuple(removed), protected=tuple(protected))

    def save(self) -> str:
        """Write the vocabulary out, atomically.

        Staged in the file's own directory and moved into place, so an
        interrupted write cannot leave a half-file where the real one goes. No
        fsync: this file is a cache, and the durability would be paid on every
        Stage for a file whose loss costs nothing.
        """
        target = self.path or vocabulary_path()
        document = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        data = document.encode("utf-8")
        directory = os.path.dirname(target)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".commcut-vocabulary-", suffix=".tmp", dir=directory or "."
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
            os.replace(temporary_path, target)
        except BaseException:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise
        self.dirty = False
        return target

    # --- construction ---

    @classmethod
    def defaults(cls, path: str | None = None) -> "Vocabulary":
        """A fresh vocabulary from the shipped defaults."""
        vocabulary = cls(path=path)
        for namespace, values in DEFAULT_VALUES.items():
            for value in values:
                vocabulary._record_one(namespace, value)
        vocabulary.dirty = False
        return vocabulary

    @classmethod
    def load(cls, path: str | None = None) -> "Vocabulary":
        """Read the vocabulary, falling back to the defaults for anything unusable.

        A missing file, unreadable JSON, a wrong shape, a version this build
        does not know -- all of them mean "there is no usable vocabulary here",
        which is the same situation as a fresh install and deserves the same
        answer. Refusing would make the one action that fixes a bad file the
        thing that fails because of it.
        """
        target = path or vocabulary_path()
        try:
            with open(target, encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return cls.defaults(target)
        except (OSError, ValueError) as error:
            log(f"vocabulary unreadable, starting from defaults: {error}")
            return cls.defaults(target)

        if not isinstance(data, dict):
            log("vocabulary is not an object, starting from defaults")
            return cls.defaults(target)

        version = data.get("version")
        if version != VOCABULARY_SCHEMA_VERSION:
            log(f"vocabulary version {version!r} is not "
                f"{VOCABULARY_SCHEMA_VERSION}, starting from defaults")
            return cls.defaults(target)

        raw_tags = data.get("tags")
        if not isinstance(raw_tags, dict):
            log("vocabulary has no tags mapping, starting from defaults")
            return cls.defaults(target)

        vocabulary = cls(path=target)
        for namespace, values in raw_tags.items():
            if canonical_tag_name(namespace) is None or namespace not in CANONICAL_TAG_KEYS:
                log(f"vocabulary holds an unknown tag namespace {namespace!r}")
                continue
            if not isinstance(values, list):
                log(f"vocabulary namespace {namespace!r} is not a list")
                continue
            # One call per value, not a dict built from the list: a comprehension
            # would key on the namespace and keep only the last value in it.
            for value in values:
                if isinstance(value, str):
                    vocabulary._record_one(namespace, value)
        vocabulary.dirty = False
        return vocabulary


#: Keyed on the resolved path rather than a bare singleton, so a caller that
#: redirects install_root (every editor test does) gets its own instance with no
#: reset hook -- and no test can inherit another's values by running after it.
_CACHE: dict[str, Vocabulary] = {}


def get_vocabulary(path: str | None = None) -> Vocabulary:
    """The process-wide vocabulary, read once and then held."""
    target = path or vocabulary_path()
    if target not in _CACHE:
        _CACHE[target] = Vocabulary.load(target)
    return _CACHE[target]


def record_use(tags: Mapping[str, str], path: str | None = None) -> bool:
    """Note the tags of a committed segment, persisting if that changed anything.

    Returns whether the file changed, which is the editor's cue to repopulate
    its dropdowns -- repopulating on every stage whether or not anything was new
    would clear and refill ten fields to no purpose.
    """
    vocabulary = get_vocabulary(path)
    if not vocabulary.record(tags):
        return False
    try:
        vocabulary.save()
    except OSError as error:
        # A vocabulary that cannot be saved is an inconvenience, not a failure:
        # every value in it is still in the .cmct and the records, and the next
        # stage will try again.
        log(f"vocabulary could not be saved: {error}")
        vocabulary.dirty = False
    return True


def forget_cached_vocabulary(path: str | None = None) -> None:
    """Drop the cached instance for a path. For tests; production never needs it."""
    _CACHE.pop(path or vocabulary_path(), None)