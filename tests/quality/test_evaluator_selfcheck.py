"""The quality calculator must distinguish success, failure, regression and omissions."""
import json
from types import SimpleNamespace

from . import runner
from .runner import compute_metrics, compare_metrics


def test_stable_pass_and_unstable_case_are_distinct():
    runs = [
        {'test_A': 'passed', 'test_B': 'passed'},
        {'test_A': 'passed', 'test_B': 'failed'},
        {'test_A': 'passed', 'test_B': 'passed'},
    ]
    result = compute_metrics(runs)
    assert result['test_A']['successes'] == 3
    assert result['test_A']['pass_at_k'] is True
    assert result['test_A']['pass_pow_k'] is True
    assert result['test_B']['successes'] == 2
    assert result['test_B']['pass_at_k'] is True
    assert result['test_B']['pass_pow_k'] is False
    assert abs(result['test_B']['success_rate']-2/3) < 1e-9


def test_skipped_case_never_becomes_success():
    result = compute_metrics([{'case': 'skipped'}])['case']
    assert result['successes'] == 0
    assert result['available_runs'] == 0
    assert result['success_rate'] is None
    assert result['pass_at_k'] is None
    assert result['pass_pow_k'] is None


def test_missing_test_case_counts_as_failure_in_stability():
    result = compute_metrics([{'case': 'passed'}, {}])['case']
    assert result['runs'] == 2
    assert result['successes'] == 1
    assert result['pass_pow_k'] is False


def test_regression_detection_compares_scenario_by_scenario():
    baseline = {'ok': {'success_rate': 1.0}, 'weakened': {'success_rate': 1.0}}
    current = {'ok': {'success_rate': 1.0}, 'weakened': {'success_rate': 0.5}, 'new': {'success_rate': 1.0}}
    delta = compare_metrics(current, baseline)
    assert delta['regressions'] == ['weakened']
    assert delta['new_tests'] == ['new']
    assert delta['missing_tests'] == []


def test_incomplete_baseline_is_not_comparable():
    current = compute_metrics([{'case': 'passed'}, {'case': 'passed'}])
    baseline = compute_metrics([{'case': 'passed'}, {'case': 'skipped'}])
    assert compare_metrics(current, baseline)['not_comparable'] == ['case']


def test_missing_fresh_xml_cannot_reuse_previous_green_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'REPORTS', tmp_path)
    previous = tmp_path / 'run_01.xml'
    previous.write_text('<testsuite><testcase classname="old" name="green"/></testsuite>', encoding='utf-8')
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=2, stdout='Synthetic collection failure', stderr=''))
    states, detail = runner.run_once(1, 10)
    assert not previous.exists()
    assert states == {'__runner_failed_to_produce_junit__': 'error'}
    assert detail['exit_code'] == 2


def test_collection_errors_are_reported_even_without_testcases(tmp_path):
    path = tmp_path / 'collection.xml'
    path.write_text('<testsuites><testsuite errors="1"/></testsuites>', encoding='utf-8')
    assert runner.parse_junit(path) == {'__pytest_collection_or_setup_error__': 'error'}


def test_failed_run_cannot_replace_baseline(tmp_path, monkeypatch):
    baseline = tmp_path / 'baseline.json'
    baseline.write_text('{"preserved": true}', encoding='utf-8')
    monkeypatch.setattr(runner, 'REPORTS', tmp_path)
    monkeypatch.setattr(runner, 'BASELINE', baseline)
    monkeypatch.setattr(runner, 'run_once', lambda *a: ({'case': 'failed'}, {'exit_code': 1}))
    assert runner.main(['--save-baseline']) == 1
    assert json.loads(baseline.read_text(encoding='utf-8')) == {'preserved': True}
