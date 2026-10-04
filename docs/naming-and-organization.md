# Naming, organization, and export

The file naming scheme, the folder organization scheme, the parser and
sanitation policy behind both, the named-export pipeline that turns a staged
segment into a file on disk, the export folder the pipeline writes to, and the
Settings window that edits all three.

Applies to: `shared/scheme.py`, `shared/naming.py`, `shared/paths.py`,
`shared/exporting.py`, `shared/ffmpeg.py`, `shared/records.py`,
`shared/catalog.py`, `shared/importing.py`, `shared/environment.py`,
`settings/settings.py`, `settings/settingswindow.ui`.

Related: [segment-model.md](segment-model.md) (the tags and the required-field
rule), [packaging.md](packaging.md) (the default export root beside the exe),
[importing.md](importing.md) (why `import/` is fixed).

## File Naming Scheme

The naming scheme is a user-editable template string stored in
`settings.json` under the key `file_naming_scheme`. Its public entry point is
`shared/naming.py:render_filename(scheme, tags)`, which delegates parsing and
rendering to `shared/scheme.py` and applies filename policy. It takes a tags
dict keyed by canonical `.cmct` keys (e.g. `filler_type`, not `type`).

### Syntax

| Construct        | Example                     | Meaning                                                        |
|----------------|------------------------------|----------------------------------------------------------------|
| Tag            | `{title}`                    | Render the tag's value, or empty if absent.                    |
| Fallback       | `{year,time_period}`         | Render first non-empty value (left to right).                  |
| AND group      | `[{block} - ]`              | Render only if **all** tags inside are non-empty; otherwise the entire group (including surrounding literals like separators) is omitted. |
| OR group       | `[{length}|{info}]`         | Render if **any** tag is non-empty. Non-empty values are joined with single spaces; the pipe is consumed (not rendered). The entire group is omitted when all tags are empty. |
| Literal parens | `[({length}|{info})]`       | Same as OR group above, with literal `(` and `)` as normal output characters. |
| Escape         | `\{`, `\}`, `\[`, `\]`, `\,`, `\|`, `\\` | Produce the literal character instead of triggering tag/group/separator parsing. |

### Tag name aliasing

The user-facing syntax uses short names; the `.cmct` format stores canonical
keys. `TagNode` retains the names written in the scheme, while
`canonical_tag_name()` and `resolve_tag_value()` resolve aliases during
validation/rendering.

`resolve_tag_value()` accepts both forms, so a scheme can use either
`{type}` or `{filler_type}` interchangeably. `VALID_TAG_NAMES` contains the
short user-facing names, `CANONICAL_TAG_KEYS` contains the stored keys, and
`shared.naming.REQUIRED_TAG_NAMES` contains the filename-specific requirement
(`title`). Folder requirements are policy-owned by `shared/paths.py`.

### Parser architecture

`shared/scheme.py` contains one recursive-descent parser (`_parse`) behind two
entry points:

- `parse_scheme()` preserves the tolerant behavior used by filenames.
- `parse_scheme_strict()` rejects malformed delimiters, empty tag names,
  dangling escapes, excessive nesting, and similar syntax defects with
  source positions. `shared/paths.py` adds the folder-specific semantic rules.

Both produce the same public AST:

- **`LiteralNode(text, escaped)`** — literal output text; strict parsing records
  whether a literal came from an escape.
- **`TagNode(names)`** — a tag with left-to-right fallback names.
- **`PipeNode`** — OR separator token (renders as a single space).
- **`GroupNode(elements, is_or)`** — a `[]` group with AND (`is_or=False`) or
  OR (`is_or=True`) semantics.

Detection rules:
- `_has_top_level_pipe(content)` scans `[]` content for unescaped `|` at
  depth 0 (outside `{}` and nested `[]`). If found, the group is OR.
- Escaped pipes (`\|`) are skipped — they become literal `|` characters
  and the group is treated as AND.
