"""QUiLoader subclass that can construct our custom promoted widgets.

PySide6's QUiLoader does not auto-resolve promoted widgets reliably across
all platforms. We override createWidget so that, for any class name that
was registered via register_widget(), we build the real widget ourselves
instead of letting QUiLoader try to instantiate it. Everything else
falls through to the base implementation.

Consumers register their custom widgets before calling loader.load():

    loader = UiLoader()
    loader.register_widget(TimelineWidget)
    self.ui = loader.load(ui_file, self)

Windows use adopt_title() rather than repeating the one line it saves — see there.
"""

from PySide6.QtWidgets import QWidget
from PySide6.QtUiTools import QUiLoader


def adopt_title(window, loaded_widget):
    """Give a `QMainWindow` the title its `.ui` already carries.

    Every `.ui` in this app puts `windowTitle` on the **central widget**, because the
    root of a promoted `.ui` is a plain `QWidget` that the window then adopts with
    `setCentralWidget`. Qt does not copy that property across, so `windowTitle()` on
    the window itself is empty and the title bar and the taskbar entry say nothing —
    which is how three windows came to be indistinguishable by name in the taskbar,
    and matters more now that the importer's three windows are told apart by their
    names alone.

    The `.ui` stays the one place a name is written down; this copies it rather than
    restating it, because a title in code and a title in a `.ui` are two values that
    would drift.
    """
    title = loaded_widget.windowTitle()
    if title:
        window.setWindowTitle(title)
    return window


class UiLoader(QUiLoader):
    """QUiLoader that can construct our custom promoted widgets.

    QUiLoader.createWidget is called for every widget in the .ui file. When
    it encounters our promoted class name we build the real widget;
    everything else falls through to the base implementation.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._custom_widgets = {}

    def register_widget(self, cls):
        """Register a custom widget class to be instantiated by createWidget."""
        self._custom_widgets[cls.__name__] = cls

    def createWidget(self, className, parent=None, name=""):
        if className in self._custom_widgets:
            widget = self._custom_widgets[className](parent)
            widget.setObjectName(name)
            return widget
        return super().createWidget(className, parent, name)
