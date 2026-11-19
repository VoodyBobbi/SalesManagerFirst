"""Market controls expose durable business results, not provider diagnostics."""
import json
import threading

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from test_ui_workflow import window, MemorySecrets
from sales_manager.research import reporting
from sales_manager.research.registry import market_source_names
from sales_manager.storage import Store, now
from sales_manager.ui import app


def saved_market(window, monkeypatch, status='complete', count=4, pending=False):
    window.countries = ['Россия']
    rows = [dict(id=i + 1, name='Товар ' + str(i + 1), basis='Подтверждённая публикация',
                 source_ids=[i + 1], count=1, demand_percent=None) for i in range(count)]
    ranking = lambda *args, **kwargs: dict(rows=rows, preliminary=True, caption='По данным публикаций')
    monkeypatch.setattr(reporting, 'market_ranking', ranking)
    monkeypatch.setattr(app, 'market_ranking', ranking)
    run_id = window.store.execute('INSERT INTO job_runs(kind,status,countries,created_at,updated_at,message) VALUES(?,?,?,?,?,?)',
                                  ('market', status, json.dumps(window.countries, ensure_ascii=False), now(), now(),
                                   'WEB: HTTP 429; GDELT временное ограничение; ответ не получен'))
    for name in ('rss', 'gdelt'):
        window.store.execute('INSERT INTO job_source_responses(run_id,provider,first_success_at) VALUES(?,?,?)',
                              (run_id, name, now()))
    if pending:
        window.store.execute('INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)',
                              (run_id, 'remaining', '{}', 'pending'))
    return run_id


def test_market_has_no_import_button_and_keeps_three_tabs(window):
    assert not any(button.text() == 'Статистика импорта'
                   for button in window.tabs.widget(0).findChildren(QPushButton))
    assert [window.tabs.tabText(i) for i in range(3)] == ['Рынок', 'Компании', 'Настройки']


def test_bottom_status_bar_stays_hidden_and_old_indicator_is_unused(window, qtbot):
    window.statusBar().showMessage('Телефон скопирован.')
    qtbot.waitUntil(lambda: window.action_notice.text() == 'Телефон скопирован.')
    assert window.statusBar().isHidden()
    assert window.busy_indicator.parentWidget() is window.market_start.parentWidget()
    assert window.busy_indicator.parentWidget() is window.tabs.widget(0)
    assert window.busy_indicator.isHidden()


@pytest.mark.parametrize('status,count,pending,prefix', [
    ('complete', 10, False, 'Поиск завершён.'),
    ('complete', 4, False, 'Поиск завершён.'),
    ('complete_partial', 4, False, 'Поиск завершён.'),
    ('paused', 4, True, 'Работа прервана.'),
    ('complete', 4, True, 'Работа прервана.'),
    ('failed', 4, True, 'Работа прервана.'),
])
def test_restored_market_summary_is_short_and_uses_real_saved_source_count(window, monkeypatch, status, count, pending, prefix):
    run_id = saved_market(window, monkeypatch, status, count, pending)
    window.restore_research_notices()
    text = window.market_research_notice.text()
    assert text.startswith(prefix)
    if not pending:
        assert ('Найдено товаров: ' + str(count)) in text
        assert f'Использовано источников: 2/{len(market_source_names())}' in text
    for technical in ('HTTP', 'WEB:', 'GDELT', '429', 'ответ не получен'):
        assert technical not in text
    assert not window.market_research_notice.isHidden()
    assert 'Продолжите исследование' not in text


def test_research_done_uses_database_summary_over_raw_error_message(window, monkeypatch):
    run_id = saved_market(window, monkeypatch, 'complete', 4)
    window.research_kind = 'market'
    window.guide('Прежнее сообщение')
    window.research_done({'run_id': run_id, 'status': 'complete', 'message': 'GDELT: HTTP 429; WEB недоступен'})
    assert window.market_research_notice.text().startswith('Поиск завершён.')
    assert 'GDELT' not in window.market_research_notice.text()
    assert window.action_row.isHidden()
    assert window.statusBar().isHidden()


def test_market_summary_survives_another_application_instance(window, monkeypatch, qtbot):
    saved_market(window, monkeypatch, 'paused', 4, pending=True)
    window.store.set_setting('research.countries', window.countries)
    window.restore_research_notices()
    expected = window.market_research_notice.text()
    reopened = app.Window(store=Store(window.store.path), secrets=MemorySecrets())
    reopened.timer.stop()
    reopened.startup_mail_timer.stop()
    reopened.refresh_timer.stop()
    qtbot.addWidget(reopened)
    assert reopened.market_research_notice.text() == expected
    assert reopened.statusBar().isHidden()


def test_paused_manual_url_does_not_intercept_regular_company_search(window, monkeypatch):
    product = window.store.ensure_product('Кофе')
    window.open_product(product['id'])
    manual_id = window.store.execute('INSERT INTO job_runs(kind,product_id,status,countries,created_at,updated_at) VALUES(?,?,?,?,?,?)',
        ('manual:website', product['id'], 'paused', json.dumps(window.countries, ensure_ascii=False), now(), now()))
    calls = []
    monkeypatch.setattr(window, 'start_research', lambda *args, **kwargs: calls.append((args, kwargs)))
    window.start_companies()
    assert calls == [(('companies',), {'run_id': None, 'manual_resume': False})]
    assert window.store.one('SELECT status FROM job_runs WHERE id=?', (manual_id,))['status'] == 'paused'


def test_regular_company_search_resumes_its_own_run_even_with_newer_manual_pause(window, monkeypatch):
    product = window.store.ensure_product('Кофе')
    window.open_product(product['id'])
    ids = []
    for kind in ('companies', 'manual:website'):
        ids.append(window.store.execute('INSERT INTO job_runs(kind,product_id,status,countries,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            (kind, product['id'], 'paused', json.dumps(window.countries, ensure_ascii=False), now(), now())))
    calls = []
    monkeypatch.setattr(window, 'start_research', lambda *args, **kwargs: calls.append((args, kwargs)))
    window.start_companies()
    assert calls == [(('companies',), {'run_id': ids[0], 'manual_resume': False})]


@pytest.mark.parametrize('name', ['tavily', 'serper', 'exa', 'firecrawl', 'jina'])
def test_keyed_provider_can_be_verified_without_claiming_free_search(window, name):
    window.secrets.set(name + '_key', 'unverified-test-key')
    window.update_search_usage()
    assert window.source_checks[name].isEnabled()
    assert 'бесплатного' in window.source_checks[name].toolTip()


def test_market_progress_does_not_expose_transport_messages(window, monkeypatch, qtbot):
    started, finish = threading.Event(), threading.Event()

    def search(countries, emit, cancel):
        started.set()
        emit('WEB: HTTP 429 GDELT недоступен')
        finish.wait(3)
        return {'status': 'complete', 'message': 'Поиск завершён частично. Найдено товаров: 0 из 10. Использовано источников: 0/8.'}

    monkeypatch.setattr(window.research, 'research_market', search, raising=False)
    window.start_market()
    try:
        qtbot.waitUntil(started.is_set, timeout=2000)
        assert window.busy_overlay.isVisible()
        assert 'HTTP' not in window.busy_overlay.message.text()
        assert window.statusBar().isHidden()
        texts = [label.text() for label in window.tabs.widget(0).findChildren(QLabel)]
        texts.append(window.action_notice.text())
        assert not any('HTTP 429' in text or 'WEB:' in text for text in texts)
    finally:
        finish.set()
        qtbot.waitUntil(lambda: not window.workers, timeout=5000)

