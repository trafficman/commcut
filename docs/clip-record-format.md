# The `.cnfo` clip record format

A complete, standalone specification of the commcut clip record (`.cnfo`):
the XML layout, schema versions, tag keys, value constraints, error
categories, and the rules a third-party reader or writer must follow to be
interoperable with commcut.

Applies to: `shared/records.py`, `shared/scheme.py`, `shared/naming.py`,
`shared/catalog.py`.

Related: [naming-and-organization.md](naming-and-organization.md) (the
clip record in the export pipeline, and why nothing parses a filename back
into tags), [importing.md](importing.md) (reading somebody else's
records), [sharing.md](sharing.md) (records travelling in a share zip).

> This document is the third-party contract. Where it and
> `shared/records.py` disagree, the code is the bug and this document
> should be fixed in the same change — see the documentation conventions
> in [README.md](README.md).

## 1. Purpose and scope

commcut exports clips from a compilation video. Every exported clip
consists of a video file (`<stem>.mp4`) and a clip record
(`<stem>.cnfo`) that sits beside it: same directory, same file stem,
different extension. The record is the **only authoritative copy** of the
clip's tags and provenance. The filename and folder are a lossy projection
of the tags under two user-editable schemes (a file naming scheme and a
folder organization scheme), and nothing — commcut or anyone else — may
recover tags by parsing a filename.

A third party that wants to *read* commcut libraries needs this format. A
third party that wants to *write* records commcut will accept needs it
plus section 9 (writer rules). A third party that neither reads nor
writes the record — for example, a tool that only needs the video — does
not need this document, and must not misinterpret the record's presence
or absence (section 8).

## 2. File identity

| Property | Value |
|---|---|
| Extension | `.cnfo` (lowercase) |
| Relationship to the video | `<stem>.cnfo` beside `<stem>.mp4`, in the same directory |
| What a clip is | **A record with a sibling video.** A `.cnfo` with no sibling `<stem>.mp4` is an orphan: it is not a clip, and readers must ignore it. A `<stem>.mp4` with no sibling `.cnfo` is not a commcut clip either; it carries no tags commcut can read, and readers must not invent tags from its filename or folder path. |
| Media type | XML text, UTF-8 (section 3) |
| Schema owner | `shared/records.py:RECORD_SCHEMA_VERSION` (currently `2`) and `shared/records.py:RECORD_ROOT_TAG` |

**Why not `.nfo`.** The `.nfo` extension belongs to media servers, which
scan for it and expect a schema commcut does not write. A file in the
wrong dialect is worse than no file, because a media server may act on
its absent keys. The distinct `.cnfo` extension keeps commcut records
out of media-server scans, and leaves a real `.nfo` free for a derived
compatibility file if one is ever wanted.

## 3. Encoding and XML basics

- The file is XML 1.0, encoded in **UTF-8**, written with the declaration
  `<?xml version="1.0" encoding="utf-8"?>` and no byte-order mark (BOM).
- Readers must also accept a leading UTF-8 BOM, e.g. from a hand-edit in
  a Windows editor. commcut's own reader opens records with `utf-8-sig`
  for exactly this reason; a BOM is not an error.
- The document is indented (two spaces) for human readability. Whitespace
  between elements is insignificant to readers; element text is
  significant (see the tag rules in section 6.5).
- The file must be well-formed XML. Anything else is a refusal
  (`not-well-formed`, section 7).

## 4. Document structure

The root element is `commcut-clip` with a `version` attribute:

```xml
<?xml version="1.0" encoding="utf-8"?>
<commcut-clip version="2">
  <source>Cartoon Network - April Fools 2000.mp4</source>
  <segment index="7" start="314.2" duration="29.9" />
  <clip_id>12345678-1234-4123-8123-123456789abc</clip_id>
  <tag key="title">Toonami Worlds Finest</tag>
  <tag key="network">Cartoon Network</tag>
  <tag key="filler_type">Promo</tag>
</commcut-clip>
```

