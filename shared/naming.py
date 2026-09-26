"""Filename template policy for commcut.

The shared tag syntax, parser, and conditional renderer live in
``shared.scheme``.  This module keeps filename-specific requirements and the
public filename rendering API while re-exporting the shared tag helpers.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.paths import sanitize_path_component
from shared.scheme import (
    CANONICAL_TAG_KEYS,
    GroupNode,
    VALID_TAG_NAMES,
    canonical_tag_name,
    normalize_tag_name,
    parse_scheme,
    parse_scheme_strict,
    render_nodes,
    resolve_tag_value,
    SchemeSyntaxError,
    TagNode,
)

REQUIRED_TAG_NAMES: frozenset[str] = frozenset({"title"})
DEFAULT_FILE_NAMING_SCHEME = (
    "{network} - {filler_type} - {year,time_period} - "
    "[{block,special} ]{title} [({length}|{information})]"
)
MAX_FILENAME_BYTES = 255


class FilenameSchemeError(ValueError):
    """A filename template or rendered filename violates export policy."""


@dataclass(frozen=True)
class FilenameScheme:
    text: str
    _nodes: tuple


def _iter_tag_nodes(nodes):
    for node in nodes:
        if isinstance(node, TagNode):
            yield node
        if isinstance(node, GroupNode):
            yield from _iter_tag_nodes(node.elements)


def compile_filename_scheme(text: str) -> FilenameScheme:
    """Strictly validate a filename template and require a real Title tag."""
    if not isinstance(text, str):
        raise FilenameSchemeError("File naming scheme must be a string")
    if not text or not text.strip():
        raise FilenameSchemeError("File naming scheme must not be empty")
    if text != text.strip():
        raise FilenameSchemeError("File naming scheme must not have surrounding whitespace")
    if any(separator in text for separator in ("\n", "\r", "\u0085", "\u2028", "\u2029")):
        raise FilenameSchemeError("File naming scheme must be a single line")

    try:
        nodes = parse_scheme_strict(text)
    except SchemeSyntaxError as error:
        raise FilenameSchemeError(f"Invalid file naming scheme syntax: {error}") from error

    for tag in _iter_tag_nodes(nodes):
        for name in tag.names:
            canonical = canonical_tag_name(name)
            if canonical is None:
                raise FilenameSchemeError(f"Unknown filename tag {name!r}")

    contains_required_title = any(
        isinstance(node, TagNode)
        and len(node.names) == 1
        and canonical_tag_name(node.names[0]) == "title"
        for node in nodes
    )
    if not contains_required_title:
        raise FilenameSchemeError(
            "The file naming scheme must include an unconditional top-level "
            "{title} to ensure uniqueness"
        )
    return FilenameScheme(text=text, _nodes=tuple(nodes))


def render_compiled_filename(scheme: FilenameScheme, tags: dict[str, str]) -> str:
    """Render a compiled filename scheme to an unsanitized stem."""
    if not isinstance(scheme, FilenameScheme):
        raise TypeError("scheme must be a compiled FilenameScheme")
    result = render_nodes(scheme._nodes, tags).strip()
    if not result:
        raise FilenameSchemeError("File naming scheme rendered an empty filename")
    return result


def sanitize_filename_stem(stem: str, extension: str = ".mp4") -> str:
    """Sanitize a rendered stem and append one portable output extension."""
    if not isinstance(stem, str):
        raise FilenameSchemeError("Rendered filename must be a string")
    if not isinstance(extension, str) or not extension.startswith("."):
        raise FilenameSchemeError("Output extension must begin with a dot")
    if any(character in extension for character in '/\\:*?"<>|'):
        raise FilenameSchemeError("Output extension contains invalid path characters")

    sanitized = sanitize_path_component(stem)
    filename = f"{sanitized}{extension}"
    if len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise FilenameSchemeError(
            f"Filename exceeds {MAX_FILENAME_BYTES} UTF-8 bytes"
        )
    return filename


def render_filename(scheme: str, tags: dict[str, str]) -> str:
    r"""Render a naming scheme into a filename stem without an extension.

    *tags* is a dict keyed by canonical .cmct keys (e.g. ``filler_type``,
    ``information``).  The scheme is parsed once and rendered against
    those values, producing a single filename string.

    Usage::

        >>> tags = {
        ...     "title": "Worlds Finale",
        ...     "filler_type": "Promo",
        ...     "network": "Cartoon Network",
        ...     "year": "2000",
        ...     "length": "30 Seconds",
        ...     "information": "Remastered",
        ... }

    Single tag::

        >>> render_filename("{title}", tags)
        'Worlds Finale'

    AND group with separator inside brackets (omitted when tag is absent):

        >>> render_filename("[{block} - ]{title}", tags)
        'Worlds Finale'

    Fallback tag inside AND group:

        >>> render_filename("{network} - {filler_type} - {year,time_period} - [{block,special} ]{title}", tags)
        'Cartoon Network - Promo - 2000 - Worlds Finale'

    OR group — both present (pipe replaced by space, literal parens kept):

        >>> render_filename("{title} [({length}|{info})]", tags)
        'Worlds Finale (30 Seconds Remastered)'

    OR group — one present (empty tag dropped, whitespace cleaned):

        >>> render_filename("{title} [({length}|{info})]", {**tags, "length": ""})
        'Worlds Finale (Remastered)'

    OR group — all absent (entire group omitted, no stray parens):

        >>> render_filename("{title} [({length}|{info})]", {**tags, "length": "", "information": ""})
        'Worlds Finale'

    Escaped braces render literally (no tag lookup):

        >>> render_filename(r"\{title\}", tags)
        '{title}'

    Backslash-pipe is a literal pipe, not an OR trigger:

        >>> render_filename(r"a \| b", tags)
        'a | b'
    """
    nodes = parse_scheme(scheme)
    result = render_nodes(nodes, tags)
    return result.strip()
