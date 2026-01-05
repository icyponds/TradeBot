"""
Trade model for representing trading data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List


@dataclass
class PositionLeg:
    """Represents a single leg of a multi-leg position."""
    
    symbol: str
    market_type: str  # 'perp', 'hip3', or 'spot'
    side: str  # 'long' or 'short'
    size: float
    entry_price: float
    order_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'market_type': self.market_type,
            'side': self.side,
            'size': self.size,
            'entry_price': self.entry_price,
            'order_id': self.order_id,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PositionLeg':
        return cls(
            symbol=data['symbol'],
            market_type=data['market_type'],
            side=data['side'],
            size=data['size'],
            entry_price=data['entry_price'],
            order_id=data.get('order_id'),
        )


@dataclass
class MultiLegPosition:
    """
    Represents a multi-leg position (e.g., funding rate arbitrage with perp + spot).
    
    Multi-leg positions are managed as a unit - all legs must be entered/exited together.
    """
    
    position_id: str  # Unique identifier for this multi-leg position
    strategy: str
    entry_time: datetime
    legs: List[PositionLeg] = field(default_factory=list)
    capital_at_risk: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)  # Strategy-specific data
    
    @property
    def primary_symbol(self) -> str:
        """Return the primary symbol (first leg's symbol without market suffix)."""
        if self.legs:
            symbol = self.legs[0].symbol
            return symbol.split('/')[0] if '/' in symbol else symbol
        return ""
    
    @property
    def net_delta(self) -> float:
        """Calculate net delta across all legs."""
        delta = 0.0
        for leg in self.legs:
            leg_delta = leg.size if leg.side == 'long' else -leg.size
            delta += leg_delta
        return delta
    
    @property
    def total_notional(self) -> float:
        """Calculate total notional value across all legs."""
        return sum(leg.size * leg.entry_price for leg in self.legs)
    
    def get_leg(self, market_type: str) -> Optional[PositionLeg]:
        """Get leg by market type."""
        for leg in self.legs:
            if leg.market_type == market_type:
                return leg
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'position_id': self.position_id,
            'strategy': self.strategy,
            'entry_time': self.entry_time.isoformat(),
            'legs': [leg.to_dict() for leg in self.legs],
            'capital_at_risk': self.capital_at_risk,
            'metadata': self.metadata,
            'primary_symbol': self.primary_symbol,
            'net_delta': self.net_delta,
            'total_notional': self.total_notional,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MultiLegPosition':
        return cls(
            position_id=data['position_id'],
            strategy=data['strategy'],
            entry_time=datetime.fromisoformat(data['entry_time']),
            legs=[PositionLeg.from_dict(leg) for leg in data['legs']],
            capital_at_risk=data.get('capital_at_risk'),
            metadata=data.get('metadata', {}),
        )


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
    leverage: Optional[float] = None  # Leverage used for the position
    
    # Trailing stop fields
    trailing_stop_enabled: bool = False
    trailing_stop_pct: float = 0.0  # e.g., 0.05 = 5% trail
    trailing_stop_activation_pct: float = 0.0  # Minimum gain before trailing starts (e.g., 0.03 = 3%)
    highest_price: Optional[float] = None  # For longs - track highest price since entry
    lowest_price: Optional[float] = None   # For shorts - track lowest price since entry
    trailing_stop_active: bool = False  # Whether trailing stop has been activated
    
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
            'leverage': self.leverage,
            'trailing_stop_enabled': self.trailing_stop_enabled,
            'trailing_stop_pct': self.trailing_stop_pct,
            'trailing_stop_activation_pct': self.trailing_stop_activation_pct,
            'highest_price': self.highest_price,
            'lowest_price': self.lowest_price,
            'trailing_stop_active': self.trailing_stop_active,
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
            leverage=data.get('leverage'),
            trailing_stop_enabled=data.get('trailing_stop_enabled', False),
            trailing_stop_pct=data.get('trailing_stop_pct', 0.0),
            trailing_stop_activation_pct=data.get('trailing_stop_activation_pct', 0.0),
            highest_price=data.get('highest_price'),
            lowest_price=data.get('lowest_price'),
            trailing_stop_active=data.get('trailing_stop_active', False),
        ) 