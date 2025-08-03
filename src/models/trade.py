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
    
    @property
    def unrealized_pnl(self) -> Optional[float]:
        """Calculate unrealized PnL."""
        if self.current_price is None:
            return None
        
        if self.side == 'long':
            return (self.current_price - self.entry_price) * self.size
        else:
            return (self.entry_price - self.current_price) * self.size
    
    @property
    def unrealized_pnl_percentage(self) -> Optional[float]:
        """Calculate unrealized PnL percentage."""
        if self.current_price is None:
            return None
        
        if self.side == 'long':
            return ((self.current_price - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - self.current_price) / self.entry_price) * 100
    
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
            'unrealized_pnl': self.unrealized_pnl,
            'unrealized_pnl_percentage': self.unrealized_pnl_percentage,
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
        ) 