- Nested groups are parsed recursively; each evaluates its own AND/OR
  condition independently. The parent group's presence check only
  considers its **direct** child tags (not tags inside sub-groups), so
  `[{block} - [({length}|{info})]]` renders `"Toonami -"` when block is
  set but both length and info are empty.

### Whitespace handling

- OR groups: empty tags leave gaps from the pipe→space substitution.
  `_cleanup_or_text()` collapses multiple spaces and trims spaces
  *inside* literal parentheses (e.g. `( Sec)` → `(Sec)`) but preserves
  spaces *outside* (e.g. ` - (30 Sec)` stays intact).
- The top-level `render_filename()` strips leading/trailing whitespace.

### The shipped default scheme

`shared/naming.py:DEFAULT_FILE_NAMING_SCHEME` is the personal default used when
`settings.json` has no `file_naming_scheme` key:

```
{network} - {type} - {year,time_period} - [{block}|{special}] {title} [({length}|{info})]
```

`{type}`/`{info}` are the short aliases for `filler_type`/`information`, and
`[{block}|{special}]` is an OR group, so when **both** are set they render
joined by a space (`Toonami Kids`) rather than the fallback form's first-only
(`Toonami`). The README's own pattern is the fallback form,
`{network} - {filler_type} - {year,time_period} - [{block,special} ]{title} [({length}|{information})]`,
which is still supported and is what `tests/test_naming.py:TestReadmeScheme`
exercises; it is not the shipped default.

Note: optional sections whose separators should be conditional must have
their separator **inside** the bracket. Wrapping `{year,time_period}` in
`[{year,time_period} - ]` prevents stray ` - ` separators when the year
is absent. The shipped default instead puts the separator *outside* its
`[{block}|{special}]` group, so when both tags are empty the rendered stem
keeps two spaces (`... -  <Title>`); only leading/trailing whitespace is
stripped today. See "Whitespace handling" above.

## Folder Organization Scheme

The folder organization scheme is a single-line, user-editable path template
stored in `settings.json` under `folder_organization_scheme`. Its default is
(`shared/paths.py:DEFAULT_FOLDER_SCHEME`):

```text
{network}/[Blocks/{block}/]{type}/{time_period}/[{special,show}/]
```

`shared/paths.py` owns the restricted folder grammar. The public flow is:

1. `compile_folder_scheme(text)` validates syntax and static path safety and
   returns a reusable `FolderScheme`.
2. `render_folder_components(scheme, tags)` resolves and sanitizes tag values,
   applies optional fragments, and returns a tuple of safe relative components.
3. `format_folder_components(components)` produces the preview form with `/`
   separators and a trailing `/`.

The renderer intentionally does **not** return a raw path. The export code joins
the returned components beneath its chosen export root and validates
that root before writing (see "Export pipeline").

### Folder syntax and policy

| Construct | Example | Meaning |
|-----------|---------|---------|
| Path separator | `/` | Structural folder separator; it cannot be escaped. |
| Tag | `{network}` | Required unless it occurs inside a conditional group. |
| Fallback | `{year,block}` | Select the first non-empty candidate. |
| Optional path fragment | `[Blocks/{block}/]` | Include every character in the group when its tag resolves; otherwise omit the complete fragment. |
| Literal | `Blocks` | Authored safe path text. Invalid/unsafe literals are configuration errors, not silently rewritten. |

Folder groups are stricter than filename groups:

- Exactly one tag expression (which may itself contain fallback names).
- Zero or more literal characters and path separators; one group can create
  several folders.
- No nested groups.
- No pipe-based OR groups.
- `{network}`, exact `{type}` or `{filler_type}`, and exact `{time_period}`
  must each appear as unguarded top-level tags. A required tag cannot be
  guarded by a group or placed in a fallback expression.
- Other known shared tags may be used wherever policy allows; users are free
  to organize beyond the default structure.

