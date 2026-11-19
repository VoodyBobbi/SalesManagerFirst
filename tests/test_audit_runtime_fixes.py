"""Recovery and public-page parsing regressions, using isolated local data."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sales_manager.research.safe_http import FetchResult, SafeFetcher, RobotsRules
from sales_manager.research.service import Paused, ResearchService
from sales_manager.storage import Store, now


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
