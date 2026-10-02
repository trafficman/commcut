"""Bringing somebody else's finished clips into this library.

Applies to: `shared/importing.py`, `shared/exporting.py`, `shared/catalog.py`,
`shared/records.py`, `shared/sources.py`, `shared/paths.py`.

This document covers the importer's **backend**. There is no window yet: nothing
displays a catalog, a proposal, or an import result. What is here is every
function a window will need, and the four decisions that shape them.

## The shape of it

A finished clip is not a segment of a compilation, so it cannot arrive through
`shared/exporting.py:plan_export`, which plans segments. What it *is* is already
somebody else's answer to the same question — where does a clip with these tags go
— so the answer is read out of their `.cnfo` and the planning is done through the
identical rules.

Two modes, and the difference is only where the tags came from.

**Tagged.** `build_catalog(root)` walks a folder and reads the clips it holds;
`candidates_from_catalog(...)` turns them into `ImportCandidate`s;
`plan_import(...)` resolves them to destinations; `execute_import(...)` writes
them. No ffmpeg runs at any point: `ClipRecord` carries `duration`, so the new
record is rendered from the old one and there is nothing to probe.

**Untagged.** There is no record, so somebody has to supply the tags. `find_videos`
lists the clips and `propose_tags_from_path` suggests candidates. **A proposal is
never a value.** Nothing in the app writes a `TagProposal` anywhere, and
`plan_import` only ever receives tags a caller has already settled — so the line
between "a person said this" and "a folder name looked like this" is a type
boundary rather than a convention somebody has to remember.

`import/` is the folder to read from, and `export/` is where the results land.

## The four decisions

These were settled while planning and each is worth knowing before changing any
of them.

### A foreign record is refused, not half-read

`shared/records.py` refuses a record holding a tag key this build does not know,
which was its behaviour before the importer existed and stays. For a record commcut
itself wrote, that is right — it means the record is corrupt. For a record from
somebody else's install it means "this build lacks that tag", which is a different
thing with the same symptom.

The importer does not loosen the reader. Instead it makes the refusal *usable*:
`CatalogProblem` carries a `reason` code beside its message, so a screen can group
a library's problems instead of showing the user four hundred sentences that all
say something is wrong. The codes come from `shared.records.record_error_reason`,
which classifies the raised message and is pinned per-branch by
`tests/test_records.py` so a reworded message fails a test rather than silently
changing a code.

### `<source>` is the imported file's name

`ClipRecord.source` is required to be a non-empty string, and an imported clip was
never cut from a compilation. Nothing in the app reads the field for logic — it is
written, checked, parsed, and asserted in tests — so it carries one honest value
under two readings: the compilation a clip was cut from, or the file it was
imported as. No schema bump, so every existing commcut still reads the output.

The alternative was a version 2 schema with explicit provenance. It was declined
because `parse_record_xml` refuses both unrecognized elements *and* versions above
the current one, so a v2 record is unreadable by every older commcut — a hard
compatibility wall, for a field no code reads.

### An occupied destination is skipped or refused, never clobbered

Export refuses any destination that already exists, which is right for a clip the
user is looking at. For an import it is wrong: the second run of an import has to
be a no-op rather than a failure, or the operation cannot be retried after a
partial cancellation.

So `plan_import` takes the destination library and decides per clip:

- taken, and the record already there carries **the same tags** → **skipped** as
  already present. `ImportPlan.already_present`.
- taken, anything else → **refused by name**, naming the tags that differ.
  `ImportPlan.refusals`.

Two details that are easy to get wrong:

- The comparison is against the **whole** tag dict, both directions. Comparing only
  the incoming tags would call an existing clip carrying a tag this one lacks
  "already present", and quietly keep the poorer copy of the pair.
- Destinations are matched in `DestinationIndex`'s **normalized key space**, never
  with `==`. On a case-insensitive volume `cartoon network/A Clip.mp4` and
  `Cartoon Network/A Clip.mp4` are one file, and string comparison would let the
  incoming clip overwrite the existing one — the single outcome worse than a
  refusal.

Worth knowing: with the **shipped** schemes a same-destination conflict is nearly
unreachable, because all ten tags appear in the filename and differing in any of
them changes the path. A conflict needs either a custom scheme that leaves a tag
out, or a sanitation collision (`A/B` and `A:B` both becoming `A-B`). Both are
real; neither is common.

### One bad clip does not take the batch down

`preflight_export_plan` refuses the *whole* batch on any problem, and that is
correct: a Stage or an Export is one clip the user is looking at. An import of
somebody else's eight hundred clips is a different operation, and one record
missing Time Period should cost one clip rather than all of them. `plan_import` is
therefore per-clip, and every skip is carried by name in `ImportPlan.skipped` with
a `reason` code and a sentence.

The price is that a partial import has to be **reported** rather than raised,
which is what `ImportSkip` is and why `ImportResult` is three lists rather than a
count.

Note that invariant 10 (editing writes are all-or-nothing) governs Stage and
Export, and does not reach here.

## Planning

```python
plan_import(candidates, schemes, export_root, *,
            transfer=TRANSFER_COPY, existing=None) -> ImportPlan
```

`schemes` and `export_root` are the same arguments `plan_export` takes, and the
destination is resolved through the **shared** helpers rather than a second copy:
`plan_clip_destination` (canonicalize tags → required-tag rule → both schemes →
sanitation → UTF-8 byte ceiling) and `DestinationIndex` (the skip set and the
cross-clip collision map). Invariant 9 would otherwise forbid the fork, and the
one thing an importer must not get wrong is putting a foreign clip somewhere an
exported one would not go. `tests/test_exporting.py` pins both halves, including
a cross-check that the two planners agree on a path.

`PlannedExportClip` is reused **as-is**, wrapped by `PlannedImportClip`. Its
`segment_index` and `start` carry no meaning for an import and are left at their
defaults rather than given invented values — which is also what the new record
gets, since an imported clip was not cut from a segment.

## Executing

```python
execute_import(plan, on_progress=None, should_cancel=None) -> ImportResult
```

- **`transfer`** is `copy` (the default), `link`, or `move`. `copy` is the only one
  safe to assume: `move` destroys the user's media if the run is cancelled or a tag
  is wrong, and `link` is not always available — exFAT and FAT32 have no hard
  links, and a link cannot cross a volume, so `link` **falls back to a copy per
  clip** rather than failing the batch. The parameter rather than a radio button is
  deliberate: the backend is complete for all three before there is a screen to
  choose between them.
- **`check_free_space`** refuses a copying import that does not fit, once, naming
  the shortfall, before the first clip. No precedent for this existed:
  `preflight_export_plan` measures *path length* in UTF-8 bytes, not free space.
  A run of eight hundred copies that dies on clip four hundred leaves the user to
  work out which half arrived. `link` and `move` need no check.
- **A cancelled run keeps everything it committed** and says so. That is the same
  rule the export follows, and the reason `already_present` is part of *planning*
  rather than something a caller arranges: re-planning the same folder after a
  cancellation skips what landed and finishes the rest.
- **The record is published before the video is committed**, and both go through a
  temporary file in the destination directory first. That ordering keeps invariant
  12 — `video present` implies `record present` — true at every instant a reader
  could observe, including the one where the process is killed mid-run. It is the
  *reverse* of the export's order and for the same reason: there the encode is the
  long part and the record identifies it; here the file copy is.
- If the record cannot be written after the video landed, **the video is removed
  again.** A clip with no record reads as a stray file, and a dangling record reads
  as a clip that exists; neither state is one the invariant allows.
- One failing clip does not stop the run. Its destination is left clean and it goes
  in `ImportResult.failed` with its reason.

No child process runs, so `no_console_kwargs` has nothing to do here — copying,
linking and moving are all filesystem operations.

## Untagged discovery

`find_videos(root)` is the deliberate inverse of `build_catalog`, which yields only
clips a record backs. That rule is right for a library this app maintains and
useless for a folder of somebody's rips, where the absence of a record is the normal
case rather than a sign of corruption.

Traversal is otherwise identical to the catalog's — `os.walk`,
`followlinks=False`, a missing root is empty rather than an error, `on_progress`
carrying no total — because one traversal rule in this app is worth more than two
that differ. The one deliberate difference: **a directory that cannot be read is
not descended into**, where the catalog reports it. A half-readable folder is the
normal state of a folder someone has been filling by hand for years, and a walk
that refused to return anything at all because of one bad subfolder would be
useless.

Videos that *do* have a sibling record are still listed, with `has_record=True`, so
a caller can hand them to the tagged path rather than making the user decide which
half they are in.

## Proposals, and why they are only proposals

`docs/naming-and-organization.md` refuses the reverse parser outright: a scheme
renders lossily, so a filename cannot be read back into tags. `propose_tags_from_path`
does not read anything back. It looks at folder names, at ` - ` separators, and at
parenthetical groups, and offers what a person's naming scheme tends to produce —
as questions.

The defence is structural rather than editorial: nothing writes a `TagProposal`, and
`plan_import` accepts only settled tags. Any future code that turns a proposal into
a stored tag without a person passing over it breaks the rule, and the type boundary
is what stops that happening by accident.

Confidence is derived, never asserted:

| Level | Means |
|---|---|
| `exact` | the token matches a value the existing library uses in exactly one namespace, normalized the way collisions are |
| `candidate` | it matches in several namespaces (all named), or only in the advisory `vocabulary.json` |
| — | anything else is **not proposed at all** |

Two deliberate omissions:

- **Nothing is proposed for a namespace the token has never been seen in.** That
  would be a suggestion to *create* a namespace, which is the mesh wizard's decision
  to make, not this function's.
- **Nothing is ever proposed for `title`.** It is unique per clip and a folder name
  is a *shared* label, which is what every other tag means. Guessing the one field
  nothing can be wrong about twice is not a kindness.

`TagProposal.evidence` is the point of the class. A proposal does not claim the path
says what it means; it claims a person might agree, and says why.

## Evidence for the value question

`match_value(value, library, vocabulary)` returns `ValueMatch`es ranked by evidence
rather than choosing one. Showing "block (14 clips), special (2)" makes a namespace
choice a click; choosing silently is how a library ends up with fourteen clips under
`block` and one under `special`.

`in_library` and `in_vocabulary` are separate columns because they are different
kinds of knowing: the library is what the user has, and `vocabulary.json` is a hint
that survives a clip being deleted — the file is a cache *downstream* of the library
and drifts. A hit only in the file is weaker evidence and is marked as such.

An empty result is **not** permission to guess. It means the value is new here, and
the caller has to ask.

## Not built

- **Every screen.** The scan summary, the mesh wizard, the manual tag queue with
  its mpv preview, the review page. One window, so `_BUILDERS`, a `.ui`,
  `UI_DATAS`, `REQUIRED_UI`, and the `WINDOW_UI` table in `tests/test_frozen_mode.py`
  — already held to each other.
- **The transfer choice as a control.** The parameter exists; the radio buttons do
  not.
- **A library browser.**
- **A recent-sources list** for the source file dialog, which is a separate gap —
  see [architecture.md](architecture.md#what-the-picker-was-carrying).
- **Tag-form reuse.** The editor's tag helpers are Qt code and stay in
  `editor/editor.py` until the queue needs them; lifting them to `shared/` is a UI
  phase prerequisite.