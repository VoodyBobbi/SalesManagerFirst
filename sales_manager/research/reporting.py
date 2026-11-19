"""Durable, user-facing market totals; transport details belong to diagnostics."""
import json

from .market import market_ranking
from .registry import market_source_names


def market_summary(store, run_id, countries, status=None):
    run = store.one('SELECT * FROM job_runs WHERE id=?', (run_id,)) if run_id else None
    supported = set(market_source_names())
    responded = {row['provider'] for row in store.query(
        'SELECT provider FROM job_source_responses WHERE run_id=?', (run_id,))} if run else set()
    used = len(responded & supported)
    count = len(market_ranking(store, countries, limit=10)['rows'])
    pending = store.one("SELECT COUNT(*) n FROM job_queue WHERE run_id=? AND status!='done'", (run_id,))['n'] if run else 0
    state = status or (run['status'] if run else 'idle')
    if pending and state in ('complete', 'complete_partial'):
        state = 'interrupted'
    if state == 'running':
        message = f'Идёт исследование. Найдено товаров: {count} из 10. Использовано источников: {used}/{len(supported)}.'
    elif state in ('paused', 'interrupted', 'failed'):
        state = 'interrupted' if state == 'paused' else state
        message = 'Работа прервана. Незавершённое исследование сохранено для восстановления.'
    elif state in ('complete', 'complete_partial'):
        state = 'complete'
        exhausted = count < 10 or (run and run['reason'] == 'sources_exhausted')
        message = 'Поиск завершён. ' + ('Доступные источники исчерпаны. ' if exhausted else '')
        message += f'Найдено товаров: {count}.'
        message += f' Использовано источников: {used}/{len(supported)}.'
    else:
        message = ''
    return dict(run_id=run_id, status=state, count=count, sources_used=used,
                sources_total=len(supported), successful_sources=sorted(responded),
                pending=pending, message=message)
