"""
Weekly Cross-Sectional Momentum (factor-style) — research round 9.

Literature construction (Liu, Tsyvinski & Wu 2022, "Common Risk Factors in
Cryptocurrency": 1-4 week momentum is a priced crypto factor): once a week,
rank the liquid universe on its trailing ~3-week return, long the top
quintile, short the bottom quintile, hold for the week.

Deliberately NOT csm_4h (round 8: no edge). csm re-ranked every 4h on a 48h
window and exited through a fixed 5% stop, a 4% trail and the engine's
forced take-profit — stop-outs were the whole loss in every autopsy. Here:

- one rebalance per week (the bar starting `rebalance_weekday`
  `rebalance_hour` UTC; signals and exits only while that bar is the latest
  CLOSED bar),
- exits by rank-band decay at the rebalance (hold while still in the
  top/bottom `hold_percent` — buffer rule), plus a wide catastrophe stop at
  `stop_vol_mult` x one-week sigma (clamped),
- no trailing stop and no engine take-profit (USES_ENGINE_TAKE_PROFIT),
- inverse-volatility sizing for free: the engine sizes notional as risk
  budget / stop distance, and the stop distance scales with volatility.

All parameters are fixed literature/structural defaults (pre-registered in
reports/oos_matrix6/PREREGISTRATION.md) — do not tune them on the test
windows.
"""

import math
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from .base_strategy import BaseStrategy


