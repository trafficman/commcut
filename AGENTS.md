# AGENTS.md

Context for AI coding agents working on the **commcut** project. This file
captures the project scope, current architecture, key data models, and the
conventions/gotchas you need to be productive here.

## What this project is

**commcut** is a Python/PySide6 desktop app for managing a personal library
of "filler" clips (commercials, promos, bumps) ripped from television. The
full vision — described in `README.md` — has three pieces:

1. **Library organization** — a folder/naming scheme driven by per-clip tags
   (Title, Network, Block, Filler Type, Year, Time Period, Show, Special,
   Length, Information).
2. **Rename Wizard** — batch-rename already-cut clips using the tag scheme.
3. **Editing Wizard** — load a compilation video (multiple clips back to
   back), auto-detect boundaries, let the user review/adjust, and smart-cut
   each segment out as a separate file.

The working tree currently contains two Wizard modules — the **Editing
Wizard** (`editor/`) and the **Segment Scanner** (`scanner/`) — plus the
standalone **Settings** window and the `shared/` library they build on. The
Rename Wizard and smart-cut export are not yet built; named library export is
now wired end to end through the current full-segment transcode.

## Layout

```
commcut/
├── README.md                # Project spec (source of truth for scope)
├── AGENTS.md                # This file
├── bin/                     # Bundled binaries, one subfolder per OS
│   ├── win/                 # Windows binaries (ffmpeg.exe, ffprobe.exe, libmpv-2.dll)
│   ├── linux/               # Linux binaries (placeholders, none shipped yet)
│   └── mac/                 # macOS binaries (placeholders, none shipped yet)
├── import/                  # Test source videos
├── temp/                    # Scratch output (e.g. 2-min scanner preview clips)
├── settings/                # Standalone Settings window
│   ├── settings.py          # Scheme persistence, validation, previews, atomic save
│   └── settingswindow.ui    # File/folder scheme editors and live previews
├── editor/                  # The Editing Wizard (current focus)
│   ├── editor.py            # Entry point: Editor window, editing state machine,
│   │                        # splash flow, PreScanWorker scaffolding
│   └── editorwindow.ui      # Qt Designer file; promoted TimelineWidget
├── scanner/                 # The Segment Scanner (detector + review)
│   ├── scanner.py           # Entry point: 2-min preview clip load, mpv playback,
│   │                        # transport, two marker timelines, detector sliders
│   ├── marker_timeline.py   # MarkerTimelineWidget (playhead + vertical marker lines)
│   └── scannerwindow.ui     # Qt Designer file; promoted MarkerTimelineWidget
├── shared/                  # Cross-module library (editor + scanner + settings)
│   ├── environment.py       # setup_environment + get_binary_path (cross-platform)
│   ├── mpv.py               # MpvBridge, create_mpv_player, scan_keyframes
│   ├── timeline.py          # TimelineWidget (segments, zoom/scroll)
│   ├── segments.py          # SegmentModel + .cmct persistence, probe_duration
│   ├── ffmpeg.py            # clip_to_temp, export_segment_clips (simple transcode)
│   ├── scheme.py            # Shared tag aliases, AST, parser, strict parser, renderer
│   ├── naming.py            # Filename scheme policy + render_filename()
│   ├── paths.py             # Folder scheme validation, sanitization, safe components
│   ├── exporting.py         # Settings snapshot, destination planner, export preflight
│   └── ui_loader.py         # UiLoader subclass for promoted custom widgets
├── prototypes/              # Earlier exploration / alternatives
│   ├── BasicUI/             # First prototype
│   └── VideoEditor/         # Pre-rename copy of the editor module
```

## The .cmct sidecar format

Segments are stored as a JSON sidecar next to the source video with extension
`.cmct`. The path is derived by swapping the extension (`compilation.mp4`
→ `compilation.cmct`).

**Transition-point model.** The source video is tiled contiguously by
segments. Each segment entry is just its start time and metadata; the end
is derived as the next entry's start (or `duration` for the last one). This
makes overlaps and gaps structurally impossible.

