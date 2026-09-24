"""Tests for strict shared scheme parsing."""

import pytest

from shared.scheme import (
    GroupNode,
    LiteralNode,
    SchemeSyntaxError,
    TagNode,
    canonical_tag_name,
    parse_scheme,
    parse_scheme_strict,
    render_nodes,
)


# ---------------------------------------------------------------------------
# Public strict AST
# ---------------------------------------------------------------------------

def test_strict_parser_exposes_public_nodes():
    """Strict parsing returns inspectable nodes for path-policy validation."""
    nodes = parse_scheme_strict("Root/[{block}/]")

    assert nodes[0] == LiteralNode("R")
    assert nodes[1] == LiteralNode("o")
    assert isinstance(nodes[5], GroupNode)
    assert isinstance(nodes[5].elements[0], TagNode)
    assert nodes[5].elements[0].names == ["block"]


def test_strict_parser_marks_escaped_literals():
    """Strict AST records whether a literal originated from an escape."""
    nodes = parse_scheme_strict(r"\{block\}")

    assert nodes[0] == LiteralNode("{", escaped=True)
    assert render_nodes(nodes, {}) == "{block}"


# ---------------------------------------------------------------------------
# Strict syntax errors and parser limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{network", "Unclosed tag"),
        ("[Blocks/{block}/", "Unclosed group"),
        ("{network}}", "Unexpected closing brace"),
        ("[Blocks]]", "Unexpected closing bracket"),
        ("{}", "Empty tag name"),
        ("{year,,time_period}", "Empty tag name"),
        ("\\", "Dangling escape"),
        ("{{network}", "Nested tag"),
    ],
)
def test_strict_parser_rejects_malformed_syntax(text, message):
    """Malformed delimiters, names, and escapes report their source position."""
    with pytest.raises(SchemeSyntaxError, match=message) as raised:
        parse_scheme_strict(text)

    assert raised.value.position >= 0


def test_lenient_parser_keeps_filename_compatibility():
    """The existing filename parser retains its tolerant legacy behavior."""
    nodes = parse_scheme("{network")

    assert nodes == [TagNode(["network"])]
    assert parse_scheme(r"\[") == [LiteralNode("[")]


def test_strict_parser_limits_group_nesting():
    """Deeply nested input fails before exhausting Python recursion."""
    text = "[" * 70 + "]" * 70

    with pytest.raises(SchemeSyntaxError, match="Group nesting exceeds"):
        parse_scheme_strict(text)


# ---------------------------------------------------------------------------
# Canonical tag-name resolution
# ---------------------------------------------------------------------------

def test_canonical_tag_name_accepts_aliases_and_canonical_keys():
    """Both user aliases and stored .cmct keys resolve to one name."""
    assert canonical_tag_name("type") == "filler_type"
    assert canonical_tag_name("filler_type") == "filler_type"
    assert canonical_tag_name("information") == "information"
    assert canonical_tag_name("color") is None
