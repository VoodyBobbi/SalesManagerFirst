"""Foreground activity stays centered, responsive and safe for dialog actions."""
import json
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from test_ui_workflow import window
from sales_manager.storage import now


@pytest.mark.parametrize('fail', [False, True])
def test_busy_card_lifecycle_geometry_stage_and_failure(window, qtbot, fail):
    gate = threading.Event()
    def operation(emit):
        emit('Изучаем сайты и собираем контакты')
        gate.wait(3)
        if fail:
            raise ValueError('Источник недоступен. Повторите позднее.')
        return {'status': 'complete'}
    errors = []
    window.submit(operation, failed=errors.append, busy_message='Ищем компании, которым может подойти товар')
    overlay = window.busy_overlay
    try:
        qtbot.waitUntil(lambda: overlay.message.text() == 'Изучаем сайты и собираем контакты')
        assert overlay.isVisible() and not window.tabs.isEnabled()
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert .22 < overlay.width() * overlay.height() / (window.width() * window.height()) < .28
        center = window.mapToGlobal(window.rect().center())
        assert (overlay.geometry().center() - center).manhattanLength() <= 2
        first = overlay.spinner.step
        qtbot.waitUntil(lambda: overlay.spinner.step != first)
        window.resize(1100, 720)
        qtbot.waitUntil(lambda: overlay.width() == round(window.width() * .52))
        center = window.mapToGlobal(window.rect().center())
        assert (overlay.geometry().center() - center).manhattanLength() <= 2
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not window.workers)
    assert overlay.isHidden() and not overlay.spinner.timer.isActive()
    assert window.tabs.isEnabled()
    assert bool(errors) is fail


def test_busy_card_blocks_modal_dialog_actions_and_releases_owner(window, qtbot):
    owner = QDialog(window)
    qtbot.addWidget(owner)
    owner.show()
    gate = threading.Event()
    calls = []
    window.submit(lambda emit: gate.wait(3), owner=owner, busy_message='Отправляем письмо')
    try:
        assert window.busy_overlay.parentWidget() is window
        assert window.busy_overlay.windowHandle().transientParent() is owner.windowHandle()
        assert not owner.isEnabled()
        assert window.submit(lambda emit: calls.append(True), busy_message='Отправляем письмо') is None
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not window.workers)
    assert owner.isEnabled() and calls == []
    assert window.busy_overlay.parentWidget() is window
    owner.close()


def test_background_mail_is_silent_and_does_not_show_busy_card(window, monkeypatch, qtbot):
    window.store.execute('INSERT INTO mail_accounts(id,email,connected,updated_at) VALUES(1,?,1,?)', ('owner@example.org', now()))
    gate = threading.Event()
    def sync(cancel):
        gate.wait(3)
        return 'Техническая фоновая проверка завершена'
    monkeypatch.setattr(window.mail, 'sync', sync, raising=False)
    window.sync_mail()
    try:
        assert window.busy_overlay.isHidden()
        assert window.tabs.isEnabled()
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not window.workers)
    assert window.action_row.isHidden()
    assert window.mail_busy is False


def test_company_summary_uses_saved_count_and_source_totals(window):
    window.research_kind = 'companies'
    window.research_done({'status': 'complete', 'count': 7, 'reason': 'sources_exhausted',
                          'sources_used': 3, 'sources_total': 12,
                          'message': 'HTTP 429: raw transport detail'})
    assert window.company_research_notice.text() == ('Поиск завершён. Доступные источники исчерпаны. '
                                                     'Найдено компаний: 7. Использовано источников: 3/12.')
    assert window.action_row.isHidden()


def test_configured_sources_in_cooldown_reach_scheduler_and_exhausted_summary(window, monkeypatch, qtbot):
    monkeypatch.setattr(window.research, 'available_sources', lambda kind: [], raising=False)
    monkeypatch.setattr(window.research, 'configured_sources', lambda kind: ['rss'], raising=False)
    monkeypatch.setattr(window.research, 'research_market', lambda *args: {
        'status': 'complete', 'message': 'Поиск завершён. Доступные источники исчерпаны. Найдено товаров: 0.'}, raising=False)
    window.start_market()
    qtbot.waitUntil(lambda: not window.workers)
    assert 'Доступные источники исчерпаны' in window.market_research_notice.text()
    assert window.busy_overlay.isHidden()


def test_interrupted_run_recovers_once_without_continue_button(window, monkeypatch, qtbot):
    run_id = window.store.execute('INSERT INTO job_runs(kind,status,countries,created_at,updated_at) VALUES(?,?,?,?,?)',
                                  ('market', 'interrupted', json.dumps(['Россия'], ensure_ascii=False), now(), now()))
    window.store.execute('INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)', (run_id, 'unfinished', '{}', 'pending'))
    calls = []
    def recover(saved_id, countries, emit, cancel):
        calls.append((saved_id, countries))
        return {'run_id': run_id, 'status': 'interrupted'}
    monkeypatch.setattr(window.research, 'research_market', lambda *args: pytest.fail('Recovery must target the exact saved run'), raising=False)
    monkeypatch.setattr(window.research, 'resume_market', recover, raising=False)
    window.recover_research()
    qtbot.waitUntil(lambda: not window.workers)
    window.recover_research()
    assert calls == [(run_id, ['Россия'])]
    assert window.market_pause.isHidden()
    assert not any('Продолж' in button.text() for button in window.findChildren(type(window.market_start)))