```json
{
  "source": "compilation.mp4",
  "duration": 120.0,
  "segments": [
    {"start": 0.0,   "ignored": false, "tags": {}},
    {"start": 42.5,  "ignored": true,  "tags": {}},
    {"start": 78.2,  "ignored": false, "tags": {}}
  ]
}
```

Tag fields (from the readme's scheme): `title`, `network`, `block`,
`filler_type`, `year`, `time_period`, `show`, `special`, `length`,
`information`. Title must be unique per segment and has **no lock button**;
all other tags do. The base required record fields are Title, Network,
Filler Type, and Time Period; Year is optional. Those four are enforced on
the front end as well as in the export planner — see "Required record
fields" below. Folder resolution requires the three structure tags and
filename validation requires Title.

The segment data model and persistence live in
`shared/segments.py:SegmentModel`. Operations:
`place_end_boundary(i, pos)`, `end_segment(i, pos)`, `start_segment(i, pos)`,
`merge_next(i)`, plus `placeholder(source, duration)` and `save/load` for
`.cmct`.

## The editing state machine

A linear left-to-right walk through the segments. The editor window holds:

- `self.segment_model: SegmentModel` — the in-memory working state.
- `self.current_index: int` — the **Active Segment**.
- `self.tag_locks: dict` — locked tag values that carry across unedited
  segments (working state, not persisted to `.cmct`).
- `self.dirty: bool` — True when the in-memory model has changes since the
  last `Stage` (which writes to `.cmct`).

### Operations on the Active Segment

| Button            | Effect                                                                                              |
|-------------------|-----------------------------------------------------------------------------------------------------|
| **End Seg**       | `place_end_boundary` at the playhead. See "End Seg" below — it either splits the active segment or moves its end boundary forward. |
| **Start Seg**     | `start_segment` at playhead. Left half marked `ignored=True`; right half becomes the new active segment, inheriting metadata. |
| **Merge Next**    | `merge_next` — absorb the next segment into the active one. Used for false-positive detections.   |
| **Skip** (check)  | Toggle `ignored` on the active segment.                                                           |
| **Stage**         | Read form tags into the active segment, save the whole model to `.cmct`, clear `dirty`, advance `current_index`. |
| **Undo**          | Reload model from `.cmct` (reverts all unstaged changes), clear `dirty`.                            |
| **Toggle Zoom**   | Toggle between zoom-to-active-segment and fit-whole-video.                                         |
| **Active ←/→**    | Move `current_index` by ±1 (clamped). On any active change, snap the playhead to the new segment's start. |

The **dirty indicator**: Stage and Undo are both `QPushButton` with
`checkable=True`. When `self.dirty` is True, both are `checked=True` and
`enabled=True` (colored, clickable). When False, both are `checked=False`
and `enabled=False` (greyed out, unclickable). The `_update_stage_button`
helper drives both from the single `self.dirty` flag.

### Locks (tag carry-over)

Locks are session-level and **pinned**: `self.tag_locks[key] = value` is
captured when the user checks a toggle, and is deliberately *not* updated by
field edits. A lock is a value that propagates to later segments, so a field
that stops matching its pin is a segment deliberately deviating from the
lock — not a reason to repoint the lock. `_inherited_tags()` filters out
empty pins, so an empty lock carries nothing and the export-time
materialization in `shared/exporting.py` skips it the same way.

Lock *button* state is derived, not imperative: `_refresh_lock_buttons()`
checks a lock iff its key is in `tag_locks` **and** the field currently holds
the pinned value. `on_tag_edited` re-derives it on every keystroke, so a
toggle switches off the moment its field stops matching and back on as soon as
it matches again. The derivation is deliberately independent of whether the
segment has been edited — deriving it from `_is_segment_edited()` (which
reads the model, and the model is written on every keystroke) used to force
all nine buttons unchecked as soon as you typed a tag, and made every
previously staged segment read as unlocked. A staged segment whose tag still
matches a pin shows as locked; one whose value deviates shows as unlocked
while the pin stays held for later segments.

Consequence: editing a field that is currently locked disengages that toggle,
and clicking the disengaged toggle re-pins the new value. Locking an empty
field is allowed but disengages as soon as anything is typed into it.

On navigation to an **unedited** segment (all model tags empty), the form
pre-fills from `self.tag_locks`. On an **edited** segment, the form shows the
stored tags. Title is excluded from the lock system, and a segment created
by **End Seg** / **Start Seg** inherits **only the locked tag values**
(`_inherited_tags()`), so the new segment, the form, and the export-time
materialization in `shared/exporting.py` all agree on what carries over.

## End Seg

A segment's end and the next segment's start are the **same stored value**,
so "put my end boundary here" has two possible answers.
`shared/segments.py:SegmentModel.place_end_boundary` decides between them and
returns one of four outcome codes (`END_BOUNDARY_INSERTED`, `_MOVED`,
`_NO_CHANGE`, `_BLOCKED`):

| Playhead | Outcome | Effect |
|----------|---------|--------|
| Inside the active segment | `INSERTED` | Delegates to `end_segment`; a new segment is created and keeps the active index. |
| Past the end, still inside the next segment | `MOVED` | Sets `segments[i+1]["start"] = position`. |
| Exactly on a boundary, or at a video edge | `NO_CHANGE` | Nothing happens. |
| At or beyond the next segment's end | `BLOCKED` | Nothing happens, **and the editor says so**. |

The guard is `end(i) < position < end(i+1)`, where `end(i+1)` is
`segments[i+2]["start"]` or `duration` for the last segment. That is what
stops End Seg from eating several segments: a playhead inside segment `i+2`
is refused rather than clamped, because absorbing a whole segment is what
**Add Next Seg** (`merge_next`) is for. `position == end(i+1)` is blocked too,
since it would leave that segment with no duration at all.

Moving the boundary necessarily resizes both neighbours, because they share
the value. No tags or `ignored` flags are touched, so an already-staged
neighbour keeps its record and only its duration changes. The invariant that
starts strictly increase is preserved by construction.

A position **at or before** the active segment's start is still a no-op; only
the forward direction is implemented. The backward case (moving the previous
segment's end back to the playhead) is the natural next addition and is a
small block in the same method.

The refusal is deliberately loud. Every out-of-range case used to be a silent
no-op, and that silence is most of what made the flow feel broken; the dialog
names **Add Next Seg** and points at navigating to the other segment.

## Required record fields

Title, Network, Filler Type, and Time Period are required on every segment
that will be exported. The rule lives in one place —
`shared/exporting.py:missing_required_tags(tags)` returns the canonical keys
that are absent or whitespace-only — and both the export preflight
(`_validate_required_tags`) and the editor use it, so the two can't drift.

**Ignored segments are exempt.** `plan_export` skips them entirely
(`if segment.get("ignored"): continue`), so the editor must not demand tags
for them either; Skip is how a user discards a false-positive detection, and
requiring a full record for it would make that workflow impossible.

In the editor, `_missing_required_labels()` reports the gap in *display* order
(Title, Network, Type, Time Period) from the **form**, not the model, so the
check reflects what the user is looking at. It gates three things:

- `on_stage` refuses the stage outright — nothing is written to the model, the
  `.cmct` sidecar is not created or modified, the active index does not
  advance, and the dialog names the missing fields plus the Skip escape
  hatch.
- `on_export` runs the same check *before* persisting, so Export cannot write
  an incomplete record to the sidecar. (It previously saved first and only
  discovered the problem inside the preflight, after the bad write.)
- `_refresh_required_fields()` outlines the missing fields in red. It is
  recomputed on every keystroke, on the Skip toggle, and on every segment
  change, so the outline always states what Stage will demand right now.

Note that the `.cmct` is still *expected* to contain empty tags for segments
the user has never staged — lock materialization happens at export time, not
at save time. The front-end rule applies to what a user actively stages and to
the active segment on export, not to the whole file.

## Architecture

The editor, scanner, and Settings window use the common library under
`shared/`. Each entry point calls `shared.environment.setup_environment(__file__)`
near the top — it puts the project root on `sys.path` (so `shared.*` resolves
when running the script directly) and prepends the per-OS `bin/<os>/` folder to
`PATH` so mpv, ffprobe, and ffmpeg resolve to the bundled versions.

The shared modules are:

- `shared/environment.py` — `setup_environment(script_path)` (sys.path +
  PATH) and `get_binary_path(name)`, which resolves ffmpeg/ffprobe/mpv per
  OS under `bin/<os>/` (`.exe` on Windows). This is the cross-platform binary
  resolution that used to live in `core.py`.
- `shared/mpv.py` — `MpvBridge` (the single Qt↔libmpv channel),
  `create_mpv_player` (wraps `mpv.MPV` for a `QFrame`, sets
  `WA_NativeWindow`), and `scan_keyframes(path)` (ffprobe I-frame scan
  returning sorted timestamps).
- `shared/timeline.py` — `TimelineWidget`, the editor's zoom/scroll segment
  timeline (red/green/blue, ignored dimming, active highlight).
 - `shared/segments.py` — `SegmentModel` + `.cmct` persistence
   (`sidecar_path`, `probe_duration`).
 - `shared/ffmpeg.py` — ffmpeg helpers (`clip_to_temp`, and `export_segment_clips`
   for the simple per-segment transcode; the future home of the smart-cut
   export).
 - `shared/scheme.py` — shared tag aliases and canonical-name resolution plus
   the public AST (`LiteralNode`, `TagNode`, `PipeNode`, `GroupNode`), lenient
   filename parsing, strict diagnostic parsing for path validation, and
   conditional node rendering. Existing filename syntax remains tolerant.
 - `shared/naming.py` — filename policy and `render_filename()`. It re-exports
   the shared tag helpers, keeps `{title}` as the filename requirement, and
   supports `{a,b}` fallback, `[...]` AND groups, `[{a}|{b}]` OR groups,
   nested groups, and `\`-escaping.
 - `shared/paths.py` — restricted folder-scheme compiler and resolver.
   `compile_folder_scheme()` validates the path-specific grammar;
   `render_folder_components()` sanitizes tag leaves and returns safe,
   display-cased relative components; `format_folder_components()` is for
   previews. Portable component sanitation also lives here for reuse by
   filename export.
 - `shared/exporting.py` — non-Qt settings snapshot and pure named-export
   planner. It validates the segment model and every destination, enforces the
   four required tags, compiles both schemes, materializes session tag locks,
   and rejects duplicate, existing, case-variant, reparse-point, traversal,
   and byte-limit conflicts before ffmpeg starts.
 - `shared/ui_loader.py` — `UiLoader(QUiLoader)` subclass that instantiates
   promoted custom widgets reliably; register a class with
   `register_widget` before `load()`.

### The MpvBridge pattern

Both the editor and scanner communicate with libmpv through a single
**`MpvBridge(QObject)`** that owns the mpv player and exposes:

- **Qt signals** (state up): `pauseChanged`, `positionChanged`,
  `durationChanged`, `fileLoaded`, `playbackEnded`. These are fed by
  `player.observe_property(...)` callbacks. **Observers fire on mpv's worker
  thread; emitting Qt signals is thread-safe and lands on the GUI thread.**
- **Methods** (commands down): `toggle_play`, `seek_exact`, `step_frames`,
  `set_keyframes`, `next_keyframe`, `prev_keyframe`, `load_file`,
  `load_and_play`.

The editor/scanner windows and widgets never read mpv state directly — they
only mirror what the bridge announces via signals.

## Splash flow (pre-work before the window appears)

`__main__` in both the editor and the scanner shows a `QSplashScreen`, then
runs `scan_keyframes` (via ffprobe) while it's up. The scan is currently
synchronous on the GUI thread — it's typically sub-second for a 2-minute
preview. `PreScanWorker` is scaffolding in the editor for future off-thread
stages, not yet wired into `__main__`.

**Hard-won gotcha:** mpv's Direct3D device initialization hangs when
another top-level window (the splash) is the active window at construction
time. Close the splash *before* constructing the mpv-backed widget.
`app.processEvents()` is called once after `splash.show()` to ensure the
splash actually paints.

## The Scanner window

The scanner (`scanner/scanner.py`) is the detector + review half of the
Editing Wizard. It mirrors the editor's splash/mpv flow, and loads a
2-minute stream-copied preview of the source via `shared/ffmpeg.clip_to_temp`
into `temp/` (fast, lossless, small clip — the full video isn't loaded until
"Finished").

Its UI (`scannerwindow.ui`) has: an embedded video frame, two
`MarkerTimelineWidget`s ("Scanner Preview" and "User Marked"), transport
controls (play/pause, frame ±, keyframe ±), a segment-controls row
(Undo, Place Boundary), detector sliders (Minimum Black Frames 0–40,
Black Levels 0–100), and a scan row (Test Scan, Finished — Scan Full
Source Video).

What's wired in `ScannerWindow.__init__`: loading the clipped preview,
transport + frame/keyframe stepping, feeding both timelines the bridge's
position/duration/seek, the slider value labels, Place Boundary (playhead
→ lower **User Marked** timeline via `add_marker`), Undo (pops the last
user marker), Test Scan (`blackdetect` → midpoint markers in the upper
**Scanner Preview** timeline), and Finished (`blackdetect` on the full
source → midpoint boundaries written to a `.cmct`, then the Video Editor
launched for manual fixes + tags).

What's wired: the Export button (`editor/editor.py:on_export` →
`shared.ffmpeg.export_named_model`) persists the in-memory `.cmct`, applies
session tag locks to wholly unedited segments, reads both persisted schemes,
plans every named destination, and frame-accurately re-encodes each
keep-segment (libx264/aac) beneath `export/`. ffmpeg writes unique temporary
MP4s and atomically commits them without overwriting; per-clip failures are
reported as a partial result. Still pending is the smart-cut (keyframe-
bracketed lossless copy + partial-keyframe transcode + concat) version.

Each scanner run begins by clearing `temp/*.mp4` (`_clear_temp_clips` in
`scanner.py`) so preview clips don't accumulate across runs; the 2-minute
preview is then stream-copied to `temp/` with a deterministic name
(`test_clip120s.mp4`).

If a `.cmct` sidecar already exists next to the source video, the scanner
skips itself and launches the Video Editor (`editor/editor.py`) instead, so
an existing project is never overwritten. When no `.cmct` exists, the
Finished button runs `blackdetect` on the full source, writes the midpoint
boundaries to `<name>.cmct` next to the source, and then launches the
 editor.

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

### Integration

The editor export flow (`editor/editor.py:on_export` →
`shared.ffmpeg.export_named_model` → `shared.exporting.plan_export`) now
requires an unconditional top-level `{title}`, sanitizes the rendered stem,
appends `.mp4`, and combines it with the folder scheme's safe components. The
planner resolves the entire keep-segment batch before starting ffmpeg and
fails on missing required tags, invalid durations, duplicate normalized paths,
existing/case-variant destinations, reparse points, or unsafe plan components.

## Folder Organization Scheme

The folder organization scheme is a single-line, user-editable path template
stored in `settings.json` under `folder_organization_scheme`. Its default is:

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

The renderer intentionally does **not** return a raw path. Future export code
must join the returned components beneath its chosen export root and validate
that root before writing.

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
  remain unwired.

Coverage lives in `tests/test_scheme.py`, `tests/test_paths.py`, and
`tests/test_settings.py`, alongside the existing `tests/test_naming.py`.

## Key conventions and gotchas

- **Bundled binaries.** Both the editor and the scanner call
  `shared/environment.setup_environment` at startup, which prepends the
  per-OS `bin/<os>/` folder to `PATH` via `get_binary_path`. mpv, ffprobe,
  and ffmpeg resolve to the bundled versions. `get_binary_path` raises
  `FileNotFoundError` if a binary is missing at the expected path.
- **Promoted widget.** The timeline is a custom `QWidget` promoted in
  `editorwindow.ui` as `TimelineWidget` / header `timeline`. PySide6's
  `QUiLoader` does not auto-resolve promoted widgets reliably, so
  `editor.py` uses a `UiLoader(QUiLoader)` subclass whose `createWidget`
  is overridden to instantiate `TimelineWidget` directly. Register the
  class with `loader.register_widget(TimelineWidget)` before `loader.load()`.
  The scanner does the same for `MarkerTimelineWidget` (header
  `scanner.marker_timeline`) in `scanner.py`.
- **Frame-accurate step.** mpv's `frame_step` plays a fraction of a second
  of audio (the audio decodes before the mute property takes effect).
  `MpvBridge.step_frames` instead seeks by `1/container_fps` and forces
  `pause = True`. This is silent.
- **mpv D3D gotcha.** Already mentioned — close the splash before
  constructing the player. See "Splash flow" above.
- **`Position` and seek clamping.** Boundary positions in
  `end_segment`/`start_segment` are clamped to `(start, end)` of the active
  segment. A no-op returns `False`.
- **Tag field programmatic updates.** `_write_tags_to_form` uses
  `blockSignals(True/False)` around `setText` so the `textChanged`
  handler (`on_tag_edited`) doesn't fire on initial load and falsely mark
  the model dirty.
- **Keyframe nav epsilon.** `_KEYFRAME_EPSILON = 0.05` seconds. Without
  it, mpv seeking to a keyframe near the current position can re-seek to
  the same spot and the buttons feel broken. Applied symmetrically in
  `next_keyframe` / `prev_keyframe`.

## What's built (and what's next)

**Built:**
- `.cmct` sidecar format and `SegmentModel` (transition-point model).
- Timeline rendering: alternating red/green/blue segments, ignored
  segments dimmed, active-segment highlight, zoom-to-active or
  fit-whole via toggle.
- Editing state machine: End Seg, Start Seg, Merge Next, Skip, Stage,
  Undo, Active navigation, Toggle Zoom.
- Tag form with lock system and dirty indicator.
- Keyframe navigation via ffprobe-scanned I-frames.
- Frame-accurate stepping (silent).
- Cross-platform binary resolution: `shared/environment.get_binary_path`
  resolves ffmpeg/ffprobe/mpv per OS under `bin/<os>/`; both the editor and
  the scanner prepend it to `PATH` via `setup_environment`.
 - Shared library layer used by the wizards and Settings: `shared/mpv`
   (MpvBridge, create_mpv_player, scan_keyframes), `shared/timeline`,
   `shared/segments`, `shared/ffmpeg`, `shared/environment`, `shared/scheme`,
   `shared/naming`, `shared/paths`, `shared/exporting`, and `shared/ui_loader`.
- Scanner skeleton: 2-minute preview clip load (stream copy into `temp/`),
  embedded mpv playback, transport + frame/keyframe stepping, Place
  Boundary + Undo on the User Marked timeline (in-memory, no `.cmct`),
  and two marker timelines driven by the bridge.
- Automated boundary detection (Test Scan): `ffmpeg blackdetect` on the
  2-minute preview, slider-mapped to `d = frames / fps` and
  `pix_th = level / 100`; skips `black_end:N/A` runs; stamps one midpoint
  `(T1 + T2) / 2` per black run into the upper Scanner Preview timeline
  (`timelineWidget1`) via `MarkerTimelineWidget.add_marker`.
- Finished (full-source scan): the same detector run against the full
  source video (not the 2-minute preview); midpoint boundaries are written
  as `.cmct` segment starts via `SegmentModel` and the Video Editor is
  launched automatically.
 - Scanner→Editor handoff: if `sidecar_path(source)` already exists, the
   scanner launches `editor/editor.py` and exits, so an existing `.cmct`
   is never overwritten (the source used is `import/test.mp4`, matching
   the editor's hardcoded media path).
 - Named export (full-segment transcode in
   `shared/ffmpeg.export_named_model`, wired to the `exportButton` in
   `editor/editor.py`): persists the in-memory model, applies session locks,
   loads both schemes, plans all keep-segments, creates their directory trees,
   and frame-accurately re-encodes each named MP4 (libx264/aac, not `-c
   copy`). Unique temporary files and no-clobber commits prevent overwrites;
   per-clip failures are returned as a partial result.
 - File naming scheme parser (`shared/scheme.py` + `shared/naming.py`):
   `render_filename(scheme, tags)` produces a filename stem from a template,
   with test coverage in `tests/test_naming.py` and `tests/test_scheme.py`. See
   the "File Naming Scheme" section above for the full syntax.
 - Folder organization resolver (`shared/paths.py`): strict folder grammar,
   required Network/Type/Time Period placeholders, optional multi-folder
   groups, tag sanitation, portable component validation, and safe relative
   component output. Covered by `tests/test_paths.py`.
  - Settings (`settings/settings.py`): independent file/folder scheme defaults,
   validation, production-resolver previews, atomic `QSaveFile` persistence,
   cancel/window-close restoration, and offscreen UI tests in
   `tests/test_settings.py`.
 - Export planning (`shared/exporting.py`): strict model/settings validation,
   four-tag requirements, lock materialization, normalized within-batch and
   existing-destination collision checks, complete relative-path limits, and
    reparse-point/traversal defenses. Covered by `tests/test_exporting.py` and
    mocked executor tests in `tests/test_ffmpeg.py`. Editor tag-lock display,
    pinned-value semantics, and locked-only segment carry-over are covered by
    `tests/test_editor_locks.py`; front-end required-tag enforcement and the
    refusal-to-write behavior are covered by
    `tests/test_editor_required_tags.py`; and End Seg boundary placement
    (insert vs. move, and the refusal guards) in
    `tests/test_end_boundary.py`. All three drive the real `MediaPlayer`
    methods through the shared `tests/editor_stub.py` widget-backed stub, so
    the shipped code is what gets tested. The full suite currently contains
    272 tests.


**Next:**
- **Smart-cut export**: per non-ignored segment, find the innermost
  keyframes bracketing the two cut points, lossless-copy between them,
  transcode only the partial-keyframe ends, then concat. The placeholder
  `clip_to_temp` (stream copy) still lives in `shared/ffmpeg.py`; the
   keyframe-bracketed smart-cut version replaces/augments `export_named_model`
  when it lands. Also wire export into a background `QThread` (today it runs on
  the GUI thread, which blocks the editor while cutting).

## Gaps to be aware of

- `prototypes/` contains earlier iterations of the editor. Treat them
  as historical — the active code is in `editor/` (and the scanner in
  `scanner/`).
- Automated boundary detection is fully wired: Test Scan (preview, in-memory
  midpoints), Finished (full-source `blackdetect` → `.cmct` → editor), and
  the Scanner→Editor handoff when a `.cmct` already exists.
- Export is present as a named full-segment transcode; the smart-cut
  (keyframe-bracketed copy+transcode+concat) version is the remaining piece.
  The legacy numeric `export_segment_clips()` helper still exists for
  compatibility, but Editor export uses the named planner/executor path.
- Export runs synchronously on the GUI thread; move to a worker `QThread`
  (see `PreScanWorker`) for the smart-cut step so the editor stays
  responsive.
