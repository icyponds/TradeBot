#!/usr/bin/env python3
"""
Generate weekly trading performance report from trades.db.

Usage:
    python scripts/generate_weekly_report.py [--days N]
    
Generates a markdown report with:
- Strategy performance breakdown
- Symbol profitability analysis
- Daily P&L trends
- Win rate and Sharpe ratio calculations
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def calculate_sharpe(returns: list, risk_free_rate: float = 0.0) -> float:
    """Calculate annualized Sharpe ratio from daily returns."""
    if not returns or len(returns) < 2:
        return 0.0
    
    import numpy as np
    mean_return = np.mean(returns)
    std_return = np.std(returns)
    
    if std_return == 0:
        return 0.0
    
    # Annualize (assuming 365 trading days)
    sharpe = (mean_return - risk_free_rate) / std_return * np.sqrt(365)
    return sharpe


def generate_weekly_report(db_path: str = "data/trades.db", days: int = 7):
    """Generate weekly performance report."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Calculate lookback date
    lookback_date = (datetime.now() - timedelta(days=days)).isoformat()
    
    # Report header
    report_lines = [
        f"# Trading Performance Report",
        f"**Period:** {lookback_date[:10]} to {datetime.now().strftime('%Y-%m-%d')} ({days} days)",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        ""
    ]
    
    # 1. Overall Performance
    overall = cursor.execute("""
        SELECT 
            COUNT(*) as total_trades,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(SUM(fees), 2) as total_fees,
            ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) as win_rate,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(MAX(pnl), 2) as best_trade,
            ROUND(MIN(pnl), 2) as worst_trade
        FROM trades
        WHERE exit_time > ?
    """, (lookback_date,)).fetchone()
    
    report_lines.extend([
        "## Overall Performance",
        "",
        f"- **Total Trades:** {overall[0]}",
        f"- **Net P&L:** ${overall[1]:.2f}",
        f"- **Total Fees:** ${overall[2]:.2f}",
        f"- **Win Rate:** {overall[3]:.1f}%",
        f"- **Avg P&L per Trade:** ${overall[4]:.2f}",
        f"- **Best Trade:** +${overall[5]:.2f}",
        f"- **Worst Trade:** ${overall[6]:.2f}",
        "",
        "---",
        ""
    ])
    
    # 2. Strategy Performance
    strategy_stats = cursor.execute("""
        SELECT 
            strategy,
            COUNT(*) as trades,
            ROUND(SUM(pnl), 2) as pnl,
            ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) as win_rate,
            ROUND(AVG(pnl), 2) as avg_pnl
        FROM trades
        WHERE exit_time > ?
        GROUP BY strategy
        ORDER BY pnl DESC
    """, (lookback_date,)).fetchall()
    
    report_lines.extend([
        "## Strategy Performance",
        "",
        "| Strategy | Trades | P&L | Win Rate | Avg P&L |",
        "|----------|--------|-----|----------|---------|"
    ])
    
    for row in strategy_stats:
        strategy, trades, pnl, win_rate, avg_pnl = row
        pnl_emoji = "✅" if pnl > 0 else "❌"
        report_lines.append(
            f"| {strategy} {pnl_emoji} | {trades} | ${pnl:.2f} | {win_rate:.1f}% | ${avg_pnl:.2f} |"
        )
    
    report_lines.extend(["", "---", ""])
    
    # 3. Symbol Performance (Top 10 winners and losers)
    symbol_stats = cursor.execute("""
        SELECT 
            symbol,
            COUNT(*) as trades,
            ROUND(SUM(pnl), 2) as pnl,
            ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) as win_rate
        FROM trades
        WHERE exit_time > ? AND symbol IS NOT NULL
        GROUP BY symbol
        ORDER BY pnl DESC
        LIMIT 20
    """, (lookback_date,)).fetchall()
    
    report_lines.extend([
        "## Top Symbols",
        "",
        "| Symbol | Trades | P&L | Win Rate |",
        "|--------|--------|-----|----------|"
    ])
    
    for row in symbol_stats[:10]:
        symbol, trades, pnl, win_rate = row
        report_lines.append(f"| {symbol} | {trades} | ${pnl:.2f} | {win_rate:.1f}% |")
    
    if len(symbol_stats) > 10:
        report_lines.extend(["", "### Bottom Symbols", "", "| Symbol | Trades | P&L | Win Rate |", "|--------|--------|-----|----------|"])
        for row in symbol_stats[-5:]:
            symbol, trades, pnl, win_rate = row
            report_lines.append(f"| {symbol} | {trades} | ${pnl:.2f} | {win_rate:.1f}% |")
    
    report_lines.extend(["", "---", ""])
    
    # 4. Daily P&L
    daily_pnl = cursor.execute("""
        SELECT 
            DATE(exit_time) as date,
            COUNT(*) as trades,
            ROUND(SUM(pnl), 2) as pnl
        FROM trades
        WHERE exit_time > ?
        GROUP BY DATE(exit_time)
        ORDER BY date DESC
    """, (lookback_date,)).fetchall()
    
    report_lines.extend([
        "## Daily Performance",
        "",
        "| Date | Trades | P&L |",
        "|------|--------|-----|"
    ])
    
    daily_returns = []
    for row in daily_pnl:
        date, trades, pnl = row
        daily_returns.append(pnl)
        pnl_emoji = "📈" if pnl > 0 else "📉"
        report_lines.append(f"| {date} {pnl_emoji} | {trades} | ${pnl:.2f} |")
    
    # Calculate Sharpe ratio
    sharpe = calculate_sharpe(daily_returns)
    
    report_lines.extend([
        "",
        f"**Daily Sharpe Ratio:** {sharpe:.2f}",
        "",
        "---",
        ""
    ])
    
    # 5. Exit Reasons
    exit_reasons = cursor.execute("""
        SELECT 
            exit_reason,
            COUNT(*) as count,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(pnl), 2) as avg_pnl
        FROM trades
        WHERE exit_time > ? AND exit_reason IS NOT NULL
        GROUP BY exit_reason
        ORDER BY count DESC
        LIMIT 10
    """, (lookback_date,)).fetchall()
    
    report_lines.extend([
        "## Top Exit Reasons",
        "",
        "| Exit Reason | Count | Total P&L | Avg P&L |",
        "|-------------|-------|-----------|---------|"
    ])
    
    for row in exit_reasons:
        reason, count, total_pnl, avg_pnl = row
        report_lines.append(f"| {reason} | {count} | ${total_pnl:.2f} | ${avg_pnl:.2f} |")
    
    report_lines.extend(["", "---", ""])
    
    conn.close()
    
    # Write report
    report_content = "\n".join(report_lines)
    output_dir = project_root / "reports"
    output_dir.mkdir(exist_ok=True)
    
    output_file = output_dir / f"weekly_performance_{datetime.now().strftime('%Y%m%d')}.md"
    output_file.write_text(report_content)
    
    print(f"✅ Report generated: {output_file}")
    print(f"\nQuick Summary:")
    print(f"  Trades: {overall[0]}")
    print(f"  Net P&L: ${overall[1]:.2f}")
    print(f"  Win Rate: {overall[3]:.1f}%")
    print(f"  Sharpe: {sharpe:.2f}")
    
    return output_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate weekly trading performance report")
    parser.add_argument("--days", type=int, default=7, help="Number of days to analyze (default: 7)")
    parser.add_argument("--db", type=str, default="data/trades.db", help="Path to trades database")
    
    args = parser.parse_args()
    
    try:
        generate_weekly_report(db_path=args.db, days=args.days)
    except Exception as e:
        print(f"❌ Error generating report: {e}")
        sys.exit(1)
