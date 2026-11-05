# The `.cmct` project file format

A complete, standalone specification of the commcut project file (`.cmct`):
the JSON layout, the transition-point model, segment and tag rules, and
the rules a third-party reader or writer must follow to be interoperable
with commcut.

Applies to: `shared/segments.py`, `shared/scheme.py`,
`shared/exporting.py:validate_segment_model`.

Related: [clip-record-format.md](clip-record-format.md) (the per-clip
record a `.cmct` becomes at export), [segment-model.md](segment-model.md)
(the editor state machine and its operations), [scanner.md](scanner.md)
(where the initial boundaries come from).

> This document is the third-party contract. Where it and
> `shared/segments.py` disagree, the code is the bug and this document
> should be fixed in the same change — see the documentation conventions
> in [README.md](README.md).

## 1. Purpose and scope

A `.cmct` is the project file for one source video: a compilation
television recording that the user is cutting into clips. It records how
the source was segmented and what tags each segment carries, so an
editing session can be interrupted and resumed, and so the export step
can turn the segments into named clips.

A third party that wants to *read* commcut projects (to preview an
editing session, to generate segments programmatically, to migrate an
archive) needs this format. A third party that wants to *write* projects
commcut will open needs it plus section 9 (writer rules). Note the
asymmetry with the `.cnfo` clip record: a `.cmct` describes **work in
progress on a source video**, not finished clips, and the two formats
are not interchangeable.

## 2. File identity

| Property | Value |
|---|---|
| Extension | `.cmct` (lowercase) |
| Relationship to the video | `<stem>.cmct` beside the source video: the video's path with its extension replaced by `.cmct` (`compilation.mp4` → `compilation.cmct`) |
| Where it lives | **Beside the source video, wherever that video is.** There is no folder it has to be in; the source can be picked from anywhere with a native file dialog or a drag-and-drop. |
| What a project is | A `.cmct` in the same folder as a video **with the same stem** belongs to that video. A `.cmct` with no sibling video is unattached working data; a video with no `.cmct` has not been started (or was started elsewhere). |
| Media type | JSON text (section 3) |
| Schema owner | `shared/segments.py:SegmentModel` |

The source path is always handed to the editor as a constructor
argument — the `.cmct` is *derived* from it by `sidecar_path`, never
searched for. No window re-resolves a default of its own, so a `.cmct`
must be named exactly as the derivation produces: same stem, `.cmct`.

## 3. Encoding and JSON basics

- The file is a single JSON object, written with
  `json.dump(..., indent=2)` — two-space indentation, UTF-8, no BOM.
- Readers must be tolerant of formatting: whitespace, key order, and
  the exact spelling of numbers are not significant.
- The file must be valid JSON whose top level is an object. Anything
  else is a refusal (section 7).

## 4. Document structure

```json
{
  "source": "compilation.mp4",
  "duration": 120.0,
  "segments": [
    {"start": 0.0,   "ignored": false, "tags": {}},
    {"start": 42.5,  "ignored": true,  "tags": {}},
    {"start": 78.2,  "ignored": false, "tags": {"title": "Worlds Finest"}}
  ]
}
```

| Top-level key | Required | Type | Meaning |
|---|---|---|---|
| `source` | yes | string | The source video's **file name** (basename, e.g. `compilation.mp4`), not a path. |
| `duration` | yes | number | The source video's total duration in seconds. |
| `segments` | yes | array of segment objects | The segmentation, in order (section 5). May be empty in a hostile or hand-written file, but an empty model cannot be exported (section 7). |

No other top-level keys exist. commcut's own reader, `SegmentModel.from_dict`,
uses `dict.get` defaults and ignores anything it does not know — but a
writer has no business emitting unknown keys, because a future schema
change would give them a meaning this section does not describe.

### 4.1 Segment object