class WeeklyMomentumStrategy(BaseStrategy):
    """Weekly long/short cross-sectional momentum."""

    PREFERRED_TIMEFRAME = '4h'
    # Exits are rank-band + catastrophe stop only; the engine's capital-based
    # take-profit would truncate exactly the winners momentum lives on.
    USES_ENGINE_TAKE_PROFIT = False

    BAR_HOURS = {'1h': 1, '4h': 4, '1d': 24}

    # Shared ranking cache: {symbol: {'score', 'return', 'bar_vol', 'bar_ts'}}
    _universe_stats: Dict[str, Dict[str, Any]] = {}

    def __init__(self, config: Dict[str, Any], timeframe: str = None, market_api=None):
        super().__init__(config, timeframe)
        cfg = config.get('strategies', {}).get('weekly_momentum', {}) or {}
        # Only used as a clock (simulation time in backtests)
        self.market_api = market_api

        self.lookback_days = float(cfg.get('lookback_days', 21))
        self.top_n_percent = float(cfg.get('top_n_percent', 0.20))
        self.bottom_n_percent = float(cfg.get('bottom_n_percent', 0.20))
        self.hold_percent = float(cfg.get('hold_percent', 0.40))
        self.rebalance_weekday = int(cfg.get('rebalance_weekday', 0))  # Monday
        self.rebalance_hour = int(cfg.get('rebalance_hour', 0))
        self.stop_vol_mult = float(cfg.get('stop_vol_mult', 2.0))
        self.min_stop_pct = float(cfg.get('min_stop_pct', 0.08))
        self.max_stop_pct = float(cfg.get('max_stop_pct', 0.35))
        self.fallback_stop_pct = float(cfg.get('fallback_stop_pct', 0.15))
        self.min_universe = int(cfg.get('min_universe', 10))
        self.direction = str(cfg.get('direction', 'both')).lower()
        self.signal_strength_value = float(cfg.get('signal_strength', 0.8))

        self.bar_hours = self.BAR_HOURS.get(self.timeframe, 4)
        self.lookback_bars = max(2, int(round(self.lookback_days * 24 / self.bar_hours)))
        self.week_bars = max(1, int(round(7 * 24 / self.bar_hours)))

        self.logger.info(
            f"Initialized Weekly Momentum: lookback={self.lookback_days:g}d ({self.lookback_bars} bars), "
            f"long top {self.top_n_percent:.0%} / short bottom {self.bottom_n_percent:.0%}, "
            f"hold band {self.hold_percent:.0%}, rebalance weekday={self.rebalance_weekday} "
            f"{self.rebalance_hour:02d}:00 UTC, stop {self.stop_vol_mult:g}x weekly sigma")

    # ------------------------------------------------------------------
    # Clock / bars
    # ------------------------------------------------------------------

    def set_market_api(self, market_api):
        """Late injection (hot-reload parity)."""
        self.market_api = market_api

    def _now_utc(self) -> datetime:
        """Simulation time in backtests, else wall-clock UTC (bar index is naive UTC)."""
        sim_time = getattr(self.market_api, 'current_time', None)
        if sim_time is not None:
            return pd.Timestamp(sim_time).to_pydatetime().replace(tzinfo=None)
        return datetime.utcnow()

    def _closed_bars(self, df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        """Drop a still-forming last bar (live OHLCV includes it; the mock never does)."""
        if df is None or len(df) == 0:
            return None
        last_start = pd.Timestamp(df.index[-1]).to_pydatetime().replace(tzinfo=None)
        if last_start + timedelta(hours=self.bar_hours) > self._now_utc():
            df = df.iloc[:-1]
        return df if len(df) else None

    def _is_rebalance_bar(self, bar_ts) -> bool:
        ts = pd.Timestamp(bar_ts)
        return ts.weekday() == self.rebalance_weekday and ts.hour == self.rebalance_hour

    # ------------------------------------------------------------------
    # Scoring / ranking
    # ------------------------------------------------------------------

    def _compute_stats(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        if df is None or len(df) < self.lookback_bars + 1:
            return None
        close = df['close'].astype(float)
        past = close.iloc[-1 - self.lookback_bars]
        if past <= 0:
            return None
        ret = close.iloc[-1] / past - 1.0
        bar_vol = close.iloc[-self.lookback_bars - 1:].pct_change().std()
        if bar_vol is None or np.isnan(bar_vol) or bar_vol <= 0:
            bar_vol = None
        # Raw-return ranking, as in the factor literature
        return {'score': float(ret), 'return': float(ret), 'bar_vol': bar_vol,
                'bar_ts': pd.Timestamp(df.index[-1])}

    def _update_stats(self, symbol: str, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        stats = self._compute_stats(df)
        if stats is not None:
            self._universe_stats[symbol] = stats
        return stats

    def _rank(self, symbol: str, bar_ts) -> Optional[float]:
        """
        Rank against every peer's LATEST score from the past week.

        The manager evaluates each symbol once per newly closed bar, in turn,
        so at the moment a symbol is evaluated some peers still carry the
        previous bar's score. Waiting for "all peers fresh" therefore lost
        most of the universe on the rebalance bar (smoke test: ~6 of 12
        names signalled); a one-bar-old 3-week return is a negligible
        error. Peers not updated for a week (dropped from the universe)
        are excluded.
        """
        bar_ts = pd.Timestamp(bar_ts)
        mine = self._universe_stats.get(symbol)
        if not mine or mine['bar_ts'] != bar_ts:
            return None
        week_ago = bar_ts - timedelta(days=7)
        scores = sorted(v['score'] for v in self._universe_stats.values() if v['bar_ts'] >= week_ago)
        if len(scores) < self.min_universe:
            return None
        my_score = mine['score']
        return sum(1 for x in scores if x <= my_score) / len(scores)

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        df = self._closed_bars(ohlcv.get(self.timeframe))
        stats = self._update_stats(symbol, df) if df is not None else None
        if stats is None or not self._is_rebalance_bar(stats['bar_ts']):
            return None

        rank = self._rank(symbol, stats['bar_ts'])
        if rank is None:
            return None

        if rank >= 1.0 - self.top_n_percent:
            signal = 'buy'
        elif rank <= self.bottom_n_percent:
            signal = 'sell'
        else:
            return None
        if (signal == 'buy' and self.direction == 'short') or (signal == 'sell' and self.direction == 'long'):
            return None

        stop_pct = self._stop_pct(stats['bar_vol'])
        side = 'Top' if signal == 'buy' else 'Bottom'
        return {
            'signal': signal,
            'reason': (f"Weekly momentum: {side} {self.top_n_percent if signal == 'buy' else self.bottom_n_percent:.0%} "
                       f"(rank {rank:.2f}, {self.lookback_days:g}d return {stats['return']:+.1%}, "
                       f"stop {stop_pct:.1%})"),
            'price': float(df['close'].iloc[-1]),
            'strategy': 'weekly_momentum',
            'confidence': self.signal_strength_value,
            'rank': rank,
            'momentum': stats['return'],
            # Precomputed stop distance (engine passes it to calculate_stop_loss)
            'stop_pct': stop_pct,
        }

    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None,
                                  signal_context: Dict[str, Any] = None) -> float:
        # Constant: strength drives leverage/conflict mechanics, not the
        # portfolio — every name in the quintile is an equal member.
        return self.signal_strength_value

    # ------------------------------------------------------------------
    # Risk
    # ------------------------------------------------------------------

    def _stop_pct(self, bar_vol: Optional[float]) -> float:
        if not bar_vol:
            return self.fallback_stop_pct
        week_sigma = bar_vol * math.sqrt(self.week_bars)
        return min(self.max_stop_pct, max(self.min_stop_pct, self.stop_vol_mult * week_sigma))

    def calculate_stop_loss(self, entry_price: float, side: str, signal_context: Dict[str, Any] = None) -> float:
        """Catastrophe stop at stop_vol_mult x one-week sigma (clamped)."""
        stop_pct = (signal_context or {}).get('stop_pct') or self.fallback_stop_pct
        if side == 'long':
            return entry_price * (1 - stop_pct)
        return entry_price * (1 + stop_pct)

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                              signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        return 0.0  # no take-profit (USES_ENGINE_TAKE_PROFIT = False)

    def get_trailing_stop_config(self, entry_price: float = None,
                                 signal_context: Dict[str, Any] = None) -> Dict[str, Any]:
        return {'enabled': False, 'trail_pct': 0.0, 'activation_pct': 0.0}

    # ------------------------------------------------------------------
    # Exits
    # ------------------------------------------------------------------

    def needs_exit_data(self) -> bool:
        return True

    def should_exit(self, position: Any, current_price: float,
                    current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """Rank-band exit, evaluated only at the weekly rebalance bar."""
        symbol = getattr(position, 'symbol', None)
        ohlcv = (current_data or {}).get('ohlcv')
        if isinstance(ohlcv, dict):
            ohlcv = ohlcv.get(self.timeframe)
        if symbol and isinstance(ohlcv, pd.DataFrame):
            df = self._closed_bars(ohlcv)
            if df is not None:
                self._update_stats(symbol, df)

        stats = self._universe_stats.get(symbol)
        if not stats or not self._is_rebalance_bar(stats['bar_ts']):
            return False, None
        rank = self._rank(symbol, stats['bar_ts'])
        if rank is None:
            return False, None

        side = str(getattr(position, 'side', '')).lower()
        if side == 'long' and rank < 1.0 - self.hold_percent:
            return True, f"weekly_rank_exit (rank {rank:.2f} < {1.0 - self.hold_percent:.2f})"
        if side == 'short' and rank > self.hold_percent:
            return True, f"weekly_rank_exit (rank {rank:.2f} > {self.hold_percent:.2f})"
        return False, None
