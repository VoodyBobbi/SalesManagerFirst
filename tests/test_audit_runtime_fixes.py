"""Recovery and public-page parsing regressions, using isolated local data."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sales_manager.research.safe_http import FetchError, FetchResult, SafeFetcher, RobotsRules
from sales_manager.research.service import Paused, ResearchService
from sales_manager.storage import Store, now


def test_fetch_dns_deadline_bounds_wait_and_outstanding_resolvers(monkeypatch):
    from sales_manager.research import safe_http

    release = threading.Event()
    slots = threading.BoundedSemaphore(2)
    calls = []
    monkeypatch.setattr(safe_http, '_DNS_SLOTS', slots)

    def blocked(*args):
        calls.append(threading.current_thread())
        assert release.wait(5)
        return ['1.1.1.1']

    monkeypatch.setattr(safe_http, 'public_addresses', blocked)
    fetcher = SafeFetcher(timeout=.1)
    start = time.monotonic()
    try:
        for _ in range(2):
            with pytest.raises(FetchError, match='время определения адреса DNS'):
                fetcher._request('https://example.org/', 1024)
        with pytest.raises(FetchError, match='DNS занята'):
            fetcher._request('https://example.org/', 1024)
        assert len(calls) == 2 and time.monotonic() - start < 1
    finally:
        release.set()
        for thread in calls:
            thread.join(1)
    assert all(not thread.is_alive() for thread in calls)


def test_diagnostics_keeps_chained_stack_locations_without_messages(tmp_path, monkeypatch):
    import logging
    from sales_manager.core import diagnostics

    logger = logging.Logger('test_diagnostics')
    monkeypatch.setattr(diagnostics, 'logger', logger)
    diagnostics.configure_diagnostics(tmp_path)
    try:
        try:
            try:
                raise OSError('Bearer test-secret https://private.example/sensitive')
            except OSError as exc:
                raise RuntimeError('customer data password=test-password') from exc
        except RuntimeError as exc:
            diagnostics.log_exception('Проверка', exc)
        log = (tmp_path / 'logs' / 'app.log').read_text(encoding='utf-8')
        assert 'RuntimeError' in log and 'OSError' in log and 'test_audit_runtime_fixes.py:' in log
        assert 'test-secret' not in log and 'test-password' not in log and 'private.example' not in log
    finally:
        for handler in logger.handlers:
            handler.close()


@pytest.mark.parametrize('declaration', [
    '<meta charset="windows-1251">',
    "<META CHARSET=windows-1251>",
    '<meta content="text/html; charset=windows-1251" http-equiv="Content-Type">',
])
def test_html_without_http_charset_uses_meta(declaration):
    text = '<html><head>' + declaration + '</head><body>Продажи и контакты</body></html>'
    result = FetchResult('https://site.example/', 200, text.encode('cp1251'), 'text/html')
    assert result.text == text


def test_explicit_http_charset_takes_precedence_over_meta():
    text = '<meta charset="windows-1251"><p>Спрос</p>'
    assert FetchResult('https://site.example/', 200, text.encode(), 'text/html', 'utf-8').text == text


@pytest.mark.parametrize('codec', ['utf-8-sig', 'utf-16'])
def test_page_without_http_charset_uses_bom(codec):
    text = '<html>Компания</html>'
    assert FetchResult('https://site.example/', 200, text.encode(codec), 'text/html').text == text


@pytest.mark.parametrize('charset', ['not-a-real-codec', 'idna', 'utf-8\x00'])
def test_invalid_meta_codec_does_not_crash(charset):
    text = '<meta charset="' + charset + '"><p>Компания</p>'
    assert FetchResult('https://site.example/', 200, text.encode(), 'text/html').text == text


def test_long_robots_wait_is_cancelled_before_network(tmp_path, monkeypatch):
    store = Store(tmp_path / 'cancel.sqlite3')
    research = ResearchService(store, object())
    fetcher = research.fetcher
    origin = 'https://site.example'
    fetcher._robots[origin] = RobotsRules(['User-agent: *', 'Crawl-delay: 60'])
    fetcher._robots_checked_at[origin] = time.monotonic()
    fetcher._last_request[origin] = time.monotonic()
    waits = []

    def cancel_during_first_wait(duration):
        waits.append(duration)
        research._cancel.set()

    monkeypatch.setattr('sales_manager.research.safe_http.time.sleep', cancel_during_first_wait)
    monkeypatch.setattr(fetcher, '_request', lambda *a, **kw: pytest.fail('No request after cancellation'))
    try:
        with pytest.raises(Paused):
            fetcher.fetch(origin + '/report')
        assert len(waits) == 1 and waits[0] <= .1
    finally:
        store.close()


def test_waiting_for_robots_lock_can_be_cancelled():
    cancelled = threading.Event()

    def check():
        if cancelled.is_set():
            raise Paused('cancelled')

    fetcher = SafeFetcher(cancel_check=check)
    origin = 'https://site.example'
    lock = fetcher._robots_locks[origin] = threading.RLock()
    lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fetcher._check_robots, origin + '/report')
            cancelled.set()
            with pytest.raises(Paused):
                future.result(timeout=2)
    finally:
        lock.release()


def test_crash_recovery_uses_current_interrupted_state(tmp_path):
    path = tmp_path / 'crash.sqlite3'
    store = Store(path)
    stamp = now()
    run_id = store.execute('INSERT INTO job_runs(kind,status,countries,created_at,updated_at) VALUES(?,?,?,?,?)',
                           ('market', 'running', '["Россия"]', stamp, stamp))
    store.execute('INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)',
                  (run_id, 'read-page', '{}', 'running'))
    store.close()
    recovered = Store(path)
    try:
        run = recovered.one('SELECT * FROM job_runs WHERE id=?', (run_id,))
        assert run['status'] == 'interrupted' and run['reason'] == 'interrupted'
        assert 'автоматически' in run['message']
        assert recovered.one('SELECT status FROM job_queue')['status'] == 'pending'
    finally:
        recovered.close()
