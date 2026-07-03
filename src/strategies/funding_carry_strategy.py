"""
Cross-Sectional Funding Carry Strategy.

Uses the funding rate as a DIRECTIONAL signal: persistently positive
funding means longs are crowded and paying to hold — fade them (short,
and RECEIVE the funding while positioned). Persistently negative funding
means shorts are crowded — go long and receive.

One leg per name, no spot hedge: this deliberately dodges the 4-leg fee
math that killed delta-neutral funding_rate_arbitrage (2026-06 research:
~$20 round trip per $10k notional vs ~$1/window of funding collected).
Here funding is the SIGNAL and the position is directional; the carry
received while holding is a tailwind, not the whole edge.

Cross-sectional construction mirrors csm: each instance call updates a
class-level universe cache of trailing funding, then ranks its own
symbol against the cached universe.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

import pandas as pd

from .base_strategy import BaseStrategy


class FundingCarryStrategy(BaseStrategy):
    """Fade crowded positioning as measured by trailing funding."""

    # Funding is paid hourly on Hyperliquid
    PREFERRED_TIMEFRAME = '1h'

    # Class-level universe cache (csm pattern): {symbol: {'funding': mean_hourly, 'ts': dt}}
    _funding_stats: Dict[str, Dict[str, Any]] = {}

    def __init__(self, config: Dict[str, Any], market_api=None, timeframe: str = None):
        super().__init__(config, timeframe)

        fc_config = config.get('strategies', {}).get('funding_carry', {})

        self.market_api = market_api
        self.funding_lookback_hours = int(fc_config.get('funding_lookback_hours', 24))
        self.exit_lookback_hours = int(fc_config.get('exit_lookback_hours', 8))
        self.top_n_percent = float(fc_config.get('top_n_percent', 0.15))
        self.min_abs_funding_apr = float(fc_config.get('min_abs_funding_apr', 0.10))
        self.min_universe = int(fc_config.get('min_universe', 10))
        self.stop_loss_pct = float(fc_config.get('stop_loss_pct', 0.05))

        # Per-(symbol, window) trailing-funding cache, refreshed when the
        # hour changes (funding only updates hourly).
        self._trailing_cache: Dict[Tuple[str, int], Tuple[datetime, Optional[float]]] = {}

        self.logger.info(
            f"Initialized Funding Carry Strategy: lookback={self.funding_lookback_hours}h, "
            f"fade top/bottom {self.top_n_percent:.0%}, min |APR|={self.min_abs_funding_apr:.0%}"
        )

    def set_market_api(self, market_api):
        """Late injection of the market API (parity with funding_rate_arbitrage)."""
        self.market_api = market_api

    # ------------------------------------------------------------------
    # Funding data access
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """Simulation time in backtests (mock exposes current_time), else wall clock."""
        sim_time = getattr(self.market_api, 'current_time', None)
        return sim_time if sim_time else datetime.now()

    def _trailing_funding(self, symbol: str, hours: int) -> Optional[float]:
        """
        Mean HOURLY funding rate over the trailing `hours`.

        Returns None when funding history is unavailable or too sparse to
        trust (fewer than half the expected hourly records).
        """
        if not self.market_api or not hasattr(self.market_api, 'get_funding_history'):
            return None

        now = self._now()
        cache_key = (symbol, hours)
        hour_bucket = now.replace(minute=0, second=0, microsecond=0)
        cached = self._trailing_cache.get(cache_key)
        if cached and cached[0] == hour_bucket:
            return cached[1]

        start_ms = int((now - timedelta(hours=hours)).timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)

        result: Optional[float] = None
        try:
            records = self.market_api.get_funding_history(symbol, start_ms, end_ms) or []
            rates = []
            for record in records:
                rate = record.get('fundingRate')
                if rate is not None:
                    rates.append(float(rate))
            if len(rates) >= max(2, hours // 2):
                result = sum(rates) / len(rates)
        except Exception as e:
            self.logger.debug(f"Funding history unavailable for {symbol}: {e}")

        self._trailing_cache[cache_key] = (hour_bucket, result)
        return result

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        tf_data = self._get_timeframe_data(ohlcv)
        if tf_data is None or len(tf_data) == 0:
            return None

        current_price = tf_data['close'].iloc[-1]
        now = self._now()

        trailing = self._trailing_funding(symbol, self.funding_lookback_hours)
        if trailing is None:
            return None

        # Update the shared universe cache and prune stale entries (symbols
        # that dropped out of the traded universe)
        self._funding_stats[symbol] = {'funding': trailing, 'ts': now}
        stale_cutoff = now - timedelta(hours=2)
        for sym in list(self._funding_stats.keys()):
            if self._funding_stats[sym]['ts'] < stale_cutoff:
                del self._funding_stats[sym]

        if len(self._funding_stats) < self.min_universe:
            return None

        # Rank this symbol's trailing funding within the universe
        values = sorted(v['funding'] for v in self._funding_stats.values())
        idx = values.index(trailing)
        rank = (idx + 1) / len(values)

        # Annualize the mean hourly rate for the absolute threshold
        funding_apr = trailing * 24 * 365

        signal = None
        reason = ''

        if rank >= (1.0 - self.top_n_percent) and funding_apr >= self.min_abs_funding_apr:
            # Crowded longs paying the most: fade them, receive funding
            signal = 'sell'
            reason = (f"Funding Carry: SHORT crowded longs (rank {rank:.2f}, "
                      f"trailing funding {funding_apr:+.1%} APR)")
        elif rank <= self.top_n_percent and funding_apr <= -self.min_abs_funding_apr:
            # Crowded shorts paying the most: fade them, receive funding
            signal = 'buy'
            reason = (f"Funding Carry: LONG crowded shorts (rank {rank:.2f}, "
                      f"trailing funding {funding_apr:+.1%} APR)")

        if signal is None:
            return None

        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'funding_carry',
            'funding_hourly': trailing,
            'funding_apr': funding_apr,
            'funding_rank': rank,
        }

    # ------------------------------------------------------------------
    # Exits & sizing hooks
    # ------------------------------------------------------------------

    def should_exit(self, position: Any, current_price: float,
                    current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Exit when the carry disappears: the short-window trailing funding
        flips against the position's receive direction.
        """
        symbol = getattr(position, 'symbol', None)
        side = getattr(position, 'side', None)
        if not symbol or side not in ('long', 'short'):
            return False, None

        recent = self._trailing_funding(symbol, self.exit_lookback_hours)
        if recent is None:
            return False, None

        if side == 'short' and recent <= 0:
            return True, f"Funding flipped ({recent * 24 * 365:+.1%} APR): short no longer receives"
        if side == 'long' and recent >= 0:
            return True, f"Funding flipped ({recent * 24 * 365:+.1%} APR): long no longer receives"

        return False, None

    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None,
                                  signal_context: Dict[str, Any] = None) -> float:
        """0.5 at the APR threshold, 1.0 at 5x the threshold."""
        apr = 0.0
        if signal_context and 'funding_apr' in signal_context:
            apr = abs(float(signal_context['funding_apr']))

        if apr <= self.min_abs_funding_apr:
            return 0.5

        apr_max = self.min_abs_funding_apr * 5.0
        if apr >= apr_max:
            return 1.0
        return 0.5 + 0.5 * (apr - self.min_abs_funding_apr) / (apr_max - self.min_abs_funding_apr)

    def calculate_stop_loss(self, current_price: float, side: str,
                            signal_context: Dict[str, Any] = None) -> float:
        if side == 'long':
            return current_price * (1.0 - self.stop_loss_pct)
        return current_price * (1.0 + self.stop_loss_pct)

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                              signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Carry positions have no natural price target — the exit is the
        funding flip (should_exit) or the trailing stop. Set a wide TP so
        it rarely binds but satisfies R:R validation.
        """
        tp_dist = entry_price * (self.stop_loss_pct * 2.0)
        if side == 'long':
            return entry_price + tp_dist
        return entry_price - tp_dist

    def get_trailing_stop_config(self, entry_price: float = None,
                                 signal_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Loose trail: protect against runaway moves, let the carry accrue."""
        return {
            'enabled': True,
            'trail_pct': 0.03,
            'activation_pct': 0.04,
        }
