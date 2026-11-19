"""Standalone project QA runner. Safe by default: all live I/O is blocked by tests.

Invoke from the project root: python tests/quality/runner.py --repeats 3
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

PACKAGE = Path(__file__).resolve().parent
PROJECT = PACKAGE.parents[1]
REPORTS = PACKAGE / 'reports'
BASELINE = REPORTS / 'baseline.json'
VALID = {'passed', 'failed', 'error', 'skipped', 'missing'}


def compute_metrics(runs: list[dict[str, str]]) -> dict[str, dict]:
    """Calculate k-run metrics. Skips are unavailable, not successes.

    Missing cases count against stability and are always reported as incomplete.
    """
    if not runs:
        return {}
    names = sorted({key for run in runs for key in run})
    result = {}
    for name in names:
        states = [run.get(name, 'missing') for run in runs]
        successes = states.count('passed')
        available_runs = sum(state in ('passed', 'failed', 'error') for state in states)
        complete = all(state in ('passed', 'failed', 'error') for state in states)
        result[name] = {
            'runs': len(runs),
            'available_runs': available_runs,
            'successes': successes,
            'status_counts': dict(Counter(states)),
            'success_rate': successes / available_runs if available_runs else None,
            'pass_at_k': (successes > 0) if complete else None,
            'pass_pow_k': (successes == len(runs)) if complete else (False if 'missing' in states else None),
            'complete': complete,
        }
    return result


def compare_metrics(current: dict, baseline: dict) -> dict:
    """Compare same testcase IDs. Missing/new tests remain visible, never discarded."""
    shared = current.keys() & baseline.keys()
    regressions = []
    improvements = []
    not_comparable = []
    for key in shared:
        a = current[key].get('success_rate')
        b = baseline[key].get('success_rate')
        if (a is None or b is None or not current[key].get('complete', True)
                or not baseline[key].get('complete', True)
                or (current[key].get('runs') is not None and baseline[key].get('runs') is not None
                    and current[key]['runs'] != baseline[key]['runs'])):
            not_comparable.append(key)
        elif a < b - 1e-12:
            regressions.append(key)
        elif a > b + 1e-12:
            improvements.append(key)
    return {'regressions': sorted(regressions),
            'improvements': sorted(improvements),
            'not_comparable': sorted(not_comparable),
            'new_tests': sorted(current.keys() - baseline.keys()),
            'missing_tests': sorted(baseline.keys() - current.keys())}


def parse_junit(path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    result = {}
    for node in root.iter('testcase'):
        identity = (node.get('classname') or '') + '::' + (node.get('name') or '')
        status = 'passed'
        for kind in ('failure', 'error', 'skipped'):
            if node.find(kind) is not None:
                status = 'failed' if kind == 'failure' else kind
                break
        result[identity] = status
    # Collection errors or setup errors can be present in testsuite's aggregate
    # even when no testcase is emitted; do not report a false success.
    suite_errors = sum(int(n.get('errors', 0)) for n in root.iter('testsuite'))
    if suite_errors and not any(v == 'error' for v in result.values()):
        result['__pytest_collection_or_setup_error__'] = 'error'
    return result


def safe_write(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding='utf-8')


def run_once(index: int, timeout: int) -> tuple[dict[str, str], dict]:
    xml_path = REPORTS / f'run_{index:02d}.xml'
    cmd = [sys.executable, '-m', 'pytest', '-c', str(PROJECT / 'pytest.ini'),
           str(PACKAGE), '-q', '-ra', '-p', 'no:cacheprovider',
           f'--basetemp={PROJECT / "build" / f"quality-temp-{index:02d}"}',
           f'--junitxml={xml_path}']
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
           'QT_QPA_PLATFORM': 'offscreen', 'PYTHONIOENCODING': 'utf-8'}
    try:
        # A failed invocation must never reuse a previous run's green XML.
        xml_path.unlink(missing_ok=True)
        p = subprocess.run(cmd, cwd=PROJECT, env=env, capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=timeout)
        log = (p.stdout or '') + '\n' + (p.stderr or '')
        safe_write(REPORTS / f'run_{index:02d}.log', log)
        if not xml_path.exists():
            return {'__runner_failed_to_produce_junit__': 'error'}, {'exit_code': p.returncode, 'log': str(REPORTS / f'run_{index:02d}.log')}
        statuses = parse_junit(xml_path)
        if not statuses:
            statuses['__no_tests_collected__'] = 'error'
        # pytest uses 0 when green, 1 assertion failures, 2 interruption, 3 internal
        # error, 4 usage error, 5 no tests. XML alone might hide invocation errors.
        if p.returncode not in (0, 1):
            statuses[f'__pytest_process_error_run_{index}__'] = 'error'
        return statuses, {'exit_code': p.returncode, 'log': str(REPORTS / f'run_{index:02d}.log')}
    except subprocess.TimeoutExpired as exc:
        safe_write(REPORTS / f'run_{index:02d}.log', f'Pytest exceeded {timeout} seconds.\n{exc}')
        return {f'__timeout_run_{index}__': 'error'}, {'exit_code': 'timeout', 'log': str(REPORTS / f'run_{index:02d}.log')}
    except (OSError, ET.ParseError) as exc:
        safe_write(REPORTS / f'run_{index:02d}.log', str(exc))
        return {f'__runner_error_run_{index}__': 'error'}, {'exit_code': 'error', 'log': str(REPORTS / f'run_{index:02d}.log')}


def make_markdown(payload: dict) -> str:
    metrics = payload['metrics']
    count = len(metrics)
    passed = sum(bool(v['pass_pow_k']) for v in metrics.values())
    flaky = sum(v['successes'] not in (0, v['runs']) and v['complete'] for v in metrics.values())
    incomplete = sum(not v['complete'] for v in metrics.values())
    lines = ['# Отчёт tests/quality', '',
             f"**Дата UTC:** {payload['timestamp']}",
             f"**Проект:** {payload['project']}",
             f"**Повторов:** {payload['repeats']}",
             f"**Тестовых случаев:** {count}",
             f"**Стабильно успешных:** {passed}/{count}",
             f"**Нестабильных:** {flaky}",
             f"**Неполных / пропущенных:** {incomplete}", '',
             '> Важно: пропуски, ошибки сбора тестов и отсутствие зависимостей не засчитываются в успех.', '',
             '## Результаты', '',
             '| Сценарий | Успехи/доступные запуски | pass@k | pass^k | Статус |',
             '|---|---:|---|---|---|']
    for name, item in sorted(metrics.items()):
        states = item['status_counts']
        if not item['complete']:
            status = 'НЕПОЛНЫЙ' if states.get('missing') else 'ПРОПУЩЕН'
        elif item['pass_pow_k']:
            status = 'OK'
        elif item['successes']:
            status = 'НЕСТАБИЛЕН'
        else:
            status = 'ОШИБКА'
        def mark(v):
            return '—' if v is None else ('да' if v else 'нет')
        lines.append(f"| `{name.replace('|', '&#124;').replace('\n', r'\n').replace('\r', r'\r')}` | {item['successes']}/{item['available_runs']} | {mark(item['pass_at_k'])} | {mark(item['pass_pow_k'])} | {status} |")
    delta = payload.get('comparison')
    if delta is not None:
        lines += ['', '## Сравнение с эталоном', '',
                  f"- Ухудшения: {len(delta['regressions'])}",
                  f"- Улучшения: {len(delta['improvements'])}",
                  f"- Новые тесты: {len(delta['new_tests'])}",
                  f"- Исчезнувшие тесты: {len(delta['missing_tests'])}",
                  f"- Не сравнимые: {len(delta['not_comparable'])}"]
        for key in ('regressions', 'missing_tests', 'not_comparable'):
            for name in delta[key]:
                lines.append(f'- **{key}**: `{name}`')
    lines += ['', '## Логи', '',
              'По каждому прогону в `reports/run_XX.log` доступны причины провалов;',
              'файлы `run_XX.xml` содержат машинно-читаемые результаты.', '',
              '**Ограничение:** офлайновые тесты с заглушками не подтверждают реальную работу внешних API или качество живых ответов GigaChat.']
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Изолированная проверка качества SALES MANAGER FIRST')
    parser.add_argument('--repeats', type=int, default=1, help='Число независимых прогонов (1-20)')
    parser.add_argument('--timeout', type=int, default=180, help='Максимум секунд на один прогон')
    parser.add_argument('--save-baseline', action='store_true', help='Сохранить эталон только при 100%% успехе')
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 20 or not 10 <= args.timeout <= 3600:
        parser.error('repeats: 1..20, timeout: 10..3600')
    if not (PROJECT / 'sales_manager' / 'storage' / 'store.py').is_file():
        print('ОШИБКА: папка tests/quality должна находиться в проекте рядом с sales_manager.', file=sys.stderr)
        return 2
    REPORTS.mkdir(parents=True, exist_ok=True)
    all_runs, details = [], []
    print(f'ПРОВЕРКА: {PROJECT}\nПовторов: {args.repeats}. Интернет и отправка писем блокируются тестами.', flush=True)
    for i in range(1, args.repeats + 1):
        print(f'Запуск {i}/{args.repeats}...', flush=True)
        states, meta = run_once(i, args.timeout)
        all_runs.append(states)
        details.append(meta)
        counter = Counter(states.values())
        print('  ' + ', '.join(f'{k}: {v}' for k, v in sorted(counter.items())), flush=True)
    metrics = compute_metrics(all_runs)
    comparison = None
    if BASELINE.exists():
        try:
            comparison = compare_metrics(metrics, json.loads(BASELINE.read_text(encoding='utf-8'))['metrics'])
        except (KeyError, ValueError, OSError) as exc:
            print(f'Эталон повреждён или несовместим: {exc}', file=sys.stderr)
            comparison = {'regressions': [], 'improvements': [], 'not_comparable': sorted(metrics),
                          'new_tests': [], 'missing_tests': []}
    payload = {'timestamp': datetime.now(timezone.utc).isoformat(), 'project': PROJECT.name,
               'repeats': args.repeats, 'metrics': metrics,
               'runs': all_runs, 'processes': details, 'comparison': comparison}
    safe_write(REPORTS / 'latest.json', json.dumps(payload, ensure_ascii=False, indent=2))
    safe_write(REPORTS / 'latest.md', make_markdown(payload))
    fully_green = bool(metrics) and all(m['pass_pow_k'] is True for m in metrics.values())
    regression_free = comparison is None or (not comparison['regressions'] and not comparison['missing_tests'] and not comparison['not_comparable'])
    if args.save_baseline:
        if fully_green and regression_free:
            safe_write(BASELINE, json.dumps({'metrics': metrics, 'repeats': args.repeats, 'created_at': payload['timestamp']}, ensure_ascii=False, indent=2))
            print('Эталон сохранён: reports/baseline.json')
        else:
            print('Эталон НЕ сохранён: есть провалы, пропуски или регрессии.')
    print(f"\nОтчёт: {REPORTS / 'latest.md'}")
    print('РЕЗУЛЬТАТ: ' + ('ВСЕ ТЕСТЫ ПРОЙДЕНЫ' if fully_green and regression_free else 'ЕСТЬ ОШИБКИ / ПРОПУСКИ / РЕГРЕССИИ'))
    return 0 if fully_green and regression_free else 1


if __name__ == '__main__':
    raise SystemExit(main())