| Key | Required | Type | Meaning |
|---|---|---|---|
| `start` | yes | number | This segment's start offset in seconds from the beginning of the source. |
| `ignored` | yes | boolean | Whether the segment is skipped. Ignored segments are not exported, and are exempt from the required-tag rule (section 8). |
| `tags` | yes | object (string → string) | The segment's tags, keyed by canonical tag key (section 6). |

## 5. The transition-point model

This is the load-bearing idea of the format. The source video is tiled
**contiguously** by its segments, and each segment entry stores **only
its start time**:

- Segment *i* spans from `segments[i].start` to `segments[i + 1].start`
  (or to the top-level `duration` for the last segment).
- Segment *end* is never stored. Its end and the next segment's start
  are the same value.

Because every segment is derived from one ordered list of start points,
**overlaps and gaps are structurally impossible**: there is no field in
which an overlap or a gap could even be written. A segment's duration is
`end − start`, computed, never persisted.

### 5.1 Invariants

A conforming file satisfies all of the following. These are enforced by
`shared/exporting.py:validate_segment_model` before anything is
exported, and a reader should treat a violation as a refusal
(section 7), not as data to repair.

1. `duration` is a finite, non-negative number.
2. `segments` is a list of objects; each `start` is a finite,
   non-negative number.
3. `segments[0].start` is exactly `0.0`. The tiling covers the whole
   video; there is no leading gap.
4. Start points are **strictly increasing**: `segments[i + 1].start >
   segments[i].start`. No two segments share a start point, which would
   give a zero-length segment.
5. Every `ignored` is a boolean (not `0`/`1`, not a string).
6. Every `tags` is an object whose values are strings.

Every segment automatically has positive length: the first by rule 3
combined with rule 4, the last because its end is `duration` and a
duration of zero with one segment yields an empty model the export
refuses (section 7).

## 6. Tag keys and values

Segment tags use the same closed vocabulary as the `.cnfo` clip record,
owned by `shared/scheme.py:CANONICAL_TAG_KEYS` — see
[clip-record-format.md](clip-record-format.md) section 6.5 for the full
table. The canonical keys are:

`title`, `network`, `block`, `filler_type`, `year`, `time_period`,
`show`, `special`, `length`, `information`.

Rules:

- Keys are **canonical** in a written file. The scheme aliases `type`
  and `info` resolve on read (via `canonical_tag_name`) for hand-edited
  projects, but a writer must emit `filler_type` and `information` only —
  the same read-tolerant/write-canonical rule as the clip record.
- Values are **raw strings**, unsanitized. Sanitation happens only when
  rendering a filesystem destination at export; a project file must
  never be sanitized.
- Unlike the clip record, an empty value is *not* omitted from a
  hand-written project file: it simply means "not set", and readers
  treat a missing key and an empty string as the same. commcut writes
  only tags the user set; an unset tag is absent.
- Unknown keys are a refusal at export time (`plan_export` raises
  naming the segment and the tag); a reader that merely *displays* a
  project may ignore them, but it must not silently drop them from a
  round-trip.

