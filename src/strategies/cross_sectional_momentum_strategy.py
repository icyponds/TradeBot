"""
Cross-Sectional Momentum Strategy.

This strategy trades based on the relative strength of assets compared to the rest of the universe.
It aims to be Market-Neutral by Longing the Top N Winners and Shorting the Bottom N Losers.

Logic:
1.  Calculate 24h Return for all assets.
2.  Rank assets by Return.
3.  Long Top Decile (e.g., Top 10%).
4.  Short Bottom Decile (e.g., Bottom 10%).
5.  Rebalance: Hourly.

Implementation Note:
Since `generate_signal` is called per-symbol, this strategy maintains a shared Class-Level 
cache of returns to determine rankings dynamically.
"""

import logging
import math
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from .base_strategy import BaseStrategy
from src.utils.statistics import calculate_adx, calculate_atr

class CrossSectionalMomentumStrategy(BaseStrategy):
    """
    Cross-Sectional Momentum (L/S Neutral) Strategy.
    """
    
    PREFERRED_TIMEFRAME = '1h'
    
    # Shared State for Cross-Sectional Ranking
    # {symbol: {'return': float, 'timestamp': datetime}}
    _universe_stats: Dict[str, Dict[str, Any]] = {}
    _last_cleanup = datetime.min
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None, market_api=None):
        super().__init__(config, timeframe)
        
        csm_config = config.get('strategies', {}).get('cross_sectional_momentum', {})
        # Only needed for the optional funding filter (funding history)
        self.market_api = market_api
        
        self.lookback_period = csm_config.get('lookback_period', 24) # 24h Momentum
        self.top_n_percent = csm_config.get('top_n_percent', 0.10)   # Top 10%
        self.bottom_n_percent = csm_config.get('bottom_n_percent', 0.10) # Bottom 10%
        self.rebalance_interval_hours = csm_config.get('rebalance_interval', 4)

        # Market Regime Filter
        self.adx_threshold = csm_config.get('adx_threshold', 25)

        # Absolute-momentum gate (dual momentum): a top-decile RANK can still
        # have a negative own-return when the whole universe is falling
        # (Dec-2025 failure mode: longing "winners" that were merely falling
        # slowest). Gate longs on return > +min_abs_momentum and shorts on
        # return < -min_abs_momentum. Int 0/1 so --param can toggle it.
        self.require_absolute_momentum = bool(int(csm_config.get('require_absolute_momentum', 0)))
        self.min_abs_momentum = float(csm_config.get('min_abs_momentum', 0.0))

        self.stop_loss_pct = float(csm_config.get('stop_loss_pct', 0.05))

        # ATR-scaled stop (Dec-2025 autopsy: a fixed 5% stop sits inside
        # crash-month noise — 32 stop-outs cost -$20.6k while winners made
        # +$12.2k; bounces stopped out positions that were directionally
        # right). >0 enables entry ± mult*ATR(14). The engine sizes positions
        # off the implied stop distance, so a wider vol-aware stop also
        # means a SMALLER position: constant dollar risk, fewer noise stops.
        self.stop_atr_mult = float(csm_config.get('stop_atr_mult', 0.0))

        # Skip-period momentum (classic 12-2 style): rank on the window
        # ending `skip_period` bars ago so entries don't chase the freshest
        # bounce (Dec autopsy: gated longs were bounce-chasers, avg -$300).
        self.skip_period = int(csm_config.get('skip_period', 0))

        # Inverted mode = SHORT-TERM REVERSAL: long the bottom decile, short
        # the top (Dec-2025 bounce cycle: 2-day losers bounced every 2-3
        # days while momentum entries were stopped out). The abs-momentum
        # gate flips with it (longs require a NEGATIVE own return — buying
        # dips). Disable the EMA200 trend filter for reversal runs; it is a
        # momentum-regime construct.
        self.invert = bool(int(csm_config.get('invert', 0)))
        self.trend_filter_enabled = bool(int(csm_config.get('trend_filter_enabled', 1)))

        # Blended-horizon ranking (research round 8). Empty = the single
        # `lookback_period` score above, bit-for-bit. Otherwise the score is
        # the mean over horizons k of ret_k / (sigma_bar * sqrt(k)) — a
        # t-stat-like normalization so short and long horizons weigh evenly;
        # blending removes dependence on one hand-picked lookback.
        raw_periods = csm_config.get('lookback_periods') or []
        if isinstance(raw_periods, str):  # CLI --param ...lookback_periods=12,42,126
            raw_periods = [p for p in raw_periods.split(',') if p.strip()]
        elif isinstance(raw_periods, (int, float)):
            raw_periods = [raw_periods]
        self.lookback_periods = [int(k) for k in raw_periods if int(k) > 1]

        # Rank-decay exit with a buffer band (research round 8). 0 = off
        # (positions end only via stop/trail/flip, the pre-2026-10 behavior).
        # >0: close a long once its rank falls out of the top
        # `exit_rank_percent` (shorts: out of the bottom). Entry at top 15% /
        # exit below top 30% is the textbook buffer rule: held names that
        # stopped being winners free their slot, while the gap between entry
        # and exit thresholds prevents churn at the boundary.
        self.exit_rank_percent = float(csm_config.get('exit_rank_percent', 0.0))

        # Funding filter (research round 8; follow-up flagged in round 7).
        # 0 = off. >0: skip longs whose trailing mean funding is above +X APR
        # (crowded longs paying to hold) and shorts below -X APR. HL baseline
        # funding is ~+11% APR, so useful thresholds sit well above it.
        self.funding_filter_apr = float(csm_config.get('funding_filter_apr', 0.0))
        self.funding_lookback_hours = int(csm_config.get('funding_lookback_hours', 24))

        # Exit mechanics (round-8 follow-up, defaults = historical behavior).
        # 1 = accept the engine's capital-based take-profit (+100% on
        # margin); 0 = opt out (USES_ENGINE_TAKE_PROFIT hook).
        self.USES_ENGINE_TAKE_PROFIT = bool(int(csm_config.get('use_engine_take_profit', 1)))
        # Trailing stop: trail_pct <= 0 disables it.
        self.trail_pct = float(csm_config.get('trail_pct', 0.04))
        self.trail_activation_pct = float(csm_config.get('trail_activation_pct', 0.05))
        self._funding_cache: Dict[str, Tuple[datetime, Optional[float]]] = {}

        self.logger.info(f"Initialized Cross-Sectional Momentum: "
                        f"Lookback={self.lookback_period}h, "
                        f"Top/Bottom={self.top_n_percent:.0%}, ADX_Min={self.adx_threshold}")

    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate L/S signal based on relative rank.
        """
        data = ohlcv.get(self.timeframe)
        if data is None or len(data) < self.lookback_period:
            return None
            
        return self._generate_signal_internal(data, symbol, ohlcv.get(self.timeframe))
        
    def _generate_signal_internal(self, ohlcv: pd.DataFrame, symbol: str, full_ohlcv: pd.DataFrame = None) -> Optional[Dict[str, Any]]:
        """
        Calculate return, update universe cache, and determine rank.
        """
        # 1. Update Universe Stats
        current_price = ohlcv['close'].iloc[-1]
        
        self._update_universe_stats(symbol, ohlcv)
        
        # 1b. Check Market Regime (ADX)
        if len(ohlcv) > 20 and 'high' in ohlcv.columns:
            adx = calculate_adx(ohlcv['high'], ohlcv['low'], ohlcv['close'])
            current_adx = adx.iloc[-1]
            if current_adx < self.adx_threshold:
                return None
        elif full_ohlcv is not None and len(full_ohlcv) > 20:
            adx = calculate_adx(full_ohlcv['high'], full_ohlcv['low'], full_ohlcv['close'])
            current_adx = adx.iloc[-1]
            if current_adx < self.adx_threshold:
                return None
        
        # 2. Clean old entries (once per cycle)
        now = datetime.now()
        if now - self._last_cleanup > timedelta(minutes=15):
             self._cleanup_cache()
             CrossSectionalMomentumStrategy._last_cleanup = now
             
        
        # 3. Determine Rank
        if len(self._universe_stats) < 5:
            return None
            
        # 4. Check Rebalance Schedule
        # Only rebalance if the current hour aligns with interval
        # Use ohlcv timestamp (index)
        latest_ts = ohlcv.index[-1]
        if hasattr(latest_ts, 'hour') and (latest_ts.hour % self.rebalance_interval_hours != 0):
             # Just hold existing positions (handled by manager), don't generate NEW signals
             # Unless we want to force exit? For now just inhibit new entries.
             return None
            
        # Use SCORE for ranking (Risk-Adjusted Momentum)
        my_score = self._universe_stats.get(symbol, {}).get('score', 0)
        my_return = self._universe_stats.get(symbol, {}).get('return', 0)
        rank = self._rank_of(my_score)

        signal = 'hold'
        reason = ''

        # 5. Generate Signal
        if rank >= (1.0 - self.top_n_percent):
            # Top Winner -> Long (momentum) / Short (reversal)
            signal = 'sell' if self.invert else 'buy'
            mode = "Reversal: fade Top" if self.invert else "Top"
            reason = f"CSM (Risk-Adj): {mode} {self.top_n_percent:.0%} Winner (Rank {rank:.2f}, Score {my_score:.2f}, Ret {my_return:.1%})"
        elif rank <= self.bottom_n_percent:
            # Bottom Loser -> Short (momentum) / Long (reversal)
            signal = 'buy' if self.invert else 'sell'
            mode = "Reversal: buy Bottom" if self.invert else "Bottom"
            reason = f"CSM (Risk-Adj): {mode} {self.bottom_n_percent:.0%} Loser (Rank {rank:.2f}, Score {my_score:.2f}, Ret {my_return:.1%})"
            
        if signal == 'sell':
            # Restrict shorting to higher timeframes (4h, 1d) to avoid whipsaws
            if self.timeframe not in ['4h', '1d']:
                self.logger.debug(f"{symbol}: Short signal rejected (Timeframe {self.timeframe} < 4h)")
                return None
        
        if signal == 'hold':
            return None

        # 5b. Absolute-momentum gate: relative rank is not enough — the asset's
        # own return must point the same way as the trade (momentum mode) or
        # against it (reversal mode: buy actual dips, fade actual rips).
        if self.require_absolute_momentum:
            buys_need_positive = not self.invert
            if signal == ('buy' if buys_need_positive else 'sell') and my_return <= self.min_abs_momentum:
                self.logger.debug(f"{symbol}: {signal} rejected by abs-momentum gate "
                                  f"(return {my_return:.2%} <= {self.min_abs_momentum:.2%})")
                return None
            if signal == ('sell' if buys_need_positive else 'buy') and my_return >= -self.min_abs_momentum:
                self.logger.debug(f"{symbol}: {signal} rejected by abs-momentum gate "
                                  f"(return {my_return:.2%} >= {-self.min_abs_momentum:.2%})")
                return None

        # 6. Trend Filter Implementation (EMA 200)
        # Only take LONG signals if Price > EMA200
        # Only take SHORT signals if Price < EMA200
        # (configurable: a momentum-regime construct, off for reversal mode)
        trend_ema = None
        if self.trend_filter_enabled:
            df_calc = ohlcv.copy()
            df_calc['ema200'] = df_calc['close'].ewm(span=200, adjust=False).mean()
            trend_ema = df_calc['ema200'].iloc[-1]

            if signal == 'buy' and current_price < trend_ema:
                 self.logger.debug(f"{symbol}: Long signal filtered (Price {current_price:.2f} < EMA200 {trend_ema:.2f})")
                 return None

            if signal == 'sell' and current_price > trend_ema:
                 self.logger.debug(f"{symbol}: Short signal filtered (Price {current_price:.2f} > EMA200 {trend_ema:.2f})")
                 return None

        # 6b. Funding filter: don't join crowded positioning
        if self.funding_filter_apr > 0:
            funding_apr = self._trailing_funding_apr(symbol)
            if funding_apr is not None:
                if signal == 'buy' and funding_apr > self.funding_filter_apr:
                    self.logger.debug(f"{symbol}: long filtered by funding ({funding_apr:.0%} APR > "
                                      f"{self.funding_filter_apr:.0%})")
                    return None
                if signal == 'sell' and funding_apr < -self.funding_filter_apr:
                    self.logger.debug(f"{symbol}: short filtered by funding ({funding_apr:.0%} APR < "
                                      f"{-self.funding_filter_apr:.0%})")
                    return None

        # Volatility Targeting for Size?
        # Higher Vol -> Smaller Size (managed by position sizing logic, but we can signal confidence)
        confidence = abs(rank - 0.5) * 2 # 0.5 -> 0, 1.0 -> 1.0

        # ATR for the vol-aware stop (only when the feature is enabled)
        current_atr = None
        if self.stop_atr_mult > 0 and 'high' in ohlcv.columns and len(ohlcv) > 15:
            try:
                atr_series = calculate_atr(ohlcv['high'], ohlcv['low'], ohlcv['close'], 14)
                atr_val = float(atr_series.iloc[-1])
                if atr_val > 0 and not np.isnan(atr_val):
                    current_atr = atr_val
            except Exception:
                pass

        trend_note = (f" [Trend Filter: {'Above' if current_price > trend_ema else 'Below'} EMA200]"
                      if trend_ema is not None else " [Trend Filter: off]")
        return {
            'signal': signal,
            'reason': reason + trend_note,
            'price': current_price,
            'strategy': 'cross_sectional_momentum',
            'confidence': confidence,
            'rank': rank,
            'momentum': my_return,
            'atr': current_atr,
        }

    # ------------------------------------------------------------------
    # Scoring / ranking helpers
    # ------------------------------------------------------------------

    def _compute_stats(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, float]]:
        """Momentum, risk-adjusted score and bar volatility for one symbol."""
        close = ohlcv['close']
        end_idx = -1 - self.skip_period

        if not self.lookback_periods:
            if len(ohlcv) < self.lookback_period + self.skip_period:
                return None
            # Calculate Momentum (Return) over the window ending `skip_period`
            # bars ago (skip=0 preserves the original behavior)
            ref_price = close.iloc[end_idx]
            past_price = close.iloc[end_idx - self.lookback_period + 1]
            momentum = (ref_price / past_price) - 1

            # Calculate Volatility (over same lookback period)
            volatility = close.iloc[-self.lookback_period:].pct_change().std()
            if volatility == 0 or np.isnan(volatility):
                volatility = 1.0 # Avoid div/0

            # Score = Risk Adjusted Return
            return {'return': momentum, 'score': momentum / volatility, 'volatility': volatility}

        longest = max(self.lookback_periods)
        if len(ohlcv) < longest + self.skip_period:
            return None
        volatility = close.iloc[-longest:].pct_change().std()
        if volatility == 0 or np.isnan(volatility):
            volatility = 1.0
        ref_price = close.iloc[end_idx]
        rets, zs = [], []
        for k in self.lookback_periods:
            ret_k = (ref_price / close.iloc[end_idx - k + 1]) - 1
            rets.append(ret_k)
            zs.append(ret_k / (volatility * math.sqrt(k)))
        return {'return': float(np.mean(rets)), 'score': float(np.mean(zs)), 'volatility': volatility}

    def _update_universe_stats(self, symbol: str, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        stats = self._compute_stats(ohlcv)
        if stats is None:
            return None
        stats['timestamp'] = datetime.now()
        # Store in shared cache
        self._universe_stats[symbol] = stats
        return stats

    def _rank_of(self, my_score: float) -> float:
        """Fraction of the universe scoring <= my_score (approximate rank)."""
        sorted_scores = sorted(v.get('score', v.get('return', 0)) for v in self._universe_stats.values())
        try:
            idx = next(i for i, x in enumerate(sorted_scores) if x >= my_score)
            return (idx + 1) / len(sorted_scores)
        except StopIteration:
            return 1.0

    # ------------------------------------------------------------------
    # Funding filter
    # ------------------------------------------------------------------

    def set_market_api(self, market_api):
        """Late injection of the market API (hot-reload parity)."""
        self.market_api = market_api

    def _now(self) -> datetime:
        """Simulation time in backtests (mock exposes current_time), else wall clock."""
        sim_time = getattr(self.market_api, 'current_time', None)
        return sim_time if sim_time else datetime.now()

    def _trailing_funding_apr(self, symbol: str) -> Optional[float]:
        """
        Trailing mean funding, annualized (hourly rate x 8760). None when the
        API or enough history is unavailable — the filter then fails OPEN
        (no data never blocks a trade).
        """
        if not self.market_api or not hasattr(self.market_api, 'get_funding_history'):
            return None
        now = self._now()
        hour_bucket = now.replace(minute=0, second=0, microsecond=0)
        cached = self._funding_cache.get(symbol)
        if cached and cached[0] == hour_bucket:
            return cached[1]

        hours = self.funding_lookback_hours
        result: Optional[float] = None
        try:
            start_ms = int((now - timedelta(hours=hours)).timestamp() * 1000)
            end_ms = int(now.timestamp() * 1000)
            records = self.market_api.get_funding_history(symbol, start_ms, end_ms) or []
            rates = [float(r['fundingRate']) for r in records if r.get('fundingRate') is not None]
            if len(rates) >= max(2, hours // 2):
                result = (sum(rates) / len(rates)) * 24 * 365
        except Exception as e:
            self.logger.debug(f"Funding history unavailable for {symbol}: {e}")

        self._funding_cache[symbol] = (hour_bucket, result)
        return result

    # ------------------------------------------------------------------
    # Rank-decay exit
    # ------------------------------------------------------------------

    def needs_exit_data(self) -> bool:
        """Only fetch OHLCV in the exit monitor when the rank exit is on."""
        return self.exit_rank_percent > 0

    def should_exit(self, position: Any, current_price: float,
                    current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Close positions whose rank decayed out of the hold band.

        Long (momentum mode): hold while rank >= 1 - exit_rank_percent.
        Short: hold while rank <= exit_rank_percent. Reversal mode mirrors
        both. The held symbol's score is recomputed from the supplied OHLCV
        so a position never rides a stale cache entry.
        """
        if self.exit_rank_percent <= 0:
            return False, None

        symbol = getattr(position, 'symbol', None)
        ohlcv = (current_data or {}).get('ohlcv')
        if isinstance(ohlcv, dict):
            ohlcv = ohlcv.get(self.timeframe)
        if symbol and isinstance(ohlcv, pd.DataFrame) and not ohlcv.empty:
            self._update_universe_stats(symbol, ohlcv)

        stats = self._universe_stats.get(symbol)
        if not stats or len(self._universe_stats) < 5:
            return False, None

        rank = self._rank_of(stats['score'])
        side = str(getattr(position, 'side', '')).lower()
        # Which tail does this position belong to?
        holds_top = (side == 'long') != self.invert
        if holds_top and rank < 1.0 - self.exit_rank_percent:
            return True, f"rank_decay (rank {rank:.2f} < {1.0 - self.exit_rank_percent:.2f})"
        if not holds_top and rank > self.exit_rank_percent:
            return True, f"rank_decay (rank {rank:.2f} > {self.exit_rank_percent:.2f})"
        return False, None

    @classmethod
    def _cleanup_cache(cls):
        """Remove stale entries (> 4h old) from universe stats."""
        # Updated to 4h since we rebalance daily, but data refreshes hourly.
        cutoff = datetime.now() - timedelta(hours=4)
        to_remove = [k for k, v in cls._universe_stats.items() if v['timestamp'] < cutoff]
        for k in to_remove:
            del cls._universe_stats[k]

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                              signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Let winners run. No fixed TP, exit on Rank deterioration (Rebalance).
        """
        return 0.0 # Disabled
        
    def get_trailing_stop_config(self, entry_price: float = None, signal_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Trailing stop to capture trend collapses (trail_pct <= 0 disables).
        """
        return {
            'enabled': self.trail_pct > 0,
            'trail_pct': self.trail_pct,
            'activation_pct': self.trail_activation_pct
        }
    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Rank Confidence.
        
        Rank Confidence = abs(Rank - 0.5) * 2
        - Top/Bottom 1% -> ~1.0
        - Top/Bottom 10% -> ~0.8
        """
        if signal_context and 'confidence' in signal_context:
            return float(signal_context['confidence'])
            
        return 0.5

    def calculate_stop_loss(self, entry_price: float, side: str, signal_context: Dict[str, Any] = None) -> float:
        """
        Stop Loss for Momentum: ATR-scaled when stop_atr_mult > 0 and the
        signal carries an ATR (vol-aware width; the engine sizes off the
        implied stop distance so dollar risk stays constant), otherwise
        fixed stop_loss_pct (default 5%).
        The ExecutionEngine will clamp this if it exceeds Max Account Risk.
        """
        atr = (signal_context or {}).get('atr')
        if self.stop_atr_mult > 0 and atr and atr > 0 and entry_price > 0:
            sl_pct = min(0.25, self.stop_atr_mult * atr / entry_price)
        else:
            sl_pct = self.stop_loss_pct

        if side == 'long':
            return entry_price * (1 - sl_pct)
        else:
            return entry_price * (1 + sl_pct)
