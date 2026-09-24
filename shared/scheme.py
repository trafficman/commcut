"""Shared tag-template parsing and rendering for commcut.

A template contains tag placeholders and optional grouping constructs:

  {tag}              — render the tag's value, or nothing if absent.
  {a, b}             — render the first non-empty value.
  [content]          — render only when every direct tag is non-empty.
  [{a}|{b}]          — join all non-empty direct tags with spaces.

Special characters may be used as literals by prefixing them with a backslash.
User-facing tag aliases are resolved against canonical keys stored in .cmct
segment data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TAG_ALIASES: dict[str, str] = {
    "title": "title",
    "network": "network",
    "block": "block",
    "type": "filler_type",
    "year": "year",
    "time_period": "time_period",
    "show": "show",
    "special": "special",
    "length": "length",
    "info": "information",
}

VALID_TAG_NAMES: frozenset[str] = frozenset(_TAG_ALIASES)
CANONICAL_TAG_KEYS: frozenset[str] = frozenset(_TAG_ALIASES.values())


def normalize_tag_name(user_name: str) -> str | None:
    """Map a user-facing tag name to its canonical .cmct key.

    >>> normalize_tag_name("type")
    'filler_type'
    >>> normalize_tag_name("info")
    'information'
    >>> normalize_tag_name("color") is None
    True
    """
    return _TAG_ALIASES.get(user_name)


def resolve_tag_value(user_name: str, tags: dict[str, str]) -> str | None:
    """Return a known tag's non-empty value, accepting aliases or canonical keys.

    >>> resolve_tag_value("type", {"filler_type": "Promo"})
    'Promo'
    >>> resolve_tag_value("filler_type", {"filler_type": "Promo"})
    'Promo'
    >>> resolve_tag_value("info", {"information": "Remastered"})
    'Remastered'
    >>> resolve_tag_value("type", {"filler_type": ""}) is None
    True
    """
    canonical = normalize_tag_name(user_name)
    if canonical is None:
        canonical = user_name if user_name in CANONICAL_TAG_KEYS else None
    if canonical is None:
        return None
    value = tags.get(canonical, "")
    return value if value else None


@dataclass
class _Literal:
    text: str


@dataclass
class _Tag:
    names: list[str]


@dataclass
class _Pipe:
    pass


@dataclass
class _Group:
    elements: list[_Node]
    is_or: bool


_Node = _Literal | _Tag | _Pipe | _Group


def _has_top_level_pipe(content: str) -> bool:
    """Return whether content contains an unescaped top-level pipe."""
    depth_brace = 0
    depth_bracket = 0
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
    """Parse a template fragment into AST nodes."""
    nodes: list[_Node] = []
    i = 0
    while i < len(text):
        c = text[i]

        if c == "\\" and i + 1 < len(text):
            nodes.append(_Literal(text[i + 1]))
            i += 2
            continue

        if c == "|" and is_or_context:
            nodes.append(_Pipe())
            i += 1
            continue

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
            i += 1
            names = [n for n in (raw.strip() for raw in inner.split(",")) if n]
            nodes.append(_Tag(names))
            continue

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
            i += 1
            is_or = _has_top_level_pipe(content)
            elements = _parse(content, is_or_context=is_or)
            nodes.append(_Group(elements, is_or))
            continue

        nodes.append(_Literal(c))
        i += 1

    return nodes


def parse_scheme(text: str) -> list[_Node]:
    """Parse a template into reusable AST nodes."""
    return _parse(text)


def _render_tag(tag: _Tag, tags: dict[str, str]) -> str:
    for name in tag.names:
        value = resolve_tag_value(name, tags)
        if value:
            return value
    return ""


def _collect_tags(elements: list[_Node]) -> list[_Tag]:
    found: list[_Tag] = []
    for elem in elements:
        if isinstance(elem, _Tag):
            found.append(elem)
    return found


def _cleanup_or_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.strip()


def _render_elements(elements: list[_Node], tags: dict[str, str]) -> str:
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
    all_tags = _collect_tags(group.elements)

    if group.is_or:
        if not any(_render_tag(t, tags) for t in all_tags):
            return ""
    else:
        for tag in all_tags:
            if not _render_tag(tag, tags):
                return ""

    rendered = _render_elements(group.elements, tags)
    if group.is_or:
        rendered = _cleanup_or_text(rendered)
    return rendered


def render_nodes(nodes: list[_Node], tags: dict[str, str]) -> str:
    """Render parsed template nodes against tag values."""
    return _render_elements(nodes, tags)
