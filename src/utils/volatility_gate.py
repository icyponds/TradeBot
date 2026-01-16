"""
Volatility Gate: Per-asset change-point detection with correlation-aware blocking.

This module provides hysteresis-based gating for mean-reverting strategies,
replacing simple time-based cooldowns with volatility-responsive controls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, TYPE_CHECKING

from src.utils.change_point import PageHinkley

if TYPE_CHECKING:
    from src.utils.correlation_manager import CorrelationManager


logger = logging.getLogger(__name__)


@dataclass
class BlockInfo:
    """Information about an active block on a symbol."""
    triggered_by: str  # The symbol that originally triggered the block
    score: float       # The score when block was triggered
    timestamp: datetime
    reason: str


class VolatilityGate:
    """
    Per-asset change-point detection with correlation-aware blocking.
    
    Uses Page-Hinkley detectors per symbol to detect volatility spikes,
    and propagates blocks to correlated assets based on correlation threshold.
    
    Features:
    - Hysteresis-based entry/exit (not time-based)
    - Correlation-aware block propagation
    - Per-symbol detector state
    """
    
    def __init__(
        self,
        correlation_manager: Optional['CorrelationManager'] = None,
        entry_threshold: float = 0.02,
        exit_threshold: float = 0.01,
        correlation_block_threshold: float = 0.70,
        delta: float = 0.0,
        alpha: float = 0.99,
        apply_to_strategies: Optional[Set[str]] = None,
    ):
        """
        Initialize the VolatilityGate.
        
        Args:
            correlation_manager: CorrelationManager for looking up correlated pairs
            entry_threshold: Score above which we block (default 0.02)
            exit_threshold: Score below which we unblock (default 0.01)
            correlation_block_threshold: Correlation threshold for propagating blocks
            delta: Page-Hinkley delta parameter
            alpha: Page-Hinkley EWMA alpha parameter
            apply_to_strategies: Set of strategy names this gate applies to
        """
        self.correlation_manager = correlation_manager
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.correlation_block_threshold = correlation_block_threshold
        self.delta = delta
        self.alpha = alpha
        self.apply_to_strategies = apply_to_strategies or {"ou_mean_reversion", "stat_arb"}
        
        # Per-symbol Page-Hinkley detectors
        self._detectors: Dict[str, PageHinkley] = {}
        
        # Per-symbol current scores (for hysteresis checking)
        self._scores: Dict[str, float] = {}
        
        # Active blocks: symbol -> BlockInfo
        self._active_blocks: Dict[str, BlockInfo] = {}
        
        # Cache of correlation lookups to avoid repeated queries
        self._correlation_cache: Dict[str, List[str]] = {}
        self._correlation_cache_time: Optional[datetime] = None
        
    def _get_or_create_detector(self, symbol: str) -> PageHinkley:
        """Get existing detector or create new one for symbol."""
        if symbol not in self._detectors:
            self._detectors[symbol] = PageHinkley(
                delta=self.delta,
                threshold=self.entry_threshold,
                alpha=self.alpha,
            )
        return self._detectors[symbol]
    
    def _get_correlated_symbols(self, symbol: str) -> List[str]:
        """
        Get all symbols correlated with the given symbol above threshold.
        
        Uses CorrelationManager's correlation matrix if available.
        """
        if self.correlation_manager is None:
            return []
        
        # Refresh cache every 5 minutes
        now = datetime.now()
        if (self._correlation_cache_time is None or 
            (now - self._correlation_cache_time).total_seconds() > 300):
            self._correlation_cache.clear()
            self._correlation_cache_time = now
        
        if symbol in self._correlation_cache:
            return self._correlation_cache[symbol]
        
        correlated = []
        try:
            # Get correlation matrix from manager
            if hasattr(self.correlation_manager, 'correlation_matrix'):
                matrix = self.correlation_manager.correlation_matrix
                if matrix is not None and symbol in matrix.index:
                    row = matrix.loc[symbol]
                    for other_symbol, corr in row.items():
                        if other_symbol != symbol and abs(corr) >= self.correlation_block_threshold:
                            correlated.append(other_symbol)
            
            # Fallback: use get_correlated_symbol for primary pair
            elif hasattr(self.correlation_manager, 'get_correlated_symbol'):
                primary = self.correlation_manager.get_correlated_symbol(symbol)
                if primary:
                    correlated.append(primary)
                    
        except Exception as e:
            logger.debug(f"Error getting correlated symbols for {symbol}: {e}")
        
        self._correlation_cache[symbol] = correlated
        return correlated
    
    def update(self, symbol: str, abs_return: float) -> bool:
        """
        Update detector with new observation and check for state changes.
        
        Args:
            symbol: The trading symbol
            abs_return: Absolute return value (|r|)
            
        Returns:
            True if symbol is currently blocked, False otherwise
        """
        detector = self._get_or_create_detector(symbol)
        _, score = detector.update(abs_return)
        self._scores[symbol] = score
        
        # Hysteresis entry: score exceeds entry threshold
        if score > self.entry_threshold and symbol not in self._active_blocks:
            self._activate_block(symbol, score)
        
        # Hysteresis exit: score drops below exit threshold
        elif score < self.exit_threshold and symbol in self._active_blocks:
            # Only clear if this symbol was the trigger (not a propagated block)
            block_info = self._active_blocks.get(symbol)
            if block_info and block_info.triggered_by == symbol:
                self._clear_block(symbol)
        
        return symbol in self._active_blocks
    
    def _activate_block(self, symbol: str, score: float) -> None:
        """Activate block on symbol and propagate to correlated assets."""
        now = datetime.now()
        reason = f"change_point(score={score:.4f})"
        
        # Block the triggering symbol
        self._active_blocks[symbol] = BlockInfo(
            triggered_by=symbol,
            score=score,
            timestamp=now,
            reason=reason,
        )
        
        blocked_symbols = [symbol]
        
        # Propagate to correlated symbols
        correlated = self._get_correlated_symbols(symbol)
        for corr_symbol in correlated:
            if corr_symbol not in self._active_blocks:
                self._active_blocks[corr_symbol] = BlockInfo(
                    triggered_by=symbol,
                    score=score,
                    timestamp=now,
                    reason=f"correlated_with({symbol})",
                )
                blocked_symbols.append(corr_symbol)
        
        logger.warning(
            f"⚠️ Change-point detected on {symbol}: score={score:.4f}. "
            f"Blocking entries for {sorted(self.apply_to_strategies)} on {blocked_symbols}."
        )
    
    def _clear_block(self, trigger_symbol: str) -> None:
        """Clear block from trigger symbol and all symbols it propagated to."""
        cleared = []
        
        # Find all symbols blocked by this trigger
        to_clear = [
            sym for sym, info in self._active_blocks.items()
            if info.triggered_by == trigger_symbol
        ]
        
        for sym in to_clear:
            del self._active_blocks[sym]
            cleared.append(sym)
            
            # Reset the detector for this symbol so it starts fresh
            if sym in self._detectors:
                self._detectors[sym].reset()
        
        if cleared:
            logger.info(f"✅ Volatility gate cleared for {cleared}")
    
    def is_blocked(self, symbol: str) -> bool:
        """Check if a symbol is currently blocked."""
        return symbol in self._active_blocks
    
    def is_strategy_blocked(self, strategy_name: str, symbol: str) -> bool:
        """
        Check if a strategy is blocked for a specific symbol.
        
        Args:
            strategy_name: Name of the strategy
            symbol: Trading symbol
            
        Returns:
            True if blocked, False otherwise
        """
        if strategy_name not in self.apply_to_strategies:
            return False
        return self.is_blocked(symbol)
    
    def get_block_reason(self, symbol: str) -> Optional[str]:
        """Get the reason for a block, or None if not blocked."""
        info = self._active_blocks.get(symbol)
        return info.reason if info else None
    
    def get_current_score(self, symbol: str) -> float:
        """Get the current volatility score for a symbol."""
        return self._scores.get(symbol, 0.0)
    
    def get_all_blocked_symbols(self) -> List[str]:
        """Get list of all currently blocked symbols."""
        return list(self._active_blocks.keys())
    
    def get_status_summary(self) -> Dict:
        """Get a summary of the current gate status."""
        return {
            "blocked_count": len(self._active_blocks),
            "blocked_symbols": self.get_all_blocked_symbols(),
            "detector_count": len(self._detectors),
            "entry_threshold": self.entry_threshold,
            "exit_threshold": self.exit_threshold,
        }
