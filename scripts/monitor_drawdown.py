#!/usr/bin/env python3
"""
Monitor bot total session PnL (realized + unrealized) and stop the bot if drawdown exceeds a threshold.

This is a safety helper for unattended runs.

Usage:
  python3 scripts/monitor_drawdown.py --threshold 10 --interval 30

Notes:
  - Unrealized PnL is read from log lines containing "TOTAL:".
  - Realized PnL is computed from SQLite (`data/trades.db`) for trades closed since the run started.
  - Kill-switch triggers if (peak_total_pnl - current_total_pnl) > threshold.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path
from datetime import datetime
import sqlite3
from typing import Optional, Tuple


TOTAL_RE = re.compile(r"TOTAL:\s*(?P<rest>.*)$")
PNL_RE = re.compile(r"PnL:\s*\$\s*(?P<pnl>[-+]?\d+(?:\.\d+)?)")
LOG_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+-\s+")


def latest_run_log(logs_dir: Path) -> Optional[Path]:
    candidates = sorted(logs_dir.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def extract_total_pnl(line: str) -> Optional[float]:
    m = TOTAL_RE.search(line)
    if not m:
        return None
    try:
        pm = PNL_RE.search(m.group("rest"))
        if not pm:
            return None
        return float(pm.group("pnl"))
    except ValueError:
        return None


def tail_last_total_line(log_path: Path) -> Optional[str]:
    try:
        # Fast path: grep for TOTAL and take last line
        proc = subprocess.run(
            ["bash", "-lc", f"grep -n 'TOTAL:' '{str(log_path)}' | tail -1"],
            capture_output=True,
            text=True,
            check=False,
        )
        out = proc.stdout.strip()
        if not out:
            return None
        # Strip leading "line_no:" prefix
        parts = out.split(":", 1)
        return parts[1] if len(parts) == 2 else out
    except Exception:
        return None


def parse_run_start_ts(log_path: Path) -> Optional[datetime]:
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(50):  # first few lines are enough
                line = f.readline()
                if not line:
                    break
                m = LOG_TS_RE.match(line.strip())
                if m:
                    return datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
        return None
    except Exception:
        return None


def realized_pnl_since(db_path: Path, since: datetime) -> float:
    try:
        if not db_path.exists():
            return 0.0
        # exit_time is stored as ISO strings; filter in Python for robustness.
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT pnl, exit_time FROM trades ORDER BY exit_time DESC LIMIT 5000")
            total = 0.0
            for pnl, exit_time in cur.fetchall():
                if not exit_time:
                    continue
                try:
                    # handle "2026-01-01T18:36:06.958791" or similar
                    exit_dt = datetime.fromisoformat(str(exit_time).replace("Z", "+00:00"))
                except Exception:
                    continue
                if exit_dt >= since:
                    total += float(pnl or 0.0)
            return total
    except Exception:
        return 0.0


def stop_bot() -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["bash", "-lc", "pkill -f \"python3 -m src\\.main\" || pkill -f \"python.*src\\.main\""],
            capture_output=True,
            text=True,
            check=False,
        )
        ok = proc.returncode == 0
        msg = (proc.stdout + proc.stderr).strip()
        return ok, msg
    except Exception as e:
        return False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=10.0, help="Max allowed drawdown in USD (peak - current)")
    ap.add_argument("--interval", type=float, default=30.0, help="Polling interval in seconds")
    ap.add_argument("--logs-dir", type=str, default="logs", help="Logs directory")
    args = ap.parse_args()

    logs_dir = Path(args.logs_dir)
    peak: Optional[float] = None
    last_seen: Optional[float] = None

    while True:
        log_path = latest_run_log(logs_dir)
        if not log_path:
            time.sleep(args.interval)
            continue
        run_start = parse_run_start_ts(log_path)

        line = tail_last_total_line(log_path)
        if not line:
            time.sleep(args.interval)
            continue

        unrealized = extract_total_pnl(line)
        if unrealized is None:
            time.sleep(args.interval)
            continue

        realized = realized_pnl_since(Path("data/trades.db"), run_start) if run_start else 0.0
        total_pnl = realized + unrealized

        # Update peak
        if peak is None or total_pnl > peak:
            peak = total_pnl
        last_seen = total_pnl

        drawdown = (peak - total_pnl) if peak is not None else 0.0
        if drawdown > args.threshold:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"[{ts}] DRAWNDOWN TRIGGER: peak_total={peak:.2f}, current_total={total_pnl:.2f}, dd={drawdown:.2f} > {args.threshold:.2f}. "
                f"(realized={realized:.2f}, unrealized={unrealized:.2f}) Stopping bot..."
            )
            ok, msg = stop_bot()
            if msg:
                print(msg)
            return 0 if ok else 2

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())


