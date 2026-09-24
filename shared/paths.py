"""Folder path scheme compilation and rendering for commcut.

Folder schemes use the shared tag-template language with a restricted path
profile:

  {tag}              render a required top-level directory component
  {a, b}             choose the first non-empty fallback value
  [literal/{tag}/]   include or omit the entire conditional path fragment

A group may contain one tag expression and arbitrary literal path text, but
folder schemes do not support nested groups or pipe-based OR groups.  The public
renderer returns validated, display-cased components rather than a raw path so
future callers can safely join them beneath a chosen export root.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from shared.scheme import (
    GroupNode,
    LiteralNode,
    Node,
    PipeNode,
    SchemeSyntaxError,
    TagNode,
    canonical_tag_name,
    parse_scheme_strict,
    render_nodes,
)

# ---------------------------------------------------------------------------
# Public defaults and portable filesystem limits
# ---------------------------------------------------------------------------

DEFAULT_FOLDER_SCHEME = "{network}/[Blocks/{block}/]{type}/{time_period}/[{special,show}/]"
REQUIRED_FOLDER_TAG_NAMES: frozenset[str] = frozenset({
    "network",
    "filler_type",
    "time_period",
})
MAX_FOLDER_COMPONENT_BYTES = 255
MAX_FOLDER_RELATIVE_PATH_BYTES = 4096

_INVALID_COMPONENT_CHARACTERS = re.compile(
    r"[\x00-\x1f\x7f-\x9f<>:\"/\\|?*]+"
)
_WHITESPACE = re.compile(r"\s+")
_PORTABLE_ESCAPED_LITERALS = frozenset("{}[],")
_WINDOWS_DEVICE_NAMES = frozenset({
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
})


# ---------------------------------------------------------------------------
# Public exceptions and compiled-scheme integrity
# ---------------------------------------------------------------------------

class FolderSchemeError(ValueError):
    """A folder scheme is structurally or semantically invalid."""


class FolderRenderError(ValueError):
    """A folder scheme cannot be rendered for the supplied tags."""


# Shared AST nodes intentionally remain mutable for filename compatibility.
# The fingerprint lets rendering detect mutation and restore the authoritative
# AST by recompiling the immutable source text.
_FOLDER_SCHEME_TOKEN = object()


def _node_fingerprint(nodes: Sequence[Node]) -> tuple[object, ...]:
    """Return a structural snapshot used to detect post-compile mutation."""
    result: list[object] = []
    for node in nodes:
        if isinstance(node, LiteralNode):
            text = node.text if isinstance(node.text, str) else None
            result.append(("literal", text, node.escaped is True))
        elif isinstance(node, TagNode):
            if isinstance(node.names, (list, tuple)) and all(
                isinstance(name, str) for name in node.names
            ):
                names: object = tuple(node.names)
            else:
                names = ("invalid", type(node.names).__name__)
            result.append(("tag", names))
        elif isinstance(node, PipeNode):
            result.append(("pipe",))
        elif isinstance(node, GroupNode):
            if isinstance(node.elements, (list, tuple)):
                elements: object = _node_fingerprint(node.elements)
            else:
                elements = ("invalid", type(node.elements).__name__)
            result.append(("group", elements, node.is_or is True))
        else:
            result.append(("invalid", type(node).__name__))
    return tuple(result)


@dataclass(frozen=True, init=False)
class FolderScheme:
    text: str
    nodes: tuple[Node, ...]
    _fingerprint: tuple[object, ...] = field(compare=False, repr=False)
    _token: object = field(compare=False, repr=False)

    @classmethod
    def _create(cls, text: str, nodes: Sequence[Node]) -> FolderScheme:
        scheme = object.__new__(cls)
        object.__setattr__(scheme, "text", text)
        object.__setattr__(scheme, "nodes", tuple(nodes))
        object.__setattr__(scheme, "_fingerprint", _node_fingerprint(scheme.nodes))
        object.__setattr__(scheme, "_token", _FOLDER_SCHEME_TOKEN)
        return scheme


# ---------------------------------------------------------------------------
# AST traversal and component sanitization
# ---------------------------------------------------------------------------


def _walk_nodes(nodes: Sequence[Node]) -> Iterator[Node]:
    """Yield every AST node in depth-first order."""
    for node in nodes:
        yield node
        if isinstance(node, GroupNode):
            yield from _walk_nodes(node.elements)


def _tag_expression(names: Sequence[str]) -> str:
    """Format a canonical tag expression for error messages."""
    return "{" + ",".join(names) + "}"


def _is_windows_device_name(component: str) -> bool:
    """Return whether a component has a Windows-reserved device base name."""
    base_name = component.split(".", 1)[0].rstrip(" .")
    return base_name.casefold() in _WINDOWS_DEVICE_NAMES


def _is_unsafe_character(character: str) -> bool:
    """Return whether a character is invalid or visually unsafe in a path."""
    return (
        _INVALID_COMPONENT_CHARACTERS.fullmatch(character) is not None
        or unicodedata.category(character) == "Cf"
        or character in "\u2028\u2029"
    )


def _replace_unsafe_characters(value: str) -> str:
    """Replace each contiguous run of unsafe characters with one hyphen."""
    result: list[str] = []
    replacing = False
    for character in value:
        if _is_unsafe_character(character):
            if not replacing:
                result.append("-")
            replacing = True
            continue
        result.append(character)
        replacing = False
    return "".join(result)


def _contains_unsafe_characters(value: str) -> bool:
    """Return whether authored scheme text contains an unsafe character."""
    return any(_is_unsafe_character(character) for character in value)


def _utf8_length(
    value: str,
    *,
    error_type: type[ValueError],
    context: str,
) -> int:
    """Measure UTF-8 size while converting encoding failures to typed errors."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise error_type(f"{context} contains invalid Unicode") from error