A missing/empty bare tag is a render error. A missing/empty group tag omits the
group. If the first nonempty fallback value sanitizes to empty, rendering
raises rather than silently trying the next fallback.

### Output sanitation and path safety

Raw tag values remain unchanged in `.cmct`; sanitation happens only when
rendering a filesystem destination. `sanitize_path_component()` currently
implements the portable folder policy:

- Normalize Unicode to NFC while preserving display casing.
- Replace contiguous control, format-control, reserved path, and visually
  unsafe characters with one `-`.
- Collapse whitespace, trim surrounding whitespace, and remove trailing dots
  or spaces.
- Prefix Windows device names (`CON`, `NUL`, `COM1`, `LPT1`, and superscript
  variants) with `_`.
- Reject empty-after-cleanup values and invalid Unicode.
- Enforce 255 UTF-8 bytes per component and 4096 UTF-8 bytes for the relative
  path.
- Provide `normalized_validation_key()` (NFC plus `casefold()`) for future
  case-insensitive collision checks without lowercasing displayed names.

Authored schemes reject rooted paths, drive-qualified/UNC forms, `.`/`..`,
empty or repeated components, portable-invalid literals, and path-size
violations. Runtime empty components caused solely by omitted optional groups
are collapsed. `FolderScheme` keeps the source text authoritative and detects
mutation of its public AST nodes via a structural fingerprint.

Filename export uses `shared/naming.py:compile_filename_scheme()`,
`render_compiled_filename()`, and `sanitize_filename_stem()`. The strict export
profile requires an unconditional top-level `{title}`, rejects unknown tags
and malformed syntax, reuses the portable component policy, prefixes reserved
device names, appends `.mp4`, and includes the extension in the 255-byte
filename limit. Raw `.cmct` tag values remain unchanged.

## Export pipeline

The editor export flow is `editor/editor.py:on_export` →
`editor/editor.py:ExportWorker.run` → `shared.exporting.plan_export` →
`shared.ffmpeg.execute_export_plan`. It persists the in-memory `.cmct`, applies
session tag locks to wholly unedited segments, reads both persisted schemes,
plans every named destination, and frame-accurately re-encodes each
keep-segment (libx264/aac, **not** `-c copy`) beneath `export/`.

`plan_export` requires an unconditional top-level `{title}`, sanitizes the
rendered stem, appends `.mp4`, and combines it with the folder scheme's safe
components. The planner resolves the entire keep-segment batch before starting
ffmpeg and fails on missing required tags, invalid durations, duplicate
normalized paths, existing/case-variant destinations, reparse points, or unsafe
plan components. `execute_export_plan` then runs the plan; ffmpeg writes unique
temporary MP4s and atomically commits them without overwriting, and per-clip
failures are returned as a partial result (`ExportExecutionResult` with
`ExportClipFailure` entries) rather than aborting the batch.

Still pending is the smart-cut (keyframe-bracketed lossless copy +
partial-keyframe transcode + concat) version — see [status.md](status.md).

## The clip record

Every exported clip gets a `<stem>.cnfo` beside its `<stem>.mp4`. The record
holds the clip's tags and the segment it was cut from; **the filename and
folder are a projection of those tags** under the two schemes. That direction is
one-way on purpose — sanitation, `{a,b}` fallbacks and `[{a}|{b}]` OR groups all
make rendering lossy, so nothing recovers a tag from a path. Whatever needs a
clip's tags reads the record. **There is no reverse parser and there must never
be one**; that shortcut is reachable in a year and it does not work.

`shared/records.py` owns the format, `shared/naming.py:record_filename` owns the
name, and `shared/ffmpeg.py:execute_export_plan` does the writing.

```xml
<?xml version="1.0" encoding="utf-8"?>
<commcut-clip version="1">
  <source>Cartoon Network - April Fools 2000.mp4</source>
  <segment index="7" start="314.2" duration="29.9" />
  <tag key="title">Toonami Worlds Finest</tag>
  <tag key="network">Cartoon Network</tag>
  <tag key="filler_type">Promo</tag>
</commcut-clip>
```

