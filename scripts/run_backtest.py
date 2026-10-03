
import sys
import os
import json
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


UNIVERSE_MODES = ('pit', 'window')
COARSE_EXCLUDED_TIMEFRAMES = ['5m', '15m', '1h']


def universe_ranking_window(start, end, mode='pit', lookback_days=30):
    """
    The period whose liquidity decides the backtest universe.

    'pit' (point-in-time, default): the `lookback_days` BEFORE the window —
    only information available at the start. 'window': the test window
    itself (pre-2026-10 behavior). Volume spikes with large moves, so
    in-window ranking stocks each month's universe with that month's
    biggest trenders — exactly what momentum profits from — and its
    coverage requirement drops names delisted mid-window (survivorship).
    """
    if mode == 'pit':
        return start - timedelta(days=lookback_days), start
    if mode == 'window':
        return start, end
    raise ValueError(f"unknown universe mode {mode!r}; choose from {UNIVERSE_MODES}")


def select_universe(db, symbols, start, end, max_n, mode='pit', lookback_days=30):
    """
    Rank candidate symbols by liquidity and history coverage instead of
    alphabetical truncation, and dedupe duplicate underlyings listed on
    multiple HIP-3 dexes (e.g. cash:NVDA / flx:NVDA / xyz:NVDA) by keeping
    the most liquid listing. Ensures backtests cover both crypto-native and
    HIP-3 perps, weighted toward what is actually tradeable.

    Ranking period: see universe_ranking_window (point-in-time by default).
    """
    rank_start, rank_end = universe_ranking_window(start, end, mode, lookback_days)
    scored = []
    window_seconds = max(1.0, (rank_end - rank_start).total_seconds())
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
        if mode == 'pit':
            # strictly before the window: the start bar is not yet observable
            window = df[(df.index >= rank_start) & (df.index < rank_end)]
        else:
            window = df[(df.index >= rank_start) & (df.index <= rank_end)]
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
    print(f"Universe ({mode}, ranked {rank_start:%Y-%m-%d}..{rank_end:%Y-%m-%d}): "
          f"{len(selected)} symbols by notional volume "
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


def apply_generic_overrides(config, overrides):
    """--set section.path.to.key=value for any top-level config section."""
    for override in overrides or []:
        section, sep, rest = override.partition('.')
        if not sep or not rest:
            print(f"  ⚠ Invalid format '{override}' — expected section.path.to.key=value")
            continue
        apply_section_overrides(config, section, [rest])


def apply_bar_resolution(config, required_timeframes, bar_resolution, active_timeframes=None):
    """
    'native' (default): use every stored timeframe (prices from the finest).
    '4h': COARSE mode for history where finer candles are unavailable
    (Hyperliquid retains only ~5000 bars per series: 1h reaches back ~7
    months, 4h ~2.3 years). Finer timeframes are excluded from loading, so
    prices and stop checks run on 4h closes. Calibrate against native runs
    on overlapping windows before trusting absolute numbers.
    Returns the timeframes a symbol must have data for.
    """
    if bar_resolution == 'native':
        return set(required_timeframes) | {'1h'}
    if bar_resolution == '4h':
        active = required_timeframes if active_timeframes is None else active_timeframes
        too_fine = sorted({tf for tf in active if tf in COARSE_EXCLUDED_TIMEFRAMES})
        if too_fine:
            raise ValueError(f"--bar-resolution 4h cannot run strategies on {too_fine}")
        config.setdefault('backtesting', {})['exclude_timeframes'] = list(COARSE_EXCLUDED_TIMEFRAMES)
        return ({tf for tf in required_timeframes if tf not in COARSE_EXCLUDED_TIMEFRAMES}) | {'4h'}
    raise ValueError(f"unknown bar resolution {bar_resolution!r}")


def result_record(report, start_date, end_date, extra=None):
    """Machine-readable run summary (one JSON line, prefixed RESULT_JSON)."""
    rec = {
        'start': start_date.strftime('%Y-%m-%d'),
        'end': end_date.strftime('%Y-%m-%d'),
        'final_equity': round(float(report.get('total_equity', 0) or 0), 2),
        'trades': int(report.get('backtest_trades', 0) or 0),
        'pnl': round(float(report.get('backtest_total_pnl', 0) or 0), 2),
        'win_rate': round(float(report.get('backtest_win_rate', 0) or 0), 2),
        'profit_factor': round(float(report.get('backtest_profit_factor', 0) or 0), 3),
        'max_dd_pct': round(float(report.get('backtest_max_drawdown_pct', 0) or 0), 2),
        'funding_paid': round(float(report.get('funding_paid', 0) or 0), 2),
    }
    rec.update(extra or {})
    return rec


def run_smoke_test(days=None, start_str=None, end_str=None, random_window=None, param_overrides=None, disable_strategies=None, enable_instances=None, max_symbols=None, universe='all', risk_param_overrides=None, trading_param_overrides=None,
                   universe_mode='pit', universe_lookback_days=30, bar_resolution='native',
                   results_db=None, generic_overrides=None, interval_minutes=15, tag=None):
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

    # Per-run results DB (backtest_* tables) so several backtests can run in
    # parallel; market data is still read from the default data/trades.db.
    if results_db:
        config.setdefault('persistence', {})['db_path'] = results_db
        print(f"Results DB: {results_db}")
    
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
    settings_tfs = [s.get('timeframe', '1h') for s in config['strategies']['instances']]
    # Instances that will actually run (for the coarse-mode resolution check)
    active_tfs = [s.get('timeframe', '1h') for s in config['strategies']['instances']
                  if not any(s['name'].startswith(d) for d in (disable_strategies or []))]
    active_tfs += [spec.split(':')[2] for spec in (enable_instances or []) if spec.count(':') == 2]
    # Native mode always adds 1h (broad market checks / funding fallback);
    # coarse 4h mode excludes sub-4h data entirely (see apply_bar_resolution)
    required_timeframes = apply_bar_resolution(config, settings_tfs, bar_resolution, active_tfs)

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
        symbols = select_universe(db, symbols, start_date, end_date, max_symbols,
                                  mode=universe_mode, lookback_days=universe_lookback_days)

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

    # 3.1c Apply any --risk-param / --trading-param / --set overrides from CLI
    apply_section_overrides(config, 'risk_management', risk_param_overrides)
    apply_section_overrides(config, 'trading', trading_param_overrides)
    apply_generic_overrides(config, generic_overrides)

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
    bt_db = BtDb(results_db, table_prefix="backtest_") if results_db else BtDb(table_prefix="backtest_")
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
    report = engine.run(start_date, end_date, interval_minutes=interval_minutes)
    
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

    maker = getattr(engine.mock_api, 'maker_stats', None) or {}
    rec = result_record(report, start_date, end_date, extra={
        'tag': tag,
        'universe_mode': universe_mode,
        'bar_resolution': bar_resolution,
        'n_symbols': len(symbols),
        'maker_attempted': int(maker.get('attempted', 0) or 0),
        'maker_filled': int(maker.get('filled', 0) or 0),
    })
    print("RESULT_JSON: " + json.dumps(rec))
    return rec


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
    parser.add_argument('--set', dest='generic_overrides', action='append', metavar='section.path=value',
                        help='Override any config value (repeatable). '
                             'E.g. --set strategy_selection.win_rate_strength_modifier=false')
    parser.add_argument('--universe-mode', choices=UNIVERSE_MODES, default='pit',
                        help="Universe ranking period: 'pit' = the --universe-lookback-days BEFORE "
                             "the window (default, no look-ahead); 'window' = in-window (legacy, biased)")
    parser.add_argument('--universe-lookback-days', type=int, default=30,
                        help='Liquidity ranking lookback for --universe-mode pit (default 30)')
    parser.add_argument('--bar-resolution', choices=['native', '4h'], default='native',
                        help="'4h' = coarse mode for long history (no sub-4h data loaded)")
    parser.add_argument('--results-db', default=None,
                        help='SQLite file for backtest_* result tables (enables parallel runs)')
    parser.add_argument('--interval-minutes', type=int, default=15,
                        help='Simulation step size in minutes (default 15)')
    parser.add_argument('--tag', default=None, help='Label echoed in RESULT_JSON')

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
            trading_param_overrides=args.trading_param,
            universe_mode=args.universe_mode,
            universe_lookback_days=args.universe_lookback_days,
            bar_resolution=args.bar_resolution,
            results_db=args.results_db,
            generic_overrides=args.generic_overrides,
            interval_minutes=args.interval_minutes,
            tag=args.tag,
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
