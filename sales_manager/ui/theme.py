"""Shared dark theme for the desktop workspace and its dialogs."""
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem


COLORS = {
    'window': '#191919', 'sidebar': '#1f1f1f', 'card': '#252525',
    'secondary': '#2a2a2a', 'input': '#303030', 'market': '#232a30',
    'analysis': '#25252c', 'search': '#242b28', 'mail': '#292725',
    'sender': '#292929', 'text': '#f0f0f0', 'muted': '#a9a9a9',
    'disabled': '#777777', 'border': '#606060', 'hover_border': '#858585',
    'focus': '#6495ed', 'primary': '#376fe0', 'warning': '#e3a02c',
    'success': '#62be91', 'error': '#e07878',
}


def palette():
    """Set Qt's fallback colors too, including popups and disabled controls."""
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: COLORS['window'],
        QPalette.ColorRole.WindowText: COLORS['text'],
        QPalette.ColorRole.Base: COLORS['input'],
        QPalette.ColorRole.AlternateBase: '#292929',
        QPalette.ColorRole.Text: COLORS['text'],
        QPalette.ColorRole.PlaceholderText: '#9b9b9b',
        QPalette.ColorRole.Button: '#303030',
        QPalette.ColorRole.ButtonText: COLORS['text'],
        QPalette.ColorRole.Highlight: '#334c6b',
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.ToolTipBase: '#30343a',
        QPalette.ColorRole.ToolTipText: COLORS['text'],
        QPalette.ColorRole.Link: '#9dbfff',
    }
    for role, value in roles.items():
        p.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(COLORS['disabled']))
    return p


def font():
    f = QFont()
    f.setFamilies(['Segoe UI Variable', 'Segoe UI'])
    f.setPointSize(10)
    return f


