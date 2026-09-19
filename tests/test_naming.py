"""Tests for shared.naming — the file naming scheme parser and renderer.

Covers: tag aliasing, fallback tags, AND groups, OR groups (with and
without literal parentheses), escaping, nested groups, and the README
default naming scheme.
"""

import pytest

from shared.naming import (
    CANONICAL_TAG_KEYS,
    REQUIRED_TAG_NAMES,
    VALID_TAG_NAMES,
    normalize_tag_name,
    resolve_tag_value,
    render_filename,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def full_tags():
    """All tags populated with canonical .cmct keys."""
    return {
        "title": "Worlds Finale",
        "network": "Cartoon Network",
        "block": "Toonami",
        "filler_type": "Promo",
        "year": "2000",
        "time_period": "1990s",
        "show": "Batman",
        "special": "Holiday",
        "length": "30 Seconds",
        "information": "Remastered",
    }


@pytest.fixture
def partial_tags():
    """Only required + year — common minimal case."""
    return {
        "title": "Worlds Finale",
        "network": "Cartoon Network",
        "filler_type": "Promo",
        "year": "2000",
    }


@pytest.fixture
def no_period_tags():
    """Only required + filler_type + network — nothing optional."""
    return {
        "title": "Worlds Finale",
        "network": "Cartoon Network",
        "filler_type": "Promo",
    }


# ---------------------------------------------------------------------------
# Alias layer
# ---------------------------------------------------------------------------

class TestAliases:
    """Tag name aliasing: user-facing to canonical."""

    @pytest.mark.parametrize("alias,canonical", [
        ("title", "title"),
        ("network", "network"),
        ("block", "block"),
        ("type", "filler_type"),
        ("year", "year"),
        ("time_period", "time_period"),
        ("show", "show"),
        ("special", "special"),
        ("length", "length"),
        ("info", "information"),
    ])
    def test_alias_mapping(self, alias, canonical):
        assert normalize_tag_name(alias) == canonical

    def test_unknown_tag_returns_none(self):
        assert normalize_tag_name("color") is None

    def test_valid_tag_names_set(self):
        assert VALID_TAG_NAMES == frozenset({
            "title", "network", "block", "type", "year",
            "time_period", "show", "special", "length", "info",
        })

    def test_canonical_keys_set(self):
        assert CANONICAL_TAG_KEYS == frozenset({
            "title", "network", "block", "filler_type", "year",
            "time_period", "show", "special", "length", "information",
        })

    def test_required_tags(self):
        assert REQUIRED_TAG_NAMES == frozenset({"title"})


class TestResolveTagValue:
    """Tag value resolution from a tags dict."""

    def test_user_facing_alias(self):
        tags = {"filler_type": "Promo"}
        assert resolve_tag_value("type", tags) == "Promo"

    def test_canonical_key(self):
        tags = {"filler_type": "Promo"}
        assert resolve_tag_value("filler_type", tags) == "Promo"

    def test_info_alias(self):
        tags = {"information": "Remastered"}
        assert resolve_tag_value("info", tags) == "Remastered"

    def test_empty_string_is_none(self):
        tags = {"filler_type": ""}
        assert resolve_tag_value("type", tags) is None

    def test_missing_key_is_none(self):
        tags = {}
        assert resolve_tag_value("type", tags) is None

    def test_unknown_tag_is_none(self):
        tags = {"color": "red"}
        assert resolve_tag_value("color", tags) is None


# ---------------------------------------------------------------------------
# Basic tag rendering
# ---------------------------------------------------------------------------

class TestBasicTags:
    """Single and multiple tag rendering at the top level."""

    def test_single_tag(self, full_tags):
        assert render_filename("{title}", full_tags) == "Worlds Finale"

    def test_multiple_tags(self, full_tags):
        assert render_filename("{network} - {filler_type}", full_tags) == "Cartoon Network - Promo"

    def test_alias_tag(self, full_tags):
        assert render_filename("{type}", full_tags) == "Promo"

    def test_info_alias(self, full_tags):
        assert render_filename("{info}", full_tags) == "Remastered"

    def test_unknown_tag_renders_empty(self, full_tags):
        assert render_filename("{color}", full_tags) == ""

    def test_missing_tag_renders_empty(self, no_period_tags):
        assert render_filename("{block}", no_period_tags) == ""


# ---------------------------------------------------------------------------
# Fallback tags
# ---------------------------------------------------------------------------

class TestFallback:
    """{a, b} fallback — first non-empty wins."""

    def test_first_present(self, full_tags):
        assert render_filename("{year,time_period}", full_tags) == "2000"

    def test_second_present(self, no_period_tags):
        tags = {**no_period_tags, "time_period": "90s"}
        assert render_filename("{year,time_period}", tags) == "90s"

    def test_neither_present(self, no_period_tags):
        assert render_filename("{year,time_period}", no_period_tags) == ""

    def test_spaces_around_comma(self, full_tags):
        assert render_filename("{year, time_period}", full_tags) == "2000"

    def test_three_way_fallback(self):
        tags = {"special": "Holiday"}
        assert render_filename("{block,special,show}", tags) == "Holiday"


# ---------------------------------------------------------------------------
# AND groups
# ---------------------------------------------------------------------------

class TestAndGroups:
    """[content] — render only if all tags are non-empty."""

    def test_present(self, full_tags):
        assert render_filename("[{block} - ]{title}", full_tags) == "Toonami - Worlds Finale"

    def test_absent(self, partial_tags):
        assert render_filename("[{block} - ]{title}", partial_tags) == "Worlds Finale"

    def test_separator_in_group(self, full_tags):
        scheme = "{network} - [{filler_type} - ]{title}"
        result = render_filename(scheme, full_tags)
        assert result == "Cartoon Network - Promo - Worlds Finale"

    def test_separator_in_group_absent(self, partial_tags):
        scheme = "{network} - [{filler_type} - ]{title}"
        result = render_filename(scheme, {**partial_tags, "filler_type": ""})
        assert result == "Cartoon Network - Worlds Finale"

    def test_fallback_tag_in_and_group_present(self, full_tags):
        assert render_filename("[{block,special} ]{title}", full_tags) == "Toonami Worlds Finale"

    def test_fallback_tag_in_and_group_absent(self, no_period_tags):
        tags = {**no_period_tags, "special": "Holiday"}
        assert render_filename("[{block,special} ]{title}", tags) == "Holiday Worlds Finale"

    def test_two_tags_both_required(self, full_tags):
        assert render_filename("[{block} {special}]{title}", full_tags) == "Toonami HolidayWorlds Finale"

    def test_two_tags_one_missing(self, full_tags):
        tags = {**full_tags, "block": ""}
        assert render_filename("[{block} {special}]{title}", tags) == "Worlds Finale"


# ---------------------------------------------------------------------------
# OR groups (no extra parentheses)
# ---------------------------------------------------------------------------

class TestOrGroups:
    """[{a}|{b}] — render if any tag is non-empty."""

    def test_both_present(self, full_tags):
        assert render_filename("[{length}|{info}]", full_tags) == "30 Seconds Remastered"

    def test_first_present(self, full_tags):
        tags = {**full_tags, "information": ""}
        assert render_filename("[{length}|{info}]", tags) == "30 Seconds"

    def test_second_present(self, full_tags):
        tags = {**full_tags, "length": ""}
        assert render_filename("[{length}|{info}]", tags) == "Remastered"

    def test_none_present(self, no_period_tags):
        tags = {**no_period_tags, "length": "", "information": ""}
        assert render_filename("[{length}|{info}]", tags) == ""

    def test_three_way_all_present(self):
        tags = {"length": "30s", "information": "Remastered", "show": "Batman"}
        assert render_filename("[{length}|{info}|{show}]", tags) == "30s Remastered Batman"

    def test_three_way_first_only(self):
        tags = {"length": "30s", "information": "", "show": ""}
        assert render_filename("[{length}|{info}|{show}]", tags) == "30s"


# ---------------------------------------------------------------------------
# OR groups with literal parentheses
# ---------------------------------------------------------------------------

class TestOrGroupsWithParens:
    """[({a}|{b})] — OR with literal parens in output."""

    def test_both_present(self, full_tags):
        assert render_filename("[({length}|{info})]", full_tags) == "(30 Seconds Remastered)"

    def test_first_present(self, full_tags):
        tags = {**full_tags, "information": ""}
        assert render_filename("[({length}|{info})]", tags) == "(30 Seconds)"

    def test_second_present(self, full_tags):
        tags = {**full_tags, "length": ""}
        assert render_filename("[({length}|{info})]", tags) == "(Remastered)"

    def test_none_present(self, no_period_tags):
        tags = {**no_period_tags, "length": "", "information": ""}
        assert render_filename("[({length}|{info})]", tags) == ""

    def test_in_full_scheme(self, full_tags):
        scheme = "{title} [({length}|{info})]"
        assert render_filename(scheme, full_tags) == "Worlds Finale (30 Seconds Remastered)"

    def test_in_full_scheme_one_empty(self, full_tags):
        scheme = "{title} [({length}|{info})]"
        tags = {**full_tags, "length": ""}
        assert render_filename(scheme, tags) == "Worlds Finale (Remastered)"

    def test_in_full_scheme_both_empty(self, full_tags):
        scheme = "{title} [({length}|{info})]"
        tags = {**full_tags, "length": "", "information": ""}
        assert render_filename(scheme, tags) == "Worlds Finale"


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

class TestEscaping:
    """Backslash escaping for literal special characters."""

    def test_escaped_braces(self, full_tags):
        assert render_filename(r"\{title\}", full_tags) == "{title}"

    def test_escaped_pipe_top_level(self, full_tags):
        assert render_filename("a \\| b", full_tags) == "a | b"

    def test_escaped_brackets(self, full_tags):
        assert render_filename(r"\[a\]", full_tags) == "[a]"

    def test_escaped_pipe_in_group_is_and(self, full_tags):
        result = render_filename("[a \\| b]", full_tags)
        assert result == "a | b"

    def test_escaped_comma_in_tag(self, full_tags):
        assert render_filename(r"\{year,time_period\}", full_tags) == "{year,time_period}"


# ---------------------------------------------------------------------------
# Nested groups
# ---------------------------------------------------------------------------

class TestNestedGroups:
    """Groups inside groups — each evaluates independently."""

    def test_and_inside_and(self, full_tags):
        assert render_filename("[[{block}]]", full_tags) == "Toonami"

    def test_and_with_special(self, full_tags):
        assert render_filename("[{block} [{special}]]", full_tags) == "Toonami Holiday"

    def test_nested_and_absent(self, partial_tags):
        assert render_filename("[{block} [{special}]]", partial_tags) == ""

    def test_or_inside_and_both_present(self, full_tags):
        scheme = "[{block} - [({length}|{info})]]"
        result = render_filename(scheme, full_tags)
        assert result == "Toonami - (30 Seconds Remastered)"

    def test_or_inside_and_and_fails(self, partial_tags):
        scheme = "[{block} - [({length}|{info})]]"
        result = render_filename(scheme, partial_tags)
        assert result == ""

    def test_or_inside_and_or_fails(self, full_tags):
        tags = {**full_tags, "block": "X", "length": "", "information": ""}
        scheme = "[{block} - [({length}|{info})]]"
        result = render_filename(scheme, tags)
        assert result == "X -"

    def test_nested_empty_group(self, full_tags):
        assert render_filename("{title}[]", full_tags) == "Worlds Finale"

    def test_empty_group(self):
        assert render_filename("[]", {}) == ""


# ---------------------------------------------------------------------------
# README default naming scheme
# ---------------------------------------------------------------------------

class TestReadmeScheme:
    """The README's default scheme in user-facing syntax."""

    # README: {network} - {filler_type} - {year,time_period} - [{block,special} ]{title} [({length}|{information})]
    SCHEME = (
        "{network} - {filler_type} - {year,time_period} - "
        "[{block,special} ]{title} [({length}|{information})]"
    )

    def test_full_tags(self, full_tags):
        expected = "Cartoon Network - Promo - 2000 - Toonami Worlds Finale (30 Seconds Remastered)"
        assert render_filename(self.SCHEME, full_tags) == expected

    def test_minimal_tags(self, partial_tags):
        expected = "Cartoon Network - Promo - 2000 - Worlds Finale"
        assert render_filename(self.SCHEME, partial_tags) == expected

    def test_time_period_only(self):
        tags = {
            "title": "Commercial Break",
            "network": "Nickelodeon",
            "filler_type": "Commercial",
            "year": "",
            "time_period": "90s",
        }
        expected = "Nickelodeon - Commercial - 90s - Commercial Break"
        assert render_filename(self.SCHEME, tags) == expected

    def test_no_optional_tags(self, no_period_tags):
        tags = {**no_period_tags, "year": "1995"}
        expected = "Cartoon Network - Promo - 1995 - Worlds Finale"
        assert render_filename(self.SCHEME, tags) == expected

    def test_special_as_fallback(self):
        tags = {
            "title": "Theme Song",
            "network": "Cartoon Network",
            "filler_type": "Theme",
            "year": "",
            "time_period": "",
            "block": "",
            "special": "Weekend",
        }
        # Year absent → stray separators remain (use proper scheme for clean output)
        expected = "Cartoon Network - Theme -  - Weekend Theme Song"
        assert render_filename(self.SCHEME, tags) == expected

    def test_proper_scheme_year_in_group(self):
        """Same as above but with year wrapped in its own group."""
        scheme = (
            "{network} - {filler_type} - [{year,time_period} - ]"
            "[{block,special} ]{title} [({length}|{information})]"
        )
        tags = {
            "title": "Theme Song",
            "network": "Cartoon Network",
            "filler_type": "Theme",
            "year": "",
            "time_period": "",
            "block": "",
            "special": "Weekend",
        }
        expected = "Cartoon Network - Theme - Weekend Theme Song"
        assert render_filename(scheme, tags) == expected
