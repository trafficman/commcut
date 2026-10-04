"""Text colour in the shipped `.ui` files.

Three labels asked for `palette(mid)`, which reads as "muted" and means nothing of
the kind to a palette: `Mid` is a **swatch** colour, chosen to sit between the
background and the highlight so it can fill buttons and frames. Measured against
its own window colour it lands at a contrast ratio of **1.7:1** on the light scheme
and **2.0:1** on the dark one, where it is a light grey on a dark grey — the
evidence line in the Untagged Library Mesh was unreadable in both, and nobody
noticed because it is the least urgent text on the screen.

The cost of getting it wrong is that nothing fails. A QSS rule that resolves to a
bad colour is not an error, and the window still opens, so this file sweeps the
shipped `.ui` files for the mistake instead of waiting for a person to squint at
it again.

The roles allowed below are the foreground ones: `Palette::Text` and friends are
what Qt draws text with, and they are re-derived by the platform for whichever
scheme is in force — which is the point of asking the palette at all.
"""

import os
import re
import xml.etree.ElementTree as ET

from test_frozen_mode import UI_FILES

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TEXT_COLOUR = re.compile(r"(?<![\w-])color\s*:\s*([^;}]+)")
_PALETTE_ROLE = re.compile(r"palette\(\s*([a-z-]+)\s*\)")

#: Palette roles whose value is a foreground, i.e. something Qt itself sets text
#: in. `link` and `link-visited` are here because they are the palette's own
#: decision about text that navigates, and the platform keeps them readable.
FOREGROUND_ROLES = frozenset({
    "text",
    "window-text",
    "bright-text",
    "placeholder-text",
    "link",
    "link-visited",
})


def declared_text_colours():
    """`(ui file, widget, colour)` for every text colour a shipped `.ui` sets."""
    found = []
    for folder, name in UI_FILES:
        tree = ET.parse(os.path.join(PROJECT_ROOT, folder, name))
        for widget in tree.iter("widget"):
            for prop in widget.findall("property"):
                if prop.get("name") != "styleSheet":
                    continue
                declared = prop.findtext("string") or ""
                for colour in _TEXT_COLOUR.findall(declared):
                    found.append((name, widget.get("name"), colour.strip()))
    return found


def test_no_ui_picks_a_swatch_colour_for_its_text():
    """Nothing in a `.ui` may name a palette role that is not a foreground.

    `Mid`, `Base`, `Window`, `Button`, `Light`, `Dark` and `Highlight` are all
    fills. Asking for one as a text colour produces a label nobody can read, in
    whichever scheme it happens to look worst in — and the app ships with no
    palette of its own, so which scheme that is belongs to the user's system.
    """
    offenders = [
        f"{name}:{widget} asks for {colour!r}"
        for name, widget, colour in declared_text_colours()
        if not _is_a_foreground(colour)
    ]

    assert not offenders, (
        "text drawn in a palette swatch instead of a palette foreground: "
        + "; ".join(offenders)
        + ". Drop the colour and let the label use the window's own text "
          "colour, which the platform keeps readable in both schemes."
    )


def _is_a_foreground(colour):
    """Whether a declared colour is one this file allows."""
    roles = _PALETTE_ROLE.findall(colour)
    if roles:
        return all(role in FOREGROUND_ROLES for role in roles)
    # A literal colour is allowed here: it is not a palette role, so whether it
    # reads is the judgement of whoever typed it, not of the palette's meaning.
    # `settings.py` styles its error preview that way.
    return True