def notice_tone(widget, tone):
    """Change only the visual tone of an existing, text-bearing notice."""
    widget.setProperty('tone', tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def demand_background(percent):
    """Muted blue-to-red demand indicator, legible with the common light text."""
    ratio = max(0.0, min(1.0, percent / 100))
    low, high = (38, 55, 71), (76, 47, 50)
    return QColor(*(round(a + (b - a) * ratio) for a, b in zip(low, high)))


class _RowHoverFilter(QObject):
    """Track the hovered row without consuming mouse or selection events."""
    def __init__(self, table):
        super().__init__(table)
        self.table = table
        self.row = -1

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseMove:
            row = self.table.rowAt(event.position().toPoint().y())
        elif event.type() == QEvent.Type.Leave:
            row = -1
        else:
            return False
        if row != self.row:
            self.row = row
            self.table.viewport().update()
        return False


class RowHoverDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        table = self.parent()
        styled_option = QStyleOptionViewItem(option)
        hover = getattr(table, '_row_hover_filter', None)
        if hover is not None and index.row() == hover.row:
            styled_option.state |= QStyle.StateFlag.State_MouseOver
        super().paint(painter, styled_option, index)


def enable_row_hover(table):
    table.setMouseTracking(True)
    table._row_hover_filter = _RowHoverFilter(table)
    table.viewport().installEventFilter(table._row_hover_filter)
    table.setItemDelegate(RowHoverDelegate(table))


class DemandDelegate(RowHoverDelegate):
    """Paint a restrained readable badge even when the application uses QSS."""
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        brush = index.data(Qt.ItemDataRole.BackgroundRole)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        if brush is None or brush.style() == Qt.BrushStyle.NoBrush or not text:
            return
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        width = min(option.rect.width() - 12, option.fontMetrics.horizontalAdvance(text) + 24)
        if width > 0:
            badge = QRectF(option.rect.left() + 6, option.rect.center().y() - 13, width, 26)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(brush)
            painter.drawRoundedRect(badge, 6, 6)
            painter.setPen(QColor(COLORS['text']))
            painter.setFont(option.font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


STYLE = '''
QWidget { color: #f0f0f0; font-size: 14px; }
QMainWindow, QDialog, QMessageBox, QInputDialog, QWidget#settingsContent { background: #191919; }
QTabWidget::pane { border: 0; background: transparent; }
QTabBar::tab {
    background: transparent; color: #a9a9a9; padding: 9px 20px 10px;
    margin-right: 6px; border: 0; border-bottom: 2px solid transparent;
}
QTabBar::tab:hover { color: #f0f0f0; background: #292929; }
QTabBar::tab:selected { color: #f0f0f0; border-bottom: 2px solid #6495ed; font-weight: 600; }
QTabBar::tab:focus { border-bottom: 2px solid #6495ed; }
QWidget#marketPage { background: #232a30; border: 1px solid #606060; border-radius: 14px; }
QWidget#companyPage { background: #252525; border: 1px solid #606060; border-radius: 14px; }
QPushButton {
    min-height: 20px; padding: 6px 13px; border: 1px solid #606060;
    border-radius: 8px; background: #303030; color: #f0f0f0;
}
QPushButton:hover { background: #3a3a3a; border-color: #858585; }
QPushButton:pressed { background: #242424; border-color: #6495ed; }
QPushButton:focus { border-color: #6495ed; }
QPushButton:disabled { color: #777777; background: #282828; border-color: #484848; }
QPushButton#primary { background: #376fe0; border-color: #5483e5; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #477ee9; border-color: #8eb1fa; }
QPushButton#primary:pressed { background: #2d5dbb; }
QPushButton#primary:disabled { color: #777777; background: #30343b; border-color: #484c53; }
QPushButton#link { color: #b9cefa; text-align: left; padding: 6px 10px; }
QPushButton#cellAction { margin: 3px 5px; padding: 4px 9px; min-height: 20px; }
QPushButton#tableLink, QPushButton#textLink {
    background: transparent; color: #b9cefa; border: 1px solid transparent;
    border-radius: 6px; text-align: left; padding: 5px 8px;
}
QPushButton#tableLink:hover, QPushButton#textLink:hover { background: #354049; color: #e2ecff; }
QPushButton#tableLink:focus, QPushButton#textLink:focus { border-color: #6495ed; }
QPushButton#tableLink:disabled, QPushButton#textLink:disabled { color: #8a8a8a; }
QPushButton::menu-indicator { subcontrol-position: right center; right: 8px; }
QPushButton#more { padding-right: 26px; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QListWidget {
    min-height: 20px; background: #303030; color: #f0f0f0;
    border: 1px solid #606060; border-radius: 8px; padding: 6px 9px;
    selection-background-color: #405b7d; selection-color: #ffffff;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QPlainTextEdit:hover, QListWidget:hover { border-color: #858585; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QListWidget:focus { border-color: #6495ed; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPlainTextEdit:disabled { color: #777777; background: #292929; border-color: #484848; }
QComboBox { padding-right: 30px; }
QComboBox QLineEdit { background: transparent; border: 0; padding: 0; min-height: 20px; }
QComboBox::drop-down { width: 26px; border: 0; }
QComboBox QAbstractItemView {
    background: #292929; color: #f0f0f0; border: 1px solid #606060;
    selection-background-color: #40556d; selection-color: #ffffff; outline: 0;
}
QComboBox QAbstractItemView::item { min-height: 28px; padding: 3px 8px; }
QSpinBox { padding-right: 25px; }
QSpinBox::up-button, QSpinBox::down-button { width: 21px; border: 0; background: #393939; }
QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; border-top-right-radius: 7px; }
QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; border-bottom-right-radius: 7px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #4a4a4a; }
QTableWidget {
    border: 1px solid #606060; border-radius: 10px; background: #252525;
    alternate-background-color: #292929; gridline-color: #3a3a3a;
    color: #f0f0f0; selection-background-color: #334c63; selection-color: #ffffff;
}
QTableWidget:focus { border-color: #6495ed; }
QTableWidget::item { padding: 7px 9px; border: 0; }
QTableWidget::item:hover { background: #333b42; }
QTableWidget::item:selected { background: #334c63; color: #ffffff; }
QTableCornerButton::section, QHeaderView::section {
    background: #32363a; color: #dedede; font-size: 13px; font-weight: 600;
    padding: 9px 10px; border: 0; border-bottom: 1px solid #606060;
}
QHeaderView::section:horizontal { border-right: 1px solid #41464a; }
QGroupBox {
    background: #252525; border: 1px solid #606060; border-radius: 11px;
    margin-top: 0; padding: 26px 13px 13px;
}
QGroupBox::title {
    subcontrol-origin: border; subcontrol-position: top left;
    top: 10px; left: 15px; padding: 0; background: transparent; color: #f0f0f0;
    font-size: 15px; font-weight: 600;
}
QGroupBox#aiCard { background: #25252c; }
QGroupBox#senderCard { background: #292929; }
QGroupBox#mailCard { background: #292725; }
QGroupBox#searchCard { background: #242b28; }
QGroupBox#sourceCard { background: #292f2b; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #858585; border-radius: 4px; background: #303030; }
QCheckBox::indicator:hover { border-color: #b0b0b0; }
QCheckBox:focus::indicator { border-color: #6495ed; }
QCheckBox::indicator:checked { background: #376fe0; border: 1px solid #6495ed; }
QCheckBox::indicator:disabled { background: #292929; border-color: #484848; }
QLabel#muted { color: #b5b5b5; font-size: 13px; }
QLabel#title { font-size: 21px; font-weight: 600; color: #f0f0f0; }
QLabel#dialogTitle { font-size: 18px; font-weight: 600; color: #f0f0f0; }
QLabel#notice { background: #283844; color: #e3edf6; padding: 9px 12px; border: 1px solid #526b7e; border-radius: 8px; }
QLabel#notice[tone="success"] { background: #263a31; color: #d8eddf; border-color: #567963; }
QLabel#notice[tone="warning"] { background: #3a3023; color: #f2d4a0; border-color: #80694a; }
QLabel#notice[tone="error"] { background: #3b2829; color: #f2d4d4; border-color: #865a5a; }
QLabel#warningNotice { background: #3a3023; color: #f2d4a0; padding: 9px 12px; border: 1px solid #80694a; border-radius: 8px; }
QStatusBar { color: #a9a9a9; background: #1f1f1f; border-top: 1px solid #454545; padding: 3px; }
QStatusBar::item { border: 0; }
QScrollArea { background: #191919; border: 0; }
QMenu { background: #292929; color: #f0f0f0; border: 1px solid #606060; padding: 5px; }
QMenu::item { padding: 8px 24px 8px 12px; border-radius: 5px; }
QMenu::item:selected { background: #40556d; color: #ffffff; }
QMenu::item:disabled { color: #777777; }
QToolTip { background: #30343a; color: #f0f0f0; border: 1px solid #858585; padding: 6px; }
QScrollBar:vertical { background: #202020; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #626262; min-height: 28px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #858585; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: #202020; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #626262; min-width: 28px; border-radius: 5px; }
QScrollBar::handle:horizontal:hover { background: #858585; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QProgressBar { border: 1px solid #606060; border-radius: 4px; background: #292929; max-height: 8px; }
QProgressBar::chunk { background: #6495ed; border-radius: 3px; }
'''

_icons = Path(__file__).parent
STYLE += '\nQComboBox::down-arrow, QSpinBox::down-arrow { image: url("' + (_icons / 'arrow-down.svg').as_posix() + '"); width: 10px; height: 6px; }'
STYLE += '\nQSpinBox::up-arrow { image: url("' + (_icons / 'arrow-up.svg').as_posix() + '"); width: 10px; height: 6px; }'
STYLE += '\nQCheckBox::indicator:checked { image: url("' + (_icons / 'check.svg').as_posix() + '"); }'