Element order in a written file is fixed and deterministic
(`source`, `segment`, `clip_id`, then `tag`s sorted by key), but readers
must not depend on it: accept the elements in any order, exactly once
each, as section 6 defines. Unknown elements are a refusal, not
something to skip (section 7).

### 4.1 Element inventory

| Element | Required | Since | Content |
|---|---|---|---|
| `<source>` | yes | v1 | The source video's **file name only** (basename, not a path). |
| `<segment>` | yes | v1 | Provenance: which slice of the source the clip was cut from. Attributes only, no text. |
| `<clip_id>` | yes in v2; forbidden in v1 | v2 | The clip's identity: a canonical UUID string (section 6.3). |
| `<tag>` | zero or more | v1 | One tag/key-value pair. Attribute `key`, text content is the value. |

There are no other elements. A record must not store a filesystem path:
the clip's location is a function of its tags and the two schemes, and
recomputing it is the only way it cannot go stale.

## 5. Schema versions

The `version` attribute on `<commcut-clip>` is an integer **schema
version owned by this format**. It is independent of the commcut release
number: a record has to outlive the release that wrote it, and a
migration has to be able to name what it is migrating from.

| Version | Rule |
|---|---|
| Absent attribute | Means `1`. Records written before the attribute existed are the first schema, and are readable forever. |
| `1` | `<source>`, `<segment>`, `<tag>` only. A v1 record has **no identity**: it reads as unidentified. A v1 document carrying `<clip_id>` is refused (that element did not exist in v1; its presence is an unrecognized element, not a migration). |
| `2` | Current. Adds a mandatory `<clip_id>` — a v2 document without a non-empty, canonical clip ID is refused. |
| Greater than the reader supports | **Refused** (`unsupported-version`). A reader must never try to read a newer schema by guessing. |
| Not an integer, or `< 1` (e.g. `0`, `one`) | Refused (`unsupported-version`). |

**Migration is lazy, rewrite-on-touch.** commcut never sweeps a library to
upgrade records. Any code path that rewrites a record assigns the clip an
ID and writes the current schema, so the library converges as it is used.
A third-party reader should treat a v1 record as valid data with an
empty identity, not as corrupt. A third-party writer must always write
the current version (section 9).

## 6. Field definitions

### 6.1 `<source>`

- Text content: the basename of the file the clip was cut from, e.g.
  `compilation.mp4`. Never a directory path, never a URI.
- Required and non-empty. A missing `<source>` is a refusal
  (`invalid-record`).

### 6.2 `<segment>`

Attributes:

| Attribute | Type | Rule |
|---|---|---|
| `index` | integer, `>= 0` | Zero-based position of the segment within the source's segment list at export time. |
| `start` | number | Start offset in seconds from the beginning of the source. |
| `duration` | number | Length of the clip in seconds. |

All three attributes are required and must parse as numbers; a missing or
non-numeric value is a refusal (`invalid-record`). Numbers are written
with Python's `repr(float(...))` (e.g. `314.2`, `29.9`, `0.0`), which
round-trips exactly; readers should accept any spelling a standard
float/integer parser accepts (`314`, `3.142e2`, …). This is provenance
only — it describes where the clip came from, it does not locate it in
the library.

### 6.3 `<clip_id>` (v2 only)

The clip's identity, independent of its filename, folder, and tags. The
filename cannot serve as identity: it is a lossy projection of the tags,
two clips can render to the same name under a fallback, and a rename
changes the name while the clip stays the clip.

- **Form:** a UUID in its canonical string form — lowercase, hyphenated,
  `8-4-4-4-12` hex digits, e.g.
  `12345678-1234-4123-8123-123456789abc`. The canonical check is
  `str(uuid.UUID(value)) == value`: it rejects uppercase, braced,
  hyphen-less, and `urn:uuid:` spellings in one expression.
