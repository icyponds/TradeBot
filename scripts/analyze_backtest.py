
import sys
import os
import pandas as pd
from datetime import datetime

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.utils.trade_database import TradeDatabase
from src.utils.performance_tracker import PerformanceTracker

def analyze_backtest():
    print("Analyzing Backtest Results...")
    
    # Initialize DB connection to backtest tables
    db = TradeDatabase(table_prefix="backtest_")
    
    # Get overall stats
    overall_stats = db.get_strategy_stats(None)
    print("\n=== Overall Performance ===")
    print(f"Total Trades: {overall_stats['total_trades']}")
    print(f"Total PnL: ${overall_stats['total_pnl']:.2f}")
    print(f"Win Rate: {overall_stats['win_rate']:.2f}%")
    print(f"Profit Factor: {overall_stats['profit_factor']:.2f}")
    
    # Get DD stats explicitly
    dd_stats = db.get_drawdown_stats()
    print(f"Max Drawdown (Equity): ${dd_stats.get('max_drawdown', 0):.2f}")
    print(f"Max Drawdown %: {dd_stats.get('max_drawdown_pct', 0):.2f}%")

    today = datetime.now()
    if today.dst():
        # Quick hack for timezone offset if needed, but not critical for simple reconciliation
        pass

    # --- Capital Analysis ---
    # Load settings for initial capital
    from src.config.settings import load_config
    config = load_config()
    initial_perp = config.get('backtesting', {}).get('initial_capital', 50000.0)
    initial_spot = config.get('backtesting', {}).get('initial_spot_balance', 0.0)
    total_initial = initial_perp + initial_spot
    
    # Use NET PnL (after fees)
    total_net_pnl = overall_stats.get('net_pnl', overall_stats['total_pnl'])
    total_fees = overall_stats.get('total_fees', 0.0)
    
    final_equity = total_initial + total_net_pnl
    roi_pct = (total_net_pnl / total_initial) * 100 if total_initial > 0 else 0.0
    
    print("\n=== Capital Analysis ===")
    print(f"Initial Capital: ${total_initial:,.2f} (Perp: ${initial_perp:,.2f}, Spot: ${initial_spot:,.2f})")
    print(f"Final Equity:    ${final_equity:,.2f}")
    print(f"Total ROI:       {roi_pct:+.2f}%")
    print(f"Total Fees:      ${total_fees:,.2f}")

    # --- Strategy Performance ---
    print("\n=== Strategy Performance ===")
    strategies = db.get_strategies()
    
    strategy_data = []
    
    for strategy in strategies:
        stats = db.get_strategy_stats(strategy)
        net_pnl = stats.get('net_pnl', stats['total_pnl'])
        fees = stats.get('total_fees', 0.0)

        # Streak stats
        streak = db.get_streak_stats(strategy)
        
        # Parse name for better display: "vol_breakout_15m" -> "vol_breakout", "15m"
        parts = strategy.split('_')
        if parts[-1] in ['5m', '15m', '1h', '4h', '1d']:
            timeframe = parts[-1]
            strat_type = "_".join(parts[:-1])
        else:
            timeframe = "N/A"
            strat_type = strategy

        strategy_data.append({
            "Strategy": strat_type,
            "TF": timeframe,
            "Trades": stats['total_trades'],
            "Net PnL": net_pnl,
            "Fees": fees,
            "Win Rate %": stats['win_rate'],
            "PF": stats['profit_factor'],
            "Avg Win": stats['avg_win'],
            "Avg Loss": stats['avg_loss'],
            "Max Win": stats['largest_win'],
            "Max Drawdown": stats['largest_loss'], 
        })
        
    if strategy_data:
        df = pd.DataFrame(strategy_data)
        # Sort by PnL descending
        df = df.sort_values('Net PnL', ascending=False)
        
        # Format for printing
        pd.set_option('display.max_columns', None)
        pd.set_option('display.width', 1000)
        # Use simple string conversion for floats to avoid truncation
        print(df.to_string(index=False, float_format=lambda x: "{:,.2f}".format(x) if isinstance(x, (float, int)) else str(x)))
        
        return df
    else:
        print("No trades found.")
        return None

if __name__ == "__main__":
    analyze_backtest()
