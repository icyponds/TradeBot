
import sqlite3
import pandas as pd
import os

DB_PATH = 'data/trades.db'

def analyze_backtest():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    
    try:
        # Check if table exists
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='backtest_trades'")
        if not cursor.fetchone():
            print("Table 'backtest_trades' not found. Backtest might not have recorded any trades yet.")
            return

        query = """
        SELECT 
            strategy,
            COUNT(*) as trade_count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl) as total_pnl,
            AVG(pnl) as avg_pnl,
            SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_profit,
            ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)) as gross_loss
        FROM backtest_trades
        GROUP BY strategy
        ORDER BY total_pnl DESC
        """
        
        df = pd.read_sql_query(query, conn)
        
        if df.empty:
            print("No trades found in backtest results.")
            return

        # Calculate additional metrics
        df['win_rate'] = (df['wins'] / df['trade_count']) * 100
        df['profit_factor'] = df.apply(lambda row: row['gross_profit'] / row['gross_loss'] if row['gross_loss'] > 0 else 999.0, axis=1)
        
        # Format for display
        display_df = df[['strategy', 'trade_count', 'win_rate', 'total_pnl', 'profit_factor']].copy()
        display_df['win_rate'] = display_df['win_rate'].map('{:.1f}%'.format)
        display_df['total_pnl'] = display_df['total_pnl'].map('${:.2f}'.format)
        display_df['profit_factor'] = display_df['profit_factor'].map('{:.2f}'.format)
        
        print("\n=== Backtest Performance by Strategy ===\n")
        print(display_df.to_string(index=False))
        
        total_pnl = df['total_pnl'].sum()
        total_trades = df['trade_count'].sum()
        print(f"\nTotal PnL: ${total_pnl:.2f} across {total_trades} trades")

    except Exception as e:
        print(f"Error analyzing results: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    analyze_backtest()
