"""
Parallel, resumable backtest matrix runner (research round 8).

Each job runs scripts/run_backtest.py in its own process with its own
results DB (--results-db), so N windows x M configs x K seeds can run
concurrently. Results are appended to <out>/results.jsonl (one RESULT_JSON
record per job, tagged); re-running skips tags already recorded.

Usage:
  python scripts/run_matrix.py --out reports/oos_matrix5 --name base_pit \\
      --windows legacy8,fwd3 --workers 8 -- --max-symbols 30 --universe all
  python scripts/run_matrix.py --out reports/oos_matrix5 --name mkr_pit \\
      --windows legacy8,fwd3 --seeds 11,22,33,44,55 --maker-fill-prob 0.7 \\
      -- --max-symbols 30 --trading-param maker_entries.enabled=true
  python scripts/run_matrix.py --out reports/oos_matrix5 --summarize
"""

import argparse
import json
import math
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Windows used by research rounds 4-7 (kept verbatim for comparability)
LEGACY8 = [
    ('nov25', '2025-11-01', '2025-12-01'),
    ('dec25', '2025-12-01', '2026-01-01'),
    ('jan26', '2026-01-01', '2026-02-01'),
    ('feb26', '2026-02-01', '2026-03-01'),
    ('mar26', '2026-03-08', '2026-04-08'),
    ('apr26', '2026-04-08', '2026-05-08'),
    ('may26', '2026-05-08', '2026-06-08'),
    ('jun26', '2026-06-01', '2026-07-01'),
]


def month_windows(first, last):
    """Calendar-month windows from 'YYYY-MM' to 'YYYY-MM' inclusive."""
    y, m = map(int, first.split('-'))
    ly, lm = map(int, last.split('-'))
    out = []
    while (y, m) <= (ly, lm):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        label = date(y, m, 1).strftime('%b%y').lower()
        out.append((label, f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"))
        y, m = ny, nm
    return out


WINDOW_SETS = {
    'legacy8': LEGACY8,
    # Never-seen forward months (data refreshed 2026-10-03)
    'fwd3': month_windows('2026-07', '2026-09'),
    # Extended crypto-only history; needs --bar-resolution 4h (1h is beyond
    # Hyperliquid retention before ~2026-03)
    'ext': month_windows('2024-09', '2025-10'),
}


def resolve_windows(spec):
    windows = []
    for part in [p.strip() for p in spec.split(',') if p.strip()]:
        if part in WINDOW_SETS:
            windows.extend(WINDOW_SETS[part])
        elif ':' in part:  # label:start:end
            label, start, end = part.split(':')
            windows.append((label, start, end))
        else:
            raise ValueError(f"unknown window set {part!r}; choose from {sorted(WINDOW_SETS)} or label:start:end")
    return windows


def build_jobs(name, windows, backtest_args, seeds=None, fill_prob=None):
    jobs = []
    for seed in (seeds or [None]):
        for label, start, end in windows:
            tag = f"{name}_{label}" if seed is None else f"{name}_s{seed}_{label}"
            env = {}
            if seed is not None:
                env['BACKTEST_MAKER_SEED'] = str(seed)
            if fill_prob is not None:
                env['BACKTEST_MAKER_FILL_PROB'] = str(fill_prob)
            jobs.append({'tag': tag, 'name': name, 'seed': seed, 'window': label,
                         'start': start, 'end': end, 'args': list(backtest_args), 'env': env})
    return jobs


def load_results(path):
    results = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    results[rec['tag']] = rec
    return results


def parse_result(output):
    for line in reversed(output.splitlines()):
        if line.startswith('RESULT_JSON: '):
            return json.loads(line[len('RESULT_JSON: '):])
    return None


def run_job(job, out_dir, python=sys.executable):
    db_dir = os.path.join(out_dir, '_dbs')
    os.makedirs(db_dir, exist_ok=True)
    results_db = os.path.join(db_dir, f"{job['tag']}.db")
    if os.path.exists(results_db):
        os.remove(results_db)
    cmd = [python, os.path.join(ROOT, 'scripts', 'run_backtest.py'),
           '--start', job['start'], '--end', job['end'],
           '--results-db', results_db, '--tag', job['tag']] + job['args']
    env = dict(os.environ)
    env.update(job['env'])
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    with open(os.path.join(out_dir, f"{job['tag']}.out"), 'w') as fh:
        fh.write(output)
    rec = parse_result(output)
    if rec is None:
        rec = {'tag': job['tag'], 'error': f"exit {proc.returncode}", 'tail': output[-500:]}
    rec.update({'name': job['name'], 'seed': job['seed'], 'window': job['window']})
    try:
        os.remove(results_db)  # results are in the record; keep disk bounded
    except OSError:
        pass
    return rec


def run_jobs(jobs, out_dir, workers):
    os.makedirs(out_dir, exist_ok=True)
    results_path = os.path.join(out_dir, 'results.jsonl')
    done = load_results(results_path)
    pending = [j for j in jobs if j['tag'] not in done or 'error' in done[j['tag']]]
    print(f"{len(jobs)} jobs, {len(jobs) - len(pending)} already done, running {len(pending)} "
          f"with {workers} workers", flush=True)
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_job, job, out_dir): job for job in pending}
        for fut in as_completed(futures):
            rec = fut.result()
            with lock:
                with open(results_path, 'a') as fh:
                    fh.write(json.dumps(rec) + '\n')
            status = rec.get('error') or f"${rec['pnl']:,.0f} ({rec['trades']}t, DD {rec['max_dd_pct']}%)"
            print(f"  {rec['tag']}: {status}", flush=True)