def normalized_validation_key(component: str) -> str:
    """Return the normalized case-insensitive key for a path component."""
    if not isinstance(component, str):
        raise TypeError("Path component must be a string")
    normalized = unicodedata.normalize("NFC", component)
    return "".join(
        character
        for character in normalized
        if (
            unicodedata.category(character) not in {"Cc", "Cf"}
            and character not in "\u2028\u2029"
        )
    ).casefold()


def sanitize_path_component(value: str) -> str:
    """Sanitize a tag value for use as one portable folder component."""
    if not isinstance(value, str):
        raise TypeError("Path component must be a string")
    # Tag values are sanitized; scheme literals are validated instead of rewritten.
    sanitized = unicodedata.normalize("NFC", value)
    sanitized = _replace_unsafe_characters(sanitized)
    sanitized = _WHITESPACE.sub(" ", sanitized).strip()
    sanitized = sanitized.rstrip(" .")
    if not sanitized:
        raise ValueError("Path component is empty after sanitization")
    if _is_windows_device_name(sanitized):
        sanitized = f"_{sanitized}"
    if _utf8_length(
        sanitized,
        error_type=ValueError,
        context="Path component",
    ) > MAX_FOLDER_COMPONENT_BYTES:
        raise ValueError(
            f"Path component exceeds {MAX_FOLDER_COMPONENT_BYTES} UTF-8 bytes"
        )
    return sanitized


# ---------------------------------------------------------------------------
# Portable path and folder-grammar validation
# ---------------------------------------------------------------------------


def _validate_literal_component(
    component: str,
    *,
    error_type: type[FolderSchemeError] | type[FolderRenderError],
    context: str,
) -> None:
    """Validate one literal or already-sanitized path component."""
    if not component:
        raise error_type(f"{context} contains an empty path component")
    if component in {".", ".."}:
        raise error_type(f"{context} contains unsupported component {component!r}")
    if len(component) >= 2 and component[1] == ":":
        raise error_type(f"{context} contains drive-qualified component {component!r}")
    if component.startswith("\\"):
        raise error_type(f"{context} contains a UNC component")
    if _contains_unsafe_characters(component):
        raise error_type(f"{context} contains portable-invalid component {component!r}")
    if component != component.strip():
        raise error_type(f"{context} contains a component with surrounding whitespace")
    if component.endswith("."):
        raise error_type(f"{context} contains a component ending in a dot")
    if _is_windows_device_name(component):
        raise error_type(f"{context} contains reserved device name {component!r}")
    if _utf8_length(
        component,
        error_type=error_type,
        context=context,
    ) > MAX_FOLDER_COMPONENT_BYTES:
        raise error_type(
            f"{context} contains a component exceeding "
            f"{MAX_FOLDER_COMPONENT_BYTES} UTF-8 bytes"
        )


