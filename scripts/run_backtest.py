
import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.config.settings import load_config
from src.backtesting.backtest_engine import BacktestEngine

def generate_synthetic_data(symbol, start_date, end_date, freq='1h'):
    """Generate synthetic sine wave data for testing."""
    dates = pd.date_range(start=start_date, end=end_date, freq=freq)
    n = len(dates)
    
    # Sine wave with trend
    t = np.linspace(0, 4*np.pi, n)
    trend = np.linspace(100, 120, n)
    noise = np.random.normal(0, 0.5, n)
    
    price = trend + 5 * np.sin(t) + noise
    
    df = pd.DataFrame(index=dates)
    df['open'] = price
    df['high'] = price + 1
    df['low'] = price - 1
    df['close'] = price
    df['volume'] = 1000
    
    return df

import random


def select_universe(db, symbols, start, end, max_n):
    """
    Rank candidate symbols by liquidity and history coverage instead of
    alphabetical truncation, and dedupe duplicate underlyings listed on
    multiple HIP-3 dexes (e.g. cash:NVDA / flx:NVDA / xyz:NVDA) by keeping
    the most liquid listing. Ensures backtests cover both crypto-native and
    HIP-3 perps, weighted toward what is actually tradeable.
    """
    scored = []
    window_seconds = max(1.0, (end - start).total_seconds())
    expected_bars = max(1, int(window_seconds // (4 * 3600)))

    # Spot listings are hedge legs, not analysis targets: strategies trade
    # perps, and a _SPOT symbol winning the dedupe (e.g. HYPE_SPOT over HYPE)
    # silently knocks the tradeable perp out of the universe.
    symbols = [s for s in symbols if not s.endswith('_SPOT')]

    for sym in symbols:
        try:
            df = db.get_market_data(sym, '4h')
        except Exception:
            continue
        if df is None or df.empty:
            continue
        window = df[(df.index >= start) & (df.index <= end)]
        coverage = len(window) / expected_bars
        if coverage < 0.7:
            continue
        # Average notional volume; corrupted zero-volume placeholder bars
        # contribute nothing, which correctly ranks them down
        notional = float((window['close'] * window['volume']).mean())
        scored.append((sym, coverage, notional))

    # Dedupe by underlying (strip dex prefix and _SPOT suffix)
    best = {}
    for sym, coverage, notional in scored:
        base = sym.split(':', 1)[-1].replace('_SPOT', '')
        current = best.get(base)
        if current is None or notional > current[2]:
            best[base] = (sym, coverage, notional)

    ranked = sorted(best.values(), key=lambda x: x[2], reverse=True)
    selected = [sym for sym, _, _ in ranked[:max_n]]

    n_hip3 = sum(1 for s in selected if ':' in s)
    print(f"Universe: {len(selected)} symbols by notional volume "
          f"({len(selected) - n_hip3} crypto, {n_hip3} HIP-3); "
          f"top 5: {selected[:5]}")
    return selected


def apply_section_overrides(config, section, overrides):
    """
    Apply dotted-path overrides under a top-level config section.

    E.g. section='risk_management', 'capital_sleeves.enabled=true' sets
    config['risk_management']['capital_sleeves']['enabled'] = True.
    Values are typed: true/false -> bool, numeric -> int/float, else str.
    """
    if not overrides:
        return
    print(f"Applying {section} overrides:")
    for override in overrides:
        try:
            key_path, value = override.split('=', 1)
        except ValueError:
            print(f"  ⚠ Invalid format '{override}' — expected path.to.key=value")
            continue
        lowered = value.strip().lower()
        if lowered in ('true', 'false'):
            typed_value = (lowered == 'true')
        else:
            try:
                typed_value = float(value)
                if typed_value == int(typed_value) and '.' not in value:
                    typed_value = int(value)
            except ValueError:
                typed_value = value
        node = config.setdefault(section, {})
        parts = key_path.split('.')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        old_value = node.get(parts[-1], 'N/A')
        node[parts[-1]] = typed_value
        print(f"  {section}.{key_path}: {old_value} → {typed_value}")


def apply_risk_overrides(config, overrides):
    """Back-compat wrapper: --risk-param targets risk_management."""
    apply_section_overrides(config, 'risk_management', overrides)


def run_smoke_test(days=None, start_str=None, end_str=None, random_window=None, param_overrides=None, disable_strategies=None, enable_instances=None, max_symbols=None, universe='all', risk_param_overrides=None, trading_param_overrides=None):
    # Increase log level and setup file logging prior to ANY imports or logic
    import logging
    
    # Ensure log directory exists
    log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'backtest.log')
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, mode='w'), # Overwrite mode
            logging.StreamHandler(sys.stdout)
        ],
        force=True
    )
    print(f"Logging to: {log_file}")

    print("Running Backtest Smoke Test...")
    
    # 1. Config
    config = load_config()
    
    # 5. Enable Backtest Mode for PairSelector (load all assets instantly)
    config['mode'] = 'backtest'
    config['backtesting']['enabled'] = True
    
    # 2. Data
    from src.utils.trade_database import TradeDatabase
    db = TradeDatabase()
    
    # Determine default end_date from DB if available, otherwise now()
    default_end = datetime.now()
    default_start = default_end - timedelta(days=days or 90)
    
    # Get Timeframes required by current config
    required_timeframes = set([s.get('timeframe', '1h') for s in config['strategies']['instances']])
    # Ensure 1h is always present as it's often used for broad market check / funding (fallback)
    required_timeframes.add('1h')
    
    print(f"Required Timeframes: {required_timeframes}")
    
    # Dynamic Universe Selection
    print("Filtering asset universe based on data availability...")
    
    # We need a rough range to query. If user didn't specify, we look for data in the last X days.
    # Note: DB queries are fast, so checking a broad range is fine.
    query_start = datetime.strptime(start_str, "%Y-%m-%d") if start_str else default_start
    query_end = datetime.strptime(end_str, "%Y-%m-%d") if end_str else default_end
    
    symbols = db.get_available_symbols_for_timeframes(list(required_timeframes), query_start, query_end)
    
    if not symbols:
         print(f"CRITICAL: No assets found with data for all timeframes {required_timeframes} in range {query_start} to {query_end}")
         return
         
    print(f"Selected {len(symbols)} assets for backtest: {symbols[:5]}...")
    
    # Override config symbols
    config['trading']['symbols'] = symbols
    
    # Retrieve actual data range for the PRIMARY asset (usually BTC or first in list) to permit precise trimming
    # But for dynamic mode, we trust the query_start/end or the user input.
    db_start = query_start
    db_end = query_end
            
    # Random Window Logic
    if random_window and db_start and db_end:
        print(f"Randomly selecting {random_window} days within range {db_start} to {db_end}...")
        total_duration = db_end - db_start
        if total_duration.days <= random_window:
             print(f"Warning: Not enough data ({total_duration.days}d) for requested random window ({random_window}d). Using full range.")
             start_date = db_start
             end_date = db_end
        else:
             # Buffer of 1 day to ensure full data availability
             max_start_offset = (db_end - timedelta(days=random_window + 1)).timestamp()
             min_start_offset = db_start.timestamp()
             
             random_start_ts = random.uniform(min_start_offset, max_start_offset)
             start_date = datetime.fromtimestamp(random_start_ts)
             end_date = start_date + timedelta(days=random_window)
    else:
        # Standard Logic
        # Parse Arguments or use Defaults
        end_date = default_end
        if end_str:
            end_date = datetime.strptime(end_str, "%Y-%m-%d")
            
        start_date = default_start
        if start_str:
            start_date = datetime.strptime(start_str, "%Y-%m-%d")
        elif days:
            # If days specified, anchor from the DETERMINED end_date (DB tip or now)
            start_date = end_date - timedelta(days=days)
    
    if symbols:
        print(f"Found {len(symbols)} symbols in DB. Using range from {symbols[0]}...")
    
    print(f"Simulating: {start_date} to {end_date}")

    if not symbols:
        print("No data in DB. Loading from CSVs as fallback...")
        pass

    # Optional asset-class restriction (hip3 = builder-dex assets, crypto = native perps)
    if universe == 'hip3':
        symbols = [s for s in symbols if ':' in s]
    elif universe == 'crypto':
        symbols = [s for s in symbols if ':' not in s and not s.endswith('_SPOT')]
    if universe != 'all':
        print(f"Universe restricted to {universe}: {len(symbols)} candidates")

    # Limit symbol count for performance (backtest with 231 symbols at 15min steps is very slow)
    # Selection is liquidity-ranked with HIP-3 dedupe, NOT alphabetical (see select_universe)
    max_symbols = max_symbols or 20
    if len(symbols) > max_symbols:
        symbols = select_universe(db, symbols, start_date, end_date, max_symbols)

    # Funding-rate arbitrage needs its spot-hedgeable perps in the analyzed
    # universe regardless of volume rank (the strategy is gated to them anyway)
    active_names = {s['name'] for s in config['strategies']['instances']}
    if enable_instances:
        active_names |= {spec.split(':')[1] for spec in enable_instances if spec.count(':') == 2}
    if any('funding_rate_arbitrage' in n for n in active_names):
        from src.api.hyperliquid_api import HyperliquidAPI
        hedgeable = [s.replace('_SPOT', '') for s in HyperliquidAPI.SPOT_INTERNAL_TO_API]
        available = set(db.get_market_data_symbols('1h'))
        added = [s for s in hedgeable if s in available and s not in symbols]
        if added:
            symbols = symbols + added
            print(f"Funding-arb: added spot-hedgeable perps to universe: {added}")

    # 3. Configure symbols and strategy overrides BEFORE engine init
    config['trading']['dynamic_pair_selection'] = True
    config['trading']['symbols'] = symbols
    
    # NOTE: Historical lookback overrides were removed (2026-06). The DB now
    # holds 7+ months of history, so strategies run with PRODUCTION lookbacks
    # ("test what you fly"). Use --param for deliberate experiments.

    # 3.1b Apply any --param overrides from CLI
    if param_overrides:
        print("Applying parameter overrides:")
        for override in param_overrides:
            try:
                key_path, value = override.split('=')
                strategy, param = key_path.split('.', 1)
                # Auto-detect type: try float, then int, then string
                try:
                    typed_value = float(value)
                    if typed_value == int(typed_value) and '.' not in value:
                        typed_value = int(value)
                except ValueError:
                    typed_value = value
                old_value = config['strategies'].get(strategy, {}).get(param, 'N/A')
                config['strategies'][strategy][param] = typed_value
                print(f"  {strategy}.{param}: {old_value} → {typed_value}")
            except ValueError:
                print(f"  ⚠ Invalid format '{override}' — expected strategy.param=value")

    # 3.1c Apply any --risk-param / --trading-param overrides from CLI
    apply_section_overrides(config, 'risk_management', risk_param_overrides)
    apply_section_overrides(config, 'trading', trading_param_overrides)

    # 3.2 Strategy instance adjustments (purely CLI-driven; the old hardcoded
    # sentiment/liquidation-hunter filter was removed - use --disable-strategy)
    if 'instances' in config['strategies']:
        # Apply --enable-instance additions (re-test disabled strategies)
        # Format: type:name:timeframe, e.g. cross_sectional_momentum:csm_4h:4h
        if enable_instances:
            existing = {s['name'] for s in config['strategies']['instances']}
            for spec in enable_instances:
                try:
                    stype, name, tf = spec.split(':')
                except ValueError:
                    print(f"  ⚠ Invalid format '{spec}' — expected type:name:timeframe")
                    continue
                if name not in existing:
                    config['strategies']['instances'].append(
                        {"type": stype, "name": name, "timeframe": tf})
                    print(f"  Enabled instance: {name} ({stype} @ {tf})")
        # Apply --disable-strategy filters
        if disable_strategies:
            before = len(config['strategies']['instances'])
            config['strategies']['instances'] = [
                s for s in config['strategies']['instances']
                if not any(s['name'].startswith(d) for d in disable_strategies)
            ]
            after = len(config['strategies']['instances'])
            print(f"Disabled strategies: {disable_strategies} (removed {before - after} instances)")
        print(f"Active Strategies: {[s['name'] for s in config['strategies']['instances']]}")

    # 3.3 CLEAN SLATE: Explicitly purge ALL stale backtest data before engine init
    # BacktestEngine only clears trades, but ghost positions leak across runs
    print("Clearing stale backtest data...")
    from src.utils.trade_database import TradeDatabase as BtDb
    bt_db = BtDb(table_prefix="backtest_")
    bt_db.delete_all_trades()
    bt_db.clear_open_positions()
    try:
        with bt_db._get_connection() as conn:
            conn.execute("DELETE FROM backtest_live_position_legs")
            conn.execute("DELETE FROM backtest_equity_snapshots")
            conn.execute("DELETE FROM backtest_daily_pnl")
            conn.commit()
    except Exception as e:
        print(f"  Warning: partial cleanup: {e}")
    print("  Backtest tables cleared.")

    # 3.4 Initialize BacktestEngine AFTER all config overrides
    config['backtesting']['reset_results_db'] = False  # We already cleaned above
    engine = BacktestEngine(config, historical_data=None)
    
    # 3.5 CRITICAL: Pre-populate PairSelector with available symbols
    # The PairSelector's background fetcher (which populates selected_pairs/ready_pairs)
    # does NOT run during backtests. Without this, get_ready_pairs() returns []
    # and run_trading_cycle() skips all analysis with "No ready trading pairs".
    pair_selector = engine.strategy_manager.pair_selector
    # Use only the CAPPED symbols list, not all engine.historical_data keys
    print(f"Injecting {len(symbols)} symbols into PairSelector ready set...")
    with pair_selector._pairs_lock:
        for sym in symbols:
            if sym not in pair_selector.selected_pairs:
                pair_selector.selected_pairs.append(sym)
            pair_selector.ready_pairs.add(sym)
    print(f"  Selected: {len(pair_selector.selected_pairs)}, Ready: {len(pair_selector.ready_pairs)}")
    
    # 3.6 CRITICAL: Purge restored live positions from backtest
    # StrategyManager._restore_statarb_state() loads live multi_leg_positions
    # from the production DB on init. These get force-closed at teardown with
    # massive losses (e.g. BCH -$1,087, SOL -$1,078 from 258h-old positions).
    ee = engine.strategy_manager.execution_engine
    if ee.multi_leg_positions:
        print(f"  Purging {len(ee.multi_leg_positions)} restored live multi-leg positions...")
        ee.multi_leg_positions.clear()
    # Also clear stat_arb active_spreads (populated by restore_active_spreads)
    for strat_name, strat in engine.strategy_manager.strategies.items():
        if hasattr(strat, 'active_spreads') and strat.active_spreads:
            print(f"  Purging {len(strat.active_spreads)} restored spreads from {strat_name}")
            strat.active_spreads.clear()
    
    # 4. Run
    # Use 15m interval to match the primary strategy timeframe
    report = engine.run(start_date, end_date, interval_minutes=15)
    
    # 5. Print Results Summary
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS SUMMARY")
    print("=" * 60)
    print(f"Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"Final Equity: ${report['total_equity']:,.2f}")
    
    # Read actual trade stats from the backtest DB
    bt_trades = report.get('backtest_trades', 0)
    bt_pnl = report.get('backtest_total_pnl', 0)
    bt_wr = report.get('backtest_win_rate', 0)
    bt_pf = report.get('backtest_profit_factor', 0)
    bt_mdd = report.get('backtest_max_drawdown_pct', 0)
    
    print(f"Total Trades: {bt_trades}")
    print(f"Total PnL: ${bt_pnl:,.2f}")
    print(f"Win Rate: {bt_wr:.1f}%")
    print(f"Profit Factor: {bt_pf:.2f}")
    print(f"Max Drawdown: {bt_mdd:.2f}%")
    print(f"Funding Paid: ${report.get('funding_paid', 0):,.2f}")
    
    # Per-strategy breakdown
    try:
        db = engine.prefixed_tracker.db
        strategies = sorted(db.get_all_strategy_stats().keys())
        if strategies:
            print("\nPer-Strategy Breakdown:")
            print("-" * 60)
            for strat in strategies:
                stats = db.get_strategy_stats(strat)
                s_trades = int(stats.get('total_trades', 0) or 0)
                s_pnl = float(stats.get('total_pnl', 0) or 0)
                s_wr = float(stats.get('win_rate', 0) or 0)
                print(f"  {strat:<30} | {s_trades:>3} trades | ${s_pnl:>10,.2f} | WR: {s_wr:.1f}%")
    except Exception as e:
        print(f"  (Could not read strategy breakdown: {e})")
    
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Run Strategy Backtest',
        epilog='Example: python run_backtest.py --days 14 --param stat_arb.z_score_threshold=2.5 --param ou_mean_reversion.zscore_entry=2.0'
    )
    parser.add_argument('--days', type=int, help='Number of days to run (default: auto-detect)')
    parser.add_argument('--start', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--random-window', type=int, help='Randomly select N days from available data')
    parser.add_argument('--param', action='append', metavar='strategy.key=value',
                        help='Override a strategy parameter (repeatable). E.g. --param stat_arb.z_score_threshold=2.5')
    parser.add_argument('--disable-strategy', action='append', metavar='name',
                        help='Disable a strategy by name prefix (repeatable). E.g. --disable-strategy stat_arb')
    parser.add_argument('--enable-instance', action='append', metavar='type:name:timeframe',
                        help='Add a strategy instance not in settings.py (repeatable). '
                             'E.g. --enable-instance cross_sectional_momentum:csm_4h:4h')
    parser.add_argument('--max-symbols', type=int, default=None,
                        help='Cap on number of symbols to simulate (default: 20)')
    parser.add_argument('--universe', choices=['all', 'crypto', 'hip3'], default='all',
                        help='Restrict the asset universe (default: all)')
    parser.add_argument('--risk-param', action='append', metavar='path.to.key=value',
                        help='Override a risk_management setting (repeatable, dotted path). '
                             'E.g. --risk-param capital_sleeves.enabled=true')
    parser.add_argument('--trading-param', action='append', metavar='path.to.key=value',
                        help='Override a trading setting (repeatable, dotted path). '
                             'E.g. --trading-param maker_entries.enabled=true')

    args = parser.parse_args()
    
    # Ensure DB directory exists
    os.makedirs('data', exist_ok=True)
    
    try:
        run_smoke_test(
            days=args.days,
            start_str=args.start,
            end_str=args.end,
            random_window=args.random_window,
            param_overrides=args.param,
            disable_strategies=args.disable_strategy,
            enable_instances=args.enable_instance,
            max_symbols=args.max_symbols,
            universe=args.universe,
            risk_param_overrides=args.risk_param,
            trading_param_overrides=args.trading_param
        )
    except KeyboardInterrupt:
        print("\nBacktest interrupted by user.")
    except Exception as e:
        # If logging is configured, this will go to file. If not (early crash), it might miss.
        # But we import logging inside run_smoke_test... 
        # We should probably configure logging globally or catch inside run_smoke_test.
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        
        # Try to log if logger exists
        import logging
        logging.getLogger("root").critical("Backtest failed with exception", exc_info=True)
