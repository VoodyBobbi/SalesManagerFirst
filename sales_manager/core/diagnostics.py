"""Local diagnostics without exception messages, request data or traceback locals."""
import logging
import re
import sqlite3
import traceback
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx

logger = logging.getLogger('sales_manager.diagnostics')
logger.propagate = False
logger.addHandler(logging.NullHandler())


def configure_diagnostics(root):
    if any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        return
    directory = root / 'logs'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(directory / 'app.log', maxBytes=5 * 1024 * 1024,
                                      backupCount=2, encoding='utf-8')
    except OSError:
        return
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def log_exception(context, exc, *, run_id=None, company_id=None):
    """Record a traceback without dynamically supplied text or source lines."""
    error_id = uuid.uuid4().hex[:8].upper()
    seen = set()
    traces = []
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        frames = '\n'.join(f'  {Path(frame.f_code.co_filename).name}:{line} ({frame.f_code.co_name})'
                           for frame, line in traceback.walk_tb(exc.__traceback__))
        traces.append(f'Traceback ({type(exc).__name__})\n{frames}')
        exc = exc.__cause__ or exc.__context__
    logger.error('%s · %s · run_id=%s company_id=%s\n%s', error_id, context,
                 run_id if type(run_id) is int else '—', company_id if type(company_id) is int else '—',
                 '\n'.join(traces))
    return error_id


def humanize_error(exc, error_id):
    if isinstance(exc, sqlite3.Error):
        return f'Ошибка локальной базы данных. Код ошибки: {error_id} (подробности в журнале).'
    if isinstance(exc, (httpx.RequestError, ConnectionError, TimeoutError)):
        return f'Нет соединения. Проверьте подключение и повторите. Код ошибки: {error_id} (подробности в журнале).'
    if isinstance(exc, (ValueError, RuntimeError)):
        message = str(exc)
        if re.search('[А-Яа-яЁё]', message):
            return message[:500]
    return f'Операция не завершена. Код ошибки: {error_id} (подробности в журнале).'
