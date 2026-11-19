"""A small nonblocking activity window shared by foreground operations."""
import math

from PySide6.QtCore import QEvent, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class Spinner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 40)
        self.step = 0
        self.timer = QTimer(self)
        self.timer.setInterval(70)
        self.timer.timeout.connect(self.advance)

    def advance(self):
        self.step = (self.step + 1) % 12
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(20, 20)
        for index in range(12):
            color = QColor(220, 224, 231, 45 + ((index - self.step) % 12) * 17)
            painter.setPen(QPen(color, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            angle = index * math.pi / 6
            painter.drawLine(QPointF(math.cos(angle) * 11, math.sin(angle) * 11),
                             QPointF(math.cos(angle) * 16, math.sin(angle) * 16))


class BusyOverlay(QWidget):
    """Tool ownership keeps the card above modal dialogs without a nested loop."""
    def __init__(self, window):
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus
        super().__init__(window, flags)
        self.main_window = window
        self.setObjectName('busyOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 32, 40, 32)
        layout.addStretch()
        self.spinner = Spinner(self)
        layout.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(24)
        self.message = QLabel(self)
        self.message.setObjectName('busyMessage')
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setStyleSheet('background: transparent; color: #f0f0f0; border: none; font-size: 23px; font-weight: 600;')
        layout.addWidget(self.message)
        self.cancel_callback = None
        self.cancel_button = QPushButton('Отменить поиск', self)
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.hide()
        layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        window.installEventFilter(self)
        self.hide()

    def recenter(self):
        window = self.main_window
        self.resize(round(window.width() * .52), round(window.height() * .48))
        center = window.mapToGlobal(window.rect().center())
        self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)

    def eventFilter(self, watched, event):
        if watched is self.main_window and event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show):
            self.recenter()
        return super().eventFilter(watched, event)

    def start(self, text, owner, cancel=None):
        # Keep QObject ownership with the main window. Native transient
        # ownership places the card above a dialog without tying its lifetime
        # to that dialog's C++ object.
        self.winId()
        owner.winId()
        self.windowHandle().setTransientParent(owner.windowHandle())
        self.message.setText(text)
        self.cancel_callback = cancel
        self.cancel_button.setVisible(cancel is not None)
        self.cancel_button.setEnabled(cancel is not None)
        self.cancel_button.setText('Отменить поиск')
        self.cancel_button.setToolTip('Останавливает исследование после текущего сетевого действия и сохраняет очередь.')
        self.recenter()
        self.spinner.timer.start()
        self.show()
        self.raise_()

    def stop(self):
        self.hide()
        self.spinner.timer.stop()
        self.cancel_callback = None
        self.cancel_button.hide()
        if self.windowHandle():
            self.windowHandle().setTransientParent(self.main_window.windowHandle())

    def _cancel(self):
        callback = self.cancel_callback
        if callback is not None:
            callback()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(128, 133, 143, 220), 1))
        painter.setBrush(QColor(35, 37, 42, 225))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 22, 22)
