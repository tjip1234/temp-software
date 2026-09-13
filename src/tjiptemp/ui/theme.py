"""Light and dark theming, shared by the Qt widgets and the pyqtgraph plots.

The look is a tracker's: tan frames, and black inset display boxes in which the
values are yellow and the labels green. The two modes differ only in the ground
those boxes sit on -- white for light, black for dark -- so the ``display_*``
tokens are nearly identical between them, while the ``surface``/``text`` tokens
are chosen per mode. Yellow is never put on white: anything yellow lives inside a
display box.

Plots, tables and inputs are display boxes, which is why charts always use the
dark-ground variant of the series colours (see :meth:`Theme.series_color`). The
categorical series colours themselves live in ``protocol.channels``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class Theme:
    name: str
    dark: bool

    surface: str          # window background
    surface_raised: str   # panels, cards
    surface_sunken: str   # plot background, inputs -- always the display ground
    border: str           # tan frames
    grid: str             # faint separators on the surface, pressed states

    text_primary: str     # text on the surface
    text_secondary: str
    text_muted: str

    accent: str           # primary buttons, focus, progress
    accent_text: str

    good: str             # state colours, on the surface
    warning: str
    serious: str
    critical: str

    chrome: str           # toolbar
    chrome_text: str
    header: str           # table column headers
    header_text: str
    title_bg: str         # group box titles
    title_text: str
    status_text: str      # status row, which is a display box
    divider: str          # splitters and toolbar separators

    display: str          # inner display boxes: plots, tables, inputs, readouts
    display_raised: str   # alternate rows, legends
    display_border: str
    display_text: str     # highlighted values
    display_label: str    # channel names, axes, grid
    display_dim: str      # stale values, units, crosshair
    display_critical: str

    selection: str        # current-row highlight
    selection_text: str

    def series_color(self, spec) -> str:
        """A channel's trace colour. Every chart is on the display ground."""
        return spec.color_dark

    @property
    def qt_palette(self) -> QPalette:
        palette = QPalette()
        role = QPalette.ColorRole
        for key, value in (
            (role.Window, self.surface),
            (role.WindowText, self.text_primary),
            (role.Base, self.display),
            (role.AlternateBase, self.display_raised),
            (role.Text, self.display_text),
            (role.PlaceholderText, self.display_dim),
            (role.Button, self.surface_raised),
            (role.ButtonText, self.text_primary),
            (role.Highlight, self.selection),
            (role.HighlightedText, self.selection_text),
            (role.ToolTipBase, self.display),
            (role.ToolTipText, self.display_text),
        ):
            palette.setColor(key, QColor(value))
        disabled = QPalette.ColorGroup.Disabled
        palette.setColor(disabled, role.Text, QColor(self.display_dim))
        palette.setColor(disabled, role.ButtonText, QColor(self.text_muted))
        palette.setColor(disabled, role.WindowText, QColor(self.text_muted))
        return palette


# The source palette, for reference: beige #C0A080, black #000000, yellow
# #FFFF54, tracker green #54AA54, white #FFFFFF, tan #9F8060, dark brown #403020,
# brick #A80000, magenta #A800A8. Everything below is one of these or a step of
# one toward legibility on its ground.

_DISPLAY = dict(
    display="#000000",
    display_raised="#17120c",
    display_text="#ffff54",
    display_label="#54aa54",
    display_dim="#9f8060",
    display_critical="#ff5454",
    selection="#403020",
    selection_text="#ffff54",
    status_text="#ffffff",
    chrome="#c0a080",
    chrome_text="#000000",
    header="#9f8060",
    header_text="#000000",
)

LIGHT = Theme(
    name="light",
    dark=False,
    surface="#ffffff",
    surface_raised="#faf6f0",
    surface_sunken="#000000",
    border="#9f8060",
    grid="#e8dccb",
    text_primary="#1a140e",
    text_secondary="#5e4a36",
    text_muted="#857058",
    accent="#403020",
    accent_text="#ffff54",
    good="#2f7a2f",
    warning="#8a6400",
    serious="#b34700",
    critical="#a80000",
    title_bg="#c0a080",
    title_text="#000000",
    divider="#a80000",
    display_border="#403020",
    **_DISPLAY,
)