- **Version nibble:** not enforced. commcut generates UUID v4, but the
  format guarantees the *shape*, not a particular version. A reader must
  accept any canonical UUID.
- **Presence:** v2 requires exactly one non-empty `<clip_id>` with a
  canonical value; missing, empty, or non-canonical is a refusal
  (`invalid-record`).
- **Uniqueness:** writers generate a fresh ID per clip
  (`shared/records.py:new_clip_id()`); a clip keeps its ID across
  renames, moves, and re-exports. Readers must not generate or alter IDs
  for records they only read.

### 6.4 `<tag key="...">`

One element per tag. The `key` attribute names the tag; the text content
is the value.

- Values are **raw**: as the user entered them, unsanitized. Sanitation
  happens only when rendering a filesystem destination, and must never be
  applied to the record.
- Empty values are **omitted** by writers. A missing tag and an empty tag
  mean the same thing; a reader that encounters an empty `<tag>` may
  treat it as absent, but a writer must not emit one.
- A tag key may appear **at most once** per record. A duplicate is a
  refusal (`invalid-record`) — including a duplicate that only appears
  after alias resolution (section 6.5).
- Tag elements are written **sorted by key**, so two records of the same
  clip render identically and diff cleanly. Readers must not depend on
  order.

### 6.5 Tag keys (the closed vocabulary)

Tag keys are canonical `.cmct` keys, owned by
`shared/scheme.py:CANONICAL_TAG_KEYS`. The vocabulary is closed:

| Canonical key | Meaning | Notes |
|---|---|---|
| `title` | Clip title | Required to export (see section 8). Unique per clip in practice, but that is a policy, not a format rule. |
| `network` | Network the filler aired on | |
| `block` | Programming block | |
| `filler_type` | Type of filler (e.g. Promo, Bumper) | |
| `year` | Year | |
| `time_period` | Time period (e.g. 2000s) | |
| `show` | Show | |
| `special` | Special | |
| `length` | Length description | |
| `information` | Free-form information | |

**Aliases are read-tolerant, write-canonical.** On reading, the two
scheme aliases `type` and `info` are accepted and resolved to
`filler_type` and `information` respectively, so a hand-edited record
still loads. Writers must always emit canonical keys only. Any other
`key` value — e.g. `colour` — is a refusal (`unknown-tag-key`), never a
tag to carry through silently.

**The vocabulary is advisory at the application level but closed at the
format level.** commcut's tag *values* are never validated against the
vocabulary file — any string is a legal value. The *keys* are a closed
set: a new tag key is a schema change, not a value a record may invent.

### 6.6 Value constraints (what XML can hold)

A tag value must be representable in XML 1.0 and must round-trip
unchanged:

- Every character must satisfy XML 1.0's `Char` production: tab (`U+0009`),
  line feed (`U+000A`), `U+0020`–`U+D7FF`, `U+E000`–`U+FFFD`, or
  `U+10000`–`U+10FFFF`. Other C0 control characters (including `U+0000`,
  `U+000B`, `U+000C`, `U+001F`), the surrogate range, and the
  non-characters `U+FFFE`/`U+FFFF` cannot appear. commcut refuses such a
  value at write time, naming the offending tag key.
- **Carriage return (`U+000D`) is forbidden even though it is legal XML.**
  XML line-end normalization turns every CR into a line feed on the way
  back in, so `Info\rMore` would silently read as `Info\nMore`. A value
  containing a CR is a writer error, not something to normalize away.
- Leading and trailing whitespace in a value is preserved; writers must
  not trim values.
- `&`, `<`, `>` in values are escaped by the XML serializer
  (`&amp;`, `&lt;`, `&gt;`) and need no special handling from readers.

## 7. Error categories

commcut's reader raises a single error type and classifies it into a
stable reason code, so a caller can group problems instead of matching
on prose. A third-party reader should produce the same distinctions.
Naming follows `shared/records.py`; the string values are the codes.

