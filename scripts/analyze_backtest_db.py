
import sqlite3
import pandas as pd
import os
import sys

def analyze_backtest(db_path='data/backtest_results.db'):
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    
    # 1. Strategy Performance
    query = """
    SELECT 
        strategy,
        COUNT(*) as trade_count,
        SUM(case when pnl > 0 then 1 else 0 end) as wins,
        SUM(pnl) as total_pnl,
        AVG(pnl) as avg_pnl,
        AVG(pnl_percentage) as avg_pnl_pct
    FROM trades
    GROUP BY strategy
    ORDER BY total_pnl DESC
    """
    
    df = pd.read_sql_query(query, conn)
    
    if not df.empty:
        df['win_rate'] = (df['wins'] / df['trade_count'] * 100).round(1)
        df['total_pnl'] = df['total_pnl'].round(2)
        df['avg_pnl'] = df['avg_pnl'].round(2)
        df['avg_pnl_pct'] = (df['avg_pnl_pct'] * 100).round(3)
        
        print("\n=== Strategy Performance Analysis ===")
        print(df[['strategy', 'trade_count', 'win_rate', 'total_pnl', 'avg_pnl', 'avg_pnl_pct']].to_string(index=False))
        
        total_pnl = df['total_pnl'].sum()
        total_trades = df['trade_count'].sum()
        print(f"\nTotal PnL: ${total_pnl:.2f} | Total Trades: {total_trades}")
    else:
        print("No trades found in database.")

    # 2. Equity Curve & Drawdown
    query_equity = "SELECT timestamp, equity FROM equity_snapshots ORDER BY timestamp"
    df_eq = pd.read_sql_query(query_equity, conn)
    
    if not df_eq.empty:
        df_eq['timestamp'] = pd.to_datetime(df_eq['timestamp'])
        peak = df_eq['equity'].cummax()
        drawdown = (df_eq['equity'] - peak) / peak
        max_drawdown = drawdown.min() * 100
        
        start_eq = df_eq['equity'].iloc[0]
        end_eq = df_eq['equity'].iloc[-1]
        ret = (end_eq - start_eq) / start_eq * 100
        
        print("\n=== Portfolio Metrics ===")
        print(f"Start Equity: ${start_eq:.2f}")
        print(f"End Equity:   ${end_eq:.2f}")
        print(f"Return:       {ret:.2f}%")
        print(f"Max Drawdown: {max_drawdown:.2f}%")
        
    conn.close()

if __name__ == "__main__":
    analyze_backtest()
