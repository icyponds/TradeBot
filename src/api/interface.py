from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Callable
import pandas as pd
from datetime import datetime

class MarketInterface(ABC):
    """
    Abstract interface for Market API implementations.
    Ensures parity between Real (HyperliquidAPI) and Mock (MockMarketAPI) implementations.
    """

    @abstractmethod
    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get the current price for a symbol."""
        pass

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """Get OHLCV data."""
        pass

    @abstractmethod
    def get_spot_token_for_perp(self, symbol: str) -> Optional[str]:
        """Get the spot token symbol corresponding to a perp symbol (e.g., 'BTC' for 'BTC')."""
        pass

    @abstractmethod
    def get_spot_price(self, token: str, quote: str = 'USDC') -> Optional[float]:
        """Get the spot price for a token."""
        pass

    @abstractmethod
    def get_spot_balance(self, asset: str) -> float:
        """Get balance of a spot asset."""
        pass

    @abstractmethod
    def get_perp_balance(self) -> Dict[str, float]:
        """Get perpetual account balance (margin/withdrawable)."""
        pass

    @abstractmethod
    def ensure_perp_funds(self, amount: float) -> bool:
        """Ensure sufficient funds in perp account (transfer if needed)."""
        pass

    @abstractmethod
    def ensure_spot_funds(self, amount: float) -> bool:
        """Ensure sufficient funds in spot account (transfer if needed)."""
        pass

    @abstractmethod
    def execute_order(self, symbol: str, side: str, size: float, 
                     reduce_only: bool = False, market_type: str = 'perp', 
                     urgency: str = 'normal', limit_price: float = None,
                     max_slippage_bps: int = None) -> Dict[str, Any]:
        """Execute an order."""
        pass

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        """Get current open positions."""
        pass

    @abstractmethod
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get current open orders."""
        pass

    @abstractmethod
    def get_account_balance(self) -> Dict[str, Any]:
        """Get overall account balance summary."""
        pass

    @abstractmethod
    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Get current funding rate."""
        pass

    @abstractmethod
    def get_market_data(self, symbol: str) -> Dict[str, Any]:
        """Get comprehensive market data for a symbol."""
        pass

    @abstractmethod
    def subscribe_symbol(self, symbol: str):
        """Subscribe to real-time updates for a symbol."""
        pass

    @abstractmethod
    def unsubscribe_symbol(self, symbol: str):
        """Unsubscribe from updates."""
        pass

    @abstractmethod
    def get_execution_fee(self, order_id: Any) -> float:
        """Get execution fee for a specific order."""
        pass