| Code | Meaning |
|---|---|
| `not-well-formed` | The bytes are not well-formed XML. |
| `not-a-record` | Well-formed XML whose root element is not `commcut-clip`. |
| `unsupported-version` | `version` is not an integer, is `< 1`, or is greater than the reader supports. |
| `unknown-tag-key` | A `<tag>` whose `key` is not a canonical key or alias (section 6.5). |
| `unknown-element` | An element other than `source`, `segment`, `clip_id`, `tag` — or `<clip_id>` in a v1 document. The reader is strict here on purpose: an unknown element must fail loudly rather than lose data silently. |
| `invalid-record` | Everything else structurally wrong: missing `<source>` or `<segment>`, non-numeric `index`/`start`/`duration`, missing/empty/non-canonical `<clip_id>` in v2, a duplicate tag key, an impossible provenance value. |
| `unreadable` | The bytes never became text: the file could not be opened, or was not valid UTF-8. This is not a record problem — commcut distinguishes "commcut refused this" from "I could not read this", because only the first is fixable by editing tags. |
| *(missing file)* | A sibling `.cnfo` that does not exist is **not** an error at the record level. It means the video is not a commcut clip (section 2). |

For a library scan, commcut collects these per-record rather than
aborting the scan: one unreadable record costs that record, not the
library.

## 8. Semantics commcut relies on (and a reader must not break)

1. **The record is the source of truth for tags.** Never derive a tag
   from a filename or folder name, even when the filename looks like
   commcut rendered it. If a filename and its record disagree, the
   record wins.
2. **No reverse parser.** There is no way back from a rendered path to
   tags — sanitation, `{a,b}` fallbacks, and `[{a}|{b}]` OR groups are
   all lossy. A tool that needs tags reads the record; a tool that has
   no record has no tags.
3. **`video present ⟹ record present`.** commcut publishes the record
   between a successful encode and the commit of the video, so a clip
   whose encode failed leaves neither file. A record with no sibling
   video is the only recoverable intermediate state and is ignored.
4. **Required tags are a policy, not a format rule.** The tags
   `title`, `network`, `filler_type`, `time_period` are required to
   *export* a clip (ignored segments are exempt), but a record missing
   one is still a readable record — it may have been written by a
   less strict writer, or hand-edited. A reader must not refuse a record
   for missing an export-required tag.
5. **A v1 record is valid data** with an empty identity (section 5),
   not a corrupt one.

## 9. Writer rules (producing records commcut will accept)

A third-party writer must:

1. Write the **current schema version** (`version="2"`) with a canonical
   `<clip_id>` (section 6.3). Never write an empty ID: an empty ID is the
   state of a v1 record read from disk, and writing it back out would
   publish a v2 document that cannot be read again.
2. Emit the XML declaration, UTF-8 encoding, no BOM, and the fixed
   element order and tag sort from section 4.
3. Omit empty tag values; use canonical keys only; never emit a duplicate
   key.
4. Refuse a value that violates section 6.6 (including any carriage
   return) **before** writing anything, naming the tag key.
5. Place the record beside the video with the shared stem, and publish
   it **atomically**: write to a temporary file in the destination
   directory (deliberately *not* named `*.cnfo`, so a half-written record
   is never collectable by a `*.cnfo` glob), flush and `fsync`, then
   rename over the final name. Replacement is correct — publishing
   replaces rather than refuses — provided the video slot has already
   been confirmed free; commcut's preflight refuses any destination
   whose video exists before the record is ever written.
6. Render the record **before** the expensive work (encoding the video)
   and publish it **after** that work succeeds, so a failure leaves
   neither file (section 8, point 3).
7. Never write a stored path into the record (section 4.1).

## 10. Reader algorithm (minimal conformant reader)

1. Read the file as bytes; decode as UTF-8, tolerating a leading BOM.
   Failure to open or decode → `unreadable`.
