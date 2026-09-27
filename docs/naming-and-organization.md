# Naming, organization, and export

The file naming scheme, the folder organization scheme, the parser and
sanitation policy behind both, the named-export pipeline that turns a staged
segment into a file on disk, and the Settings window that edits the two
schemes.

Applies to: `shared/scheme.py`, `shared/naming.py`, `shared/paths.py`,
`shared/exporting.py`, `shared/ffmpeg.py`, `settings/settings.py`,
`settings/settingswindow.ui`.

Related: [segment-model.md](segment-model.md) (the tags and the required-field
rule), [packaging.md](packaging.md) (the export root beside the exe).

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

A cancelled run leaves the clips it already committed on disk. The preflight
refuses any destination that exists, so a plain retry would fail on every one of
them. The editor therefore remembers the run's own committed destinations
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
session's own cancelled run wrote it.** A skip inferred from "the file is
already there" would let a re-tagged clip be silently skipped forever, and would
quietly weaken the no-clobber rule. Reopening the editor discards the list.

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

Every outcome is written to `commcut.log` via `shared.diagnostics.log`. A dialog
appears only when something needs saying: a refusal, per-clip failures, or a
cancel. A clean export gets none.

## Settings Scheme UI

`settings/settings.py` and `settings/settingswindow.ui` mirror the file-scheme
editor for folder schemes:

- `file_naming_scheme` defaults to the README file template when absent.
- `folder_organization_scheme` defaults to the folder template above.
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
- Import/Export directory fields and Browse buttons are present in the UI but
  **deliberately locked**: for this alpha both folders are fixed beside
  `commcut.exe`, and `SettingsWindow._lock_folder_choices()` disables the four
  widgets and marks the two labels "(coming soon)" rather than removing them,
  so a tester can see they are not wired yet. Choosing the import folder is
  the picker's job (it lists `import/`), not a setting.

## Coverage

`tests/test_scheme.py` (strict parsing), `tests/test_paths.py` (folder grammar
and sanitation), `tests/test_naming.py` (filename rendering, including the
README pattern), `tests/test_settings.py` (the window, previews, atomic save,
and the help panels), `tests/test_exporting.py` (settings, planning, preflight,
resume skips), `tests/test_ffmpeg.py` (plan execution, progress, cancel,
partial failures), and `tests/test_editor_export.py` (the worker, the progress
dialog, cancel, resume, and closing mid-run).
