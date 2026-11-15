"""Shared tree-view building and sorting for the library windows.

Both the Library Manager and Library Sharing build a clip-driven
`QTreeWidget` of folders and clips, and both sort every level the
same way: folders first, then clips, each group casefolded
alphabetically.
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem


def leaf_display(components):
    """The text a clip's row shows: its filename, with any folder value
    it leads with struck from it.

    A clip's filename is a projection of its tags, and the folder
    above the row is those same tags rendered again, so a
    `Cartoon Network` clip in a `Cartoon Network` folder shows a name
    that starts with `Cartoon Network` -- the folder said twice.
    Striking the folder values from the front of the name hands the
    horizontal room back.

    Only a *leading* value is struck, and only when something is
    left: a name that is nothing but its folder values stays whole
    rather than showing an empty row, and a value that appears
    mid-name is left alone.
    """
    stem, extension = os.path.splitext(components[-1])
    for folder in components[:-1]:
        if not stem.lower().startswith(folder.lower()):
            continue
        remainder = stem[len(folder):].lstrip(" -_.")
        if remainder:
            stem = remainder
    return stem + extension


def set_subtree_checks(item, checked):
    """Check or uncheck every row of `item`'s subtree, inclusive.

    Used for folder rows set to checked/unchecked: the cascade is the
    folder's whole content. Folder parents recompute their own state
    from their descendants; this function never reads it back.
    """
    state = Qt.Checked if checked else Qt.Unchecked
    item.setCheckState(0, state)
    for index in range(item.childCount()):
        set_subtree_checks(item.child(index), checked)


def clip_items(tree):
    """Every clip leaf in `tree`, regardless of order or nesting."""
    found = []

    def walk(item):
        if _is_clip(item):
            found.append(item)
            return
        for index in range(item.childCount()):
            walk(item.child(index))

    for index in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(index))
    return found


def _is_clip(item):
    return bool(item.flags() & Qt.ItemIsSelectable) and item.childCount() == 0


def _sort_key(item):
    """Folders (not selectable) before clips (selectable), then name."""
    return (
        bool(item.flags() & Qt.ItemIsSelectable),
        item.text(0).casefold(),
    )


def sort_tree(tree):
    """Sort every level of the tree: folders first, then clips.

    Each group is sorted casefolded alphabetically. Top-level items
    are taken out and re-added in order, then each subtree is
    sorted recursively.
    """
    top_level = [
        tree.topLevelItem(index)
        for index in range(tree.topLevelItemCount())
    ]
    top_level.sort(key=_sort_key)
    for item in top_level:
        tree.takeTopLevelItem(tree.indexOfTopLevelItem(item))
    for item in top_level:
        tree.addTopLevelItem(item)
    for item in top_level:
        sort_subtree(item)


def sort_subtree(item):
    """Sort one folder's children: folders first, then clips."""
    children = [
        item.child(index) for index in range(item.childCount())
    ]
    children.sort(key=_sort_key)
    for child in children:
        item.removeChild(child)
    for child in children:
        item.addChild(child)
    for child in children:
        sort_subtree(child)


def build_tree(tree, clips, clips_by_path, clip_items, *, checkboxes=False):
    """Rebuild the sidebar tree from `clips`, folders first.

    `tree` is the `QTreeWidget` to fill. `clips_by_path` and
    `clip_items` are populated with the clip keyed by its relative
    path and by its clip ID respectively.

    `checkboxes=True` makes every row checkable and folders tri-state —
    a folder set to checked toggles its whole subtree, and a tri-state
    partial state reflects mixed descendants. Used by the Library
    Manager's bulk edit; Library Sharing's tree stays checkless.
    """
    tree.clear()
    clips_by_path.clear()
    clip_items.clear()
    folders: dict[tuple[str, ...], QTreeWidgetItem] = {}
    for clip in clips:
        components = clip.relative_path.split("/")
        parent = None
        for depth in range(len(components) - 1):
            key = tuple(components[:depth + 1])
            node = folders.get(key)
            if node is None:
                node = QTreeWidgetItem(parent, [components[depth]])
                if parent is None:
                    tree.addTopLevelItem(node)
                node.setFlags(node.flags() & ~Qt.ItemIsSelectable)
                if checkboxes:
                    node.setFlags(
                        node.flags() | Qt.ItemIsUserCheckable
                        | Qt.ItemIsAutoTristate)
                    node.setCheckState(0, Qt.Unchecked)
                folders[key] = node
            parent = node
        leaf = QTreeWidgetItem(parent, [leaf_display(components)])
        if parent is None:
            tree.addTopLevelItem(leaf)
        leaf.setData(0, Qt.UserRole, clip.record.clip_id)
        leaf.setData(0, Qt.UserRole + 1, clip.relative_path)
        if checkboxes:
            leaf.setFlags(leaf.flags() | Qt.ItemIsUserCheckable)
            leaf.setCheckState(0, Qt.Unchecked)
        clips_by_path[clip.relative_path] = clip
        if clip.record.clip_id:
            clip_items[clip.record.clip_id] = leaf
    sort_tree(tree)
