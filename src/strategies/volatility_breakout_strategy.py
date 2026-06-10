"""
Volatility Breakout Strategy.

This strategy captures explosive moves that rarely occur but offer high reward-to-risk ratios.
It identifies periods of market consolidation ("squeezes") and enters when price breaks out
with expanding volatility.

Logic:
1. Identify Squeeze: Bollinger Band Width in the lowest percentile of its own
   trailing distribution (falls back to an absolute threshold with short history).
2. Signal Breakout: Last CLOSED candle closes above Upper Band (Long) or below
   Lower Band (Short). The currently forming candle is never used - intrabar
   spikes that fade before the close would otherwise cause churn entries.
3. Confirmation: Volume expansion on the breakout candle.
4. Exit: ATR-based (chandelier-style) trailing stop, TP, or time decay.
"""

import logging
import time
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy
from src.utils.statistics import hurst_exponent, calculate_atr, calculate_bollinger_bands

class VolatilityBreakoutStrategy(BaseStrategy):
    """
    Volatility Breakout Strategy (Bollinger Band Squeeze).
    
    Captures trends starting from consolidation zones.
    """
    
    # 1h timeframe is ideal for capturing multi-hour/day moves
    PREFERRED_TIMEFRAME = '1h'
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)
        
        # Strategy parameters from config
        vb_config = config.get('strategies', {}).get('volatility_breakout', {})
        
        # Bollinger Band settings
        self.bb_length = vb_config.get('bb_length', 20)
        self.bb_std = vb_config.get('bb_std', 2.0)
        
        # Squeeze detection (percentile-based, asset-relative):
        # squeeze = bandwidth in the lowest `squeeze_percentile` of its own
        # trailing `squeeze_window` distribution. An absolute threshold means
        # completely different things for BTC vs a high-vol small cap, which
        # biases which pairs can ever signal; the percentile rule does not.
        self.squeeze_percentile = vb_config.get('squeeze_percentile', 0.20)
        self.squeeze_window = vb_config.get('squeeze_window', 100)
        # Absolute fallback used while bandwidth history is shorter than squeeze_window
        self.squeeze_threshold = vb_config.get('squeeze_threshold', 0.10)

        # Volume confirmation: breakout candle volume must exceed
        # `volume_mult` x the trailing median. Skipped when volume data is
        # unavailable (e.g. tick-built bars carry zero volume).
        self.volume_mult = vb_config.get('volume_mult', 1.5)
        self.volume_lookback = vb_config.get('volume_lookback', 20)

        # ATR Trailing Stop settings
        self.atr_length = vb_config.get('atr_length', 14)
        self.atr_multiplier_sl = vb_config.get('atr_multiplier_sl', 2.0)   # Initial Stop Loss
        self.atr_multiplier_tp = vb_config.get('atr_multiplier_tp', 4.0)   # Take Profit (optional)
        # Chandelier-style trail: distances in ATR units (converted to pct at entry)
        self.trail_atr_mult = vb_config.get('trail_atr_mult', 2.5)
        self.trail_activation_atr_mult = vb_config.get('trail_activation_atr_mult', 1.0)

        # Regime filter: minimum Hurst exponent to enter (0.5 = random walk)
        self.min_hurst = vb_config.get('min_hurst', 0.5)

        # Macro trend filter: only take breakouts in the direction of the
        # long EMA (no shorts above it, no longs below it). Counter-trend
        # breakouts were the dominant loss source on liquid symbols
        # (2026-06 backtest: rally-month shorts stopped out en masse).
        self.trend_filter_enabled = vb_config.get('trend_filter_enabled', True)
        self.trend_ema_period = vb_config.get('trend_ema_period', 200)

        # Time decay: exit stagnant breakouts after N hours if not in profit
        self.time_decay_hours = vb_config.get('time_decay_hours', 16)

        self.logger.info(f"Initialized Volatility Breakout Strategy: "
                        f"BB({self.bb_length},{self.bb_std}), "
                        f"Squeeze<p{self.squeeze_percentile*100:.0f} (fallback<{self.squeeze_threshold}), "
                        f"Vol>{self.volume_mult}x, Hurst>{self.min_hurst}, "
                        f"TimeDecay={self.time_decay_hours}h")
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate breakout signal.
        """
        tf_data = self._get_timeframe_data(ohlcv)
        if tf_data is None:
            return None
        
        return self._generate_signal_internal(tf_data, symbol)
    
    def _drop_forming_bar(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """
        Return only closed candles.

        The live OHLCV cache includes the currently forming bar as the last
        row. Evaluating breakouts on it causes intrabar flip-flops (spikes
        that fade before the close) and contaminates the Bollinger Bands with
        the current price itself.
        """
        if len(ohlcv) < 2:
            return ohlcv
        try:
            interval_s = self.TIMEFRAME_MINUTES.get(self.timeframe, 60) * 60
            last_ts = ohlcv.index[-1].timestamp()
            if last_ts + interval_s > time.time():
                return ohlcv.iloc[:-1]
        except Exception:
            pass
        return ohlcv

    def _is_squeeze_at(self, bandwidth: pd.Series, idx: int) -> bool:
        """
        Squeeze check for the bar at position `idx` (negative index).

        Uses the percentile rule against the bar's own trailing distribution;
        falls back to the absolute threshold when history is too short.
        """
        bw = bandwidth.iloc[idx]
        if pd.isna(bw):
            return False

        # Trailing window strictly BEFORE the bar being evaluated
        end = idx if idx != -1 else None
        history = bandwidth.iloc[:end].dropna().tail(self.squeeze_window) if end is not None \
            else bandwidth.iloc[:-1].dropna().tail(self.squeeze_window)

        if len(history) >= self.squeeze_window // 2:
            return bool(bw <= history.quantile(self.squeeze_percentile))
        return bool(bw < self.squeeze_threshold)

    def _generate_signal_internal(self, ohlcv: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
        """Internal signal generation logic. Evaluates the last CLOSED candle."""

        bars = self._drop_forming_bar(ohlcv)
        if len(bars) < max(self.bb_length, self.atr_length) + 5:
            return None

        closes = bars['close']
        highs = bars['high']
        lows = bars['low']
        breakout_close = closes.iloc[-1]  # close of the last CLOSED candle

        # 1. Calculate Bollinger Bands
        bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes, self.bb_length, self.bb_std)

        # 2. Calculate Bandwidth = (Upper - Lower) / Middle
        bandwidth = (bb_upper - bb_lower) / bb_middle
        current_bandwidth = bandwidth.iloc[-1]

        # 3. Check for Squeeze on the bars BEFORE the breakout candle
        # (the breakout itself expands the bands, so we look at -2 / -3)
        valid_setup = self._is_squeeze_at(bandwidth, -2) or self._is_squeeze_at(bandwidth, -3)

        # 4. Regime Filter: Hurst Exponent (Trend Checking)
        # We only want to enter breakouts if the market is in a Trending Regime (H > 0.5)
        hurst = hurst_exponent(closes)
        valid_regime = hurst > self.min_hurst

        signal = 'hold'
        reason = ''

        if valid_setup and valid_regime:
            # Fresh breakout only: this candle closed outside the band while
            # the previous one closed inside (prevents re-signaling the same move)
            if breakout_close > bb_upper.iloc[-1] and closes.iloc[-2] <= bb_upper.iloc[-2]:
                signal = 'buy'
                reason = (f"Volatility Breakout: Close {breakout_close} > Upper BB "
                          f"(BW={current_bandwidth:.3f}, H={hurst:.2f})")
            elif breakout_close < bb_lower.iloc[-1] and closes.iloc[-2] >= bb_lower.iloc[-2]:
                signal = 'sell'
                reason = (f"Volatility Breakout: Close {breakout_close} < Lower BB "
                          f"(BW={current_bandwidth:.3f}, H={hurst:.2f})")

        if signal == 'hold':
            return None

        # Macro trend filter: breakouts against the long EMA fail far more
        # often than they follow through. Applied only when enough history
        # exists to compute the EMA.
        if self.trend_filter_enabled and len(closes) >= self.trend_ema_period:
            trend_ema = closes.ewm(span=self.trend_ema_period, adjust=False).mean().iloc[-1]
            if (signal == 'sell' and breakout_close > trend_ema) or \
               (signal == 'buy' and breakout_close < trend_ema):
                self.logger.debug(
                    f"[{symbol}] Breakout rejected: counter-trend vs EMA{self.trend_ema_period} "
                    f"(close={breakout_close:.4f}, ema={trend_ema:.4f})"
                )
                return None

        # 5. Volume confirmation: breakout candle must show expansion vs the
        # trailing median. Skipped when bars carry no volume data (tick-built).
        volumes = bars.get('volume')
        if volumes is not None and len(volumes) > self.volume_lookback:
            ref_volume = volumes.iloc[-(self.volume_lookback + 1):-1].median()
            breakout_volume = volumes.iloc[-1]
            if ref_volume > 0 and breakout_volume < self.volume_mult * ref_volume:
                self.logger.debug(
                    f"[{symbol}] Breakout rejected: no volume expansion "
                    f"({breakout_volume:.0f} < {self.volume_mult}x median {ref_volume:.0f})"
                )
                return None

        # Calculate ATR for dynamic stops
        atr = calculate_atr(highs, lows, closes, self.atr_length)
        current_atr = atr.iloc[-1]

        return {
            'signal': signal,
            'reason': reason,
            'price': breakout_close,
            'strategy': 'volatility_breakout',
            'atr': current_atr,
            'bandwidth': current_bandwidth,
            'hurst': hurst
        }

    def calculate_stop_loss(self, entry_price: float, side: str, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate Stop Loss based on Average True Range (ATR).
        Requires 'atr' to be passed in signal_context, or uses a default fallback.
        """
        atr = None
        if signal_context and 'atr' in signal_context:
            atr = signal_context['atr']
        
        # Fallback if ATR is missing from context
        if atr is None or atr <= 0:
            atr = entry_price * 0.02  # Default to 2% if missing
            
        sl_dist = atr * self.atr_multiplier_sl
        
        if side == 'long':
            return entry_price - sl_dist
        else:
            return entry_price + sl_dist



    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate Take Profit based on Average True Range (ATR).
        Aligns reward expectations with the actual market volatility.
        """
        tp_dist = entry_price * 0.10  # Default fallback
        
        if ohlcv is not None:
            try:
                tf_data = self._get_timeframe_data(ohlcv)
                if tf_data is not None and len(tf_data) > self.atr_length:
                    atr = calculate_atr(tf_data['high'], tf_data['low'], tf_data['close'], self.atr_length)
                    current_atr = atr.iloc[-1]
                    tp_dist = current_atr * self.atr_multiplier_tp
            except Exception as e:
                self.logger.error(f"Error calculating dynamic TP: {e}")
        
        if side == 'long':
            return entry_price + tp_dist
        else:
            return entry_price - tp_dist
            
    def get_trailing_stop_config(self, entry_price: float = None, signal_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Get trailing stop configuration (chandelier-style).

        Distances are derived from the ATR at entry so the trail uses the same
        volatility units as the SL/TP stack - a fixed 3% trail on a 4h
        timeframe can sit INSIDE the 1.5x ATR initial stop and silently
        override the designed risk geometry.
        """
        atr = signal_context.get('atr') if signal_context else None

        if entry_price and atr and atr > 0:
            atr_pct = atr / entry_price
            # Clamp to sane bounds so a corrupt ATR can't disable the trail
            trail_pct = min(0.15, max(0.005, self.trail_atr_mult * atr_pct))
            activation_pct = min(0.10, max(0.003, self.trail_activation_atr_mult * atr_pct))
            return {
                'enabled': True,
                'trail_pct': trail_pct,
                'activation_pct': activation_pct,
            }

        # Fallback when ATR is unavailable: previous fixed-percentage behavior
        return {
            'enabled': True,
            'trail_pct': 0.03,         # 3% trailing stop (tight-ish for breakouts)
            'activation_pct': 0.02,    # Activate quickly after 2% gain
        }

    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Exit logic beyond stops: Time-based stop (Time Decay).
        If a volatility breakout doesn't materialize into a trend quickly,
        the premise of the trade is broken. Exit after 16 hours if not in profit.
        """
        if hasattr(position, 'entry_time') and position.entry_time:
            entry_time = position.entry_time
            try:
                if isinstance(entry_time, str):
                    entry_time = pd.to_datetime(entry_time)
                
                from datetime import datetime, timedelta
                
                # CRITICAL: Use simulation time from current_data if available, else fallback to real time
                now = current_data.get('timestamp') if current_data and 'timestamp' in current_data else datetime.now()
                if isinstance(now, str):
                    now = pd.to_datetime(now)
                
                # Timezone awareness safety
                if entry_time.tzinfo and not getattr(now, 'tzinfo', None):
                   now = now.astimezone() if hasattr(now, 'astimezone') else now.replace(tzinfo=entry_time.tzinfo)
                elif not getattr(entry_time, 'tzinfo', None) and getattr(now, 'tzinfo', None):
                   entry_time = entry_time.replace(tzinfo=now.tzinfo)

                time_held = now - entry_time
                
                # Check Time Decay (e.g. breakout failed to trend)
                if time_held > timedelta(hours=self.time_decay_hours):
                    side = getattr(position, 'side', None)
                    entry_price = getattr(position, 'entry_price', current_price)
                    
                    pnl_pct = 0.0
                    if side == 'long':
                         pnl_pct = (current_price - entry_price) / entry_price
                    elif side == 'short':
                         pnl_pct = (entry_price - current_price) / entry_price
                         
                    # Exit if stagnant (less than 1% profit after 16 hours)
                    if pnl_pct < 0.01:
                        return True, f"time_decay_stop (held {time_held}, pnl {pnl_pct*100:.2f}%)"
                        
            except Exception as e:
                self.logger.warning(f"Error checking time decay in breakout: {e}")
                
        return False, None
    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Trend Persistence (Hurst Exponent).
        
        Mapping:
        - Hurst 0.5 -> 0.5 Strength
        - Hurst 1.0 -> 1.0 Strength
        """
        if signal_context and 'hurst' in signal_context:
            hurst = float(signal_context['hurst'])
            # Clamp and pass through as it maps naturally (0.5 to 1.0)
            return max(0.5, min(1.0, hurst))
            
        return 0.5
