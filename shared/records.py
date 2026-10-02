"""The clip record: the one place an exported clip's tags live.

Applies to: `shared/records.py`, `shared/naming.py`, `shared/exporting.py`,
`shared/ffmpeg.py`.

Every clip commcut exports gets a ``<stem>.cnfo`` beside its ``<stem>.mp4``. The
record holds the clip's tags and the segment it was cut from; the filename and
folder are a *projection* of those tags under the two schemes. That direction is
one-way on purpose. Sanitation, fallback expressions and OR groups all make
rendering lossy, so nothing can recover a tag from a path. Whatever needs a
clip's tags reads the record.

The extension is deliberately not ``.nfo``. That one belongs to media servers,
which scan for it and expect a schema commcut does not write -- a file in the
wrong dialect is worse than no file, because the tool may act on its absence of
keys. A distinct extension keeps them out of the folder and leaves a real
``.nfo`` free for a derived compatibility file if one is ever wanted.

The format is XML carrying a schema version on the root element. The version is
an integer owned by this module and is independent of ``shared.version.VERSION``:
a record has to outlive the release that wrote it, and a migration has to be able
to name what it is migrating from.
"""

from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from shared.scheme import canonical_tag_name

#: Sidecar extension. Shares the clip's stem and directory.
RECORD_EXTENSION = ".cnfo"

#: The schema this module writes, and the newest one it can read.
RECORD_SCHEMA_VERSION = 1

#: The root element. Renaming it is a schema change.
RECORD_ROOT_TAG = "commcut-clip"

#: A staged record is written in its final directory so publishing it is a
#: same-volume rename.
_TEMP_PREFIX = ".commcut-record-"

#: Deliberately not RECORD_EXTENSION: an interrupted write must not be
#: collectable by anything globbing `*.cnfo`.
_TEMP_SUFFIX = ".tmp"


class RecordError(ValueError):
    """A clip record is missing, malformed, or cannot be represented."""


#: Why a record could not be read, as a code a caller can group by rather than a
#: sentence it has to match on. The Library Importer is the caller that needs it:
#: "this build does not know the tag key `colour`" and "this file is corrupt" are
#: the same symptom to a walk and completely different things to a person, and a
#: screen cannot tell them apart from prose without this.
REASON_NOT_WELL_FORMED = "not-well-formed"
REASON_NOT_A_RECORD = "not-a-record"
REASON_UNSUPPORTED_VERSION = "unsupported-version"
REASON_UNKNOWN_TAG_KEY = "unknown-tag-key"
REASON_UNKNOWN_ELEMENT = "unknown-element"
REASON_INVALID = "invalid-record"


def record_error_reason(error: RecordError) -> str:
    """The code for why `error` was raised.

    Classified from the message, which is a coupling worth being honest about: the
    alternatives are a subclass per cause, or a code passed alongside every raise
    in this module. Both are worse for now — `parse_record_xml` has one public
    failure type on purpose, so a caller never has to know which of several to
    catch. The mapping lives here rather than in a caller so there is one owner,
    and `tests/test_records.py` pins every branch against the exact string it
    reads, so a reworded message fails a test rather than silently changing a code.
    """
    message = str(error)
    if "not well-formed" in message:
        return REASON_NOT_WELL_FORMED
    if "unknown tag key" in message:
        return REASON_UNKNOWN_TAG_KEY
    if "unrecognized element" in message:
        return REASON_UNKNOWN_ELEMENT
    if "record schema version" in message or "is not a schema version" in message \
            or "is not an integer" in message:
        return REASON_UNSUPPORTED_VERSION
    if "Record root is" in message:
        return REASON_NOT_A_RECORD
    return REASON_INVALID


