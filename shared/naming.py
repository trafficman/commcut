"""File naming scheme parsing and rendering for commcut.

A naming scheme is a user-editable template string containing tag
placeholders and optional grouping constructs:

  {tag}              — render the tag's value, or nothing if absent.
  {a, b}             — fallback: render *a* if present, else *b*.
  [content]          — AND group: render only if every tag in *content*
                       is non-empty; otherwise the whole group (including
                       surrounding literals like separators) is omitted.
  [{a}|{b}]          — OR group: triggered by a pipe between two tag
                       placeholders inside brackets.  If any tag is
                       non-empty, join the non-empty values with single
                       spaces and render them within the surrounding
                       literal text; omit the entire group when all tags
                       are empty.  The pipe itself is consumed (not
                       rendered); any literal parentheses the user types
                       (e.g. ``[({a}|{b})]``) appear as normal output.

Special characters (``{ } [ ] , |``) may be used as literals by prefixing
them with a backslash — e.g. ``\\{title\\}`` renders as the literal text
``{title}`` instead of a tag lookup.

Tags are exposed to the user under short names (e.g. ``{type}``), but the
.cmct segment data stores canonical keys (e.g. ``filler_type``).  The alias
map below normalizes user-facing names to canonical keys at parse time, so
scheme text uses short forms while persistence stays consistent with
``shared.segments`` and ``editor``'s ``_TAG_FIELDS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Alias map: user-facing tag name to canonical key stored in .cmct segment tags.
#
# Most tags share the same name in both contexts; only the abbreviated aliases
# in the user-facing syntax need remapping.  These canonical keys match the
# keys used in editor.py _TAG_FIELDS and the .cmct "tags" dict structure.
# ---------------------------------------------------------------------------
_TAG_ALIASES: dict[str, str] = {
    "title":       "title",
    "network":     "network",
    "block":       "block",
    "type":        "filler_type",
    "year":        "year",
    "time_period": "time_period",
    "show":        "show",
    "special":     "special",
    "length":      "length",
    "info":        "information",
}

# Every recognized user-facing tag name (for validation and help text).
VALID_TAG_NAMES: frozenset[str] = frozenset(_TAG_ALIASES)

# Canonical keys only (the values stored in .cmct).  Reverse of the alias map.
CANONICAL_TAG_KEYS: frozenset[str] = frozenset(_TAG_ALIASES.values())

# Tags that must appear at least once in any valid naming scheme.
# Title is the only hard requirement — it guarantees per-segment uniqueness
# on export.  All other tags may be absent (the grouping syntax handles
# omission gracefully).
REQUIRED_TAG_NAMES: frozenset[str] = frozenset({"title"})


# ---------------------------------------------------------------------------
# Public alias helpers
# ---------------------------------------------------------------------------

def normalize_tag_name(user_name: str) -> str | None:
    """Map a user-facing tag name to its canonical .cmct key.

    >>> normalize_tag_name("type")
    'filler_type'
    >>> normalize_tag_name("info")
    'information'
    >>> normalize_tag_name("title")
    'title'
    >>> normalize_tag_name("color") is None
    True
    """
    return _TAG_ALIASES.get(user_name)


def resolve_tag_value(user_name: str, tags: dict[str, str]) -> str | None:
    """Look up a tag's value by its user-facing name from a tags dict.

    Accepts both user-facing aliases (e.g. ``"type"``) and canonical
    keys (e.g. ``"filler_type"``) so the parser works regardless of
    whether the scheme uses the short or canonical form.

    Returns the value string if the tag is known and non-empty, or
    ``None`` if the tag is unknown or has no value in *tags*.

    >>> resolve_tag_value("type", {"filler_type": "Promo"})
    'Promo'
    >>> resolve_tag_value("filler_type", {"filler_type": "Promo"})
    'Promo'
    >>> resolve_tag_value("info", {"information": "Remastered"})
    'Remastered'
    >>> resolve_tag_value("type", {"filler_type": ""}) is None
    True
    >>> resolve_tag_value("type", {}) is None
    True
    >>> resolve_tag_value("color", {"color": "red"}) is None
    True
    """
    canonical = normalize_tag_name(user_name)
    if canonical is None:
        canonical = user_name if user_name in CANONICAL_TAG_KEYS else None
    if canonical is None:
        return None
    value = tags.get(canonical, "")
    return value if value else None


# ---------------------------------------------------------------------------
# AST node types (private — internal to the parser/renderer)
# ---------------------------------------------------------------------------

@dataclass
class _Literal:
    text: str


@dataclass
class _Tag:
    names: list[str]           # user-facing names, left-to-right fallback


@dataclass
class _Pipe:
    pass                      # OR separator token


@dataclass
class _Group:
    elements: list[_Node]     # child nodes
    is_or: bool               # True = OR semantics, False = AND


_Node = _Literal | _Tag | _Pipe | _Group


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _has_top_level_pipe(content: str) -> bool:
    """Return True if an unescaped ``|`` appears outside ``{}`` and ``[]``.

    Escaped pipes (``\\|``) are skipped, as are pipes nested inside tag
    braces or bracketed sub-groups.
    """
    depth_brace: int = 0
    depth_bracket: int = 0
    i = 0
    while i < len(content):
        c = content[i]
        if c == "\\" and i + 1 < len(content):
            i += 2
            continue
        if c == "{":
            depth_brace += 1
        elif c == "}":
            depth_brace -= 1
        elif c == "[":
            depth_bracket += 1
        elif c == "]":
            depth_bracket -= 1
        elif c == "|" and depth_brace == 0 and depth_bracket == 0:
            return True
        i += 1
    return False


def _parse(text: str, is_or_context: bool = False) -> list[_Node]:
    """Parse a scheme fragment into a list of AST nodes.

    When *is_or_context* is True, unescaped ``|`` characters at the top
    level of *text* become ``_Pipe`` tokens; otherwise they are plain
    literals.
    """
    nodes: list[_Node] = []
    i = 0
    while i < len(text):
        c = text[i]

        # Escaped character → literal
        if c == "\\" and i + 1 < len(text):
            nodes.append(_Literal(text[i + 1]))
            i += 2
            continue

        # Pipe in an OR group → delimiter token
        if c == "|" and is_or_context:
            nodes.append(_Pipe())
            i += 1
            continue

        # Tag: {name} or {a, b}
        if c == "{":
            i += 1
            start = i
            while i < len(text):
                if text[i] == "\\" and i + 1 < len(text):
                    i += 2
                    continue
                if text[i] == "}":
                    break
                i += 1
            inner = text[start:i]
            i += 1  # consume "}"
            names = [n for n in (raw.strip() for raw in inner.split(",")) if n]
            nodes.append(_Tag(names))
            continue

        # Group: [content]
        if c == "[":
            i += 1
            start = i
            depth = 1
            while i < len(text):
                if text[i] == "\\" and i + 1 < len(text):
                    i += 2
                    continue
                if text[i] == "[":
                    depth += 1
                elif text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            content = text[start:i]
            i += 1  # consume "]"
            is_or = _has_top_level_pipe(content)
            elements = _parse(content, is_or_context=is_or)
            nodes.append(_Group(elements, is_or))
            continue

        # Plain literal character
        nodes.append(_Literal(c))
        i += 1

    return nodes


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _render_tag(tag: _Tag, tags: dict[str, str]) -> str:
    """Resolve a tag with fallback, returning the first non-empty value."""
    for name in tag.names:
        value = resolve_tag_value(name, tags)
        if value:
            return value
    return ""


def _collect_tags(elements: list[_Node]) -> list[_Tag]:
    """Extract _Tag nodes that are *direct* children of the element list.

    Tags inside nested sub-groups are not collected — each sub-group
    evaluates its own presence condition independently.  This lets a
    parent AND group render even when a child OR group produces no
    output (e.g. ``[{block} - [{length}|{info}]]`` renders ``Toonami -``
    when block is set but both length and info are empty).
    """
    found: list[_Tag] = []
    for elem in elements:
        if isinstance(elem, _Tag):
            found.append(elem)
    return found


def _cleanup_or_text(text: str) -> str:
    """Collapse whitespace left by empty tags in an OR group.

    Multiple spaces collapse to one.  Spaces *inside* literal parentheses
    are trimmed (e.g. ``( Sec)`` becomes ``(Sec)``), but spaces *outside*
    them are preserved so separators like `` - (``) survive.
    """
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\(\s+", "(", text)   # trim space after "("
    text = re.sub(r"\s+\)", ")", text)   # trim space before ")"
    return text.strip()


def _render_elements(elements: list[_Node], tags: dict[str, str]) -> str:
    """Render a flat list of nodes into a string."""
    parts: list[str] = []
    for elem in elements:
        if isinstance(elem, _Literal):
            parts.append(elem.text)
        elif isinstance(elem, _Tag):
            parts.append(_render_tag(elem, tags))
        elif isinstance(elem, _Pipe):
            parts.append(" ")
        elif isinstance(elem, _Group):
            parts.append(_render_group(elem, tags))
    return "".join(parts)


def _render_group(group: _Group, tags: dict[str, str]) -> str:
    """Render a group with AND or OR semantics."""
    all_tags = _collect_tags(group.elements)

    if group.is_or:
        # OR: render if any tag resolves to a non-empty value
        if not any(_render_tag(t, tags) for t in all_tags):
            return ""
    else:
        # AND: render only if every tag resolves to a non-empty value
        for t in all_tags:
            if not _render_tag(t, tags):
                return ""

    rendered = _render_elements(group.elements, tags)
    if group.is_or:
        rendered = _cleanup_or_text(rendered)
    return rendered


def _render_nodes(nodes: list[_Node], tags: dict[str, str]) -> str:
    """Render a list of top-level AST nodes."""
    return _render_elements(nodes, tags)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_filename(scheme: str, tags: dict[str, str]) -> str:
    """Render a naming scheme into a filename stem (without extension).

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

        >>> render_filename("a \| b", tags)
        'a | b'
    """
    nodes = _parse(scheme)
    result = _render_nodes(nodes, tags)
    return result.strip()
