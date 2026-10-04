"""Turning somebody else's tag values into yours: the Tagged Library Mesh's model.

Applies to: `shared/values.py`, `importer/values.py`, `shared/importing.py`
(`match_value`, `ValueMatch`), `shared/catalog.py` (`Catalog`), `shared/records.py`.

An imported library's records are **correct in their own vocabulary**. `network: CN`
and `filler_type: Promo (30s)` mean exactly what the person who wrote them meant;
they are simply not the words this library uses. The Untagged Library Mesh fixed the
*tags* — it decided that a folder called `Cartoon Network` is a network — and left
the values alone, because a folder name is only ever one value. What is left is the
other half of the same job: for each value that is in play, is this the same thing as
one of mine, and if not what should it be?

Three properties are **structural**, not conventions anybody has to remember:

- **A session starts empty.** Every entry is `UNTRANSLATED`, and only `translate`,
  `delete_tag` or `keep` changes that. A value that matches the user's library
  exactly is a *suggestion* the screen pre-fills; the user still presses a button.
  There is no code path that fills the table without an explicit call.
- **A value is one entry, whatever its clip count.** `CN` on 214 clips is one
  question, because a value is shared and a title is not. That is the whole reason
  this is a table rather than a second per-clip queue, and it is why the residue —
  the clips still missing a required tag — is left to the queue that already exists.
- **A value becomes another value, or stops existing. Nothing else.** There is no
  namespace parameter on any of the three answers, because a translation that could
  move a tag between namespaces would be a different operation with a different set
  of conflicts. See the "Why not MeshSession" note below.

Nothing here holds a Qt type. `ValueSession.next_prompt()` answers every question the
window has; the window renders that and does no deciding of its own — the same
division of labour `shared/mesh.py` is built on.

Why not `MeshSession`
------------------------------------------------------------------------------

It is tempting to run the existing Wizard again in a "tagged" mode. That would be
wrong, and not for want of shared machinery but because the collision rule is
actively the wrong rule:

- `shared/mesh.py:accumulate` resolves *two claims on one namespace* by taking
  **neither** value and flagging the clip, and `MeshSession._shares_a_path` decides
  when that can happen.
- For a value rename, two *different* values resolving to one namespace is the
  **normal** case — `CN` -> `Cartoon Network` and `CN2` -> `Cartoon Network Two` —
  and no clip ever sees both, because a record holds one value per namespace.

`docs/importing.md` records that exact mistake being made once in the folder wizard
and fixed: a version that warned on every second `filler_type` folder, which fires on
the ordinary shape of any real library and is therefore dismissed reflexively. A
warning that fires on the normal case is worse than no warning for the one case that
matters. Here the same reuse would refuse to translate `CN` at all.

The two tables are also keyed differently, and deliberately so: `AliasEntry` is keyed
by one string, because a folder name is exactly itself, while an entry here is keyed
by `(namespace, value)`. A folder named `Promo` meshed to `filler_type:Promo` and a
record holding `filler_type:Promo` are **not** the same answer, and must not be made
to be one — they are different phases over different data, the folder pass happening
before any record exists and this one after. Folding them into a single table would
be the bug, not the safety.

What *is* shared is the evidence: `match_value` is the same ranked, counted answer to
"where do I already say this?", and invariant 9 wants one owner of it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from shared.catalog import Catalog
from shared.diagnostics import log
from shared.importing import ValueMatch, match_value
from shared.records import ClipRecord, RecordError, render_record_xml, write_record
from shared.scheme import CANONICAL_TAG_KEYS
from shared.vocabulary import value_dedup_key

#: A value's state. `untranslated` is the absence of an answer; the other three are
#: answers a person gave.
UNTRANSLATED = "untranslated"
KEPT = "kept"
TRANSLATED = "translated"
DELETED = "deleted"

#: The states that count as answered.
ANSWERED = (KEPT, TRANSLATED, DELETED)

#: The one tag a value may not be asked about.
#:
#: A title is unique per clip, so there is no shared value to translate: "translate
#: `Toonami Ep 12`" would rename every clip with that title, which is a bulk title
#: edit and not this window's business. This is the same exclusion
#: `shared.mesh.namespace_choices` and `MeshSession.learn_rule` already make, for the
#: same reason. Renaming a clip's title is the Rename Wizard's job.
TITLE_TAG = "title"

#: The shape `ValueTable.to_dict` writes. Nothing writes one yet — see
#: `docs/importing.md` — but the shape is settled here so persistence is an additive
#: change rather than a redesign.
VALUE_TABLE_VERSION = 1


def translatable_namespaces() -> tuple[str, ...]:
    """The namespaces a value may be asked about: every canonical tag but `title`."""
    return tuple(sorted(name for name in CANONICAL_TAG_KEYS if name != TITLE_TAG))


# ---------------------------------------------------------------------------
# What one value became
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValueEntry:
    """One tag value in `import/`, and what a person decided it means here.

    Keyed by `(namespace, value)` rather than by a name, because a value only means
    something inside its namespace: `Promo` is a `filler_type` and something else is
    a `block`, and the two answers are unrelated.
    """

    namespace: str
    #: The value as it appears, which is what is shown and what a record holds.
    value: str
    state: str = UNTRANSLATED
    #: Where this value goes. Set only for `TRANSLATED`.
    translated_to: str = ""
    #: Clips in the import folder carrying it. Display only.
    clip_count: int = 0
    #: A clip that carries it. Display only, and deliberately not serialized: it is
    #: recomputed from the tree every run, so persisting it would freeze a path that
    #: goes stale the moment a folder moves.
    example_path: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """The identity used for lookup and for matching a record's tag.

        The value's part is `value_dedup_key`, so `Promo` and `PROMO` are one entry.
        They render to one folder, so asking about them separately would invite the
        user to give them different meanings and file two spellings of one thing.
        This is the app's identity rule for a value and it has one owner —
        `shared.vocabulary.value_dedup_key`.
        """
        return (self.namespace, value_dedup_key(self.value))

    @property
    def is_answered(self) -> bool:
        return self.state in ANSWERED

    def describe(self) -> str:
        """One line for the table."""
        if self.state == TRANSLATED:
            return f"{self.namespace}: {self.value}  ->  {self.translated_to}"
        if self.state == DELETED:
            return f"{self.namespace}: {self.value}  —  tag removed"
        if self.state == KEPT:
            return f"{self.namespace}: {self.value}  —  kept"
        return f"{self.namespace}: {self.value}"

    def resolved_value(self) -> str:
        """What this value becomes, or "" when it stops existing.

        The single place a state is read to produce a tag. `UNTRANSLATED` resolves to
        the value it already is, because a question nobody has answered yet must not
        change anything — the session is complete before anything is written, and a
        half-answered one is never planned at all.
        """
        if self.state == TRANSLATED:
            return self.translated_to
        if self.state == DELETED:
            return ""
        return self.value


@dataclass(frozen=True)
class ValueMerge:
    """Two or more distinct values that would end up as one.

    Legal, and sometimes exactly what is wanted — `Promo` and `Advertisement` are
    often the same thing and merging them is the point. But it is the one outcome of
    this window that makes two clips indistinguishable, so it is reported rather than
    discovered later in an import summary.
    """

    namespace: str
    #: The value they all become.
    value: str
    #: The distinct values that would land on it, sorted.
    sources: tuple[str, ...]

    def describe(self) -> str:
        listed = ", ".join(f"'{source}'" for source in self.sources)
        return (f"{self.namespace}: {listed} would all become '{self.value}', so "
                f"those clips would be indistinguishable from each other afterwards.")


@dataclass(frozen=True)
class ValuePrompt:
    """Everything the screen needs to show one question.

    A plain value, like `shared.mesh.MeshPrompt`, so the window renders it and the
    tests read it and the two cannot disagree about what is being asked.
    """

    #: The entry being asked about, carried whole rather than as loose fields so the
    #: screen and the test are looking at the same thing.
    entry: ValueEntry
    #: What the user already calls this value in *this* namespace, best evidence
    #: first. `match_value` filtered to the namespace, because a `block` called
    #: `Promo` says nothing about what a `filler_type` called `Promo` should be.
    matches: tuple[ValueMatch, ...]
    #: Values still waiting for an answer.
    untranslated: int
    kept: int
    translated: int
    deleted: int


@dataclass(frozen=True)
class ValueTable:
    """The session's output: every answer, in one serialisable value.

    `to_dict` is what a `value-map.json` would hold; `from_dict` is what a future run
    would read. Nothing writes one today, so re-running asks the same questions again
    — which is the honest state, and the shape is settled so it is not a redesign
    either.
    """

    entries: tuple[ValueEntry, ...] = ()

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict:
        return {
            "version": VALUE_TABLE_VERSION,
            "entries": [
                {"namespace": e.namespace, "value": e.value, "state": e.state,
                 "translated_to": e.translated_to}
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ValueTable":
        """Read a table back.

        Tolerates an unknown state by treating the entry as untranslated rather than
        raising, for the reason `AliasTable.from_dict` does: a file written by a
        newer build should cost the user their answers, not the whole table.
        Everything else is refused, because a half-read value map is how a tag gets
        applied wrongly.
        """
        version = data.get("version")
        if version != VALUE_TABLE_VERSION:
            raise ValueError(
                f"value table version {version!r} is not version "
                f"{VALUE_TABLE_VERSION}"
            )
        entries = []
        for raw in data.get("entries", []):
            state = raw.get("state")
            namespace = raw.get("namespace")
            if namespace not in translatable_namespaces():
                raise ValueError(
                    f"{namespace!r} is not a tag a value can be translated on"
                )
            entries.append(ValueEntry(
                namespace=namespace,
                value=raw["value"],
                state=state if state in ANSWERED else UNTRANSLATED,
                translated_to=raw.get("translated_to", "") or "",
            ))
        return cls(entries=tuple(entries))


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

class ValueSession:
    """One run of the Tagged Library Mesh over one folder of tagged clips.

    `catalog` is `build_catalog(import_folder())`, which yields **only clips a
    record backs** — exactly the set this window is about. `library` is the
    destination library's catalog and `vocabulary` a `Vocabulary`, both optional and
    both only ever read: the mesh changes neither, it only ranks suggestions by them.
    """

    def __init__(
        self,
        root: str,
        catalog: Catalog | None,
        library=None,
        vocabulary=None,
    ) -> None:
        self.root = os.path.abspath(os.fspath(root))
        #: Held rather than re-walked by the planner, so a caller cannot hand
        #: `plan_translation` a catalog the session was not built from and get an
        #: answer about the wrong folder.
        self.catalog = catalog if catalog is not None else Catalog()
        self._library = library
        self._vocabulary = vocabulary
        #: (namespace, dedup key) -> entry. Two parts because a value only means
        #: something inside its namespace.
        self._entries: dict[tuple[str, str], ValueEntry] = {}

        for clip in self.catalog.clips:
            for namespace, value in clip.tags:
                if not value or namespace == TITLE_TAG:
                    continue
                if namespace not in CANONICAL_TAG_KEYS:
                    continue
                key = (namespace, value_dedup_key(value))
                existing = self._entries.get(key)
                if existing is None:
                    self._entries[key] = ValueEntry(
                        namespace=namespace, value=value, clip_count=1,
                        example_path=clip.relative_path,
                    )
                else:
                    self._entries[key] = replace(
                        existing, clip_count=existing.clip_count + 1)

    # -- introspection ----------------------------------------------------

    @property
    def library(self):
        """The destination library's catalog, or None. Read only."""
        return self._library

    @property
    def vocabulary(self):
        """The vocabulary file, or None. The weaker of the two kinds of evidence, and
        labelled as such wherever it is shown."""
        return self._vocabulary

    def entries(self) -> tuple[ValueEntry, ...]:
        """Every value in the folder, sorted for display."""
        return tuple(sorted(
            self._entries.values(),
            key=lambda e: (e.namespace, e.value.casefold()),
        ))

    def entry(self, namespace: str, value: str) -> ValueEntry:
        """The entry for one value, raising `KeyError` when there is not one.

        The raising counterpart to `find`, for a caller that has just read the value
        off a prompt and means it.
        """
        found = self.find(namespace, value)
        if found is None:
            raise KeyError(f"{namespace}: {value!r} is not a value in {self.root}")
        return found

    def find(self, namespace: str, value: str) -> ValueEntry | None:
        """What this value already means here, or None.

        The non-raising counterpart, for a caller asking a question rather than
        reporting a mistake about a value that should be there.
        """
        return self._entries.get((namespace, value_dedup_key(value)))

    def pending(self) -> tuple[tuple[str, str], ...]:
        """`(namespace, value)` pairs still waiting for an answer, sorted."""
        return tuple(sorted(
            (key for key, entry in self._entries.items()
             if entry.state == UNTRANSLATED),
            key=lambda key: (key[0], self._entries[key].value.casefold()),
        ))

    def is_complete(self) -> bool:
        """True when every value has been answered."""
        return not self.pending()

    def counts(self) -> tuple[int, int, int, int]:
        """(kept, translated, deleted, untranslated), as one tuple so a caller cannot
        read two of them at different moments."""
        kept = sum(1 for e in self._entries.values() if e.state == KEPT)
        translated = sum(1 for e in self._entries.values() if e.state == TRANSLATED)
        deleted = sum(1 for e in self._entries.values() if e.state == DELETED)
        return kept, translated, deleted, len(self._entries) - kept - translated - deleted

    def clip_count(self) -> int:
        """Clips in the folder this session knows about."""
        return len(self.catalog.clips)

    # -- deciding ---------------------------------------------------------

    def _check_unanswered(self, namespace: str, value: str) -> ValueEntry:
        entry = self._entries.get((namespace, value_dedup_key(value)))
        if entry is None:
            raise KeyError(
                f"{namespace}: {value!r} is not a value in {self.root}")
        if entry.state != UNTRANSLATED:
            raise ValueError(
                f"{namespace}: {value!r} has already been {entry.state}; an answer "
                f"is given once and applies to every clip carrying that value"
            )
        return entry

    def translate(self, namespace: str, value: str, new_value: str) -> ValueEntry:
        """Tie this value to another one, in the same namespace.

        The only way an entry becomes `translated`. The namespace is not a parameter
        of the *change* — it is the entry's — so there is no call that can move a tag
        from one namespace to another, and nothing downstream has to check.

        The new value is validated by **rendering** it into a record rather than by a
        hand-written character check, because `shared/records.py` has the real rule
        (`_require_xml_safe_text`) and it is the thing that will refuse the write. A
        value the record could never hold is found out here, at the question, rather
        than once per clip at the end of a run.
        """
        entry = self._check_unanswered(namespace, value)
        if namespace not in translatable_namespaces():
            raise ValueError(
                f"{namespace!r} is not a tag a value can be translated on. A value "
                f"may become another value or stop existing, but it cannot move "
                f"between tags."
            )
        new_value = (new_value or "").strip()
        if not new_value:
            raise ValueError(
                "A translated value needs somewhere to go. Either one of your own "
                "or one of your own making — or remove the tag instead."
            )
        try:
            render_record_xml(ClipRecord(
                source="value-mesh", segment_index=0, start=0.0, duration=0.0,
                tags=((namespace, new_value),),
            ))
        except (RecordError, ValueError) as error:
            raise ValueError(
                f"{new_value!r} could not be stored as a tag value: {error}"
            ) from error

        translated = replace(entry, state=TRANSLATED, translated_to=new_value)
        self._entries[entry.key] = translated
        return translated

    def delete_tag(self, namespace: str, value: str) -> ValueEntry:
        """Decide this value has no equivalent here, and the tag goes.

        Not a fourth state and not a special case of translating: a record holds one
        value per tag, so "this tag should not be on the clip at all" is a genuinely
        different answer from "it should be called something else", and the one thing
        a foreign library routinely carries that this library has no word for is a
        tag rather than a spelling.

        Deleting a **required** tag does not fail — it sends the clips carrying it to
        the Tag Editor, which asks for it by name. That is the designed path.
        """
        entry = self._check_unanswered(namespace, value)
        deleted = replace(entry, state=DELETED, translated_to="")
        self._entries[entry.key] = deleted
        return deleted

    def keep(self, namespace: str, value: str) -> ValueEntry:
        """Decide this value is already one of mine.

        An answer, not a no-op: it is what takes a value out of the pending set, and
        it is the answer given most often on a library whose author used the same
        words this library does.
        """
        entry = self._check_unanswered(namespace, value)
        kept = replace(entry, state=KEPT, translated_to="")
        self._entries[entry.key] = kept
        return kept

    # -- asking -----------------------------------------------------------

    def _best_key(self) -> tuple[str, str] | None:
        """The value on the most clips, and its key.

        Most-first for the same reason `MeshSession._best_chain` is: the value that
        decides the most is the one worth showing, and the rest are then cheap
        because the answer applies to all of them. Ties break on the sorted key, so
        a session is reproducible.
        """
        best: tuple[str, str] | None = None
        best_count = 0
        for key in self.pending():
            count = self._entries[key].clip_count
            if best is None or count > best_count:
                best, best_count = key, count
        return best

    def next_prompt(self) -> ValuePrompt | None:
        """The next question, or None when every value has been answered.

        The whole of the mesh's decision-making. The window asks for this, shows it,
        and calls `translate`, `delete_tag` or `keep` with the answer.
        """
        key = self._best_key()
        if key is None:
            return None
        entry = self._entries[key]
        kept, translated, deleted, untranslated = self.counts()
        return ValuePrompt(
            entry=entry,
            matches=self.matches_for(entry.namespace, entry.value),
            untranslated=untranslated,
            kept=kept,
            translated=translated,
            deleted=deleted,
        )

    def matches_for(self, namespace: str, value: str) -> tuple[ValueMatch, ...]:
        """What the user already calls this value, in *this* namespace, best first.

        `match_value` answers across every namespace, which is the right question for
        the folder wizard — "which of my tags does this word mean?" — and the wrong
        one here, where the tag is already settled and only the spelling is in
        question. A `block` called `Promo` says nothing about what a `filler_type`
        called `Promo` should become.

        Ranked rather than chosen, and an empty result is **not** permission to
        guess: it means the value is new here and the caller has to ask.
        """
        return tuple(
            match for match in match_value(value, self._library, self._vocabulary)
            if match.namespace == namespace
        )

    # -- the outcome ------------------------------------------------------

    def merges(self) -> tuple[ValueMerge, ...]:
        """Distinct values that would end up as one, per namespace.

        The one outcome of this window that makes two clips indistinguishable, and
        it is legal — merging two names for one thing is frequently the point. It is
        computed from the answers rather than from the folder, so it is only
        meaningful once the session is complete, and it is reported rather than
        refused.

        Keyed on the resolved value's own key, and reported with the value the clips
        will actually carry — which is the spelling the user typed as the translation,
        and the only one they will recognise in the list afterwards.
        """
        targets: dict[tuple[str, str], tuple[str, set[str]]] = {}
        for entry in self._entries.values():
            value = entry.resolved_value()
            if not value:
                continue
            key = (entry.namespace, value_dedup_key(value))
            if key in targets:
                _, sources = targets[key]
            else:
                sources = set()
                targets[key] = (value, sources)
            sources.add(entry.value)

        merges: list[ValueMerge] = []
        for (namespace, _key), (value, sources) in targets.items():
            if len(sources) < 2:
                continue
            merges.append(ValueMerge(
                namespace=namespace, value=value,
                sources=tuple(sorted(sources, key=str.casefold)),
            ))
        return tuple(sorted(merges, key=lambda m: (m.namespace, m.value.casefold())))

    def table(self) -> ValueTable:
        """Every answer. The session's output."""
        return ValueTable(entries=tuple(
            entry for entry in self.entries() if entry.is_answered
        ))

    def report(self) -> str:
        """The whole outcome as text.

        A pure function of the session, and what both the completion screen and the
        tests read — so the two cannot disagree about what happened.
        """
        kept, translated, deleted, untranslated = self.counts()
        # The folder's clip count, not the sum of the entries' — that sum counts
        # tag-clip pairs, so three clips sharing a network would report four "clips"
        # and the screen would be quoting a number that does not mean anything.
        lines = [
            f"Read {len(self._entries)} tag value(s) from {self.root}, across "
            f"{self.clip_count()} clip(s)",
            f"  {translated} translated, {deleted} tag(s) removed, "
            f"{kept} kept, {untranslated} left to go",
        ]

        table = self.table()
        if table.entries:
            lines.append("")
            lines.append("Answers:")
            lines.extend(
                f"  - {entry.describe()}   ({entry.clip_count} clip(s))"
                for entry in table.entries
            )

        merges = self.merges()
        if merges:
            lines.append("")
            lines.append(
                f"{len(merges)} value(s) will end up as one, so those clips will be "
                f"indistinguishable from each other:")
            lines.extend(f"  - {merge.describe()}" for merge in merges)

        if untranslated:
            lines.append("")
            lines.append(
                f"{untranslated} value(s) have no answer, so nothing will be written "
                f"for them and they will keep the values they arrived with."
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

#: Why a clip was left alone, as a code rather than a sentence, so a caller can group
#: a long report. `REASON_UNREADABLE` is `shared.catalog`'s own code, reused rather
#: than restated.
REASON_WRITE_FAILED = "record-write-failed"
REASON_INVALID_TAGS = "invalid-tags"


@dataclass(frozen=True)
class ValueSkip:
    """One clip's record that could not be rewritten, and why.

    Carried by name, for the reason `ImportSkip` is: a silently skipped clip is one
    the user cannot find afterwards, which is the failure every summary screen in
    this app exists to stop.
    """

    record_path: str
    relative_path: str
    reason: str
    reason_text: str

    def __str__(self) -> str:
        return f"{self.relative_path}: {self.reason_text}"


@dataclass(frozen=True)
class PlannedValueClip:
    """One record to rewrite, and what it becomes."""

    record_path: str
    #: For display and for the report, so nothing has to carry an absolute path.
    relative_path: str
    record: ClipRecord
    #: True when this clip's tags are only changed by sharing a value with another
    #: clip that is being rewritten — i.e. it is one end of a merge.
    merges: bool = False


@dataclass(frozen=True)
class TranslationPlan:
    """What a translation run will do, resolved before anything is written."""

    clips: tuple[PlannedValueClip, ...] = ()
    #: Clips whose record would become invalid, or cannot be read. Never silently
    #: empty.
    skipped: tuple[ValueSkip, ...] = ()
    #: The merges this plan causes, carried so the screen can say so before and
    #: after rather than only in a report the user reads afterwards.
    merges: tuple[ValueMerge, ...] = ()


def _validated_record(clip, tags: list[tuple[str, str]]) -> ClipRecord:
    """A record carrying `tags`, checked by rendering it.

    Every field other than the tags is carried over from the record that was read, so
    a translation never invents provenance or a duration: it changes what the clip is
    *called*, and nothing else about where it came from.
    """
    return ClipRecord(
        source=clip.record.source,
        segment_index=clip.record.segment_index,
        start=clip.record.start,
        duration=clip.record.duration,
        tags=tuple(sorted(tags)),
    )


def plan_translation(session: ValueSession) -> TranslationPlan:
    """Resolve every clip's record to the values the session decided, and check it.

    Per-clip and all-or-nothing *per record*: a clip whose new tags would not render
    is skipped by name and the rest are still rewritten. That is the same bargain
    `plan_import` strikes — one bad record should cost one clip, not the batch — for
    the same reason.

    Clips nothing would change are **not** in the plan and their records are not
    touched. A user who answered "keep" to everything gets a run that writes
    nothing, which is the honest outcome rather than eight hundred rewrites of
    identical content.

    Refuses to plan a session with questions still open. An unanswered value resolves
    to itself, so a half-answered session would silently write the folder's own
    values into records — which is the one thing this window exists to stop.
    """
    if not isinstance(session, ValueSession):
        raise TypeError("session must be a ValueSession")
    if not session.is_complete():
        untranslated = session.counts()[3]
        raise ValueError(
            f"{untranslated} value(s) in {session.root} have no answer yet. Every "
            f"value has to be translated, kept, or removed before anything can be "
            f"written — an unanswered one would keep the value it arrived with, "
            f"which is what this window exists to change."
        )

    resolved: dict[tuple[str, str], str] = {}
    dropped: set[tuple[str, str]] = set()
    for entry in session.entries():
        value = entry.resolved_value()
        if value:
            resolved[entry.key] = value
        else:
            dropped.add(entry.key)

    def new_tags(clip) -> list[tuple[str, str]]:
        tags: list[tuple[str, str]] = []
        for namespace, value in clip.tags:
            if not value or namespace == TITLE_TAG:
                tags.append((namespace, value))
                continue
            key = (namespace, value_dedup_key(value))
            if key in dropped:
                continue
            tags.append((namespace, resolved.get(key, value)))
        return tags

    merges = session.merges()
    merged_namespaces = {merge.namespace for merge in merges}

    clips: list[PlannedValueClip] = []
    skipped: list[ValueSkip] = []
    for clip in session.catalog.clips:
        tags = new_tags(clip)
        if tags == sorted(clip.tags):
            continue
        try:
            record = _validated_record(clip, tags)
            render_record_xml(record)
        except (RecordError, ValueError) as error:
            skipped.append(ValueSkip(
                record_path=clip.record_path, relative_path=clip.relative_path,
                reason=REASON_INVALID_TAGS, reason_text=str(error),
            ))
            continue
        clips.append(PlannedValueClip(
            record_path=clip.record_path,
            relative_path=clip.relative_path,
            record=record,
            merges=any(namespace in merged_namespaces for namespace, _ in tags),
        ))

    return TranslationPlan(
        clips=tuple(clips), skipped=tuple(skipped), merges=merges)


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TranslationResult:
    """What a translation run actually did.

    Three lists and a flag, because "translated 214 clips" is the number a bug
    produces. Every record this run committed stays committed, and every one it did
    not is named.
    """

    #: Records written, in the order they were committed.
    written: tuple[str, ...] = ()
    #: Records that could not be rewritten, each with its reason.
    failed: tuple[ValueSkip, ...] = ()
    cancelled: bool = False

    @property
    def committed(self) -> int:
        return len(self.written)


def execute_translation(
    plan: TranslationPlan,
    on_progress: Callable[[int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> TranslationResult:
    """Rewrite every record in `plan`, and report exactly what happened.

    **Only records are touched.** The video is never opened, moved or rewritten, so
    invariant 12 — `video present` implies `record present` — holds throughout: a
    record is only ever *replaced*, through `shared.records.write_record`, which
    publishes atomically in the record's own directory. There is no instant at which
    a reader could see a half-written record or a record without its video.

    A cancelled run stops and says so, and **keeps everything it committed**, the same
    rule the export and the import follow. Re-planning after a cancellation finishes
    the rest, because `plan_translation` is a pure function of the records as they
    stand.

    One failing record does not stop the run. Its destination is a file that was never
    created, so there is nothing to clean up.
    """
    if on_progress is None:
        on_progress = lambda _done, _path: None  # noqa: E731
    if should_cancel is None:
        should_cancel = lambda: False  # noqa: E731

    written: list[str] = []
    failed: list[ValueSkip] = []

    for position, clip in enumerate(plan.clips):
        if should_cancel():
            return TranslationResult(
                written=tuple(written), failed=tuple(failed), cancelled=True)
        on_progress(position, clip.relative_path)
        try:
            write_record(clip.record_path, clip.record)
        except (OSError, ValueError) as error:
            log(f"a translated record could not be written: {clip.relative_path}")
            failed.append(ValueSkip(
                record_path=clip.record_path, relative_path=clip.relative_path,
                reason=REASON_WRITE_FAILED,
                reason_text=f"{clip.relative_path} could not be rewritten: {error}",
            ))
            continue
        written.append(clip.relative_path)

    on_progress(len(plan.clips), "")
    return TranslationResult(written=tuple(written), failed=tuple(failed))
