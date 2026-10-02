"""Turning folder names into tags: the Library Mesh Wizard's model.

Applies to: `shared/mesh.py`, `shared/importing.py` (`match_value`,
`ValueMatch`, `find_videos`), `shared/catalog.py`, `shared/vocabulary.py`.

An untagged library tells you almost nothing about its clips, and the one thing it
does tell you is the folder names. `Cartoon Network/2000s/Promo/` says a network, a
period and a kind; that is three tags' worth of information and it is *exactly*
three tags' worth — no more. This module is how a person turns those folder names
into tags, one question at a time.

It is the one place in the app that looks at a **path** for meaning, and the
distinction from the reverse parser `docs/naming-and-organization.md` refuses is
narrow enough to be worth stating precisely:

> The result is a **user-authored mapping**, from a folder name to a tag the user
> explicitly chose. It is never *inferred*. Nothing here reads a name and concludes
> a tag; a name becomes a tag only through `MeshSession.assign`, which a person
> called. A proposal engine would violate the rule, and one was removed rather than
> left unused — see `shared/importing.py`, where filename parsing is now a
> documented gap rather than dead code.

Three properties are **structural**, not conventions anybody has to remember:

- **A session starts empty.** Every entry is `unmeshed`, and only `assign` or
  `reject` changes that. A folder name matching the library exactly is a
  *suggestion* the screen pre-selects; the user still presses Assign. There is no
  code path that fills the table without an explicit call.
- **A folder name is one entry, whatever its path count.** `CN` in forty folders is
  one decision applying to all of them. That is what makes the alias idea work, and
  it is why a *path* is needed for display: context, not identity.
- **A name, once meshed or rejected, never comes back.** `pending()` is derived
  from state, so there is nothing to forget and nothing to reconcile.

Nothing here holds a Qt type. `MeshSession.next_prompt()` answers every question
the window has; the window renders that and does no deciding of its own.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from shared.importing import FoundVideo, ValueMatch, match_value
from shared.paths import normalized_validation_key
from shared.scheme import CANONICAL_TAG_KEYS
from shared.vocabulary import Vocabulary

#: A folder name's state. `rejected` is distinct from `unmeshed` because Reject is
#: a *decision* -- "this folder's name is not a tag" -- while unmeshed is the
#: absence of one. The Manual Edit queue, when it exists, is a third destination
#: and needs no change here.
UNMESHED = "unmeshed"
MESHED = "meshed"
REJECTED = "rejected"

#: Palette keys for the path bar. Chosen for being tellable apart at a glance and
#: for surviving on both light and dark backgrounds; cycled when a path has more
#: segments than there are colours.
COLOURS = (
    "royalblue", "darkorange", "seagreen", "crimson",
    "purple", "teal", "sienna", "slateblue",
)

#: How many folders in a chain are "enough to be worth showing". A deep library
#: would otherwise print its whole path on every page.
MAX_PATH_DEPTH = 6


# ---------------------------------------------------------------------------
# What one folder name became
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AliasEntry:
    """One folder name, and what a person decided about it."""

    #: The name exactly as it appears on disk, because that is what has to match
    #: when the table is applied to a path.
    name: str
    state: str
    namespace: str | None = None
    value: str | None = None
    #: Videos whose path contains a folder of this name. Display only, but it is
    #: the number that tells a user how much a decision affects.
    paths: int = 0


@dataclass(frozen=True)
class PathSegment:
    """One folder name on the path being shown, for colouring."""

    name: str
    colour: str
    state: str
    is_current: bool = False


@dataclass(frozen=True)
class MeshConflict:
    """Two folder names that would give **one clip** two values for one namespace.

    Only two folder names that appear in the *same* folder path can do that. A
    library laid out as `Promo/…` and `Bumper/…` is a dozen folders all meaning
    `filler_type`, which is the ordinary shape of any real library and **not** a
    conflict: no clip ever sees both. A path like `Up Next/Promo/A.mp4` where both
    folders mean `filler_type` is the case, because that one clip would be handed
    two values.

    So the test is co-occurrence, not namespace reuse. An earlier version compared
    two folder names globally and warned on every second `filler_type` folder in a
    library, which is a warning that fires on the normal case and is therefore
    dismissed reflexively — the worst possible fate for the one case that matters.
    """

    namespace: str
    existing_name: str
    existing_value: str
    incoming_name: str
    incoming_value: str
    #: A path both folder names appear on, so the user can see which clips are
    #: affected rather than being asked to take it on trust.
    example_path: str = ""

    def describe(self) -> str:
        """What goes wrong, in words, with somewhere to look.

        Deliberately not phrased as one folder *claiming* a namespace. A
        namespace is a kind of tag, not a slot to be owned: a library
        legitimately has a dozen folder names mapped to `filler_type`, and
        wording it as exclusivity would make the ordinary case read as a fault.

        What actually goes wrong is narrower and worse: **one clip ends up with
        two values for one tag**, and a record holds one. So the message says that,
        shows a path where it happens, and says those clips need a person.
        """
        where = (f"\n\nIt happens in:\n  {self.example_path}"
                 if self.example_path else "")
        return (
            f"'{self.incoming_name}' and '{self.existing_name}' both appear in "
            f"the same folder path.{where}\n\n"
            f"A clip in that folder would get {self.namespace} twice: "
            f"'{self.existing_value}' from one and '{self.incoming_value}' from "
            f"the other. A clip record holds one value per tag, so commcut cannot "
            f"choose for you and those clips will need editing by hand."
        )


@dataclass(frozen=True)
class MeshPrompt:
    """Everything the screen needs to show one question.

    A plain value rather than a widget description: the window renders it and the
    tests read it, so the two cannot disagree about what is being asked.
    """

    name: str
    segments: tuple[PathSegment, ...]
    #: Pre-selected in the namespace dropdown, or None. Set only when there is
    #: exactly one match: "if Toonami is only present in the block namespace,
    #: assume it is a block", and with two candidates assuming either one is the
    #: guess the wizard is supposed to prevent.
    suggested_namespace: str | None
    #: Ranked existing values, most evidence first. The pre-selection.
    suggested_values: tuple[ValueMatch, ...]
    #: Videos still under at least one un-meshed folder name.
    unmeshed_paths: int
    meshed: int
    rejected: int


@dataclass(frozen=True)
class AliasTable:
    """The session's output: every decision, in one serialisable value.

    In-memory for now, because the wizard is still being designed -- but the shape
    is settled here so persistence is a later additive change rather than a
    redesign. `to_dict` is what a `folder-aliases.json` would hold; `from_dict` is
    what a future run would read.
    """

    entries: tuple[AliasEntry, ...] = ()

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def meshed(self) -> tuple[AliasEntry, ...]:
        return tuple(e for e in self.entries if e.state == MESHED)

    @property
    def rejected(self) -> tuple[AliasEntry, ...]:
        return tuple(e for e in self.entries if e.state == REJECTED)

    def to_dict(self) -> dict:
        return {
            "version": ALIAS_TABLE_VERSION,
            "entries": [
                {"name": e.name, "namespace": e.namespace, "value": e.value,
                 "state": e.state}
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "AliasTable":
        """Read a table back.

        Tolerates an unknown state by treating the entry as unmeshed rather than
        raising: a file written by a newer build should cost the user their saved
        decisions for that name, not the whole table. Everything else is refused,
        because a half-read alias table is how a tag gets applied wrongly.
        """
        version = data.get("version")
        if version != ALIAS_TABLE_VERSION:
            raise ValueError(
                f"alias table version {version!r} is not version "
                f"{ALIAS_TABLE_VERSION}"
            )
        entries = []
        for raw in data.get("entries", []):
            state = raw.get("state")
            entries.append(AliasEntry(
                name=raw["name"],
                state=state if state in (MESHED, REJECTED) else UNMESHED,
                namespace=raw.get("namespace"),
                value=raw.get("value"),
            ))
        return cls(entries=tuple(entries))


ALIAS_TABLE_VERSION = 1


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

def folder_chain(relative_path: str) -> tuple[str, ...]:
    """The folder names in a relative path, in order, without the file name.

    Names only, exactly as they appear. This is the module's whole relationship
    with a path: it reads folder names and nothing else. No filename parsing, no
    separators, no guessing — a folder is called what it is called.
    """
    parts = [part for part in relative_path.split("/") if part]
    return tuple(parts[:-1]) if parts else ()


@dataclass(frozen=True)
class ResolvedTags:
    """What one clip's path resolves to, and whether that is the whole of it.

    `resolved` is the field to read before using `tags`. When it is False a
    namespace was claimed twice on this path and **the tags are incomplete**: the
    contested namespace is deliberately absent rather than filled in with whichever
    folder happened to come first, because a guess written to a `.cnfo` is the
    "silently wrong forever" failure the wizard exists to prevent. Those clips go to
    a person.

    The determinate tags are still here on purpose: an edit screen can prefill
    everything that *is* decided and ask about only the one that is not.
    """

    tags: dict[str, str]
    conflicts: tuple[MeshConflict, ...] = ()

    @property
    def resolved(self) -> bool:
        """False when this clip needs a person before it can be imported."""
        return not self.conflicts

    @property
    def needs_manual_edit(self) -> bool:
        """The same fact, named the way the queue will name it."""
        return not self.resolved


class MeshSession:
    """One run of the wizard over one folder tree.

    `videos` is what `shared.importing.find_videos` returns, or any iterable of
    things with a `relative_path`. `library` is the destination library's
    `Catalog` and `vocabulary` a `Vocabulary`, both optional and both only ever
    read — the wizard changes neither.
    """

    def __init__(
        self,
        root: str,
        videos: Iterable[FoundVideo],
        library=None,
        vocabulary: Vocabulary | None = None,
    ) -> None:
        self.root = os.path.abspath(os.fspath(root))
        self._library = library
        self._vocabulary = vocabulary
        self._entries: dict[str, AliasEntry] = {}
        #: Every video's folder chain, deduplicated. Storing chains rather than
        #: videos is what lets a path be chosen for its *un-meshed* count.
        self._chains: dict[tuple[str, ...], int] = {}

        for video in videos:
            chain = folder_chain(video.relative_path)
            if not chain:
                # A video at the top of the folder has no folder name to mesh.
                continue
            self._chains[chain] = self._chains.get(chain, 0) + 1
            for name in chain:
                existing = self._entries.get(name)
                if existing is None:
                    self._entries[name] = AliasEntry(
                        name=name, state=UNMESHED, paths=1)
                else:
                    self._entries[name] = replace(
                        existing, paths=existing.paths + 1)

        self._colours = self._assign_colours()
        self._conflicts: list[MeshConflict] = []

    # -- introspection ----------------------------------------------------

    def _assign_colours(self) -> dict[str, str]:
        """A colour per folder name, stable for the run.

        By first appearance in sorted order, **not** by hashing: Python salts
        `hash()` per process, so a hash-derived colour would change between runs
        for no reason the user could see or explain. Sorted first appearance is
        deterministic and depends only on the tree.
        """
        colours: dict[str, str] = {}
        for chain in sorted(self._chains):
            for name in chain:
                if name not in colours:
                    colours[name] = COLOURS[len(colours) % len(COLOURS)]
        return colours

    def entries(self) -> tuple[AliasEntry, ...]:
        return tuple(sorted(self._entries.values(), key=lambda e: e.name.casefold()))

    def entry(self, name: str) -> AliasEntry:
        return self._entries[name]

    def pending(self) -> tuple[str, ...]:
        """Folder names still waiting for an answer, sorted."""
        return tuple(sorted(
            (name for name, entry in self._entries.items()
             if entry.state == UNMESHED),
            key=str.casefold,
        ))

    def is_complete(self) -> bool:
        """True when every folder name has been meshed or rejected."""
        return not self.pending()

    def counts(self) -> tuple[int, int, int]:
        """(meshed, rejected, unmeshed). One tuple rather than three properties so
        a caller cannot read two of them at different moments."""
        meshed = sum(1 for e in self._entries.values() if e.state == MESHED)
        rejected = sum(1 for e in self._entries.values() if e.state == REJECTED)
        return meshed, rejected, len(self._entries) - meshed - rejected

    # -- deciding ---------------------------------------------------------

    def _check_unmeshed(self, name: str) -> AliasEntry:
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(f"{name!r} is not a folder name in {self.root}")
        if entry.state != UNMESHED:
            raise ValueError(
                f"{name!r} has already been {entry.state}; a decision is made once "
                f"and applies to every path containing it"
            )
        return entry

    def _shares_a_path(self, first: str, second: str) -> str | None:
        """A path both folder names appear on, or None.

        The whole of the conflict rule. Two folder names can only put two values in
        front of one clip if some path literally contains both, and asking whether
        they *can* rather than whether they *do* is what keeps a dozen
        `filler_type` folders from looking like a dozen problems.

        Structural, so it is true before either is meshed and stays true whatever
        order they were answered in.
        """
        for chain in self._chains:
            if first in chain and second in chain:
                return "/".join(chain)
        return None

    def preview_conflict(self, name: str, namespace: str,
                         value: str) -> MeshConflict | None:
        """Whether meshing `name` here would put two values in front of one clip.

        Non-mutating, and separate from `assign` for one reason: a screen has to be
        able to ask the user *before* committing, because "Assign anyway?" is only
        a meaningful question if declining leaves the table as it was.

        Fires only when the two folder names share a folder path — see
        `MeshConflict`. Two folders meaning the same namespace on *different* paths
        is a library shaped like every other library, and saying so is noise.

        Returns None for an unmeshed or unknown name rather than raising — a
        preview is a question, not an operation, and the authoritative check stays
        in `assign`.
        """
        entry = self._entries.get(name)
        if entry is None or entry.state != UNMESHED:
            return None
        needle = normalized_validation_key(value)
        for other in sorted(self._entries.values(), key=lambda e: e.name.casefold()):
            if (other.name == name or other.state != MESHED
                    or other.namespace != namespace
                    or normalized_validation_key(other.value) == needle):
                continue
            shared = self._shares_a_path(name, other.name)
            if shared is None:
                continue
            return MeshConflict(
                namespace=namespace,
                existing_name=other.name,
                existing_value=other.value,
                incoming_name=name,
                incoming_value=value,
                example_path=shared,
            )
        return None

    def assign(self, name: str, namespace: str, value: str) -> MeshConflict | None:
        """Tie `name` to `namespace: value`. Returns a conflict, or None.

        The only way an entry becomes `meshed`. Nothing else in this module, and
        nothing outside it, writes a tag into the table — which is the whole of the
        never-inferred rule.

        A conflict is *returned* rather than raised and rather than refused: both
        answers were correct when given, only the combination is ambiguous, and
        that is the user's to settle. The caller shows it and asks; to ask without
        committing, call `preview_conflict` first.
        """
        entry = self._check_unmeshed(name)
        if namespace not in CANONICAL_TAG_KEYS:
            raise ValueError(
                f"{namespace!r} is not a tag commcut knows; a folder name can only "
                f"be meshed onto one of {', '.join(sorted(CANONICAL_TAG_KEYS))}"
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "A meshed folder name needs a tag value. Either an existing one or "
                "one of your own."
            )
        value = value.strip()

        conflict = self._find_conflict(name, namespace, value)
        self._entries[name] = replace(entry, state=MESHED, namespace=namespace,
                                      value=value)
        if conflict is not None:
            self._conflicts.append(conflict)
        return conflict

    def reject(self, name: str) -> None:
        """Decide that `name` is not a tag at all.

        Its videos still import; they simply contribute nothing from that folder
        name. Distinct from leaving it unmeshed, and it is the state the Manual Edit
        queue will later take over.
        """
        entry = self._check_unmeshed(name)
        self._entries[name] = replace(entry, state=REJECTED)

    def _find_conflict(self, name: str, namespace: str, value: str):
        return self.preview_conflict(name, namespace, value)

    # -- asking -----------------------------------------------------------

    def _best_chain(self) -> tuple[tuple[str, ...], int] | None:
        """The chain with the most un-meshed names, and how many that is.

        Most-first rather than depth-first because it gets the user through the
        work fastest: a path whose names are all decided teaches nothing, and a
        path with five open questions is the one worth showing. Ties break on the
        sorted chain, so a session is reproducible.
        """
        best = None
        for chain in sorted(self._chains):
            unmeshed = [name for name in chain
                        if self._entries[name].state == UNMESHED]
            if not unmeshed:
                continue
            if best is None or len(unmeshed) > best[1]:
                best = (chain, len(unmeshed))
        return best

    def next_prompt(self) -> MeshPrompt | None:
        """The next question, or None when every name has been dealt with.

        The whole of the wizard's decision-making. The window asks for this, shows
        it, and calls `assign` or `reject` with the answer.
        """
        chosen = self._best_chain()
        if chosen is None:
            return None
        chain, unmeshed_names = chosen

        # Leftmost un-meshed name first. `network/Cartoon Network` should be
        # decided before `network/Cartoon Network/2000s`, because a user who has
        # just called the outer folder a network has the context to answer for the
        # inner one.
        current = next(name for name in chain
                       if self._entries[name].state == UNMESHED)

        shown = chain[-MAX_PATH_DEPTH:] if len(chain) > MAX_PATH_DEPTH else chain
        segments = tuple(
            PathSegment(
                name=name,
                colour=self._colours[name],
                state=self._entries[name].state,
                is_current=(name == current and name in shown),
            )
            for name in shown
        )

        matches = match_value(current, self._library, self._vocabulary)
        meshed, rejected, _ = self.counts()
        return MeshPrompt(
            name=current,
            segments=segments,
            suggested_namespace=(
                matches[0].namespace if len(matches) == 1 else None),
            suggested_values=matches,
            unmeshed_paths=unmeshed_names,
            meshed=meshed,
            rejected=rejected,
        )

    # -- the outcome ------------------------------------------------------

    def tags_for(self, relative_path: str) -> ResolvedTags:
        """The tags a clip at `relative_path` would get."""
        return self._tags_for_chain(folder_chain(relative_path), relative_path)

    def _tags_for_chain(self, chain, example_path: str = "") -> ResolvedTags:
        """The tags for one already-split folder chain.

        Every meshed name in the chain contributes one tag, which is the whole
        model: the path is matched against the decisions, and what matches is what
        the clip gets.

        Where a namespace is claimed twice in one chain, **neither value is
        taken**. The alternatives were shallowest-wins and deepest-wins, and both
        write a guess: an arbitrary one, decided by folder order, into the same
        record a deliberate tag would go in, where nothing later can tell them
        apart. So the namespace is left out, `resolved` goes False, and a person
        decides — which is what `needs_manual_edit` is for.

        Takes a chain rather than a path so callers that already hold one do not
        have to re-split a string, and so nothing has to invent a fake path to
        stand in for a file name.
        """
        tags: dict[str, str] = {}
        conflicts: list[MeshConflict] = []
        #: Namespaces already contested in this chain. A third folder must not
        #: quietly re-populate one after the second removed it.
        contested: set[str] = set()

        for name in chain:
            entry = self._entries.get(name)
            if entry is None or entry.state != MESHED:
                continue
            existing = tags.get(entry.namespace)
            if existing is None and entry.namespace in contested:
                continue
            if existing is not None and existing != entry.value:
                conflicts.append(MeshConflict(
                    namespace=entry.namespace,
                    existing_name=name,
                    existing_value=existing,
                    incoming_name=name,
                    incoming_value=entry.value,
                    example_path=example_path,
                ))
                contested.add(entry.namespace)
                del tags[entry.namespace]
                continue
            tags[entry.namespace] = entry.value

        return ResolvedTags(tags=tags, conflicts=tuple(conflicts))

    def aliases(self) -> AliasTable:
        """Every decision, meshed or rejected. The session's output."""
        return AliasTable(entries=tuple(
            entry for entry in self.entries() if entry.state != UNMESHED
        ))

    def affected_paths(self) -> tuple[tuple[MeshConflict, int], ...]:
        """Every distinct namespace collision in the tree, and how many clips hit it.

        Deduped by the *pair of values* rather than per clip, so a mistake in one
        folder name is reported once with a count instead of once per affected file,
        which on a real library is the difference between a sentence and a
        scrollback. Derived from the paths rather than from the answers given while
        meshing, because this is what the folders actually produce.
        """
        tally: dict[tuple[str, str, str], MeshConflict] = {}
        counts: dict[tuple[str, str, str], int] = {}
        for chain, clips in sorted(self._chains.items()):
            result = self._tags_for_chain(chain, "/".join(chain))
            for conflict in result.conflicts:
                key = (conflict.namespace, conflict.existing_value,
                       conflict.incoming_value)
                tally.setdefault(key, conflict)
                counts[key] = counts.get(key, 0) + clips
        return tuple((tally[key], counts[key]) for key in sorted(tally))

    def conflicts(self) -> tuple[MeshConflict, ...]:
        """Conflicts raised while meshing, in the order they were found.

        These are the ones the user was asked about. `affected_paths` is the
        authoritative count of what the folders actually produce.
        """
        return tuple(self._conflicts)

    def report(self) -> str:
        """The whole outcome as text.

        A pure function of the session, and what both the completion screen and the
        tests read — so the two cannot disagree about what happened.
        """
        meshed, rejected, unmeshed = self.counts()
        total_paths = sum(self._chains.values())
        lines = [
            f"Read {total_paths} video(s) from {self.root}",
            f"  {len(self._entries)} folder name(s): {meshed} meshed, "
            f"{rejected} rejected, {unmeshed} left unmeshed",
        ]

        table = self.aliases()
        if table.meshed:
            lines.append("")
            lines.append("Meshed:")
            lines.extend(
                f"  - {entry.name}  ->  {entry.namespace}: {entry.value}"
                f"   ({entry.paths} video(s))"
                for entry in table.meshed
            )
        if table.rejected:
            lines.append("")
            lines.append("Rejected, so these folder names are not tags:")
            lines.extend(
                f"  - {entry.name}   ({entry.paths} video(s))"
                for entry in table.rejected
            )

        path_conflicts = self.affected_paths()
        if path_conflicts:
            lines.append("")
            lines.append(
                f"{len(path_conflicts)} tag(s) could not be resolved, because one "
                f"folder path claims the same tag twice:"
            )
            lines.extend(
                f"  - {conflict.namespace}: '{conflict.existing_value}' and "
                f"'{conflict.incoming_value}' — {clips} video(s) need editing by "
                f"hand (e.g. {conflict.example_path})"
                for conflict, clips in path_conflicts
            )
        elif self._conflicts:
            lines.append("")
            lines.append("A conflict was raised while meshing but no clip is "
                         "affected by it now.")

        return "\n".join(lines)


def namespace_choices() -> tuple[str, ...]:
    """The namespaces a folder name may be meshed onto: every canonical tag except
    `title`.

    `title` is excluded because it is unique per clip while everything else is a
    shared label — a value a whole folder stands for. Meshing a folder onto
    `title` would give every clip under it the same name.
    """
    return tuple(sorted(name for name in CANONICAL_TAG_KEYS if name != "title"))


def unmeshed_names(videos: Iterable[FoundVideo]) -> tuple[str, ...]:
    """Every distinct folder name in `videos`, sorted.

    The wizard's input, as a plain list. Exposed so a caller can show a count
    before building a session, and so the one-name-one-decision rule can be read off
    the data rather than inferred from the session's internals.
    """
    names: set[str] = set()
    for video in videos:
        names.update(folder_chain(video.relative_path))
    return tuple(sorted(names, key=str.casefold))