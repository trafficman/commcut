"""File naming scheme parsing and rendering for commcut.

A naming scheme is a user-editable template string containing tag
placeholders and optional grouping constructs:

  {tag}              — render the tag's value, or nothing if absent.
  {a,b}              — fallback: render *a* if present, else *b*.
  [content]          — AND group: render only if every tag in *content*
                       is non-empty; otherwise the whole group (including
                       surrounding literals like separators) is omitted.
  [{a}|{b}]              — OR group: render if any tag is present, joining
                       non-empty values with spaces; omit entirely when
                       all are empty.

Tags are exposed to the user under short names (e.g. ``{type}``), but the
.cmct segment data stores canonical keys (e.g. ``filler_type``).  The alias
map below normalizes user-facing names to canonical keys at parse time, so
scheme text uses short forms while persistence stays consistent with
``shared.segments`` and ``editor``'s ``_TAG_FIELDS``.
"""

# ---------------------------------------------------------------------------
# Alias map: user-facing tag name → canonical key stored in .cmct segment tags.
#
# Most tags share the same name in both contexts; only the abbreviated aliases
# in the user-facing syntax need remapping.  These canonical keys match the
# keys used in editor.py's _TAG_FIELDS and the .cmct "tags" dict structure.
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

    Returns the value string if the tag is known and non-empty, or
    ``None`` if the tag is unknown or has no value in *tags*.

    >>> resolve_tag_value("type", {"filler_type": "Promo"})
    'Promo'
    >>> resolve_tag_value("type", {"filler_type": ""}) is None
    True
    >>> resolve_tag_value("type", {}) is None
    True
    >>> resolve_tag_value("color", {"color": "red"}) is None
    True
    """
    canonical = normalize_tag_name(user_name)
    if canonical is None:
        return None
    value = tags.get(canonical, "")
    return value if value else None