DARK = Theme(
    name="dark",
    dark=True,
    surface="#0a0806",
    surface_raised="#1c150e",
    surface_sunken="#000000",
    border="#9f8060",
    grid="#403020",
    text_primary="#ffffff",
    text_secondary="#c0a080",
    text_muted="#9f8060",
    accent="#c0a080",
    accent_text="#000000",
    good="#54aa54",
    warning="#e0a040",
    serious="#ff7f3f",
    critical="#ff5454",
    title_bg="#1c150e",
    title_text="#ffff54",
    divider="#a80000",
    display_border="#9f8060",
    **_DISPLAY,
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
    background: {chrome};
    border: none;
    border-bottom: 2px solid {header};
    padding: 3px 6px;
    spacing: 2px;
}}
QToolBar QToolButton {{
    color: {chrome_text};
    background: transparent;
    border: 1px solid transparent;
    border-radius: 2px;
    padding: 4px 8px;
}}
QToolBar QToolButton:hover {{ border-color: {selection}; }}
QToolBar QToolButton:pressed {{ background: {header}; }}
QToolBar::separator {{
    background: {divider};
    width: 1px;
    margin: 4px 6px;
}}
QStatusBar {{
    background: {display};
    border-top: 2px solid {border};
    color: {status_text};
}}
QStatusBar QLabel {{ color: {status_text}; }}
QStatusBar::item {{ border: none; }}
QGroupBox {{
    background: {surface_raised};
    border: 1px solid {border};
    border-radius: 2px;
    margin-top: 14px;
    padding: 10px 10px 8px 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    background: {title_bg};
    color: {title_text};
}}
QTabWidget::pane {{
    border: 1px solid {border};
    border-radius: 2px;
    background: {surface_raised};
}}
QTabBar::tab {{
    background: transparent;
    color: {text_secondary};
    padding: 7px 14px;
    border: 1px solid transparent;
    border-top-left-radius: 2px;
    border-top-right-radius: 2px;
}}
QTabBar::tab:selected {{
    background: {surface_raised};
    color: {text_primary};
    border-color: {border};
    border-top: 2px solid {divider};
    border-bottom-color: {surface_raised};
}}
QTableView, QTreeView, QListView {{
    background: {display};
    alternate-background-color: {display_raised};
    color: {display_text};
    border: 1px solid {display_border};
    border-radius: 2px;
    gridline-color: {selection};
    selection-background-color: {selection};
    selection-color: {selection_text};
}}
QHeaderView::section {{
    background: {header};
    color: {header_text};
    border: none;
    border-bottom: 1px solid {selection};
    border-right: 1px solid {selection};
    padding: 5px 8px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background: {header}; border: none; }}
QPushButton {{
    background: {surface_raised};
    border: 1px solid {border};
    border-radius: 2px;
    padding: 6px 13px;
    color: {text_primary};
}}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:pressed {{ background: {grid}; }}
QPushButton:disabled {{ color: {text_muted}; border-color: {grid}; }}
QPushButton[primary="true"] {{
    background: {accent};
    border-color: {accent};
    color: {accent_text};
    font-weight: 600;
}}
QPushButton[destructive="true"] {{
    color: {critical};
    border-color: {critical};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {display};
    color: {display_text};
    border: 1px solid {display_border};
    border-radius: 2px;
    padding: 5px 7px;
    selection-background-color: {selection};
    selection-color: {selection_text};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {display_text};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {display_dim};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {display};
    color: {display_text};
    border: 1px solid {display_border};
    selection-background-color: {selection};
    selection-color: {selection_text};
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {display};
    border: 1px solid {display_dim};
}}
QSlider::sub-page:horizontal {{ background: {display_label}; }}
QSlider::handle:horizontal {{
    width: 10px;
    margin: -6px 0;
    background: {chrome};
    border: 1px solid {selection};
}}
QSlider::handle:horizontal:hover {{ background: {display_text}; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 11px;
    height: 11px;
    background: {display};
    border: 1px solid {display_dim};
}}
QRadioButton::indicator {{ border-radius: 6px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {display_text};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {display_text};
    border-color: {display_dim};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {selection};
}}
QCheckBox::indicator:checked:disabled, QRadioButton::indicator:checked:disabled {{
    background: {display_dim};
}}
QSplitter::handle {{ background: {divider}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QScrollBar:vertical {{
    background: transparent; width: 11px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {border}; border-radius: 2px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; }}
QScrollBar::handle:horizontal {{
    background: {border}; border-radius: 2px; min-width: 30px;
}}
QToolTip {{
    background: {display};
    color: {display_text};
    border: 1px solid {border};
    padding: 5px 7px;
}}
QProgressBar {{
    background: {display};
    color: {display_text};
    border: 1px solid {display_border};
    border-radius: 2px;
    text-align: center;
    height: 16px;
}}
QProgressBar::chunk {{ background: {display_label}; border-radius: 1px; }}
"""


def apply(app: QApplication, theme: Theme) -> None:
    """Apply a theme to the whole application, including pyqtgraph."""
    app.setStyle("Fusion")
    app.setPalette(theme.qt_palette)
    app.setStyleSheet(STYLESHEET.format(**asdict(theme)))

    try:
        import pyqtgraph as pg

        pg.setConfigOption("background", theme.display)
        pg.setConfigOption("foreground", theme.display_label)
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
