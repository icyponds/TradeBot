"""
Comprehensive performance tracking for trading strategies.

Tracks all standard trading metrics including:
- PnL (total, realized, unrealized)
- Win Rate
- Risk-Reward Ratio
- Profit Factor
- Maximum Drawdown
- Sharpe Ratio
- Expectancy
- Per-strategy performance
- Time-based analytics

Storage: SQLite database
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
import statistics

from .trade_database import TradeDatabase


@dataclass
class CompletedTrade:
    """Represents a completed (closed) trade with full metrics."""
    
    symbol: str
    strategy: str
    side: str  # 'long' or 'short'
    entry_price: float
    exit_price: float
    size: float
    entry_time: datetime
    exit_time: datetime
    pnl: float
    pnl_percentage: float
    capital_at_risk: float
    exit_reason: str  # 'stop_loss', 'take_profit', 'manual', 'timeout', etc.
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: Optional[float] = None
    fees: float = 0.0
    
    @property
    def is_winner(self) -> bool:
        """Check if trade was profitable."""
        return self.pnl > 0
    
    @property
    def risk_reward_achieved(self) -> Optional[float]:
        """Calculate the achieved risk-reward ratio."""
        if self.stop_loss is None or self.entry_price == self.stop_loss:
            return None
        
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.exit_price - self.entry_price)
        
        if risk == 0:
            return None
        
        return reward / risk
    
    @property
    def duration_hours(self) -> float:
        """Calculate trade duration in hours."""
        return (self.exit_time - self.entry_time).total_seconds() / 3600
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'symbol': self.symbol,
            'strategy': self.strategy,
            'side': self.side,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'size': self.size,
            'entry_time': self.entry_time.isoformat(),
            'exit_time': self.exit_time.isoformat(),
            'pnl': self.pnl,
            'pnl_percentage': self.pnl_percentage,
            'capital_at_risk': self.capital_at_risk,
            'exit_reason': self.exit_reason,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'leverage': self.leverage,
            'fees': self.fees,
            'is_winner': self.is_winner,
            'risk_reward_achieved': self.risk_reward_achieved,
            'duration_hours': self.duration_hours,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CompletedTrade':
        """Create from dictionary."""
        return cls(
            symbol=data['symbol'],
            strategy=data['strategy'],
            side=data['side'],
            entry_price=data['entry_price'],
            exit_price=data['exit_price'],
            size=data['size'],
            entry_time=datetime.fromisoformat(data['entry_time']),
            exit_time=datetime.fromisoformat(data['exit_time']),
            pnl=data['pnl'],
            pnl_percentage=data['pnl_percentage'],
            capital_at_risk=data['capital_at_risk'],
            exit_reason=data['exit_reason'],
            stop_loss=data.get('stop_loss'),
            take_profit=data.get('take_profit'),
            leverage=data.get('leverage'),
            fees=data.get('fees', 0.0),
        )


@dataclass
class PerformanceMetrics:
    """Container for calculated performance metrics."""
    
    # Basic counts
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    
    # PnL metrics
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    average_pnl: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    
    # Percentage metrics
    win_rate: float = 0.0
    loss_rate: float = 0.0
    average_pnl_percentage: float = 0.0
    average_win_percentage: float = 0.0
    average_loss_percentage: float = 0.0
    
    # Risk metrics
    profit_factor: float = 0.0
    risk_reward_ratio: float = 0.0
    expectancy: float = 0.0
    
    # Drawdown metrics
    max_drawdown: float = 0.0
    max_drawdown_percentage: float = 0.0
    current_drawdown: float = 0.0
    peak_equity: float = 0.0
    
    # Advanced metrics
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    
    # Time-based metrics
    average_trade_duration_hours: float = 0.0
    longest_trade_hours: float = 0.0
    shortest_trade_hours: float = 0.0
    
    # Streak metrics
    current_win_streak: int = 0
    current_lose_streak: int = 0
    max_win_streak: int = 0
    max_lose_streak: int = 0
    
    # Exit reason breakdown
    exit_reasons: Dict[str, int] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'breakeven_trades': self.breakeven_trades,
            'total_pnl': round(self.total_pnl, 2),
            'gross_profit': round(self.gross_profit, 2),
            'gross_loss': round(self.gross_loss, 2),
            'average_pnl': round(self.average_pnl, 2),
            'average_win': round(self.average_win, 2),
            'average_loss': round(self.average_loss, 2),
            'largest_win': round(self.largest_win, 2),
            'largest_loss': round(self.largest_loss, 2),
            'win_rate': round(self.win_rate, 2),
            'loss_rate': round(self.loss_rate, 2),
            'average_pnl_percentage': round(self.average_pnl_percentage, 2),
            'average_win_percentage': round(self.average_win_percentage, 2),
            'average_loss_percentage': round(self.average_loss_percentage, 2),
            'profit_factor': round(self.profit_factor, 2),
            'risk_reward_ratio': round(self.risk_reward_ratio, 2),
            'expectancy': round(self.expectancy, 2),
            'max_drawdown': round(self.max_drawdown, 2),
            'max_drawdown_percentage': round(self.max_drawdown_percentage, 2),
            'current_drawdown': round(self.current_drawdown, 2),
            'peak_equity': round(self.peak_equity, 2),
            'sharpe_ratio': round(self.sharpe_ratio, 2),
            'sortino_ratio': round(self.sortino_ratio, 2),
            'calmar_ratio': round(self.calmar_ratio, 2),
            'average_trade_duration_hours': round(self.average_trade_duration_hours, 2),
            'longest_trade_hours': round(self.longest_trade_hours, 2),
            'shortest_trade_hours': round(self.shortest_trade_hours, 2),
            'current_win_streak': self.current_win_streak,
            'current_lose_streak': self.current_lose_streak,
            'max_win_streak': self.max_win_streak,
            'max_lose_streak': self.max_lose_streak,
            'exit_reasons': self.exit_reasons,
        }


class PerformanceTracker:
    """
    Comprehensive performance tracking for trading strategies.
    
    Tracks all completed trades and calculates standard trading metrics
    both overall and per-strategy.
    
    Storage: SQLite database for efficient querying and persistence.
    """
    
    def __init__(self, config: Dict[str, Any], data_dir: str = 'data', table_prefix: str = ""):
        """
        Initialize the performance tracker.
        
        Args:
            config: Configuration dictionary
            data_dir: Directory for storing performance data
            table_prefix: Prefix for database tables (e.g. 'backtest_')
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        
        # Initialize SQLite database
        # Check config for override (e.g., for backtesting)
        custom_db_path = self.config.get('persistence', {}).get('db_path')
        if custom_db_path:
            db_path = Path(custom_db_path)
        else:
            db_path = self.data_dir / 'trades.db'
            
        self.db = TradeDatabase(str(db_path), table_prefix=table_prefix)
        
        # In-memory cache for current session (capped at 30 days)
        self.completed_trades: List[CompletedTrade] = []
        self._load_recent_trades_to_cache()
        
        # Equity tracking for drawdown calculation
        self.initial_equity: float = 0.0
        self.equity_curve: List[Dict[str, Any]] = []
        
        # NOTE: Per-strategy and per-symbol tracking dicts were removed.
        # All queries go through SQLite (get_strategy_stats, get_trades_by_symbol).
        # Daily/weekly/monthly PnL also served from DB directly.
        
        trade_count = self.db.get_trade_count()
        self.logger.info(f"PerformanceTracker initialized with SQLite ({trade_count} trades in database)")
    
    def _load_recent_trades_to_cache(self, days: int = 30):
        """Load recent trades into memory cache for fast access."""
        try:
            recent = self.db.get_recent_trades(days=days)
            self.completed_trades = [
                CompletedTrade.from_dict(t) for t in recent
            ]
            self.logger.debug(f"Loaded {len(self.completed_trades)} recent trades to cache")
        except Exception as e:
            self.logger.error(f"Error loading trades to cache: {e}")
            self.completed_trades = []
    
    def set_initial_equity(self, equity: float):
        """Set the initial equity for tracking."""
        self.initial_equity = equity
        if not self.equity_curve:
            self.equity_curve.append({
                'timestamp': datetime.now().isoformat(),
                'equity': equity,
                'pnl': 0.0,
            })
    
    def record_trade(self, trade: CompletedTrade):
        """
        Record a completed trade.
        
        Args:
            trade: The completed trade to record
        """
        # Insert into SQLite database
        trade_id = self.db.insert_trade(trade.to_dict())
        
        # Update in-memory cache (keep last 30 days only)
        self.completed_trades.append(trade)
        self._trim_completed_trades_cache()
        
        # Update equity curve in database
        current_equity = self.initial_equity + sum(t.pnl for t in self.completed_trades)
        self.db.insert_equity_snapshot(
            equity=current_equity,
            pnl=trade.pnl,
            trade_id=trade_id,
            trade_symbol=trade.symbol
        )
        
        self.logger.info(f"Recorded trade: {trade.symbol} {trade.side} PnL=${trade.pnl:.2f} ({trade.pnl_percentage:.2f}%)")
    
    def record_trade_from_position(
        self,
        symbol: str,
        strategy: str,
        side: str,
        entry_price: float,
        exit_price: float,
        size: float,
        entry_time: datetime,
        exit_time: datetime,
        capital_at_risk: float,
        exit_reason: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        leverage: Optional[float] = None,
        fees: float = 0.0,
        pnl_override: Optional[float] = None,
        pnl_percentage_override: Optional[float] = None,
    ):
        """
        Record a trade from position closure data.
        
        This is a convenience method that creates a CompletedTrade from
        the data available when closing a position.
        """
        # Calculate PnL
        if pnl_override is not None:
            pnl = float(pnl_override)
        else:
            abs_size = abs(size)
            if side == 'long':
                pnl = (exit_price - entry_price) * abs_size - fees
            elif side == 'short':
                pnl = (entry_price - exit_price) * abs_size - fees
            else:
                raise ValueError(
                    f"record_trade_from_position() requires pnl_override for side='{side}'. "
                    "Valid price-based sides are 'long' or 'short'."
                )
        
        # Ensure reported size is positive
        size = abs(size)
        
        # Calculate PnL percentage based on capital at risk
        if pnl_percentage_override is not None:
            pnl_percentage = float(pnl_percentage_override)
        else:
            pnl_percentage = (pnl / capital_at_risk * 100) if capital_at_risk > 0 else 0
        
        trade = CompletedTrade(
            symbol=symbol,
            strategy=strategy,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            size=size,
            entry_time=entry_time,
            exit_time=exit_time,
            pnl=pnl,
            pnl_percentage=pnl_percentage,
            capital_at_risk=capital_at_risk,
            exit_reason=exit_reason,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=leverage,
            fees=fees,
        )
        
        self.record_trade(trade)
        return trade
    
    def calculate_metrics(self, trades: Optional[List[CompletedTrade]] = None) -> PerformanceMetrics:
        """
        Calculate performance metrics for a list of trades.
        
        Args:
            trades: List of trades to analyze. If None, uses all completed trades.
            
        Returns:
            PerformanceMetrics object with all calculated metrics
        """
        if trades is None:
            trades = self.completed_trades
        
        metrics = PerformanceMetrics()
        
        if not trades:
            return metrics
        
        # Basic counts
        metrics.total_trades = len(trades)
        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl < 0]
        breakeven = [t for t in trades if t.pnl == 0]
        
        metrics.winning_trades = len(winners)
        metrics.losing_trades = len(losers)
        metrics.breakeven_trades = len(breakeven)
        
        # PnL metrics
        pnls = [t.pnl for t in trades]
        metrics.total_pnl = sum(pnls)
        metrics.gross_profit = sum(t.pnl for t in winners)
        metrics.gross_loss = abs(sum(t.pnl for t in losers))
        metrics.average_pnl = statistics.mean(pnls) if pnls else 0
        metrics.average_win = statistics.mean([t.pnl for t in winners]) if winners else 0
        metrics.average_loss = statistics.mean([t.pnl for t in losers]) if losers else 0
        metrics.largest_win = max([t.pnl for t in winners]) if winners else 0
        metrics.largest_loss = min([t.pnl for t in losers]) if losers else 0
        
        # Win/Loss rates
        metrics.win_rate = (len(winners) / len(trades) * 100) if trades else 0
        metrics.loss_rate = (len(losers) / len(trades) * 100) if trades else 0
        
        # Percentage metrics
        pnl_percentages = [t.pnl_percentage for t in trades]
        win_percentages = [t.pnl_percentage for t in winners]
        loss_percentages = [t.pnl_percentage for t in losers]
        
        metrics.average_pnl_percentage = statistics.mean(pnl_percentages) if pnl_percentages else 0
        metrics.average_win_percentage = statistics.mean(win_percentages) if win_percentages else 0
        metrics.average_loss_percentage = statistics.mean(loss_percentages) if loss_percentages else 0
        
        # Profit Factor: Gross Profit / Gross Loss
        metrics.profit_factor = (metrics.gross_profit / metrics.gross_loss) if metrics.gross_loss > 0 else float('inf') if metrics.gross_profit > 0 else 0
        
        # Risk-Reward Ratio: Average Win / Average Loss
        metrics.risk_reward_ratio = (abs(metrics.average_win) / abs(metrics.average_loss)) if metrics.average_loss != 0 else float('inf') if metrics.average_win > 0 else 0
        
        # Expectancy: (Win Rate * Average Win) - (Loss Rate * Average Loss)
        win_rate_decimal = metrics.win_rate / 100
        loss_rate_decimal = metrics.loss_rate / 100
        metrics.expectancy = (win_rate_decimal * metrics.average_win) - (loss_rate_decimal * abs(metrics.average_loss))
        
        # Drawdown calculations
        self._calculate_drawdown(trades, metrics)
        
        # Advanced metrics
        self._calculate_advanced_metrics(trades, metrics)
        
        # Trade duration metrics
        durations = [t.duration_hours for t in trades]
        metrics.average_trade_duration_hours = statistics.mean(durations) if durations else 0
        metrics.longest_trade_hours = max(durations) if durations else 0
        metrics.shortest_trade_hours = min(durations) if durations else 0
        
        # Streak calculations
        self._calculate_streaks(trades, metrics)
        
        # Exit reason breakdown
        for trade in trades:
            metrics.exit_reasons[trade.exit_reason] = metrics.exit_reasons.get(trade.exit_reason, 0) + 1
        
        return metrics
    
    def _calculate_drawdown(self, trades: List[CompletedTrade], metrics: PerformanceMetrics):
        """Calculate drawdown metrics."""
        if not trades:
            return
        
        # Sort trades by exit time
        sorted_trades = sorted(trades, key=lambda t: t.exit_time)
        
        # Calculate equity curve
        equity = self.initial_equity
        peak = equity
        max_dd = 0
        max_dd_pct = 0
        
        for trade in sorted_trades:
            equity += trade.pnl
            
            if equity > peak:
                peak = equity
            
            drawdown = peak - equity
            drawdown_pct = (drawdown / peak * 100) if peak > 0 else 0
            
            if drawdown > max_dd:
                max_dd = drawdown
            if drawdown_pct > max_dd_pct:
                max_dd_pct = drawdown_pct
        
        metrics.max_drawdown = max_dd
        metrics.max_drawdown_percentage = max_dd_pct
        metrics.current_drawdown = peak - equity
        metrics.peak_equity = peak
    
    def _calculate_advanced_metrics(self, trades: List[CompletedTrade], metrics: PerformanceMetrics):
        """Calculate Sharpe, Sortino, and Calmar ratios."""
        if len(trades) < 2:
            return
        
        # Get PnL percentages for ratio calculations
        returns = [t.pnl_percentage for t in trades]
        
        # Sharpe Ratio: (Mean Return - Risk Free Rate) / Std Dev of Returns
        # Assuming 0% risk-free rate for simplicity
        try:
            mean_return = statistics.mean(returns)
            std_return = statistics.stdev(returns)
            metrics.sharpe_ratio = (mean_return / std_return) if std_return > 0 else 0
        except (statistics.StatisticsError, ZeroDivisionError):
            metrics.sharpe_ratio = 0
        
        # Sortino Ratio: (Mean Return - Risk Free Rate) / Downside Deviation
        # Only uses negative returns for volatility calculation
        try:
            negative_returns = [r for r in returns if r < 0]
            if negative_returns:
                downside_deviation = statistics.stdev(negative_returns)
                metrics.sortino_ratio = (mean_return / downside_deviation) if downside_deviation > 0 else 0
            else:
                metrics.sortino_ratio = float('inf') if mean_return > 0 else 0
        except (statistics.StatisticsError, ZeroDivisionError):
            metrics.sortino_ratio = 0
        
        # Calmar Ratio: Annualized Return / Max Drawdown
        # Simplified: Total Return / Max Drawdown
        try:
            if metrics.max_drawdown_percentage > 0:
                total_return_pct = sum(returns)
                metrics.calmar_ratio = total_return_pct / metrics.max_drawdown_percentage
            else:
                metrics.calmar_ratio = float('inf') if sum(returns) > 0 else 0
        except ZeroDivisionError:
            metrics.calmar_ratio = 0
    
    def _calculate_streaks(self, trades: List[CompletedTrade], metrics: PerformanceMetrics):
        """Calculate win/lose streak metrics."""
        if not trades:
            return
        
        # Sort by exit time
        sorted_trades = sorted(trades, key=lambda t: t.exit_time)
        
        current_win_streak = 0
        current_lose_streak = 0
        max_win_streak = 0
        max_lose_streak = 0
        
        for trade in sorted_trades:
            if trade.pnl > 0:
                current_win_streak += 1
                current_lose_streak = 0
                max_win_streak = max(max_win_streak, current_win_streak)
            elif trade.pnl < 0:
                current_lose_streak += 1
                current_win_streak = 0
                max_lose_streak = max(max_lose_streak, current_lose_streak)
            else:
                # Breakeven doesn't reset streaks
                pass
        
        metrics.current_win_streak = current_win_streak
        metrics.current_lose_streak = current_lose_streak
        metrics.max_win_streak = max_win_streak
        metrics.max_lose_streak = max_lose_streak
    
    def get_overall_metrics(self) -> PerformanceMetrics:
        """Get overall performance metrics for all trades."""
        # Use SQLite aggregation for efficiency
        stats = self.db.get_strategy_stats()  # None = all strategies
        return self._stats_to_metrics(stats)
    
    def get_strategy_metrics(self, strategy: str) -> PerformanceMetrics:
        """
        Get performance metrics for a specific strategy.
        
        Args:
            strategy: Strategy name
            
        Returns:
            PerformanceMetrics for that strategy
        """
        # Use SQLite for efficiency
        stats = self.db.get_strategy_stats(strategy)
        metrics = self._stats_to_metrics(stats)
        
        # Add streak stats
        streak_stats = self.db.get_streak_stats(strategy)
        metrics.current_win_streak = streak_stats['current_win_streak']
        metrics.current_lose_streak = streak_stats['current_lose_streak']
        metrics.max_win_streak = streak_stats['max_win_streak']
        metrics.max_lose_streak = streak_stats['max_lose_streak']
        
        return metrics
    
    def get_all_strategy_metrics(self) -> Dict[str, PerformanceMetrics]:
        """Get performance metrics for all strategies."""
        strategies = self.db.get_strategies()
        return {
            strategy: self.get_strategy_metrics(strategy)
            for strategy in strategies
        }
    
    def get_symbol_metrics(self, symbol: str) -> PerformanceMetrics:
        """Get performance metrics for a specific symbol."""
        trades = self.db.get_trades_by_symbol(symbol)
        trade_objects = [CompletedTrade.from_dict(t) for t in trades]
        return self.calculate_metrics(trade_objects)
    
    def _stats_to_metrics(self, stats: Dict[str, Any]) -> PerformanceMetrics:
        """Convert database stats dictionary to PerformanceMetrics object."""
        metrics = PerformanceMetrics()
        
        if not stats or stats.get('total_trades', 0) == 0:
            return metrics
        
        # Basic counts
        metrics.total_trades = stats.get('total_trades', 0)
        metrics.winning_trades = stats.get('winning_trades', 0)
        metrics.losing_trades = stats.get('losing_trades', 0)
        metrics.breakeven_trades = stats.get('breakeven_trades', 0)
        
        # PnL metrics
        metrics.total_pnl = stats.get('total_pnl', 0) or 0
        metrics.gross_profit = stats.get('gross_profit', 0) or 0
        metrics.gross_loss = stats.get('gross_loss', 0) or 0
        metrics.average_pnl = stats.get('avg_pnl', 0) or 0
        metrics.average_win = stats.get('avg_win', 0) or 0
        metrics.average_loss = stats.get('avg_loss', 0) or 0
        metrics.largest_win = stats.get('largest_win', 0) or 0
        metrics.largest_loss = stats.get('largest_loss', 0) or 0
        
        # Percentage metrics
        metrics.win_rate = stats.get('win_rate', 0) or 0
        metrics.loss_rate = stats.get('loss_rate', 0) or 0
        metrics.average_pnl_percentage = stats.get('avg_pnl_pct', 0) or 0
        metrics.average_win_percentage = stats.get('avg_win_pct', 0) or 0
        metrics.average_loss_percentage = stats.get('avg_loss_pct', 0) or 0
        
        # Risk metrics
        metrics.profit_factor = stats.get('profit_factor', 0) or 0
        metrics.risk_reward_ratio = stats.get('risk_reward_ratio', 0) or 0
        metrics.expectancy = stats.get('expectancy', 0) or 0
        
        # Time metrics
        metrics.average_trade_duration_hours = stats.get('avg_duration_hours', 0) or 0
        
        # Get drawdown from DB
        dd_stats = self.db.get_drawdown_stats()
        metrics.max_drawdown = dd_stats.get('max_drawdown', 0)
        metrics.max_drawdown_percentage = dd_stats.get('max_drawdown_pct', 0)
        metrics.current_drawdown = dd_stats.get('current_drawdown', 0)
        metrics.peak_equity = dd_stats.get('peak_equity', 0)
        
        return metrics
    
    def get_time_period_metrics(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> PerformanceMetrics:
        """Get metrics for a specific time period."""
        trades = self.completed_trades
        
        if start_time:
            trades = [t for t in trades if t.exit_time >= start_time]
        if end_time:
            trades = [t for t in trades if t.exit_time <= end_time]
        
        return self.calculate_metrics(trades)
    
    def get_daily_metrics(self, date: Optional[str] = None) -> Dict[str, Any]:
        """Get daily PnL and metrics."""
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        # Get daily PnL from database
        daily_pnl = self.db.get_daily_pnl(days=1)
        pnl = daily_pnl.get(date, 0.0)
        
        # Get trades for detailed metrics
        start = datetime.strptime(date, '%Y-%m-%d')
        end = start + timedelta(days=1)
        daily_trades_data = self.db.get_trades_in_range(start, end)
        daily_trades = [CompletedTrade.from_dict(t) for t in daily_trades_data]
        
        metrics = self.calculate_metrics(daily_trades)
        
        return {
            'date': date,
            'pnl': pnl,
            'trades': len(daily_trades),
            'metrics': metrics.to_dict(),
        }
    
    def get_weekly_metrics(self, week: Optional[str] = None) -> Dict[str, Any]:
        """Get weekly PnL and metrics."""
        if week is None:
            week = datetime.now().strftime('%Y-W%W')
        
        # Calculate week start/end
        year, week_num = int(week[:4]), int(week.split('W')[1])
        start = datetime.strptime(f'{year}-W{week_num}-1', '%Y-W%W-%w')
        end = start + timedelta(days=7)
        
        weekly_trades_data = self.db.get_trades_in_range(start, end)
        weekly_trades = [CompletedTrade.from_dict(t) for t in weekly_trades_data]
        
        metrics = self.calculate_metrics(weekly_trades)
        pnl = sum(t.pnl for t in weekly_trades)
        
        return {
            'week': week,
            'pnl': pnl,
            'trades': len(weekly_trades),
            'metrics': metrics.to_dict(),
        }
    
    def get_monthly_metrics(self, month: Optional[str] = None) -> Dict[str, Any]:
        """Get monthly PnL and metrics."""
        if month is None:
            month = datetime.now().strftime('%Y-%m')
        
        # Get monthly PnL from database
        monthly_pnl = self.db.get_monthly_pnl(months=1)
        pnl = monthly_pnl.get(month, 0.0)
        
        # Calculate month start/end
        start = datetime.strptime(f'{month}-01', '%Y-%m-%d')
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        
        monthly_trades_data = self.db.get_trades_in_range(start, end)
        monthly_trades = [CompletedTrade.from_dict(t) for t in monthly_trades_data]
        
        metrics = self.calculate_metrics(monthly_trades)
        
        return {
            'month': month,
            'pnl': pnl,
            'trades': len(monthly_trades),
            'metrics': metrics.to_dict(),
        }
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """
        Get a comprehensive performance summary.
        
        Returns a dictionary containing:
        - Overall metrics
        - Per-strategy metrics
        - Time-based breakdown
        - Recent performance
        """
        overall = self.get_overall_metrics()
        
        # Recent performance (last 7 days) from database
        recent_trades_data = self.db.get_recent_trades(days=7)
        recent_trades = [CompletedTrade.from_dict(t) for t in recent_trades_data]
        recent_metrics = self.calculate_metrics(recent_trades)
        
        # Strategy breakdown from database
        all_strategy_stats = self.db.get_all_strategy_stats()
        strategy_breakdown = {}
        for strategy, stats in all_strategy_stats.items():
            strategy_breakdown[strategy] = {
                'trades': stats.get('total_trades', 0),
                'pnl': stats.get('total_pnl', 0) or 0,
                'win_rate': stats.get('win_rate', 0) or 0,
                'profit_factor': stats.get('profit_factor', 0) or 0,
                'expectancy': stats.get('expectancy', 0) or 0,
            }
        
        # Top/worst symbols from database
        symbols = self.db.get_symbols()
        symbol_pnl = {}
        for symbol in symbols:
            stats = self.db.get_symbol_stats(symbol)
            symbol_pnl[symbol] = stats.get('total_pnl', 0) or 0
        
        top_symbols = sorted(symbol_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
        worst_symbols = sorted(symbol_pnl.items(), key=lambda x: x[1])[:5]
        
        # Daily and monthly PnL from database
        daily_pnl = self.db.get_daily_pnl(days=7)
        monthly_pnl = self.db.get_monthly_pnl(months=3)
        
        # Get total trade count and first trade date
        total_trades = self.db.get_trade_count()
        all_trades = self.db.get_all_trades(limit=1)
        first_trade_date = all_trades[0]['entry_time'] if all_trades else None
        
        return {
            'overall': overall.to_dict(),
            'recent_7_days': {
                'trades': len(recent_trades),
                'pnl': sum(t.pnl for t in recent_trades),
                'metrics': recent_metrics.to_dict(),
            },
            'strategy_breakdown': strategy_breakdown,
            'top_symbols': dict(top_symbols),
            'worst_symbols': dict(worst_symbols),
            'daily_pnl': daily_pnl,
            'monthly_pnl': monthly_pnl,
            'total_completed_trades': total_trades,
            'tracking_since': first_trade_date,
        }
    
    def log_performance_report(self):
        """Log a formatted performance report."""
        summary = self.get_performance_summary()
        overall = summary['overall']
        
        self.logger.info("=" * 70)
        self.logger.info("📊 PERFORMANCE REPORT")
        self.logger.info("=" * 70)
        
        self.logger.info(f"Total Trades: {overall['total_trades']}")
        self.logger.info(f"Win Rate: {overall['win_rate']:.1f}% ({overall['winning_trades']}W / {overall['losing_trades']}L)")
        self.logger.info(f"Total PnL: ${overall['total_pnl']:.2f}")
        self.logger.info(f"Average PnL: ${overall['average_pnl']:.2f} ({overall['average_pnl_percentage']:.2f}%)")
        
        self.logger.info("-" * 70)
        self.logger.info("RISK METRICS")
        self.logger.info(f"Profit Factor: {overall['profit_factor']:.2f}")
        self.logger.info(f"Risk-Reward Ratio: {overall['risk_reward_ratio']:.2f}")
        self.logger.info(f"Expectancy: ${overall['expectancy']:.2f}")
        self.logger.info(f"Max Drawdown: ${overall['max_drawdown']:.2f} ({overall['max_drawdown_percentage']:.1f}%)")
        
        self.logger.info("-" * 70)
        self.logger.info("ADVANCED METRICS")
        self.logger.info(f"Sharpe Ratio: {overall['sharpe_ratio']:.2f}")
        self.logger.info(f"Sortino Ratio: {overall['sortino_ratio']:.2f}")
        self.logger.info(f"Calmar Ratio: {overall['calmar_ratio']:.2f}")
        
        self.logger.info("-" * 70)
        self.logger.info("WIN/LOSS ANALYSIS")
        self.logger.info(f"Average Win: ${overall['average_win']:.2f} ({overall['average_win_percentage']:.2f}%)")
        self.logger.info(f"Average Loss: ${overall['average_loss']:.2f} ({overall['average_loss_percentage']:.2f}%)")
        self.logger.info(f"Largest Win: ${overall['largest_win']:.2f}")
        self.logger.info(f"Largest Loss: ${overall['largest_loss']:.2f}")
        self.logger.info(f"Max Win Streak: {overall['max_win_streak']}")
        self.logger.info(f"Max Lose Streak: {overall['max_lose_streak']}")
        
        self.logger.info("-" * 70)
        self.logger.info("STRATEGY BREAKDOWN")
        for strategy, data in summary['strategy_breakdown'].items():
            self.logger.info(f"  {strategy}: {data['trades']} trades, ${data['pnl']:.2f} PnL, {data['win_rate']:.1f}% win rate, PF: {data['profit_factor']:.2f}")
        
        exit_reasons = overall.get('exit_reasons', {})
        if exit_reasons:
            self.logger.info("-" * 70)
            self.logger.info("EXIT REASONS")
            for reason, count in exit_reasons.items():
                self.logger.info(f"  {reason}: {count}")
        
        self.logger.info("=" * 70)
    
    def _trim_completed_trades_cache(self):
        """Trim the in-memory completed_trades to only keep the last 30 days."""
        if len(self.completed_trades) > 500:  # Only check periodically
            cutoff = datetime.now() - timedelta(days=30)
            self.completed_trades = [t for t in self.completed_trades if t.exit_time >= cutoff]

    def reset(self):
        """Reset all performance data."""
        # Clear in-memory cache
        self.completed_trades = []
        self.equity_curve = []
        
        # Clear database
        self.db.delete_all_trades()
        self.logger.info("Performance tracker reset - all data cleared from database")
    
    def get_database_stats(self) -> Dict[str, Any]:
        """Get statistics about the database."""
        return {
            'total_trades': self.db.get_trade_count(),
            'strategies': self.db.get_strategies(),
            'symbols': self.db.get_symbols(),
            'database_path': str(self.db.db_path),
        }