**Why the two formats share a vocabulary but not a shape.** The `.cmct`
is a *working* document: tags are edited in place, segment by segment,
and a missing tag is an ordinary state ("this segment is not tagged
yet"). The `.cnfo` is a *published* document: at export, the keep
segments' tags are canonicalized, required-validated, sorted, emptied,
and frozen into the record. The vocabulary is the same because the
export is a direct transcription of a segment's tags into a record —
it is the same data at two points in its life.

## 7. Error categories

commcut has no dedicated error type for project files: `SegmentModel.load`
raises the standard JSON/OS errors, and the structural refusal lives in
the export preflight. A third-party reader should produce these
distinctions:

| Category | Meaning |
|---|---|
| `unreadable` | The file could not be opened, or is not valid UTF-8/JSON. |
| `not-a-project` | Valid JSON whose top level is not an object, or that lacks `source`/`segments`/`duration`. |
| `invalid-model` | Structurally a project, but violating a section 5.1 invariant: first start not `0.0`, non-increasing starts, negative or non-finite times, a non-boolean `ignored`, a non-object `tags`. |
| `unexportable` | Structurally valid, but the export cannot use it: no segments at all, every segment ignored, or a keep-segment with an invalid duration. |
| *(missing file)* | No `.cmct` beside the video means "not started", not an error. A new session creates a placeholder (section 8). |
| *(missing source)* | A `.cmct` whose sibling video is gone is unattached working data; it describes a video its reader cannot open. Do not guess at a new source. |

## 8. Semantics commcut relies on (and a reader must not break)

1. **The `.cmct` is rewritten on every Stage.** The editor persists the
   in-memory model on each staged segment, and again before export. A
   reader must not treat the file as immutable, nor assume a project on
   disk is complete: it is the last *staged* state, and unstaged edits
   exist only in the editor's memory.
2. **Required tags are an export rule, not a file rule.** A segment that
   is about to be exported needs `title`, `network`, `filler_type`,
   `time_period` non-blank (`shared/exporting.py:missing_required_tags`);
   Stage and Export both refuse to persist an incomplete record, and
   **ignored segments are exempt**. A project file full of untagged
   segments is a perfectly readable project — it is just not yet
   exportable. A reader must not refuse a project for missing tags.
3. **A missing `.cmct` is a fresh start.** Opening a source with no
   sidecar creates a placeholder: one segment spanning the whole video
   (`start: 0.0`, `ignored: false`, `tags: {}`), with the duration
   probed from the video. An *existing* `.cmct` is left alone — no probe,
   no rewrite.
4. **The source folder must be writable.** The editor writes the sidecar
   beside the video for the whole session, so `validate_source_video`
   refuses a folder that cannot take a write *before* any editing, and
   the editor's own write is the backstop if the folder stops being
   writable mid-session. Never write beside a source video from anywhere
   else.
5. **The scanner creates the initial file on the full scan.** "Finished
   — Scan Full Source Video" writes the scanned boundaries to a `.cmct`
   and hands off to the editor; "Skip Scan" writes the placeholder.
   Either way, what the editor opens is always a `.cmct` in this format.
6. **Tag locks are session state, not file state.** Locked tag values
   that carry across unedited segments are **not persisted** to the
   `.cmct`; they exist only while the editor is open. A project file
   therefore never contains a lock, and a reader will never find one.
7. **New segments inherit only locked tags**, not the previous segment's
   tags. Tag inheritance is an editor behavior, not a file property: a
   reader that sees identical tag maps on adjacent segments must not
   infer that one was copied from the other.

## 9. Writer rules (producing projects commcut will open)

A third-party writer must:

1. Write a single JSON object with exactly the keys `source`,
   `duration`, `segments`, two-space indentation, UTF-8, no BOM.
2. Satisfy every section 5.1 invariant: first `start` exactly `0.0`,
   strictly increasing starts, all times finite and non-negative.
3. Store only segment **start** points. Never an end point, a duration
   per segment, or a gap/overlap — the model has no way to represent
   them.
4. Use canonical tag keys only, string values, raw (unsanitized).
   Emit `ignored` as a JSON boolean, `tags` as an object even when
   empty.
5. Place the file beside the video with the shared stem (section 2),
   and check the folder is writable before doing any expensive work —
   commcut does exactly this in `validate_source_video`.
6. Write the file **atomically** if the writer can: write to a
   temporary name in the same directory and rename over the final
   name, so a reader never sees a half-written project. (commcut's own
   `SegmentModel.save` writes in place; a third-party writer may do
   better, but a reader must tolerate a truncated file as `unreadable`,
   not crash.)
7. Not invent a version field. There is no format version in a `.cmct`
   document: the transition-point model is the whole schema. A future
   schema would be a document change argued in this file, not a key
   smuggled into the JSON.

## 10. Reader algorithm (minimal conformant reader)

1. Read the file as bytes; decode as UTF-8. Failure to open or decode
   → `unreadable`.
2. Parse as JSON. Parse failure → `unreadable`. Top level not an object,
   or missing `source`/`segments`/`duration` → `not-a-project`.
3. Check `duration` is a finite, non-negative number; `segments` is a
   list; each segment is an object with a finite `start`, a boolean
   `ignored`, and an object `tags`. Failure → `invalid-model`.
4. Check `segments` is non-empty, `segments[0].start == 0.0`, and starts
   are strictly increasing. Failure → `invalid-model`.
5. Derive each segment's end as the next segment's `start` (or
   `duration` for the last). The derived ends are the segments.
6. Missing file → "not started": offer a placeholder (one segment
   spanning the whole video), not an error.

## 11. Worked examples

### 11.1 A fresh placeholder (one segment, nothing tagged)

```json
{
  "source": "compilation.mp4",
  "duration": 120.0,
  "segments": [
    {"start": 0.0, "ignored": false, "tags": {}}
  ]
}
```

### 11.2 A scanned project with one ignored segment

```json
{
  "source": "compilation.mp4",
  "duration": 120.0,
  "segments": [
    {"start": 0.0,   "ignored": false, "tags": {}},
    {"start": 42.5,  "ignored": true,  "tags": {}},
    {"start": 78.2,  "ignored": false, "tags": {"title": "Worlds Finest"}}
  ]
}
```

Derived spans: `0.0–42.5`, `42.5–78.2` (ignored), `78.2–120.0`.
Note that the second segment's end, `78.2`, is stored only as the third
segment's start.

### 11.3 The same project after tagging (what export sees)

```json
{
  "source": "compilation.mp4",
  "duration": 120.0,
  "segments": [
    {"start": 0.0,   "ignored": false, "tags": {"title": "Opening", "network": "Cartoon Network", "filler_type": "Promo", "time_period": "2000s"}},
    {"start": 42.5,  "ignored": true,  "tags": {}},
    {"start": 78.2,  "ignored": false, "tags": {"title": "Worlds Finest", "network": "Cartoon Network", "filler_type": "Promo", "time_period": "2000s"}}
  ]
}
```

The ignored middle segment carries no tags and is exempt from the
required-tag rule; the two keep segments each carry all four required
tags and are exportable. At export, each keep segment's tags become a
`.cnfo` record (section 6), the filename and folder are rendered from
them, and this file is left behind with the source — it is never
shipped with the clips.

## 12. Conformance checklist

A reader conforms if it:

- [ ] Derives segment ends from the next start point (or `duration`),
      never expecting a stored end.
- [ ] Treats a missing `.cmct` as "not started", not as an error.
- [ ] Accepts untagged and placeholder projects as valid.
- [ ] Exempts ignored segments from any tag-completeness assumption.
- [ ] Resolves the `type`/`info` aliases on read, and doesn't drop
      unknown keys from a round-trip.
- [ ] Treats a `.cmct` beside a missing video as unattached data,
      without guessing a new source.

A writer conforms if it additionally:

- [ ] Satisfies the section 5.1 invariants (first start `0.0`, strictly
      increasing, finite non-negative times).
- [ ] Stores start points only, canonical keys, raw values, boolean
      `ignored`, and an object `tags` on every segment.
- [ ] Places the file beside the video by stem derivation and checks
      the folder is writable before writing.

## Coverage

`tests/test_editor_prescan.py` (placeholder creation, existing
sidecar left alone), `tests/test_editor_required_tags.py` (Stage/export
refusals and the sidecar write), `tests/test_end_boundary.py` (the
transition-point operations: insert vs. move vs. blocked),
`tests/test_exporting.py` (`validate_segment_model` and the export
preflight), `tests/test_sources.py` (the writable-folder rule),
`tests/test_source_handoff.py` (the sidecar beside the source),
`tests/test_scanner_threads.py` (the scanner's initial write).