### Choices behind that shape

**`.cnfo`, not `.nfo`.** The `.nfo` extension belongs to media servers, which
scan for it and expect a schema commcut does not write. A file in the wrong
dialect is worse than no file, because the tool may act on its absent keys. The
distinct extension keeps them out of the folder and leaves a real `.nfo` free
for a derived compatibility file if one is ever wanted.

**`version` is an attribute, and it is not `shared/version.py:VERSION`.** It is
a schema integer owned by `shared/records.py:RECORD_SCHEMA_VERSION`. A record
outlives the release that wrote it, and a migration has to be able to name what
it is migrating from. `parse_record_xml` refuses a version above the one it
implements, and treats a missing attribute as `1`. It also refuses an unknown
element or tag key rather than dropping data silently.

**`<tag key="...">`, not one element per tag.** The tag vocabulary already has
an owner — `shared/scheme.py:CANONICAL_TAG_KEYS`, checked by
`canonical_tag_name`. Named elements would duplicate it into the file format, so
adding a tag would mean a schema bump. Values are **raw**, sorted, and empty
ones omitted; a missing tag and an empty one mean the same thing.

**`<source>` is a basename.** A record travels with its clip, and the user's
folder layout is not part of the clip's identity.

**No stored path.** It is a pure function of the tags and the two schemes, so
persisting it would mean a second thing to go stale the moment a scheme changes.
Recompute it.

### Ordering, and what a failure leaves

The record is **rendered before the encode and published in the window between a
successful encode and the commit of the video** (`_run_planned_clip`'s
`before_commit`). Both halves matter:

- rendering early refuses a clip before spending an encode on it, and an encode
  is the expensive part;
- publishing late means a clip the encoder failed leaves **nothing** — not a
  video, not a record, and the temporary file goes with the `finally` that has
  always cleaned it up.

The rule that makes the ordering safe is: **a catalog entry is a record with a
sibling video**. So `video present ⟹ record present`, and the only recoverable
state is a record with no video, which a scan ignores and the next run replaces.

**Replacement, not no-clobber.** The preflight has already refused any
destination whose *video* exists, so the video slot is free and overwriting the
record for it cannot destroy anything of the user's. The no-clobber rule would
instead make a re-export fail on the orphan a previous failure left behind — the
delete-a-clip-and-cut-it-again case, which is exactly when the user is most
likely to retry. An orphan record also never blocks anything: the
existing-destination check keys on the `.mp4` path and never sees it.

A record that cannot be published fails **its own clip** with nothing on disk,
and a value XML cannot represent fails it before the encode. `ElementTree`
escapes `&`, `<` and `>` but emits control characters raw, producing a file its
own parser then refuses — and a carriage return, while legal XML, is normalized
to a line feed on the way back in, which would drift the tag silently. Both are
refused by `render_record_xml`, naming the tag.

### Where the name comes from

`shared/naming.py:record_filename` derives the record's name from the video's,
and the record's destination is resolved in `plan_export` and validated in
`_validate_plan_structure` — including that it **sits beside its clip**. A record
written anywhere else is one nothing will ever find, so the shared parent is part
of the invariant rather than a convention, and `plan_export` satisfies it by
construction.

`.cnfo` is one character longer than `.mp4`, and `sanitize_filename_stem` counts
the extension in its 255-byte limit, so there is exactly one stem length that is
a legal video filename and an illegal record name. Checking each name against its
own extension would leave that as a trap; `record_filename` checks the record's,
and `plan_export` refuses the batch for it before anything encodes.

### Reading the library back: the catalog

