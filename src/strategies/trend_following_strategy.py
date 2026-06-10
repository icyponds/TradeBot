"""
Time-Series Momentum (Donchian Trend-Following) Strategy.

Per-asset ABSOLUTE momentum, unlike CrossSectionalMomentumStrategy which
trades RELATIVE rank (and can therefore long assets that are merely falling
slowest). Classic Donchian channel breakout with ATR-scaled exits:

1. Entry: last CLOSED candle closes above the `entry_lookback`-bar high
   (long) or below the `entry_lookback`-bar low (short), channel computed
   strictly BEFORE the breakout bar. Fresh breakouts only.
2. Exit: close crossing the `exit_lookback`-bar opposite channel, plus a
   chandelier ATR trailing stop. No fixed take-profit (let winners run).
3. Deliberately low trade frequency: fee drag (5bps taker per leg) is the
   dominant loss source across every higher-frequency strategy tested on
   this book (2026-06 OOS matrix).
"""

import time
from typing import Dict, Any, Optional, Tuple
import pandas as pd
from .base_strategy import BaseStrategy
from src.utils.statistics import calculate_atr


class TrendFollowingStrategy(BaseStrategy):
    """Donchian channel trend-following (time-series momentum)."""

    PREFERRED_TIMEFRAME = '4h'

    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)

        tf_config = config.get('strategies', {}).get('trend_following', {})

        self.entry_lookback = int(tf_config.get('entry_lookback', 60))   # 60 x 4h = 10 days
        self.exit_lookback = int(tf_config.get('exit_lookback', 30))     # 30 x 4h = 5 days
        self.atr_length = int(tf_config.get('atr_length', 14))
        self.atr_multiplier_sl = float(tf_config.get('atr_multiplier_sl', 2.0))
        # Chandelier trail, wider than vol_breakout's: trends need room
        self.trail_atr_mult = float(tf_config.get('trail_atr_mult', 3.0))
        self.trail_activation_atr_mult = float(tf_config.get('trail_activation_atr_mult', 1.5))
        # 'both' | 'long_only' | 'short_only'
        self.direction = tf_config.get('direction', 'both')

        self.logger.info(f"Initialized Trend Following Strategy: "
                         f"Donchian({self.entry_lookback}/{self.exit_lookback}), "
                         f"SL={self.atr_multiplier_sl}xATR, Trail={self.trail_atr_mult}xATR, "
                         f"Direction={self.direction}")

    def _drop_forming_bar(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """Return only closed candles (live cache includes the forming bar)."""
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

    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        tf_data = self._get_timeframe_data(ohlcv)
        if tf_data is None:
            return None
        return self._generate_signal_internal(tf_data, symbol)

    def _generate_signal_internal(self, ohlcv: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
        bars = self._drop_forming_bar(ohlcv)
        # +2: one bar for the breakout itself, one for the freshness check
        if len(bars) < self.entry_lookback + max(self.atr_length, 2) + 2:
            return None

        closes = bars['close']
        highs = bars['high']
        lows = bars['low']
        breakout_close = closes.iloc[-1]

        # Channel over the `entry_lookback` bars strictly BEFORE the breakout
        # bar (including it would make the breakout unreachable by definition).
        channel_high = highs.iloc[-(self.entry_lookback + 1):-1].max()
        channel_low = lows.iloc[-(self.entry_lookback + 1):-1].min()
        # Same channel as seen one bar earlier, for the freshness check
        prev_channel_high = highs.iloc[-(self.entry_lookback + 2):-2].max()
        prev_channel_low = lows.iloc[-(self.entry_lookback + 2):-2].min()
        prev_close = closes.iloc[-2]

        signal = 'hold'
        reason = ''
        if breakout_close > channel_high and prev_close <= prev_channel_high:
            signal = 'buy'
            reason = (f"TSMOM: Close {breakout_close} > {self.entry_lookback}-bar high "
                      f"{channel_high}")
        elif breakout_close < channel_low and prev_close >= prev_channel_low:
            signal = 'sell'
            reason = (f"TSMOM: Close {breakout_close} < {self.entry_lookback}-bar low "
                      f"{channel_low}")

        if signal == 'hold':
            return None

        if (self.direction == 'long_only' and signal == 'sell') or \
           (self.direction == 'short_only' and signal == 'buy'):
            return None

        atr = calculate_atr(highs, lows, closes, self.atr_length)
        current_atr = float(atr.iloc[-1])
        if pd.isna(current_atr) or current_atr <= 0:
            return None

        # Breakout margin in ATR units -> confidence (0.5 at the channel edge,
        # saturating toward 1.0 at a 2-ATR overshoot)
        margin = (breakout_close - channel_high) if signal == 'buy' else (channel_low - breakout_close)
        confidence = max(0.5, min(1.0, 0.5 + (margin / current_atr) / 4))

        return {
            'signal': signal,
            'reason': reason,
            'price': breakout_close,
            'strategy': 'trend_following',
            'atr': current_atr,
            'confidence': confidence,
        }

    def calculate_stop_loss(self, entry_price: float, side: str, signal_context: Dict[str, Any] = None) -> float:
        """ATR-based initial stop (context 'atr', 2% fallback)."""
        atr = None
        if signal_context and 'atr' in signal_context:
            atr = signal_context['atr']
        if atr is None or atr <= 0:
            atr = entry_price * 0.02

        sl_dist = atr * self.atr_multiplier_sl
        if side == 'long':
            return entry_price - sl_dist
        return entry_price + sl_dist

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                              signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """No fixed TP — exits are channel cross or trailing stop."""
        return 0.0

    def get_trailing_stop_config(self, entry_price: float = None, signal_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Chandelier trail in ATR units, converted to pct at entry."""
        atr = signal_context.get('atr') if signal_context else None

        if entry_price and atr and atr > 0:
            atr_pct = atr / entry_price
            trail_pct = min(0.20, max(0.01, self.trail_atr_mult * atr_pct))
            activation_pct = min(0.15, max(0.005, self.trail_activation_atr_mult * atr_pct))
            return {
                'enabled': True,
                'trail_pct': trail_pct,
                'activation_pct': activation_pct,
            }

        return {
            'enabled': True,
            'trail_pct': 0.06,
            'activation_pct': 0.03,
        }

    def should_exit(self, position: Any, current_price: float,
                    current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """Donchian opposite-channel exit (turtle-style)."""
        ohlcv = current_data.get('ohlcv') if current_data else None
        if ohlcv is None or len(ohlcv) < self.exit_lookback + 2:
            return False, None

        bars = self._drop_forming_bar(ohlcv)
        if len(bars) < self.exit_lookback + 1:
            return False, None

        side = getattr(position, 'side', None)
        last_close = bars['close'].iloc[-1]
        # Channel strictly before the bar being evaluated
        if side == 'long':
            exit_level = bars['low'].iloc[-(self.exit_lookback + 1):-1].min()
            if last_close < exit_level:
                return True, (f"donchian_exit (close {last_close} < "
                              f"{self.exit_lookback}-bar low {exit_level})")
        elif side == 'short':
            exit_level = bars['high'].iloc[-(self.exit_lookback + 1):-1].max()
            if last_close > exit_level:
                return True, (f"donchian_exit (close {last_close} > "
                              f"{self.exit_lookback}-bar high {exit_level})")

        return False, None

    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None,
                                  signal_context: Dict[str, Any] = None) -> float:
        if signal_context and 'confidence' in signal_context:
            return float(signal_context['confidence'])
        return 0.5
