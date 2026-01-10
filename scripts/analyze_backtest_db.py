import sqlite3
import pandas as pd
import os

DB_PATH = 'data/trades.db'

def analyze_backtest():
    if not os.path.exists(DB_PATH):
        print(f"Error: {DB_PATH} not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    
    try:
        # Check tables
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        print(f"Tables in DB: {tables}")
        
        if 'backtest_trades' not in tables:
            print("Error: 'backtest_trades' table not found.")
            return

        # Query Trades
        df = pd.read_sql_query("SELECT * FROM backtest_trades", conn)
        
        print(f"\n--- Backtest Trades Analysis ---")
        print(f"Total Rows: {len(df)}")
        
        if len(df) == 0:
            print("No trades found.")
            return

        # Calculate Totals
        print(f"Columns: {df.columns.tolist()}")
        
        total_pnl = df['pnl'].sum()
        total_fees = df['fees'].sum() if 'fees' in df.columns else 0
        net_pnl = total_pnl - total_fees
        
        print(f"Total Gross PnL (DB): ${total_pnl:,.2f}")
        print(f"Total Fees (DB): ${total_fees:,.2f}")
        print(f"Total Net PnL (DB): ${net_pnl:,.2f}")
        print(f"Total Trades (DB Rows): {len(df)}")
        
        # Check if the final trade matches the log
        last_trade = df.iloc[-1]
        print(f"\nLast Trade in DB: {last_trade['symbol']} {last_trade['strategy']} PnL=${last_trade['pnl']:.2f}")

        # Breakdown by Strategy
        print("\n--- Breakdown by Strategy ---")
        if 'strategy' in df.columns:
            print(df.groupby('strategy')['pnl'].sum())
            print(df.groupby('strategy')['pnl'].count().rename("count"))

        # Analyze Equity Snapshots
        print("\n--- Equity Snapshots ---")
        if 'backtest_equity_snapshots' in tables:
            df_eq = pd.read_sql_query("SELECT * FROM backtest_equity_snapshots ORDER BY timestamp ASC", conn)
            if not df_eq.empty:
                print(f"First Snapshot: {df_eq.iloc[0].to_dict()}")
                print(f"Last Snapshot: {df_eq.iloc[-1].to_dict()}")
                print(f"Total Snapshots: {len(df_eq)}")
                # Plot or analyze trend?
                # Just print start/end for now.
            else:
                print("No equity snapshots found.")
        else:
            print("backtest_equity_snapshots table missing.")
            
    except Exception as e:
        print(f"Error analyzing DB: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    analyze_backtest()
