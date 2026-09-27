"""The documentation set has to stay navigable.

AGENTS.md was one 1000-line file that every session had to load in full. It is
now a short orientation plus a doc set, and a doc set can rot in ways a single
file cannot: a document nobody links, a link to a document that was renamed, a
test file named in a doc that no longer exists, and AGENTS.md quietly regrowing
into the thing it was split up to stop. These tests fail on each of those.
"""

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_MD = os.path.join(PROJECT_ROOT, "AGENTS.md")
README_MD = os.path.join(PROJECT_ROOT, "README.md")
PACKAGING_README = os.path.join(PROJECT_ROOT, "packaging", "README.md")
DOCS_ROOT = os.path.join(PROJECT_ROOT, "docs")
DOCS_README = os.path.join(DOCS_ROOT, "README.md")
AGENTS_LINE_CEILING = 240

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_TEST_FILE = re.compile(r"^(?:tests/)?(test_[A-Za-z0-9_]+\.py)$")


def _doc_paths():
    """Every markdown file under docs/, as root-relative posix paths."""
    found = []
    for directory, _dirs, files in os.walk(DOCS_ROOT):
        for name in files:
            if name.endswith(".md"):
                absolute = os.path.join(directory, name)
                found.append(
                    os.path.relpath(absolute, PROJECT_ROOT).replace(os.sep, "/"))
    return sorted(found)


def _markdown_files():
    """AGENTS.md, the two project readmes, and every document in docs/."""
    return [AGENTS_MD, README_MD, PACKAGING_README] + [
        os.path.join(PROJECT_ROOT, path) for path in _doc_paths()
    ]


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _relative_links(text):
    """The local targets of every markdown link, anchors and URLs removed."""
    targets = []
    for raw in _LINK.findall(text):
        target = raw.split("#", 1)[0].strip()
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        targets.append(target)
    return targets


def _resolved(containing_file, target):
    """`target` as a root-relative posix path, relative to `containing_file`."""
    base = os.path.dirname(containing_file)
    return os.path.relpath(
        os.path.normpath(os.path.join(base, target)), PROJECT_ROOT
    ).replace(os.sep, "/")


def _linked_documents(containing_file):
    """Root-relative paths `containing_file` links to."""
    return {
        _resolved(containing_file, target)
        for target in _relative_links(_read(containing_file))
    }


def test_every_doc_is_referenced_from_an_index():
    """A document nobody links is a document nobody reads."""
    for index in (AGENTS_MD, DOCS_README):
        index_relative = os.path.relpath(index, PROJECT_ROOT).replace(os.sep, "/")
        linked = _linked_documents(index)
        missing = [
            path for path in _doc_paths()
            if path not in linked and path != index_relative
        ]

        assert not missing, (
            f"{index_relative} does not reference {missing}. Add a row to "
            f"its index, or delete the file."
        )


def test_relative_doc_links_resolve():
    """Local markdown links point at a markdown file that exists.

    Source files are referenced as backticked paths, not links, so a module can
    move without forcing a documentation edit; a document is not allowed to.
    """
    broken = []
    for path in _markdown_files():
        text = _read(path)
        for target in _relative_links(text):
            resolved = os.path.normpath(
                os.path.join(os.path.dirname(path), target.split("#", 1)[0]))
            if not target.endswith(".md"):
                broken.append(f"{os.path.basename(path)} -> {target} (not a .md)")
            elif not os.path.isfile(resolved):
                broken.append(f"{os.path.basename(path)} -> {target} (missing)")

    assert not broken, "unresolvable documentation links: " + "; ".join(broken)


def test_test_files_named_in_docs_exist():
    """A named test file is a coverage claim, so it is checked like one."""
    broken = []
    for path in _markdown_files():
        for span in _BACKTICKED.findall(_read(path)):
            match = _TEST_FILE.match(span.strip())
            if match is None:
                continue
            if not os.path.isfile(os.path.join(PROJECT_ROOT, "tests", match.group(1))):
                broken.append(f"{os.path.basename(path)} -> {span}")

    assert not broken, f"docs name test files that do not exist: {broken}"


def test_agents_md_stays_small():
    """AGENTS.md is force-loaded; detail belongs in docs/."""
    with open(AGENTS_MD, encoding="utf-8") as handle:
        line_count = len(handle.read().splitlines())

    assert line_count <= AGENTS_LINE_CEILING, (
        f"AGENTS.md is {line_count} lines, over the {AGENTS_LINE_CEILING}-line "
        f"ceiling. Move the detail into docs/ and link it."
    )
