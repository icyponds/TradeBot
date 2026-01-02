#!/usr/bin/env python3
"""
Write periodic snapshots (positions count + TOTAL PnL line) to a file.

Usage:
  python3 scripts/monitor_snapshots.py --interval 60
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path
from datetime import datetime
import re
import sqlite3


def latest_run_log(logs_dir: Path) -> Path | None:
    candidates = sorted(logs_dir.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def last_total_line(log_path: Path) -> str | None:
    proc = subprocess.run(
        ["bash", "-lc", f"grep -n 'TOTAL:' '{str(log_path)}' | tail -1"],
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout.strip()
    if not out:
        return None
    # Strip leading "line_no:"
    parts = out.split(":", 1)
    return parts[1] if len(parts) == 2 else out


LOG_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+-\s+")
PNL_RE = re.compile(r"PnL:\s*\$\s*(?P<pnl>[-+]?\d+(?:\.\d+)?)")


def parse_run_start_ts(log_path: Path) -> datetime | None:
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                m = LOG_TS_RE.match(line.strip())
                if m:
                    return datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
        return None
    except Exception:
        return None


def extract_unrealized_from_total_line(total_line: str) -> float | None:
    m = PNL_RE.search(total_line)
    if not m:
        return None
    try:
        return float(m.group("pnl"))
    except ValueError:
        return None


def realized_pnl_since(db_path: Path, since: datetime) -> float:
    try:
        if not db_path.exists():
            return 0.0
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT pnl, exit_time FROM trades ORDER BY exit_time DESC LIMIT 5000")
            total = 0.0
            for pnl, exit_time in cur.fetchall():
                if not exit_time:
                    continue
                try:
                    exit_dt = datetime.fromisoformat(str(exit_time).replace("Z", "+00:00"))
                except Exception:
                    continue
                if exit_dt >= since:
                    total += float(pnl or 0.0)
            return total
    except Exception:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--logs-dir", type=str, default="logs")
    ap.add_argument("--out", type=str, default="logs/monitor_snapshots.log")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir = Path(args.logs_dir)

    while True:
        log_path = latest_run_log(logs_dir)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if not log_path:
            out_path.write_text(out_path.read_text() + f"[{ts}] No run log found\n" if out_path.exists() else f"[{ts}] No run log found\n")
            time.sleep(args.interval)
            continue

        total = last_total_line(log_path) or "(no TOTAL line yet)"
        run_start = parse_run_start_ts(log_path)
        unrealized = extract_unrealized_from_total_line(total) if "TOTAL:" in total else None
        realized = realized_pnl_since(Path("data/trades.db"), run_start) if run_start else 0.0
        total_session = (realized + unrealized) if unrealized is not None else None

        with out_path.open("a", encoding="utf-8") as f:
            if total_session is not None:
                f.write(f"[{ts}] {log_path.name} | {total} | Realized: {realized:.2f} | TotalSession: {total_session:.2f}\n")
            else:
                f.write(f"[{ts}] {log_path.name} | {total} | Realized: {realized:.2f}\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())