`shared/catalog.py:build_catalog` applies the same rule from the other direction.
A clip is **a record with a sibling video**; anything else under the export root
is ignored. `export/` is also where a person drops a half-organized working
folder, and a scan that turned those into clips would have to invent tags from
folder names — the reverse parse [above](#why-nothing-parses-a-filename-back-into-tags)
refuses, and for the same reasons.

So the reader is the one place that is allowed to say a clip's tags, and it says
them only from the record. A clip whose filename looks like commcut rendered it —
`Cartoon Network - Promo - 2000s - Toonami Worlds Finest.mp4` — and whose record
says otherwise reports the record, which
`tests/test_catalog.py::test_tags_come_from_the_record_and_never_from_the_filename`
pins as invariant 12's guard.

What it returns is `Catalog(clips, problems, cancelled)`. A clip carries its
video path, a root-relative posix path, its raw tags, and its record. A **problem**
is a record that could not be read, named with its path and the reason: a corrupt
record, one from a schema version this build does not know, one holding a tag key
that is not a tag. They are collected rather than raised, which deliberately
differs from the export preflight's walk — that one is about to *write* into the
tree, so an unreadable directory must refuse the batch, while a read-only scan has
nothing to protect and should cost that one record rather than the library. A
missing root is an empty catalog, not an error, the same stance
`shared/sources.py:list_source_videos` takes.

Two things it deliberately does not do: follow directory symlinks (`followlinks=False`,
matching the preflight), and count anything. Counts are derivable from
`Catalog.clips` and `Catalog.tag_index` when a consumer wants them, so no cache
here can be a second place for them to be wrong.

### What is not built

Nothing **shows** a user what is in `export/`. The walk exists and
[one button](tag-vocabulary.md#syncing-from-the-library) reads it; a library
browser does not. A source video is picked with a native file dialog, which has
no tags to show either.

Clips exported before this build have no records and cannot be backfilled from —
the records *are* the source of truth, so there is nothing to derive them from.
The catalog sees them as videos with no record and yields nothing for them.

The editor's tag fields do offer previously-used values, from a separate file
that is deliberately *not* derived from these records — see
[tag-vocabulary.md](tag-vocabulary.md).

### The export runs off the GUI thread

`on_export` splits in two. `_prepare_export` stays on the GUI thread, because it
touches the form and the live model and must report a refusal immediately: it
validates the required tags, writes the `.cmct`, and hands the worker a **model
snapshot** from `model_with_tag_locks` (a new `SegmentModel`, never the live
one) plus the two schemes and the export root. Everything after that —
`plan_export` and `execute_export_plan` — runs on `ExportWorker` in a `QThread`,
so neither the event loop nor mpv stops for the length of the batch.

While a run is in flight the window is disabled and mpv is paused. The whole
window rather than just the export button, because the worker holds a snapshot:
an edit made during a run would silently not reach the files, and a control that
appears live but does nothing is worse than one that is visibly frozen. The
snapshot boundary is the reason the freeze is honest rather than a workaround.

Two callbacks connect the executor to the dialog. `on_progress(clips_done,
total, current_relative_path)` fires before every clip and once more as
`(total, total, "")`, which is all a clip-count bar needs — the bar advances per
clip, not per frame, so it sits still for the length of one long segment. The
dialog is indeterminate while planning, because `plan_export` walks the whole
export tree twice before ffmpeg starts. `should_cancel()` is checked before
every clip and while each one runs.

`execute_export_plan` drives ffmpeg through `subprocess.Popen` rather than
`subprocess.run`, for two reasons. `stderr` goes to a `tempfile.TemporaryFile`
instead of a `PIPE` because nothing drains a pipe while the encode runs and a
full 64 KB buffer stalls ffmpeg; it is read back only to report a failure, so
the `RuntimeError` message is unchanged. And the process handle is what a cancel
terminates. On Windows `terminate()` is `TerminateProcess` — immediate rather
than graceful, so no grace/kill escalation follows it — and the clip's
uncommitted temporary file is discarded by the same `finally` that has always
handled a failed encode. A cancelled clip therefore leaves neither a destination
nor a temp file, and is recorded as cancelled (`ExportExecutionResult.cancelled`)
rather than as an `ExportClipFailure`: the user stopped it on purpose, and
`ffmpeg exited with code -15` is not an error to report.

### Cancelling, and resuming

A run that did not finish — cancelled, or left with per-clip failures — leaves the
clips it already committed on disk. The preflight refuses any destination that
exists, so a plain retry would fail on every one of them. The editor therefore
remembers the run's own committed destinations
(`ExportExecutionResult.written_relative_paths`) and offers to resume:

- **Export the rest** passes them to `plan_export(..., skip_destinations=...)`,
  which excludes them from `ExportPlan.clips` — so the existing-destination
  check never sees them — and returns them in `ExportPlan.skipped` for the
  dialog to name. Skips are matched in the same normalized key space as the
  conflict check, so a case-variant cannot make one miss.
- **Start over** clears the list, so the batch is planned normally and the
  preflight refusal names each existing file. This is the honest outcome when
  the user has re-tagged a clip and wants it written under a new name.

The skip list is `MediaPlayer` state, not something derived from the filesystem,
and that is the invariant: **a destination is only ever skipped if this editing
session's own unfinished run wrote it.** A skip inferred from "the file is
already there" would let a re-tagged clip be silently skipped forever, and would
quietly weaken the no-clobber rule. Reopening the editor discards the list.

A cancelled run stashes the list and points at the Export button; a run with
per-clip failures gets **Export the rest** on its summary screen. Both arm the
same list from the same place, because both are the same situation. Only the
clips that run committed are in it, so the invariant above is untouched by
treating a partial run like a cancelled one: nothing is skipped that this
session did not write.


Skipping is a *destination* decision and is applied after the segment's tags are
canonicalized and required-validated, so a skipped clip with an incomplete
record still refuses the whole batch.

### Closing the window mid-run

`MediaPlayer.closeEvent` ignores the close while a run is in flight and asks
whether to cancel it and close. Confirming sets the cancel event and a
close-after flag; the window closes from `_on_export_stopped`, i.e. once the
thread has actually stopped. Destroying a `QThread` that is still running aborts
the process, so there is no path that lets the window go first. The progress
dialog is deliberately **parentless** — `setEnabled(False)` cascades to child
widgets, and a disabled dialog's Cancel button does nothing — and deliberately
not application-modal, so the window's own close button still reaches
`closeEvent`.

`setAutoClose`/`setAutoReset` are both off for the same reason: a `QProgressDialog`
closes itself and emits `canceled` when its value reaches the maximum, which
would read as the user cancelling a successful export and then offer to resume a
batch that had nothing to resume.

Every outcome is written to `commcut.log` via `shared.diagnostics.log`. A run that
actually transcoded gets a summary screen; a refusal and a cancel, which have nothing
to summarize, keep their message boxes.

### The summary screen

`editor/editor.py:ExportSummaryDialog`, shown from `_report_export_outcome` over an
editor that stays disabled behind it. It replaced silence on success, which was
defensible as a default and wrong here: **Finished - Export** is the end of the
wizard, and a user who pressed it, waited out a long batch, and was dropped back into
a timeline with no word about whether the clips landed had been told nothing about
the one thing they asked for.

It is built in code rather than loaded from a `.ui` file, because it is a transient
modal with no layout worth designing — no `resource_path`, nothing to add to the
packaged `.ui` payload, and the export path already builds its own widget there
(the progress dialog).

What it says: how many clips were written, how many failed, the destination folder as
selectable text, and how many clips a resume left alone. That last one is not
decoration — "Exported 8 clip(s)" after a 12-clip session otherwise reads as four
clips having vanished, and the count only exists because `ExportOutcome` now carries
the planner's `len(plan.skipped)` rather than the window guessing at it.

Three ways out, plus one that is not a way out:

| Button | Effect |
|---|---|
| **Back to main menu** | Closes the editor. The shell brings the main menu back — the same window, hidden while the editor was up, never rebuilt. Nothing is relaunched and no second menu is created. |
| **Keep editing** | Closes the dialog and hands the editor back. The default for Escape and the window close button, because leaving the wizard should be a decision rather than the absence of one. |
| **Export the rest** | Retries the clips this run did not write, skipping the ones it did. Offered only when something was both written and failed; with nothing written there is nothing to skip, and a plain re-export would fail identically. |
| **Open export folder** | Opens the destination in the desktop's file browser, and leaves the summary up. |

The numbers come from `_export_summary(outcome)`, a pure function over the outcome,
so what is on screen is the batch's own accounting rather than a recount. A clean
run always has at least one written clip: `plan_export` refuses an empty batch.

**Back to main menu** is the only mechanism for leaving, and it is deliberately just
`close()`. An editor started from source with no menu behind it still closes; the
button cannot promise a window it has no way to verify exists.

**The dialog is released before the editor closes.** Qt ends the event loop when the
last top-level window goes away, so a summary that was only hidden would keep that
count above zero, the editor's process would linger with nothing on screen, and
leaving the wizard would look like it had done nothing at all.

The **Export the rest** path passes its skips to `_begin_export(skip_destinations=...)`
rather than re-entering `on_export`, so the resume question is not asked a second time
about a run the user has just answered for.

## Settings Scheme UI

`settings/settings.py` and `settings/settingswindow.ui` mirror the file-scheme
editor for folder schemes:

- `file_naming_scheme` defaults to the README file template when absent.
- `folder_organization_scheme` defaults to the folder template above.
- `export_folder` defaults to `export/` beside the app when the key is absent;
  an empty value means the same thing. See "Settings Scheme UI" below.
- Both fields load independently; a malformed stored folder value disables
  only the folder editor and preserves the rest of the JSON object.
- File and folder updates are written together through one `QSaveFile`, so a
  failed validation or filesystem write cannot partially persist one field.
  Unchanged legacy file values are preserved even if future stricter filename
  validation would reject them.
- File preview continues to call `render_filename()`.
- Folder preview calls the exact production path
  `compile_folder_scheme()` → `render_folder_components()` →
  `format_folder_components()` with `PREVIEW_TAGS`; Settings must not duplicate
  resolver logic.
- Cancel and window-manager close restore both last-saved values. Constructor
  warnings are deferred with `QTimer` so headless construction cannot block
  before the event loop starts.
- Each scheme has an in-app help panel (`textBrowserFileScheme` /
  `textBrowserFolderScheme`) so the window is usable without these docs. The
  file panel documents the tag set, `{a,b}` fallback, optional `[ ]` AND
  groups (including keeping the separator inside the brackets), `[{a}|{b}]` OR
  groups, nesting, backslash escaping, and the unconditional top-level
  `{title}` rule. **These panels are documentation:** if the parser changes,
  update them in the same change.
  `test_file_help_examples_behave_as_documented` renders every construct the
  file panel names through the real preview, and
  `test_file_help_states_the_title_rule_the_compiler_enforces` checks the
  `{title}` wording against `file_scheme_error`, so the help cannot drift
  into lying. `test_file_help_documents_the_syntax_and_the_title_requirement`
  asserts the help *names* each construct but matches on the construct rather
  than one exact notation, so rewording the help does not fail the suite while
  dropping a construct does. The window is intentionally compact (780x515), so
  the panels scroll; `test_help_panels_lay_out_and_can_scroll` guards that
  each panel lays out and can still reach text taller than itself.
- The **export folder** is a third field, `export_folder`, and it is
  configurable: an editable `lineEditExport` plus a `fileBrowseExport` button
  that opens a **native** `QFileDialog` in `Directory` mode with `ShowDirsOnly`
  — for the same reasons as `mainwindow.choose_source_video`, and because a
  folder picker is what stops a mistyped path from silently creating a new empty
  folder at export time.
- **An empty field means the default**, `export/` beside the app, and it is
  stored as an **absent key** rather than an empty string, so the file reads the
  same way to a hand editor as it does to this app. `shared/exporting.py:export_folder`
  is the one owner of the resolution and is not cached; the window stores the
  `os.path.abspath` of whatever is in the field, so one folder has one spelling
  on disk.
- The save refuses the whole set atomically when the folder is unusable, exactly
  as it does for a scheme, and the rules live in
  `shared/exporting.py:export_folder_setting_error` so the window and the
  resolver cannot disagree. Three of them: the path must be absolute; it must not
  be `import/`, inside it, or contain it; and it must not be an existing file.
  The overlap check compares `os.path.normcase` on both sides, because
  `C:\CommCut\Import` and `c:\commcut\import` are one folder on Windows.
- Writability is **not** checked. `shared/exporting.py:_validate_export_root`
  already accepts a root that does not exist yet as long as its nearest existing
  ancestor is a directory, and `shared/ffmpeg.py` creates the tree when it
  writes, so a chosen folder that has not been made yet is a normal state and
  probing it here would refuse something that works. A folder that goes away
  *after* the save is caught by `_validate_export_root`, which names the path.
- **An unusable stored value raises rather than falling back.** Settings refuses
  to save one, so it is only reachable by hand-editing a file the readers
  explicitly support editing, and `export_folder()` reports it with the key and
  the fix in the message. That is the editor refusing to start an export, the
  Settings sync button reporting it, and `Shell.open_safely` reporting the three
  importer windows — all four read the same message rather than rewording it.
- An **unchanged** unusable value does not block a save, the same rule the
  scheme fields follow: only a user-edited field blocks the write, and an
  unchanged legacy value is preserved. Otherwise a folder left behind by a
  stricter past would make the naming schemes unsavable, which for a user who
  has never heard of this setting is the only way out of it.
- A stored value that is **present but not a string** — including a JSON
  `null` — disables both widgets and warns rather than being read as the
  default, because there is nothing to put in a text box and an absent key is
  already how the default is spelled. A present-but-invalid *string* is shown
  in the field instead, so the user can fix a hand edit here.
- The **import folder has no row at all.** It is not a locked row, a disabled
  row, or a coming-soon row: `import/` is fixed beside the app because the
  Library Importer *moves and deletes* from it, so the one folder this app
  destroys stays inside the program root where a single mis-click cannot reach
  it. A **source video** is not a folder choice either — the main menu's file
  dialog takes any video from anywhere.
  `tests/test_settings.py::test_the_import_folder_has_no_row_and_the_export_folder_does`
  guards both halves, because the mistake is symmetric: a leftover disabled row
  reads as "coming soon", and a re-enabled export row in the `.ui` alone would
  imply a setting nothing saves.

## Coverage

`tests/test_scheme.py` (strict parsing), `tests/test_paths.py` (folder grammar
and sanitation), `tests/test_naming.py` (filename rendering, including the
README pattern), `tests/test_records.py` (the record format, its reader, and how
it is published), `tests/test_catalog.py` (the library walk: what a clip is, what is ignored, what is
reported, and the record-over-filename guard), `tests/test_importing.py` (the
Library Importer's backend, including that an imported clip lands where an
exported one would), `tests/test_settings.py` (the window, previews, atomic save,
the help panels, the export folder, and the vocabulary sync button),
`tests/test_exporting.py`
(settings, planning, preflight, resume skips, the record's destination, and the
export folder and its rules), `tests/test_ffmpeg.py` (plan execution, progress, cancel, partial
failures, and the record beside each clip), and `tests/test_editor_export.py` (the
worker, the progress dialog, cancel, resume, closing mid-run, and that the batch
goes to the configured export folder).