def _validate_rendered_path(
    rendered: str,
    *,
    error_type: type[FolderSchemeError] | type[FolderRenderError],
    context: str,
    collapse_omitted_separators: bool = False,
) -> tuple[str, ...]:
    """Validate a rendered relative path and return its display-cased parts."""
    if not rendered:
        raise error_type(f"{context} rendered an empty relative path")
    if rendered.startswith("/") and not collapse_omitted_separators:
        raise error_type(f"{context} rendered an absolute path")
    if "\\" in rendered:
        raise error_type(f"{context} rendered a backslash path separator")

    body = rendered[:-1] if rendered.endswith("/") else rendered
    if not body:
        raise error_type(f"{context} rendered an empty relative path")
    components = body.split("/")
    if collapse_omitted_separators:
        # Authored duplicate separators are rejected at compile time.  Any
        # runtime gaps therefore come from optional groups being omitted.
        components = [component for component in components if component]
        if not components:
            raise error_type(f"{context} rendered an empty relative path")
    for component in components:
        _validate_literal_component(
            component,
            error_type=error_type,
            context=context,
        )

    relative_path = "/".join(components)
    if _utf8_length(
        relative_path,
        error_type=error_type,
        context=context,
    ) > MAX_FOLDER_RELATIVE_PATH_BYTES:
        raise error_type(
            f"{context} exceeds {MAX_FOLDER_RELATIVE_PATH_BYTES} UTF-8 bytes"
        )
    return tuple(components)


def _validate_nodes(nodes: Sequence[Node]) -> None:
    """Apply the restricted folder grammar and required-tag policy to an AST."""
    required_references: set[str] = set()

    def visit(elements: Sequence[Node], *, inside_group: bool) -> None:
        for node in elements:
            if isinstance(node, LiteralNode):
                if node.escaped and node.text == "/":
                    raise FolderSchemeError("Folder scheme cannot escape '/'")
                if node.escaped and node.text not in _PORTABLE_ESCAPED_LITERALS:
                    raise FolderSchemeError(
                        f"Escaped literal {node.text!r} is not valid in a folder scheme"
                    )
                continue

            if isinstance(node, TagNode):
                canonical_names: list[str] = []
                for name in node.names:
                    canonical = canonical_tag_name(name)
                    if canonical is None:
                        raise FolderSchemeError(f"Unknown folder tag {name!r}")
                    canonical_names.append(canonical)

                for canonical in canonical_names:
                    if canonical not in REQUIRED_FOLDER_TAG_NAMES:
                        continue
                    if inside_group or len(node.names) != 1:
                        raise FolderSchemeError(
                            f"Required tag {canonical!r} must be an exact top-level tag"
                        )
                    required_references.add(canonical)
                continue

            if isinstance(node, PipeNode):
                raise FolderSchemeError("Folder groups cannot use pipe separators")

            if inside_group:
                raise FolderSchemeError("Folder groups cannot be nested")

            if node.is_or or any(isinstance(elem, PipeNode) for elem in node.elements):
                raise FolderSchemeError("Folder groups cannot use pipe separators")

            tags = [elem for elem in node.elements if isinstance(elem, TagNode)]
            nested = [elem for elem in node.elements if isinstance(elem, GroupNode)]
            if nested:
                raise FolderSchemeError("Folder groups cannot be nested")
            if len(tags) != 1:
                raise FolderSchemeError(
                    "Each folder group must contain exactly one tag expression"
                )
            visit(node.elements, inside_group=True)

    visit(nodes, inside_group=False)

    missing = REQUIRED_FOLDER_TAG_NAMES - required_references
    if missing:
        missing_names = ", ".join(sorted(missing))
        raise FolderSchemeError(
            f"Folder scheme is missing required top-level tags: {missing_names}"
        )


def _placeholder_tags(nodes: Sequence[Node]) -> dict[str, str]:
    """Supply safe values so static literals inside optional groups are checked."""
    placeholders: dict[str, str] = {}
    for node in _walk_nodes(nodes):
        if not isinstance(node, TagNode):
            continue
        for name in node.names:
            canonical = canonical_tag_name(name)
            if canonical is not None:
                placeholders[canonical] = "Tag"
    return placeholders


def _validate_scheme_nodes(nodes: Sequence[Node]) -> None:
    """Validate both folder syntax and the path produced with placeholders."""
    _validate_nodes(nodes)
    rendered = render_nodes(nodes, _placeholder_tags(nodes))
    _validate_rendered_path(
        rendered,
        error_type=FolderSchemeError,
        context="Folder scheme",
    )


