"""Small Qt worker wrapper; all callbacks are delivered on the UI thread."""
from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function, cleanup=None):
        super().__init__()
        self.signals = WorkerSignals()
        self.function = function
        self.cleanup = cleanup

    @Slot()
    def run(self):
        try:
            try:
                result = self.function(self.signals.progress.emit)
            finally:
                # Thread-local SQLite connections must be closed on their own
                # worker thread, before completion is reported to the UI.
                if self.cleanup:
                    self.cleanup()
            self.signals.succeeded.emit(result)
        except Exception as exc:
            # No tracebacks or request objects: credentials must never reach UI logs.
            message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'Операция не завершена. Проверьте подключение и повторите.'
            self.signals.failed.emit(message[:500])
        finally:
            self.signals.finished.emit()
