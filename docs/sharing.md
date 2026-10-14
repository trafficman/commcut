# Library Sharing

The export library, or a tag-filtered part of it, as a zip a
recipient drops into their `import/` folder.

Applies to: `sharing/sharing.py`, `sharing/sharingwindow.ui`,
`shared/sharing.py`, `shared/environment.py:share_folder`,
`tests/test_sharing.py`.

Related: [naming-and-organization.md](naming-and-organization.md)
(the library a share is read from, and the record a share carries),
[importing.md](importing.md) (the recipient's side),
[tag-vocabulary.md](tag-vocabulary.md) (the advisory vocabulary the
filter's dropdowns are not), and
[architecture.md](architecture.md) (the shell that opens the window
and the MpvBridge pattern the preview uses).

## What a share is

A share is a zip of clips from the export library. Every clip's
video travels with its `.cnfo` record, at the same relative path
beneath one root folder named after the zip. A recipient extracts
the zip into their `import/` folder and imports it through the
usual pipeline, which reads the tags straight out of the records —
so a share round-trips through the *recipient's* two schemes, not
a re-rendering here. Nothing parses a filename back into tags,
ever (invariant 12), which is why the record has to travel with
the video: the filename is a lossy projection of the tags, and the
record is the only complete copy.

A share is a **copy**, not a move. The library it came from is
untouched, and a share can be deleted once it has been received.

## The window

`sharing/sharing.py` is the Library Manager's shape
([architecture.md](architecture.md#the-library-manager)) with the
tag form replaced by the share filter: a read-only tree of what a
share would hold on the left, an mpv preview on the right, and the
filter below the player instead of the tag form. Selecting a clip
leaf loads its preview through the real `MpvBridge`, and
`closeEvent` shuts the player down before the window is destroyed
(invariant 4).

The window is opened by the shell from the main menu's
**Library Sharing** button, with no arguments: the library is the
export folder, which Settings configures, and the zip lands in the
`share/` folder beside the app. There is no source path to hand
off, because a share reads the library rather than a video.

## The filter

The filter is the one place its semantics live, in
`shared/sharing.py:filter_clips`:

- **Within one namespace, the rules are OR'd.** A clip matching
  any of that namespace's values counts.
- **Across namespaces, they are AND'd.** A clip has to match every
  namespace's rule to be included.
- **An empty include list is every clip** — the whole library,
  which is the default.
- **An exclusion drops a clip the inclusions would otherwise have
  kept**, and is applied after the inclusion.

Matching happens in `value_dedup_key` space, so `cartoon network`
and `Cartoon Network` are one value — the same identity rule the
vocabulary and the destination planner use, so a value typed into a
row and a folder the library rendered agree. A namespace is matched
by its canonical key, its alias (`type` for `filler_type`), or any
capitalization, because the spelling is casefolded before the alias
table is consulted.

Both combos are editable and neither validates: the vocabulary is
advisory (invariant 13), and a filter is a question about the
library, not a claim the library has to answer. The namespace combo
offers the nine suggestable fields (`title` is unique per clip, so
it has no dropdown and no filter); the value combo offers the
catalog's own values for the chosen namespace — the library's truth,
not the advisory `vocabulary.json`. A row the user has not finished
typing is not an error; it is a rule that matches nothing, and it is
dropped rather than refusing the whole filter.

## The tree is the oracle

The tree is derived state: it is rebuilt from a fresh
`build_catalog` walk on every open, and the filter is re-run
**synchronously** on every row change, so what the tree shows is
always what the share would hold. That is the point of running the
filter on the GUI thread rather than on a worker: a row the user is
typing into is answered immediately by the clips it match, and the
tree is the answer. The Share button follows the result, so a filter
that matches nothing cannot be shared.

Only folders that contain clips exist, and every level is sorted
`casefold()`, so a folder named `B` sorts before a clip named
`b.mp4` however the catalog's path order interleaved them.

## The zip

`shared/sharing.py:write_share_zip` owns the write, and the window
owns only the threads around it:

- **Contents**: each clip's video and its `.cnfo` record, at their
  relative paths beneath one root folder named after the zip's own
  stem. `ZIP_STORED`, because compressing media is wasted CPU.
- **Naming**: `shared/sharing.py:share_zip_name` applies the
  ` (Created with CommCut)` suffix before the extension and
  deduplicates it, so `My Share` and `My Share (Created with
  CommCut)` name the same file. A name that sanitizes to nothing
  falls back to `CommCut Share`.
- **No clobber**: a taken name becomes `stem (1).zip`, `stem (2).zip`,
  and so on.
- **Atomic**: the zip is written to a `.part` beside its final name
  and renamed into place, so an interrupted write leaves no half-file
  where a share is expected.
- **Resilient**: a clip whose video is no longer on disk is skipped —
  and its record with it, since a record with no video describes
  nothing — rather than refusing the whole share, because one deleted
  clip on a network share should not cost the other eight hundred.
- **Cancellable**: the cancel event is checked before each clip, and
  a cancel removes the partial file and returns `None`.

The zip runs on a worker `QThread` behind a non-modal, cancellable
progress dialog that takes a real denominator from the first progress
report. The window stays up while a share writes, because a share is
a copy and there is nothing to freeze the user out of. Both the walk
and the zip hang their teardown off `QThread.finished` (invariant 14),
and `closeEvent` refuses to close over either thread, deferring the
real close to the teardown that follows the thread actually stopping.

## The Share folder

Shares are written to `share/` beside the app —
`shared/environment.py:share_folder`, fixed rather than configurable
for the same reason `import/` is: it is the one folder this feature
writes, and a known location is easier to find than a save dialog. It
is created on first run with the other app folders, and the portable
build ships it with a `README.txt` placeholder, because an empty
folder does not survive being zipped. The export root is refused if it
overlaps `share/` (`shared/exporting.py:export_folder_setting_error`),
so exported clips cannot land in the folder shares are written to.

`share/` is kept out of the source release
(`packaging/source_release.py`), alongside `import/`, `export/` and
`temp/`: for a source install the project root *is* the install root,
so a developer's real shares would otherwise be published. It is
capitalized to distinguish it from the lowercase `sharing/` code
folder beside it, which *is* in the manifest.

## What is not here

There is no manifest in the zip. The records carry the schema version
and the importer refuses a version it does not know, so a manifest
would be a second copy of a fact the record already states. There is
no way to share a single clip's tags without its video, because a
record with no video describes nothing. And there is no re-rendering
of names here: the recipient's schemes decide what a shared clip is
called, which is the whole point of sending the record.