# ---------------------------------------------------------------------------
# Public compile and render API
# ---------------------------------------------------------------------------


def compile_folder_scheme(text: str) -> FolderScheme:
    """Validate and compile a folder organization scheme."""
    if not isinstance(text, str):
        raise FolderSchemeError("Folder scheme must be a string")
    if not text or not text.strip():
        raise FolderSchemeError("Folder scheme must not be empty")
    if text != text.strip():
        raise FolderSchemeError("Folder scheme must not have surrounding whitespace")
    if any(separator in text for separator in ("\n", "\r", "\u0085", "\u2028", "\u2029")):
        raise FolderSchemeError("Folder scheme must be a single line")

    try:
        nodes = parse_scheme_strict(text)
    except SchemeSyntaxError as error:
        raise FolderSchemeError(f"Invalid folder scheme syntax: {error}") from error
    except RecursionError as error:
        raise FolderSchemeError("Folder scheme nesting is too deep") from error

    _validate_scheme_nodes(nodes)
    return FolderScheme._create(text, nodes)


def _resolve_tag_expression(
    tag: TagNode,
    tags: Mapping[str, str],
) -> tuple[str, str] | None:
    """Select and sanitize the first non-empty candidate in a tag expression."""
    for name in tag.names:
        canonical = canonical_tag_name(name)
        if canonical is None:
            raise FolderRenderError(f"Unknown folder tag {name!r}")
        value = tags.get(canonical)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise FolderRenderError(
                f"Tag {canonical!r} must contain a string value"
            )
        try:
            sanitized = sanitize_path_component(value)
        except (TypeError, ValueError) as error:
            raise FolderRenderError(
                f"Tag {canonical!r} cannot form a folder component: {error}"
            ) from error
        return canonical, sanitized
    return None


def render_folder_components(
    scheme: FolderScheme,
    tags: Mapping[str, str],
) -> tuple[str, ...]:
    """Render a compiled folder scheme into safe relative components."""
    if not isinstance(scheme, FolderScheme):
        raise TypeError("scheme must be a compiled FolderScheme")
    if not isinstance(tags, Mapping):
        raise TypeError("tags must be a mapping")
    # Only compiler-produced objects are accepted as the source of truth.
    if scheme._token is not _FOLDER_SCHEME_TOKEN:
        raise FolderRenderError("scheme was not produced by compile_folder_scheme")

    try:
        # Public AST nodes can be mutated by callers, so restore from text when
        # the structural fingerprint no longer matches the compiled snapshot.
        if _node_fingerprint(scheme.nodes) != scheme._fingerprint:
            scheme = compile_folder_scheme(scheme.text)
        _validate_scheme_nodes(scheme.nodes)
    except (FolderSchemeError, RecursionError, TypeError, AttributeError) as error:
        raise FolderRenderError(
            f"Compiled folder scheme is invalid: {error}"
        ) from error

    # Repeated expressions in one scheme resolve consistently without repeatedly
    # running the sanitizer.
    expression_cache: dict[tuple[str, ...], tuple[str, str] | None] = {}

    def resolve(tag: TagNode) -> tuple[str, str] | None:
        key = tuple(tag.names)
        if key not in expression_cache:
            expression_cache[key] = _resolve_tag_expression(tag, tags)
        return expression_cache[key]

    # Only top-level tags are unconditional.  Missing group tags are allowed
    # and cause their entire optional fragment to disappear.
    for node in scheme.nodes:
        if isinstance(node, TagNode) and resolve(node) is None:
            raise FolderRenderError(
                f"Tag expression {_tag_expression(node.names)} is required by the folder scheme"
            )

    # The generic renderer only ever receives sanitized tag values.
    render_tags: dict[str, str] = {}
    for node in _walk_nodes(scheme.nodes):
        if not isinstance(node, TagNode):
            continue
        selected = resolve(node)
        if selected is not None:
            canonical, sanitized = selected
            render_tags[canonical] = sanitized

    rendered = render_nodes(scheme.nodes, render_tags)
    return _validate_rendered_path(
        rendered,
        error_type=FolderRenderError,
        context="Rendered folder scheme",
        collapse_omitted_separators=True,
    )


def format_folder_components(components: Sequence[str]) -> str:
    """Format relative components for display with a trailing separator."""
    if not components:
        return ""
    return "/".join(components) + "/"
