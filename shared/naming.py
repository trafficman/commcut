"""Filename template policy for commcut.

The shared tag syntax, parser, and conditional renderer live in
``shared.scheme``.  This module keeps filename-specific requirements and the
public filename rendering API while re-exporting the shared tag helpers.
"""

from __future__ import annotations

from shared.scheme import (
    CANONICAL_TAG_KEYS,
    VALID_TAG_NAMES,
    normalize_tag_name,
    parse_scheme,
    render_nodes,
    resolve_tag_value,
)

REQUIRED_TAG_NAMES: frozenset[str] = frozenset({"title"})


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
