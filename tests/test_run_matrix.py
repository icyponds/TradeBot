"""scripts/run_matrix.py: window sets, job building, resume, summary stats."""
import importlib.util
import json
import os

import pytest

_spec = importlib.util.spec_from_file_location(
    "run_matrix", os.path.join(os.path.dirname(__file__), "..", "scripts", "run_matrix.py"))
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)


def test_month_windows_cross_year():
    assert rm.month_windows('2024-11', '2025-01') == [
        ('nov24', '2024-11-01', '2024-12-01'),
        ('dec24', '2024-12-01', '2025-01-01'),
        ('jan25', '2025-01-01', '2025-02-01'),
    ]


def test_resolve_windows_sets_and_custom():
    w = rm.resolve_windows('legacy8,fwd3,x:2026-01-01:2026-01-08')
    assert len(w) == 12
    assert w[0] == ('nov25', '2025-11-01', '2025-12-01')
    assert w[8] == ('jul26', '2026-07-01', '2026-08-01')
    assert w[-1] == ('x', '2026-01-01', '2026-01-08')
    assert len(rm.resolve_windows('ext')) == 14
    with pytest.raises(ValueError):
        rm.resolve_windows('bogus')


def test_build_jobs_tags_and_seed_env():
    jobs = rm.build_jobs('mkr', [('may26', '2026-05-08', '2026-06-08')], ['--max-symbols', '30'],
                         seeds=[11, 22], fill_prob=0.7)
    assert [j['tag'] for j in jobs] == ['mkr_s11_may26', 'mkr_s22_may26']
    assert jobs[0]['env'] == {'BACKTEST_MAKER_SEED': '11', 'BACKTEST_MAKER_FILL_PROB': '0.7'}
    assert jobs[0]['args'] == ['--max-symbols', '30']
    det = rm.build_jobs('base', [('may26', 'a', 'b')], [])
    assert det[0]['tag'] == 'base_may26' and det[0]['env'] == {}


def test_parse_result_takes_last_json_line():
    out = "noise\nRESULT_JSON: {\"tag\": \"a\", \"pnl\": 1}\nRESULT_JSON: {\"tag\": \"a\", \"pnl\": 2}\n"
    assert rm.parse_result(out)['pnl'] == 2
    assert rm.parse_result("crashed") is None


def test_run_jobs_resumes_and_retries_errors(tmp_path, monkeypatch):
    out = str(tmp_path)
    with open(os.path.join(out, 'results.jsonl'), 'w') as fh:
        fh.write(json.dumps({'tag': 'c_a', 'name': 'c', 'window': 'a', 'pnl': 5.0, 'trades': 1, 'max_dd_pct': 0}) + '\n')
        fh.write(json.dumps({'tag': 'c_b', 'name': 'c', 'window': 'b', 'error': 'exit 1'}) + '\n')
    ran = []

    def fake_run(job, out_dir, python=None):
        ran.append(job['tag'])
        return {'tag': job['tag'], 'name': job['name'], 'seed': None, 'window': job['window'],
                'pnl': 1.0, 'trades': 2, 'max_dd_pct': 1.0}

    monkeypatch.setattr(rm, 'run_job', fake_run)
    jobs = rm.build_jobs('c', [('a', 's', 'e'), ('b', 's', 'e'), ('c', 's', 'e')], [])
    rm.run_jobs(jobs, out, workers=2)
    assert sorted(ran) == ['c_b', 'c_c']  # done job skipped, errored job retried
    results = rm.load_results(os.path.join(out, 'results.jsonl'))
    assert results['c_b']['pnl'] == 1.0  # later record supersedes the error


def test_summary_averages_seeds_then_windows():
    results = {
        'm_s1_a': {'tag': 'm_s1_a', 'name': 'm', 'window': 'a', 'pnl': 100.0},
        'm_s2_a': {'tag': 'm_s2_a', 'name': 'm', 'window': 'a', 'pnl': 300.0},
        'm_s1_b': {'tag': 'm_s1_b', 'name': 'm', 'window': 'b', 'pnl': -100.0},
        'm_s2_b': {'tag': 'm_s2_b', 'name': 'm', 'window': 'b', 'pnl': -100.0},
        'bad': {'tag': 'bad', 'name': 'm', 'window': 'c', 'error': 'x'},
    }
    s = rm.summarize(results)['m']
    assert s['windows'] == {'a': 200.0, 'b': -100.0}
    assert s['seeds'] == 2
    assert s['total'] == 100.0 and s['mean'] == 50.0
    assert s['sd'] == pytest.approx(212.132, rel=1e-4)
    assert s['t'] == pytest.approx(50.0 / (212.132 / 2 ** 0.5), rel=1e-4)


def test_stats_degenerate_cases():
    assert rm.stats([])['n'] == 0
    assert rm.stats([5.0]) == {'n': 1, 'total': 5.0, 'mean': 5.0, 'sd': 0.0, 't': 0.0}
