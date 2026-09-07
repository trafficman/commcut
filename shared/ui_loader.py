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
"""

from PySide6.QtUiTools import QUiLoader


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
