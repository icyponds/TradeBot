"""
Online change-point detection utilities.

We use lightweight streaming detectors to identify distribution shifts in a market proxy
(e.g., BTC returns/volatility) and temporarily gate new entries for fragile strategies.

This is intentionally simple and dependency-light to keep runtime stable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass
class PageHinkley:
    """
    Page-Hinkley test for change detection.

    Typical usage:
        ph = PageHinkley(delta=0.0001, threshold=0.02)
        triggered, score = ph.update(x)

    Notes:
    - This implementation detects positive shifts in the mean of x.
      For volatility shift detection, feed x = abs(return) or a volatility estimate.
    """

    delta: float = 0.0
    threshold: float = 0.02
    alpha: float = 0.99  # EWMA for mean estimate

    _mean: float = 0.0
    _cum: float = 0.0
    _min_cum: float = 0.0
    _initialized: bool = False

    def reset(self) -> None:
        self._mean = 0.0
        self._cum = 0.0
        self._min_cum = 0.0
        self._initialized = False

    def update(self, x: float) -> Tuple[bool, float]:
        """
        Update detector with a new observation.

        Returns:
            (triggered, score)
        """
        if not self._initialized:
            self._mean = float(x)
            self._cum = 0.0
            self._min_cum = 0.0
            self._initialized = True
            return False, 0.0

        # EWMA mean for numerical stability in non-stationary series
        self._mean = self.alpha * self._mean + (1.0 - self.alpha) * float(x)

        # cumulative deviation from mean, adjusted by delta
        self._cum += (float(x) - self._mean - self.delta)
        self._min_cum = min(self._min_cum, self._cum)

        score = self._cum - self._min_cum
        triggered = score > self.threshold
        return triggered, score

    def get_current_score(self) -> float:
        """Return current cumulative deviation score without updating."""
        return self._cum - self._min_cum

