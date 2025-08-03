"""
Trade model for representing trading data.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any


@dataclass
class Trade:
    """Represents a single trade."""
    
    symbol: str
    side: str  # 'buy' or 'sell'
    price: float
    size: float
    timestamp: datetime
    strategy: str
    order_id: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    pnl: Optional[float] = None
    pnl_percentage: Optional[float] = None
    analysis: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert trade to dictionary."""
        return {
            'symbol': self.symbol,
            'side': self.side,
            'price': self.price,
            'size': self.size,
            'timestamp': self.timestamp.isoformat(),
            'strategy': self.strategy,
            'order_id': self.order_id,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'pnl': self.pnl,
            'pnl_percentage': self.pnl_percentage,
            'analysis': self.analysis,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trade':
        """Create trade from dictionary."""
        return cls(
            symbol=data['symbol'],
            side=data['side'],
            price=data['price'],
            size=data['size'],
            timestamp=datetime.fromisoformat(data['timestamp']),
            strategy=data['strategy'],
            order_id=data.get('order_id'),
            stop_loss=data.get('stop_loss'),
            take_profit=data.get('take_profit'),
            pnl=data.get('pnl'),
            pnl_percentage=data.get('pnl_percentage'),
            analysis=data.get('analysis'),
        )


@dataclass
class Position:
    """Represents an open position."""
    
    symbol: str
    side: str  # 'long' or 'short'
    entry_price: float
    size: float
    entry_time: datetime
    strategy: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    current_price: Optional[float] = None
    capital_at_risk: Optional[float] = None  # Actual capital at risk
    
    @property
    def unrealized_pnl(self) -> Optional[float]:
        """Calculate unrealized PnL based on actual capital at risk."""
        if self.current_price is None:
            return None
        
        # Calculate price change percentage
        if self.side == 'long':
            price_change_pct = (self.current_price - self.entry_price) / self.entry_price
        else:
            price_change_pct = (self.entry_price - self.current_price) / self.entry_price
        
        # PnL is the price change percentage applied to the capital at risk
        if self.capital_at_risk is not None:
            return self.capital_at_risk * price_change_pct
        else:
            # Fallback to notional calculation if capital_at_risk is not set
            if self.side == 'long':
                return (self.current_price - self.entry_price) * self.size
            else:
                return (self.entry_price - self.current_price) * self.size
    
    @property
    def unrealized_pnl_percentage(self) -> Optional[float]:
        """Calculate unrealized PnL percentage based on capital at risk."""
        if self.current_price is None:
            return None
        
        if self.side == 'long':
            return ((self.current_price - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - self.current_price) / self.entry_price) * 100
    
    @property
    def capital_at_risk_pnl_percentage(self) -> Optional[float]:
        """Calculate PnL percentage based on capital at risk."""
        if self.current_price is None or self.capital_at_risk is None:
            return None
        
        pnl = self.unrealized_pnl
        if pnl is None:
            return None
        
        return (pnl / self.capital_at_risk) * 100
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert position to dictionary."""
        return {
            'symbol': self.symbol,
            'side': self.side,
            'entry_price': self.entry_price,
            'size': self.size,
            'entry_time': self.entry_time.isoformat(),
            'strategy': self.strategy,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'current_price': self.current_price,
            'capital_at_risk': self.capital_at_risk,
            'unrealized_pnl': self.unrealized_pnl,
            'unrealized_pnl_percentage': self.unrealized_pnl_percentage,
            'capital_at_risk_pnl_percentage': self.capital_at_risk_pnl_percentage,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Position':
        """Create position from dictionary."""
        return cls(
            symbol=data['symbol'],
            side=data['side'],
            entry_price=data['entry_price'],
            size=data['size'],
            entry_time=datetime.fromisoformat(data['entry_time']),
            strategy=data['strategy'],
            stop_loss=data.get('stop_loss'),
            take_profit=data.get('take_profit'),
            current_price=data.get('current_price'),
            capital_at_risk=data.get('capital_at_risk'),
        ) 