2. Parse as XML. Parse failure → `not-well-formed`.
3. Check the root tag is `commcut-clip`. Otherwise → `not-a-record`.
4. Resolve `version`: absent → `1`; non-integer or `< 1` →
   `unsupported-version`; greater than the newest version you
   implement → `unsupported-version`.
5. Walk the root's children. `source`, `segment`, `clip_id`,
   `tag` are recognized; anything else → `unknown-element`. `<clip_id>`
   with version `< 2` → `unknown-element`.
6. Resolve each `<tag key>` through the alias table
   (`type` → `filler_type`, `info` → `information`). Unresolvable key →
   `unknown-tag-key`. Sort the results and check for a duplicate
   canonical key → `invalid-record` on a duplicate.
7. Validate the rest: `<source>` present and non-empty; `<segment>`
   present with numeric `index` (`>= 0`), `start`, `duration`; for
   version `>= 2`, exactly one canonical `<clip_id>`. Failures →
   `invalid-record`.
8. Return the record: source basename, segment provenance, clip ID (`""`
   for v1), and the tag map.

## 11. Worked examples

### 11.1 Canonical v2 record

```xml
<?xml version="1.0" encoding="utf-8"?>
<commcut-clip version="2">
  <source>Cartoon Network - April Fools 2000.mp4</source>
  <segment index="7" start="314.2" duration="29.9" />
  <clip_id>12345678-1234-4123-8123-123456789abc</clip_id>
  <tag key="block">Toonami</tag>
  <tag key="filler_type">Promo</tag>
  <tag key="network">Cartoon Network</tag>
  <tag key="title">Worlds Finest</tag>
</commcut-clip>
```

(A missing tag and an empty tag are the same: note there is no `length`
tag rather than `<tag key="length"></tag>`.)

### 11.2 Legacy v1 record

```xml
<?xml version="1.0" encoding="utf-8"?>
<commcut-clip version="1">
  <source>a.mp4</source>
  <segment index="0" start="0" duration="1.0" />
  <tag key="title">A</tag>
</commcut-clip>
```

Reads as: source `a.mp4`, segment 0, no clip ID (`""`), tags
`{title: "A"}`. The `version="1"` attribute may also be absent entirely.

### 11.3 Hand-edited alias (reads, but is not how to write)

```xml
<commcut-clip version="1">
  <source>a.mp4</source>
  <segment index="0" start="0" duration="1" />
  <tag key="type">Promo</tag>
</commcut-clip>
```

Reads as `filler_type: "Promo"`. A writer must emit `filler_type`.

## 12. Conformance checklist

A reader conforms if it:

- [ ] Takes tags only from the record, never from the filename or path.
- [ ] Treats a clip as a record with a sibling video, and ignores
      orphans and record-less videos (without inventing tags).
- [ ] Accepts v1 (with or without the `version` attribute) as an
      unidentified record, and refuses a `version` above its newest
      supported schema.
- [ ] Refuses unknown elements and unknown tag keys rather than
      skipping them.
- [ ] Resolves the `type`/`info` aliases on read.
- [ ] Distinguishes `unreadable` (could not open/decode) from a refused
      record, and per-record errors from scan-aborting errors.
- [ ] Verifies the canonical `<clip_id>` shape in v2 without enforcing
      a UUID version.

A writer conforms if it additionally:

- [ ] Writes v2 with a canonical clip ID, canonical keys, sorted tags,
      and no empty values.
- [ ] Refuses values XML cannot hold, including any carriage return,
      before writing.
- [ ] Publishes atomically, record-after-encode, beside the video —
      and stores no path in the record.

## Coverage

`tests/test_records.py` (the record format, its reader, and how it is
published, including the reason codes above), `tests/test_catalog.py`
(the library walk: what a clip is, what is ignored, what is reported,
and the record-over-filename guard), `tests/test_importing.py` (reading
a foreign record), and `tests/test_exporting.py` (the record's
destination and publication order).
