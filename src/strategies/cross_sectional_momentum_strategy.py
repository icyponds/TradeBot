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
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)
        
        csm_config = config.get('strategies', {}).get('cross_sectional_momentum', {})
        
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
        
        if len(ohlcv) >= self.lookback_period + self.skip_period:
            # Calculate Momentum (Return) over the window ending `skip_period`
            # bars ago (skip=0 preserves the original behavior)
            end_idx = -1 - self.skip_period
            ref_price = ohlcv['close'].iloc[end_idx]
            past_price = ohlcv['close'].iloc[end_idx - self.lookback_period + 1]
            momentum = (ref_price / past_price) - 1

            # Calculate Volatility (over same lookback period)
            volatility = ohlcv['close'].iloc[-self.lookback_period:].pct_change().std()
            if volatility == 0 or np.isnan(volatility):
                volatility = 1.0 # Avoid div/0
                
            # Score = Risk Adjusted Return
            score = momentum / volatility
            
            # Store in shared cache
            self._universe_stats[symbol] = {
                'return': momentum,
                'score': score,
                'timestamp': datetime.now(),
                'volatility': volatility
            }
        
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
        scores = [v.get('score', v.get('return', 0)) for k, v in self._universe_stats.items()]
        my_score = self._universe_stats.get(symbol, {}).get('score', 0)
        my_return = self._universe_stats.get(symbol, {}).get('return', 0)
        
        sorted_scores = sorted(scores)
        try:
             # Find approximate rank
             idx = next(i for i, x in enumerate(sorted_scores) if x >= my_score)
             rank = (idx + 1) / len(sorted_scores)
        except StopIteration:
            rank = 1.0

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
        Trailing stop to capture trend collapses.
        """
        return {
            'enabled': True,
            'trail_pct': 0.04,
            'activation_pct': 0.05
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
