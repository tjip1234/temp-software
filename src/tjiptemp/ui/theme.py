"""Light and dark theming, shared by the Qt widgets and the pyqtgraph plots.

Both modes are chosen, not derived: the dark values are the same hues re-stepped
against a dark surface, because a naive inversion produces colours that are either
invisible or garish. The categorical series colours live in
``protocol.channels`` -- this module supplies the surfaces, text and chrome those
sit on.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class Theme:
    name: str
    dark: bool

    surface: str          # window background
    surface_raised: str   # panels, cards
    surface_sunken: str   # plot background, inputs
    border: str
    grid: str

    text_primary: str
    text_secondary: str
    text_muted: str

    accent: str
    good: str
    warning: str
    serious: str
    critical: str

    @property
    def qt_palette(self) -> QPalette:
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(self.surface))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(self.text_primary))
        palette.setColor(QPalette.ColorRole.Base, QColor(self.surface_sunken))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(self.surface_raised))
        palette.setColor(QPalette.ColorRole.Text, QColor(self.text_primary))
        palette.setColor(QPalette.ColorRole.Button, QColor(self.surface_raised))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(self.text_primary))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(self.accent))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(self.surface_raised))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(self.text_primary))
        palette.setColor(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(self.text_muted)
        )
        palette.setColor(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(self.text_muted)
        )
        return palette


LIGHT = Theme(
    name="light",
    dark=False,
    surface="#f4f3f0",
    surface_raised="#fcfcfb",
    surface_sunken="#ffffff",
    border="#d8d7d1",
    grid="#e5e4e0",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    text_muted="#8a8984",
    accent="#2a78d6",
    good="#0f7a4d",
    warning="#a86b00",
    serious="#c1521f",
    critical="#c02a2a",
)

DARK = Theme(
    name="dark",
    dark=True,
    surface="#131312",
    surface_raised="#1f1f1e",
    surface_sunken="#1a1a19",
    border="#33322f",
    grid="#2c2b29",
    text_primary="#f4f3ef",
    text_secondary="#c3c2b7",
    text_muted="#8a8984",
    accent="#3987e5",
    good="#3fbb85",
    warning="#d6a13a",
    serious="#e07a45",
    critical="#e66767",
)


def resolve(preference: str) -> Theme:
    """Turn the ``auto``/``light``/``dark`` setting into a concrete theme."""
    if preference == "light":
        return LIGHT
    if preference == "dark":
        return DARK
    app = QApplication.instance()
    if app is not None:
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QGuiApplication

            scheme = QGuiApplication.styleHints().colorScheme()
            return DARK if scheme == Qt.ColorScheme.Dark else LIGHT
        except (AttributeError, RuntimeError):
            window = app.palette().color(QPalette.ColorRole.Window)
            return DARK if window.lightness() < 128 else LIGHT
    return LIGHT


STYLESHEET = """
QWidget {{
    font-size: 13px;
}}
QMainWindow, QDialog {{
    background: {surface};
}}
QToolBar {{
    background: {surface_raised};
    border: none;
    border-bottom: 1px solid {border};
    padding: 4px 6px;
    spacing: 4px;
}}
QStatusBar {{
    background: {surface_raised};
    border-top: 1px solid {border};
    color: {text_secondary};
}}
QStatusBar::item {{ border: none; }}
QGroupBox {{
    background: {surface_raised};
    border: 1px solid {border};
    border-radius: 8px;
    margin-top: 14px;
    padding: 10px 10px 8px 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {text_secondary};
}}
QTabWidget::pane {{
    border: 1px solid {border};
    border-radius: 8px;
    background: {surface_raised};
}}
QTabBar::tab {{
    background: transparent;
    color: {text_secondary};
    padding: 7px 14px;
    border: 1px solid transparent;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}}
QTabBar::tab:selected {{
    background: {surface_raised};
    color: {text_primary};
    border-color: {border};
    border-bottom-color: {surface_raised};
}}
QTableView, QTreeView, QListView {{
    background: {surface_sunken};
    alternate-background-color: {surface_raised};
    border: 1px solid {border};
    border-radius: 6px;
    gridline-color: {grid};
    selection-background-color: {accent};
    selection-color: #ffffff;
}}
QHeaderView::section {{
    background: {surface_raised};
    color: {text_secondary};
    border: none;
    border-bottom: 1px solid {border};
    border-right: 1px solid {border};
    padding: 5px 8px;
    font-weight: 600;
}}
QPushButton {{
    background: {surface_raised};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 6px 13px;
    color: {text_primary};
}}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:pressed {{ background: {grid}; }}
QPushButton:disabled {{ color: {text_muted}; border-color: {grid}; }}
QPushButton[primary="true"] {{
    background: {accent};
    border-color: {accent};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[destructive="true"] {{
    color: {critical};
    border-color: {critical};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {surface_sunken};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 5px 7px;
    selection-background-color: {accent};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {accent};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QSplitter::handle {{ background: {border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QScrollBar:vertical {{
    background: transparent; width: 11px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {border}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; }}
QScrollBar::handle:horizontal {{
    background: {border}; border-radius: 5px; min-width: 30px;
}}
QToolTip {{
    background: {surface_raised};
    color: {text_primary};
    border: 1px solid {border};
    padding: 5px 7px;
}}
QProgressBar {{
    background: {surface_sunken};
    border: 1px solid {border};
    border-radius: 6px;
    text-align: center;
    height: 16px;
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}
"""


def apply(app: QApplication, theme: Theme) -> None:
    """Apply a theme to the whole application, including pyqtgraph."""
    app.setStyle("Fusion")
    app.setPalette(theme.qt_palette)
    app.setStyleSheet(STYLESHEET.format(**{
        "surface": theme.surface,
        "surface_raised": theme.surface_raised,
        "surface_sunken": theme.surface_sunken,
        "border": theme.border,
        "grid": theme.grid,
        "text_primary": theme.text_primary,
        "text_secondary": theme.text_secondary,
        "text_muted": theme.text_muted,
        "accent": theme.accent,
        "critical": theme.critical,
    }))

    try:
        import pyqtgraph as pg

        pg.setConfigOption("background", theme.surface_sunken)
        pg.setConfigOption("foreground", theme.text_secondary)
        pg.setConfigOptions(antialias=True)
    except ImportError:
        pass


#: State colours, used for status pills and fault badges. Never colour alone --
#: every use is paired with a word.
def state_color(theme: Theme, state: str) -> str:
    return {
        "online": theme.good,
        "connecting": theme.warning,
        "degraded": theme.warning,
        "error": theme.critical,
        "offline": theme.text_muted,
    }.get(state, theme.text_muted)