@dataclass(frozen=True)
class ClipRecord:
    """One exported clip's tags and provenance, as the record stores them.

    `tags` is a tuple of ``(key, value)`` pairs rather than a dict so the frozen
    dataclass is immutable in fact as well as in name. Keys are canonical
    (`.cmct` keys, not the scheme's short aliases), which is what
    `shared.scheme.canonical_tag_name` is for.
    """

    source: str
    segment_index: int
    start: float
    duration: float
    tags: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise RecordError("A clip record needs the source video's name")
        if not isinstance(self.segment_index, int) or isinstance(
            self.segment_index, bool
        ) or self.segment_index < 0:
            raise RecordError("A clip record needs a non-negative segment index")
        for field_name in ("start", "duration"):
            value = getattr(self, field_name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise RecordError(f"A clip record needs a numeric {field_name}")
        if not isinstance(self.tags, tuple):
            raise RecordError("A clip record's tags must be a tuple of pairs")

        seen: set[str] = set()
        for pair in self.tags:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise RecordError("A clip record's tags must be (key, value) pairs")
            key, value = pair
            if not isinstance(key, str) or canonical_tag_name(key) != key:
                raise RecordError(f"{key!r} is not a canonical tag key")
            if not isinstance(value, str):
                raise RecordError(f"Tag {key!r} must hold a string")
            if key in seen:
                raise RecordError(f"Tag {key!r} appears twice in one record")
            seen.add(key)

    @property
    def tag_dict(self) -> dict[str, str]:
        """The tags as a plain dict, for callers that want one."""
        return dict(self.tags)


def _require_xml_safe_text(value: str, key: str) -> None:
    """Refuse a value a record could not hand back unchanged.

    Two separate reasons, and the second is the one that is easy to miss:

    `ElementTree` escapes `&`, `<` and `>` but emits control characters raw, so
    writing one produces a document its own parser then refuses. XML 1.0's `Char`
    production allows tab, LF and CR, the printable ranges, and everything above
    U+FFFF; it excludes the other C0 controls, the surrogate range, and the two
    non-characters U+FFFE/U+FFFF.

    A carriage return is *legal* and still cannot be stored, because line-end
    normalization (XML 1.0 section 2.11) turns every CR into LF on the way back
    in. A tag that read `Info\\rMore` would come back as `Info\\nMore`, which is
    the silent drift this format exists to prevent -- so CR is refused on the
    same grounds as the characters XML cannot hold at all.
    """
    for character in value:
        code = ord(character)
        if character == "\r":
            raise RecordError(
                f"Tag {key!r} contains a carriage return, which an XML reader "
                "normalizes to a line feed"
            )
        if (
            code in (0x09, 0x0A)
            or 0x20 <= code <= 0xD7FF
            or 0xE000 <= code <= 0xFFFD
            or 0x10000 <= code <= 0x10FFFF
        ):
            continue
        raise RecordError(
            f"Tag {key!r} contains U+{code:04X}, which XML 1.0 cannot represent"
        )


def render_record_xml(record: ClipRecord) -> str:
    """Render a record as an indented XML document with a declaration.

    Sorted by tag key and with empty values omitted, so the output is
    deterministic, diffs readably, and two records of the same clip render
    identically. A missing tag and an empty one mean the same thing here, so
    dropping the empty case loses nothing.
    """
    if not isinstance(record, ClipRecord):
        raise TypeError("record must be a ClipRecord")

    root = ET.Element(RECORD_ROOT_TAG, {"version": str(RECORD_SCHEMA_VERSION)})
    ET.SubElement(root, "source").text = record.source
    ET.SubElement(
        root,
        "segment",
        {
            "index": str(record.segment_index),
            "start": repr(float(record.start)),
            "duration": repr(float(record.duration)),
        },
    )
    for key, value in sorted(record.tags):
        if not value:
            continue
        _require_xml_safe_text(value, key)
        ET.SubElement(root, "tag", {"key": key}).text = value

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    return f'<?xml version="1.0" encoding="utf-8"?>\n{body}\n'


def _parse_version(root: ET.Element) -> int:
    raw = root.get("version")
    if raw is None:
        # A record written before the attribute existed is the first schema.
        return 1
    try:
        version = int(raw)
    except ValueError:
        raise RecordError(f"Record version {raw!r} is not an integer") from None
    if version < 1:
        raise RecordError(f"Record version {version} is not a schema version")
    if version > RECORD_SCHEMA_VERSION:
        raise RecordError(
            f"This commcut reads record schema version "
            f"{RECORD_SCHEMA_VERSION} and older; the record is version {version}"
        )
    return version


def parse_record_xml(text: str) -> ClipRecord:
    """Parse one record document.

    Strict about anything it does not recognize. The version is the
    compatibility gate, and refusing an unknown element or tag key means a
    hand-edited or newer record fails loudly here instead of silently losing a
    tag on the way through.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise RecordError(f"Record is not well-formed XML: {error}") from error

    if root.tag != RECORD_ROOT_TAG:
        raise RecordError(
            f"Record root is {root.tag!r}, expected {RECORD_ROOT_TAG!r}"
        )
    _parse_version(root)

    source = None
    segment: tuple[int, float, float] | None = None
    tags: list[tuple[str, str]] = []

    for element in root:
        if element.tag == "source":
            source = element.text or ""
        elif element.tag == "segment":
            try:
                segment = (
                    int(element.get("index", "")),
                    float(element.get("start", "")),
                    float(element.get("duration", "")),
                )
            except (TypeError, ValueError):
                raise RecordError(
                    "A record's <segment> needs numeric index, start and duration"
                ) from None
        elif element.tag == "tag":
            key = element.get("key")
            if key is None or canonical_tag_name(key) is None:
                raise RecordError(f"Record holds an unknown tag key {key!r}")
            canonical = canonical_tag_name(key)
            tags.append((canonical, element.text or ""))
        else:
            raise RecordError(
                f"Record holds an unrecognized element {element.tag!r}"
            )

    if source is None:
        raise RecordError("A record needs a <source> element")
    if segment is None:
        raise RecordError("A record needs a <segment> element")
    return ClipRecord(
        source=source,
        segment_index=segment[0],
        start=segment[1],
        duration=segment[2],
        tags=tuple(sorted(tags)),
    )


def load_record(path: str) -> ClipRecord:
    """Read one record from disk.

    `utf-8-sig` for the same reason as the settings readers: a record is small
    user data that a person may hand-edit, and a byte-order mark from a
    PowerShell or Notepad save is not an error worth reporting.
    """
    try:
        with open(path, encoding="utf-8-sig") as record_file:
            return parse_record_xml(record_file.read())
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise RecordError(f"Record could not be read: {error}") from error


def write_record_document(path: str, document: str) -> str:
    """Publish an already-rendered record document at `path`.

    The write is staged in the destination directory and moved into place, so a
    reader never sees a half-written record and a crash cannot leave one at the
    real name. Replacement is deliberate and is *not* the export's no-clobber
    rule: the preflight has already refused any destination whose video exists,
    so the video slot is free and overwriting the record for it cannot destroy
    anything of the user's. The opposite rule would make a re-export fail on the
    orphan a previous failure left behind.

    The caller owns creating the directory, exactly as it owns creating it for
    the video.
    """
    if not isinstance(document, str):
        raise TypeError("document must be a string")
    data = document.encode("utf-8")
    directory = os.path.dirname(path)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX, dir=directory or "."
    )
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        raise
    return path


def write_record(path: str, record: ClipRecord) -> str:
    """Render and publish a record at `path`, replacing whatever is there."""
    return write_record_document(path, render_record_xml(record))