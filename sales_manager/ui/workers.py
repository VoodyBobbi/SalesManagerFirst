"""Small Qt worker wrapper; all callbacks are delivered on the UI thread."""
from PySide6.QtCore import QObject, QRunnable, Signal, Slot
from sales_manager.core.diagnostics import humanize_error, log_exception


class WorkerSignals(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function, cleanup=None, *, context='Фоновая операция', run_id=None, company_id=None):
        super().__init__()
        self.signals = WorkerSignals()
        self.function = function
        self.cleanup = cleanup
        self.context = context
        self.run_id = run_id
        self.company_id = company_id

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
            error_id = log_exception(self.context, exc, run_id=self.run_id, company_id=self.company_id)
            self.signals.failed.emit(humanize_error(exc, error_id))
        finally:
            self.signals.finished.emit()
