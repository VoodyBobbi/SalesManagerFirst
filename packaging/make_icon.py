"""Convert the supplied application artwork to a Windows multi-resolution icon."""
import struct
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter


def make_icon():
    assets = Path(__file__).resolve().parents[1] / 'sales_manager' / 'assets'
    original = QImage(str(assets / 'app.png'))
    if original.isNull():
        raise RuntimeError('Application artwork is missing')
    sizes = (16, 24, 32, 48, 64, 128, 256)
    chunks = []
    for size in sizes:
        canvas = QImage(size, size, QImage.Format.Format_ARGB32)
        canvas.fill(Qt.GlobalColor.transparent)
        scaled = original.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        painter = QPainter(canvas)
        painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not canvas.save(buffer, 'PNG'):
            raise RuntimeError('Cannot encode icon')
        chunks.append(bytes(data))
    offset = 6 + 16 * len(sizes)
    header = bytearray(struct.pack('<HHH', 0, 1, len(sizes)))
    for size, chunk in zip(sizes, chunks):
        header.extend(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32,
                                  len(chunk), offset))
        offset += len(chunk)
    (assets / 'app.ico').write_bytes(bytes(header) + b''.join(chunks))


if __name__ == '__main__':
    make_icon()
