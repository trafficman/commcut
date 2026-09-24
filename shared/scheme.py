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
from typing import Sequence

_MAX_STRICT_GROUP_DEPTH = 64

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


def canonical_tag_name(user_name: str) -> str | None:
    """Return a known tag's canonical name from an alias or canonical key."""
    canonical = normalize_tag_name(user_name)
    if canonical is not None:
        return canonical
    return user_name if user_name in CANONICAL_TAG_KEYS else None


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
    canonical = canonical_tag_name(user_name)
    if canonical is None:
        return None
    value = tags.get(canonical, "")
    return value if value else None


class SchemeSyntaxError(ValueError):
    """A template syntax error at a zero-based source position."""

    def __init__(self, message: str, position: int) -> None:
        self.reason = message
        self.position = position
        super().__init__(f"{message} at position {position}")


@dataclass
class LiteralNode:
    text: str
    escaped: bool = False


@dataclass
class TagNode:
    names: list[str]


@dataclass
class PipeNode:
    pass


@dataclass
class GroupNode:
    elements: list[Node]
    is_or: bool


Node = LiteralNode | TagNode | PipeNode | GroupNode

_Literal = LiteralNode
_Tag = TagNode
_Pipe = PipeNode
_Group = GroupNode
_Node = Node


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


def _parse(
    text: str,
    is_or_context: bool = False,
    *,
    strict: bool = False,
    offset: int = 0,
    group_depth: int = 0,
) -> list[Node]:
    """Parse a template fragment into AST nodes."""
    nodes: list[Node] = []
    i = 0
    while i < len(text):
        c = text[i]

        if c == "\\":
            if i + 1 < len(text):
                nodes.append(LiteralNode(text[i + 1], escaped=strict))
                i += 2
                continue
            if strict:
                raise SchemeSyntaxError("Dangling escape", offset + i)
            nodes.append(LiteralNode(c))
            i += 1
            continue

        if c == "|" and is_or_context:
            nodes.append(PipeNode())
            i += 1
            continue

        if c == "{":
            opening_position = offset + i
            i += 1
            start = i
            while i < len(text):
                if text[i] == "\\" and i + 1 < len(text):
                    i += 2
                    continue
                if strict and text[i] == "{":
                    raise SchemeSyntaxError("Nested tag", offset + i)
                if text[i] == "}":
                    break
                i += 1
            inner = text[start:i]
            if i >= len(text) and strict:
                raise SchemeSyntaxError("Unclosed tag", opening_position)
            i += 1
            raw_names = [raw.strip() for raw in inner.split(",")]
            if strict and any(not name for name in raw_names):
                raise SchemeSyntaxError("Empty tag name", opening_position)
            names = [name for name in raw_names if name]
            nodes.append(TagNode(names))
            continue

        if c == "[":
            opening_position = offset + i
            if strict and group_depth >= _MAX_STRICT_GROUP_DEPTH:
                raise SchemeSyntaxError(
                    "Group nesting exceeds the strict parser limit",
                    opening_position,
                )
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
            if i >= len(text) and strict:
                raise SchemeSyntaxError("Unclosed group", opening_position)
            i += 1
            is_or = _has_top_level_pipe(content)
            elements = _parse(
                content,
                is_or_context=is_or,
                strict=strict,
                offset=offset + start,
                group_depth=group_depth + 1,
            )
            nodes.append(GroupNode(elements, is_or))
            continue

        if strict and c in "}]":
            label = "Unexpected closing brace" if c == "}" else "Unexpected closing bracket"
            raise SchemeSyntaxError(label, offset + i)

        nodes.append(LiteralNode(c))
        i += 1

    return nodes


def parse_scheme(text: str) -> list[Node]:
    """Parse a template leniently into reusable AST nodes."""
    return _parse(text)


def parse_scheme_strict(text: str) -> list[Node]:
    """Parse a template, rejecting malformed syntax and empty tag names."""
    return _parse(text, strict=True)


def _render_tag(tag: TagNode, tags: dict[str, str]) -> str:
    for name in tag.names:
        value = resolve_tag_value(name, tags)
        if value:
            return value
    return ""


def _collect_tags(elements: list[Node]) -> list[TagNode]:
    found: list[TagNode] = []
    for elem in elements:
        if isinstance(elem, TagNode):
            found.append(elem)
    return found


def _cleanup_or_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.strip()


def _render_elements(elements: Sequence[Node], tags: dict[str, str]) -> str:
    parts: list[str] = []
    for elem in elements:
        if isinstance(elem, LiteralNode):
            parts.append(elem.text)
        elif isinstance(elem, TagNode):
            parts.append(_render_tag(elem, tags))
        elif isinstance(elem, PipeNode):
            parts.append(" ")
        elif isinstance(elem, GroupNode):
            parts.append(_render_group(elem, tags))
    return "".join(parts)


def _render_group(group: GroupNode, tags: dict[str, str]) -> str:
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


def render_nodes(nodes: Sequence[Node], tags: dict[str, str]) -> str:
    """Render parsed template nodes against tag values."""
    return _render_elements(nodes, tags)