def stats(values):
    n = len(values)
    if n == 0:
        return {'n': 0, 'total': 0.0, 'mean': 0.0, 'sd': 0.0, 't': 0.0}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return {'n': n, 'total': sum(values), 'mean': mean, 'sd': sd, 't': t}


def summarize(results, names=None):
    """Per config: window means across seeds, then stats over windows."""
    by_name = {}
    for rec in results.values():
        if 'error' in rec or 'pnl' not in rec:
            continue
        if names and rec['name'] not in names:
            continue
        by_name.setdefault(rec['name'], {}).setdefault(rec['window'], []).append(rec['pnl'])
    out = {}
    for name, windows in by_name.items():
        window_means = {w: sum(v) / len(v) for w, v in windows.items()}
        seeds = max(len(v) for v in windows.values())
        out[name] = {'windows': window_means, 'seeds': seeds, **stats(list(window_means.values()))}
    return out


def print_summary(summary):
    for name in sorted(summary):
        s = summary[name]
        cells = ' '.join(f"{w}:{v:+,.0f}" for w, v in s['windows'].items())
        print(f"{name:<28} n={s['n']:<2} seeds={s['seeds']} total={s['total']:+,.0f} "
              f"mean={s['mean']:+,.0f} sd={s['sd']:,.0f} t={s['t']:+.2f}")
        print(f"    {cells}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough = []
    if '--' in argv:
        idx = argv.index('--')
        argv, passthrough = argv[:idx], argv[idx + 1:]
    parser = argparse.ArgumentParser(description='Parallel backtest matrix runner')
    parser.add_argument('--out', required=True, help='Output directory (results.jsonl + per-job .out)')
    parser.add_argument('--name', help='Config name (tag prefix)')
    parser.add_argument('--windows', default='legacy8', help='Window sets / label:start:end, comma-separated')
    parser.add_argument('--seeds', default=None, help='Comma-separated maker seeds (sets BACKTEST_MAKER_SEED)')
    parser.add_argument('--maker-fill-prob', type=float, default=None)
    parser.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument('--summarize', action='store_true', help='Only print the summary of results.jsonl')
    parser.add_argument('--names', default=None, help='Restrict --summarize to these config names')
    args = parser.parse_args(argv)

    results_path = os.path.join(args.out, 'results.jsonl')
    if not args.summarize:
        if not args.name:
            parser.error('--name is required unless --summarize')
        seeds = [int(s) for s in args.seeds.split(',')] if args.seeds else None
        jobs = build_jobs(args.name, resolve_windows(args.windows), passthrough, seeds, args.maker_fill_prob)
        run_jobs(jobs, args.out, args.workers)
    names = set(args.names.split(',')) if args.names else ({args.name} if args.name and not args.summarize else None)
    print_summary(summarize(load_results(results_path), names))


if __name__ == '__main__':
    main()
