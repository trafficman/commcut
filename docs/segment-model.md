# The segment model and the Editing Wizard state machine

The `.cmct` sidecar format, the segment operations, the editor's Active Segment
state machine (including the boundary peek and tag locks), the End Seg outcome
rules, and the required-record-field enforcement.

Applies to: `shared/segments.py`, `editor/editor.py`, `editor/editorwindow.ui`,
`shared/timeline.py`, `shared/exporting.py:missing_required_tags`.

Related: [scanner.md](scanner.md) (where the boundaries come from),
[naming-and-organization.md](naming-and-organization.md) (where the tags go),
[testing.md](testing.md).

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
`.cmct`. `probe_duration(path)` is the ffprobe-backed duration used when a
sidecar is created.

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
| **Stage**         | Read form tags into the active segment, save the whole model to `.cmct`, clear `dirty`, advance `current_index`. Staging the **last** segment reports that the set is complete and names **Finished - Export** — see "The end of editing" below. |
| **Undo**          | Reload model from `.cmct` (reverts all unstaged changes), clear `dirty`.                            |
| **Toggle Zoom**   | Toggle between zoom-to-active-segment and fit-whole-video.                                         |
| **Active ←/→**    | Move `current_index` by ±1 (clamped). On any active change, snap the playhead to the new segment's start, with the boundary peek described below. |

**The boundary peek.** A segment boundary is a transition point and therefore
usually a black frame, so resting the playhead there is correct but useless to
look at: stepping through segments with Active ←/→ showed nothing but black. On
an active-segment change the playhead now seeks to the boundary, seeks
`SEGMENT_PREVIEW_FRAMES` (15) frames past it, waits `SEGMENT_PREVIEW_DWELL_MS`
(450), and seeks back to the exact boundary. The resting position is still the
cut point, which is what **End Seg** and **Start Seg** read, so nothing about
the editing model changes — the peek only ever lends the picture.

All of that lives in `shared/mpv.py:BoundaryPreview`, which owns the
single-shot `QTimer`. The editor drives it through
`MediaPlayer._activate(index, preview=True)`, the single place a *step through
segments* changes the active segment (`_move_active`, `on_start_segment`,
`on_stage`); `on_undo` passes `preview=False` and the initial load snaps
directly. The peek is skipped, leaving a plain seek to the boundary, when
`frames` is 0/None, when the frame rate is not known yet, or when mpv is
playing (a seek out and back would stutter mid-playback).

**A pending peek is always given up before the playhead is used for anything
else**, because a timer that fires 450ms later would otherwise move the
playhead out from under an action in progress. `cancel()` is called by
`on_seek_requested` (timeline scrub), `on_step_frames`,
`on_step_keyframe`, `on_transport_clicked`, `on_end_segment`,
`on_start_segment`, `on_stage`, `on_undo`, and `on_export`, plus every call to
`_snap_playhead_to_active_start`. As a second line of defence the return seek
also skips itself when `bridge.position` has drifted more than
`_PREVIEW_POSITION_EPSILON` from where the peek left it.

`BoundaryPreview.frames` is the off switch a future settings key would drive —
assigning `0` restores the original snap-to-boundary behavior — and
`dwell_ms`/`_timer.setInterval` tune the delay. There is no settings UI for it
yet; it is always on at the constants above.

**Zoom mode is owned by the timeline widget, not the button.** The toggle is
the only thing that picks a mode: `on_toggle_zoom` hands it to
`TimelineWidget.set_zoom_mode(ZOOM_SEGMENT | ZOOM_FIT)`, the widget stores it,
and `resizeEvent` re-applies *that* mode. This is a bug fix, not a
refactor — `resizeEvent` used to call `zoom_to_segment(active_index)`
unconditionally, so widening the window silently undid a zoom-to-fit while the
toggle still showed the fit state, and the only way back was two clicks.
`set_active_index` also updates the remembered segment, so a resize in segment
mode re-centres on the segment currently being edited. The editor reads the
toggle's state in `_refresh_timeline` rather than keeping a second copy of the
mode, so the button and the view cannot drift apart.

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

## The end of editing

`on_stage` on the last segment has nowhere to advance to, so it says so: a dialog
that the set is complete and points at **Finished - Export**. The record is still
written and the `.cmct` still saved first — it is a stage, not a shortcut past one.

This used to be `print("Editing complete.")`, which is the same class of bug the
export summary screen fixed: a print goes nowhere in a windowed build, so the one
moment the user learns they are done told them nothing. The two belong together —
Stage says the editing is finished, Export says what became of it, and the summary
screen is where the wizard ends and hands the user back to the main menu. See
[naming-and-organization.md](naming-and-organization.md#the-summary-screen).

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
  discovered the problem inside the preflight, after the bad write.) The check
  and the sidecar save both stay on the GUI thread; only the transcode that
  follows them is handed to a worker, from a snapshot of the model — see
  [naming-and-organization.md](naming-and-organization.md#the-export-runs-off-the-gui-thread).
- `_refresh_required_fields()` outlines the missing fields in red. It is
  recomputed on every keystroke, on the Skip toggle, and on every segment
  change, so the outline always states what Stage will demand right now.

Note that the `.cmct` is still *expected* to contain empty tags for segments
the user has never staged — lock materialization happens at export time, not
at save time. The front-end rule applies to what a user actively stages and to
the active segment on export, not to the whole file.

## Conventions and gotchas

- **`Position` and seek clamping.** Boundary positions in
  `end_segment`/`start_segment` are clamped to `(start, end)` of the active
  segment. A no-op returns `False`.
- **Tag field programmatic updates.** `_write_tags_to_form` uses
  `blockSignals(True/False)` around `setText` so the `textChanged`
  handler (`on_tag_edited`) doesn't fire on initial load and falsely mark
  the model dirty.
- **New segments inherit only locked tags.** Not the previous segment's tags.
  See "Locks" above for why the three places that could disagree were made to
  agree.
