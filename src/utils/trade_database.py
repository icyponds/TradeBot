"""
SQLite database for trade history and performance metrics.

Provides efficient storage and querying of trade data with:
- Fast indexed queries by strategy, symbol, time
- Aggregation queries for metrics calculation
- Automatic schema migrations
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from contextlib import contextmanager
import pandas as pd


class TradeDatabase:
    """
    SQLite-based storage for trade history and performance data.
    
    Much more efficient than JSON for:
    - Querying trades by strategy, symbol, or time range
    - Calculating aggregate metrics (sum, avg, count)
    - Handling large numbers of trades (10K+)
    - Concurrent read access
    """
    
    SCHEMA_VERSION = 1
    
    def __init__(self, db_path: str = "data/trades.db"):
        """
        Initialize the trade database.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.logger = logging.getLogger(__name__)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database
        self._init_database()
        
        self.logger.info(f"TradeDatabase initialized at {self.db_path}")
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Return rows as dict-like objects
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def _init_database(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Create trades table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    size REAL NOT NULL,
                    entry_time TIMESTAMP NOT NULL,
                    exit_time TIMESTAMP NOT NULL,
                    pnl REAL NOT NULL,
                    pnl_percentage REAL NOT NULL,
                    capital_at_risk REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    leverage REAL,
                    fees REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes for fast queries
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy_exit ON trades(strategy, exit_time)")
            
            # Create equity snapshots table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_equity REAL NOT NULL,
                    available_margin REAL NOT NULL,
                    used_margin REAL NOT NULL,
                    pnl_24h REAL DEFAULT 0.0,
                    open_positions INTEGER DEFAULT 0
                )
            """)
            
            # Create market_candles table for incremental loading
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_candles (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (symbol, timeframe, timestamp)
                )
            """)
            
            # Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_candles_lookup ON market_candles(symbol, timeframe, timestamp DESC)")
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP NOT NULL,
                    equity REAL NOT NULL,
                    pnl REAL NOT NULL,
                    trade_id INTEGER,
                    trade_symbol TEXT,
                    FOREIGN KEY (trade_id) REFERENCES trades(id)
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_equity_timestamp ON equity_snapshots(timestamp)")
            
            # Create daily PnL table (materialized for fast access)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_pnl (
                    date TEXT PRIMARY KEY,
                    pnl REAL NOT NULL,
                    trade_count INTEGER NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create metadata table for schema version and settings
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            
            # Set schema version
            cursor.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                ("schema_version", str(self.SCHEMA_VERSION))
            )

            # Market Data Table (Phase 9)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_data (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (symbol, timeframe, timestamp)
                ) WITHOUT ROWID
            """)
            
            # Index for fast range queries
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_market_data_range ON market_data(symbol, timeframe, timestamp)")
            
            # Funding Rates Table (Phase 12)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS funding_rates (
                    symbol TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    funding_rate REAL NOT NULL,
                    PRIMARY KEY (symbol, timestamp)
                ) WITHOUT ROWID
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_funding_rates_range ON funding_rates(symbol, timestamp)")
            
            self.logger.info("Database schema initialized")
    
    def insert_trade(self, trade_data: Dict[str, Any]) -> int:
        """
        Insert a completed trade into the database.
        
        Args:
            trade_data: Dictionary with trade information
            
        Returns:
            The ID of the inserted trade
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO trades (
                    symbol, strategy, side, entry_price, exit_price, size,
                    entry_time, exit_time, pnl, pnl_percentage, capital_at_risk,
                    exit_reason, stop_loss, take_profit, leverage, fees
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['symbol'],
                trade_data['strategy'],
                trade_data['side'],
                trade_data['entry_price'],
                trade_data['exit_price'],
                trade_data['size'],
                trade_data['entry_time'],
                trade_data['exit_time'],
                trade_data['pnl'],
                trade_data['pnl_percentage'],
                trade_data['capital_at_risk'],
                trade_data['exit_reason'],
                trade_data.get('stop_loss'),
                trade_data.get('take_profit'),
                trade_data.get('leverage'),
                trade_data.get('fees', 0.0),
            ))
            
            trade_id = cursor.lastrowid
            
            # Update daily PnL
            trade_date = trade_data['exit_time'][:10] if isinstance(trade_data['exit_time'], str) else trade_data['exit_time'].strftime('%Y-%m-%d')
            cursor.execute("""
                INSERT INTO daily_pnl (date, pnl, trade_count)
                VALUES (?, ?, 1)
                ON CONFLICT(date) DO UPDATE SET
                    pnl = pnl + excluded.pnl,
                    trade_count = trade_count + 1,
                    updated_at = CURRENT_TIMESTAMP
            """, (trade_date, trade_data['pnl']))
            
            return trade_id
    
    def insert_equity_snapshot(self, equity: float, pnl: float, 
                               trade_id: Optional[int] = None, 
                               trade_symbol: Optional[str] = None):
        """Insert an equity snapshot."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO equity_snapshots (timestamp, equity, pnl, trade_id, trade_symbol)
                VALUES (?, ?, ?, ?, ?)
            """, (datetime.now().isoformat(), equity, pnl, trade_id, trade_symbol))
    
    def get_all_trades(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all trades, optionally limited."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM trades ORDER BY exit_time DESC"
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_trades_by_strategy(self, strategy: str, 
                               limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get trades for a specific strategy."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM trades WHERE strategy = ? ORDER BY exit_time DESC"
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, (strategy,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_trades_by_symbol(self, symbol: str,
                             limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get trades for a specific symbol."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM trades WHERE symbol = ? ORDER BY exit_time DESC"
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, (symbol,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_trades_in_range(self, start_time: datetime, 
                            end_time: datetime) -> List[Dict[str, Any]]:
        """Get trades within a time range."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades 
                WHERE exit_time >= ? AND exit_time <= ?
                ORDER BY exit_time DESC
            """, (start_time.isoformat(), end_time.isoformat()))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_recent_trades(self, days: int = 7) -> List[Dict[str, Any]]:
        """Get trades from the last N days."""
        start_time = datetime.now() - timedelta(days=days)
        return self.get_trades_in_range(start_time, datetime.now())
    
    # ==================== AGGREGATION QUERIES ====================
    
    def get_strategy_stats(self, strategy: Optional[str] = None) -> Dict[str, Any]:
        """
        Get aggregate statistics for a strategy (or all if None).
        
        Returns metrics directly from SQL for efficiency.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            where_clause = "WHERE strategy = ?" if strategy else ""
            params = (strategy,) if strategy else ()
            
            cursor.execute(f"""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                    SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END) as breakeven_trades,
                    SUM(pnl) as total_pnl,
                    AVG(pnl) as avg_pnl,
                    SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_profit,
                    ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)) as gross_loss,
                    AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
                    AVG(CASE WHEN pnl < 0 THEN pnl END) as avg_loss,
                    MAX(pnl) as largest_win,
                    MIN(pnl) as largest_loss,
                    AVG(pnl_percentage) as avg_pnl_pct,
                    AVG(CASE WHEN pnl > 0 THEN pnl_percentage END) as avg_win_pct,
                    AVG(CASE WHEN pnl < 0 THEN pnl_percentage END) as avg_loss_pct,
                    AVG((julianday(exit_time) - julianday(entry_time)) * 24) as avg_duration_hours,
                    MIN(entry_time) as first_trade,
                    MAX(exit_time) as last_trade
                FROM trades
                {where_clause}
            """, params)
            
            row = cursor.fetchone()
            if not row or row['total_trades'] == 0:
                return self._empty_stats()
            
            stats = dict(row)
            
            # Calculate derived metrics
            total = stats['total_trades']
            winners = stats['winning_trades'] or 0
            
            stats['win_rate'] = (winners / total * 100) if total > 0 else 0
            stats['loss_rate'] = ((stats['losing_trades'] or 0) / total * 100) if total > 0 else 0
            
            # Profit factor
            gross_profit = stats['gross_profit'] or 0
            gross_loss = stats['gross_loss'] or 0
            stats['profit_factor'] = (gross_profit / gross_loss) if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
            
            # Risk-reward ratio
            avg_win = abs(stats['avg_win'] or 0)
            avg_loss = abs(stats['avg_loss'] or 0)
            stats['risk_reward_ratio'] = (avg_win / avg_loss) if avg_loss > 0 else float('inf') if avg_win > 0 else 0
            
            # Expectancy
            win_rate_dec = stats['win_rate'] / 100
            loss_rate_dec = stats['loss_rate'] / 100
            stats['expectancy'] = (win_rate_dec * avg_win) - (loss_rate_dec * avg_loss)
            
            return stats
    
    def get_all_strategy_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all strategies."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT strategy FROM trades")
            strategies = [row[0] for row in cursor.fetchall()]
        
        return {strategy: self.get_strategy_stats(strategy) for strategy in strategies}
    
    def get_symbol_stats(self, symbol: str) -> Dict[str, Any]:
        """Get aggregate statistics for a symbol."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(pnl) as total_pnl,
                    AVG(pnl) as avg_pnl,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades
                FROM trades
                WHERE symbol = ?
            """, (symbol,))
            
            row = cursor.fetchone()
            stats = dict(row) if row else {}
            
            if stats.get('total_trades', 0) > 0:
                stats['win_rate'] = (stats['winning_trades'] / stats['total_trades']) * 100
            
            return stats
    
    def get_daily_pnl(self, days: int = 30) -> Dict[str, float]:
        """Get daily PnL for the last N days."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT date, pnl FROM daily_pnl
                ORDER BY date DESC
                LIMIT ?
            """, (days,))
            
            return {row['date']: row['pnl'] for row in cursor.fetchall()}
    
    def get_monthly_pnl(self, months: int = 12) -> Dict[str, float]:
        """Get monthly PnL."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    strftime('%Y-%m', exit_time) as month,
                    SUM(pnl) as pnl
                FROM trades
                GROUP BY month
                ORDER BY month DESC
                LIMIT ?
            """, (months,))
            
            return {row['month']: row['pnl'] for row in cursor.fetchall()}
    
    def get_equity_curve(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get equity curve data."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM equity_snapshots ORDER BY timestamp"
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_drawdown_stats(self) -> Dict[str, float]:
        """Calculate drawdown statistics from equity curve."""
        equity_curve = self.get_equity_curve()
        
        if not equity_curve:
            return {'max_drawdown': 0, 'max_drawdown_pct': 0, 'current_drawdown': 0}
        
        peak = equity_curve[0]['equity']
        max_dd = 0
        max_dd_pct = 0
        
        for point in equity_curve:
            equity = point['equity']
            if equity > peak:
                peak = equity
            
            drawdown = peak - equity
            drawdown_pct = (drawdown / peak * 100) if peak > 0 else 0
            
            if drawdown > max_dd:
                max_dd = drawdown
            if drawdown_pct > max_dd_pct:
                max_dd_pct = drawdown_pct
        
        current_equity = equity_curve[-1]['equity']
        current_dd = peak - current_equity
        
        return {
            'max_drawdown': max_dd,
            'max_drawdown_pct': max_dd_pct,
            'current_drawdown': current_dd,
            'peak_equity': peak,
        }
    
    def get_streak_stats(self, strategy: Optional[str] = None) -> Dict[str, int]:
        """Calculate win/lose streak statistics."""
        trades = self.get_trades_by_strategy(strategy) if strategy else self.get_all_trades()
        
        if not trades:
            return {'current_win_streak': 0, 'current_lose_streak': 0, 
                    'max_win_streak': 0, 'max_lose_streak': 0}
        
        # Sort by exit_time ascending
        trades = sorted(trades, key=lambda t: t['exit_time'])
        
        current_win = 0
        current_lose = 0
        max_win = 0
        max_lose = 0
        
        for trade in trades:
            if trade['pnl'] > 0:
                current_win += 1
                current_lose = 0
                max_win = max(max_win, current_win)
            elif trade['pnl'] < 0:
                current_lose += 1
                current_win = 0
                max_lose = max(max_lose, current_lose)
        
        return {
            'current_win_streak': current_win,
            'current_lose_streak': current_lose,
            'max_win_streak': max_win,
            'max_lose_streak': max_lose,
        }
    
    def _empty_stats(self) -> Dict[str, Any]:
        """Return empty statistics structure."""
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'breakeven_trades': 0,
            'total_pnl': 0,
            'avg_pnl': 0,
            'gross_profit': 0,
            'gross_loss': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'win_rate': 0,
            'loss_rate': 0,
            'profit_factor': 0,
            'risk_reward_ratio': 0,
            'expectancy': 0,
        }
    
    def get_trade_count(self) -> int:
        """Get total number of trades in database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades")
            return cursor.fetchone()[0]

    def delete_all_trades(self) -> None:
        """
        Delete all trade/performance rows.

        This is primarily used for backtesting runs that should not be mixed with
        previous backtest results.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trades")
            cursor.execute("DELETE FROM equity_snapshots")
            cursor.execute("DELETE FROM daily_pnl")
    
    def get_strategies(self) -> List[str]:
        """Get list of all strategies with trades."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT strategy FROM trades ORDER BY strategy")
            return [row[0] for row in cursor.fetchall()]
    
    def get_symbols(self) -> List[str]:
        """Get list of all symbols traded."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT symbol FROM trades ORDER BY symbol")
            return [row[0] for row in cursor.fetchall()]
    
    # ==================== MARKET DATA METHODS ====================

    def insert_market_data(self, df: pd.DataFrame, symbol: str, timeframe: str):
        """
        Insert market data (OHLCV) into the database.
        
        Args:
            df: DataFrame with datetime index and columns [open, high, low, close, volume]
            symbol: Trading pair symbol (e.g., 'BTC')
            timeframe: Timeframe string (e.g., '1h')
        """
        if df.empty:
            return

        # Prepare list of tuples for batch insert
        data_to_insert = []
        for timestamp, row in df.iterrows():
            # Handle string or datetime timestamps
            ts_str = timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp)
            
            data_to_insert.append((
                symbol,
                timeframe,
                ts_str,
                float(row['open']),
                float(row['high']),
                float(row['low']),
                float(row['close']),
                float(row['volume'])
            ))

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # USE OR REPLACE to update existing candles if re-fetched
            cursor.executemany("""
                INSERT OR REPLACE INTO market_data 
                (symbol, timeframe, timestamp, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, data_to_insert)
            
            self.logger.info(f"Inserted {len(data_to_insert)} candles for {symbol} {timeframe}")

    def get_market_data(self, symbol: str, timeframe: str, 
                       start_date: Optional[datetime] = None, 
                       end_date: Optional[datetime] = None) -> pd.DataFrame:
        """
        Retrieve market data as a Pandas DataFrame.
        """
        query = """
            SELECT timestamp, open, high, low, close, volume 
            FROM market_data 
            WHERE symbol = ? AND timeframe = ?
        """
        params = [symbol, timeframe]
        
        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date.isoformat())
        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date.isoformat())
            
        query += " ORDER BY timestamp ASC"

        with self._get_connection() as conn:
            # Load directly into DataFrame
            df = pd.read_sql_query(
                query, 
                conn, 
                params=params,
                parse_dates=['timestamp'],
                index_col='timestamp'
            )
            
            return df
            
    def get_available_data_range(self, symbol: str, timeframe: str) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Get the start and end timestamp for available data."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT MIN(timestamp), MAX(timestamp) 
                FROM market_data 
                WHERE symbol = ? AND timeframe = ?
            """, (symbol, timeframe))
            
            row = cursor.fetchone()
            if row and row[0] and row[1]:
                # Parse strings to datetime
                try:
                    start = datetime.fromisoformat(row[0])
                    end = datetime.fromisoformat(row[1])
                    return start, end
                except ValueError:
                    return None, None
            return None, None

    def get_market_data_symbols(self, timeframe: str) -> List[str]:
        """Get list of symbols that have data for this timeframe."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT symbol FROM market_data WHERE timeframe = ?
            """, (timeframe,))
            return [row[0] for row in cursor.fetchall()]


    def insert_funding_rates(self, df: pd.DataFrame, symbol: str):
        """
        Insert funding rates into the database.
        
        Args:
            df: DataFrame with datetime index and 'funding' column
            symbol: Trading symbol
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            data_to_insert = []
            for timestamp, row in df.iterrows():
                # Timestamp is the index
                ts = timestamp.strftime('%Y-%m-%d %H:%M:%S') if isinstance(timestamp, pd.Timestamp) else str(timestamp)
                
                # Check for funding rate column
                rate = row.get('funding') or row.get('fundingRate')
                if rate is not None:
                     data_to_insert.append((symbol, ts, float(rate)))
            
            if not data_to_insert:
                return 0
                
            cursor.executemany("""
                INSERT OR REPLACE INTO funding_rates (symbol, timestamp, funding_rate)
                VALUES (?, ?, ?)
            """, data_to_insert)
            
            return len(data_to_insert)

    def get_funding_rates(self, symbol: str, start_date: datetime = None, end_date: datetime = None) -> pd.DataFrame:
        """
        Get historical funding rates for a symbol.
        
        Args:
            symbol: Trading symbol
            start_date: Start datetime
            end_date: End datetime
            
        Returns:
            DataFrame with funding rates indexed by timestamp
        """
        query = "SELECT timestamp, funding_rate FROM funding_rates WHERE symbol = ?"
        params = [symbol]
        
        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)
        
        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date)
            
        query += " ORDER BY timestamp ASC"
        
        try:
            with self._get_connection() as conn:
                df = pd.read_sql_query(query, conn, params=params, parse_dates=['timestamp'])
                
            if df.empty:
                return pd.DataFrame()
                
            df.set_index('timestamp', inplace=True)
            return df
            
        except Exception as e:
            self.logger.error(f"Error fetching funding rates for {symbol}: {e}")
            return pd.DataFrame()

    def get_latest_candle_time(self, symbol: str, timeframe: str) -> Optional[int]:
        """
        Get timestamp of the most recent candle for a symbol/timeframe.
        
        Returns:
            Timestamp in milliseconds or None if no data exists.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT MAX(timestamp) as last_ts 
                    FROM market_candles 
                    WHERE symbol = ? AND timeframe = ?
                """, (symbol, timeframe))
                row = cursor.fetchone()
                return row['last_ts'] if row and row['last_ts'] else None
        except Exception as e:
            self.logger.error(f"Error checking latest candle for {symbol}: {e}")
            return None

    def save_candles(self, symbol: str, timeframe: str, candles: List[Dict[str, Any]]):
        """
        Save new candles to the database.
        
        Args:
            symbol: Trading symbol
            timeframe: Candle timeframe (e.g. '1h')
            candles: List of candle dicts with keys: time (s or ms), open, high, low, close, volume
        """
        if not candles:
            return

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                data_to_insert = []
                for c in candles:
                    # Normalize timestamp to milliseconds
                    ts = c.get('time') or c.get('t')
                    if ts < 10000000000:  # Seconds detection (heuristic)
                        ts *= 1000
                    
                    data_to_insert.append((
                        symbol,
                        timeframe,
                        int(ts),
                        float(c.get('open') or c.get('o')),
                        float(c.get('high') or c.get('h')),
                        float(c.get('low') or c.get('l')),
                        float(c.get('close') or c.get('c')),
                        float(c.get('volume') or c.get('v'))
                    ))
                
                # Use INSERT OR IGNORE to handle overlaps
                cursor.executemany("""
                    INSERT OR IGNORE INTO market_candles 
                    (symbol, timeframe, timestamp, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, data_to_insert)
        except Exception as e:
            self.logger.error(f"Error saving candles for {symbol}: {e}")

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Retrieve recent candles from database.
        
        Returns:
            List of dictionaries compliant with API format.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Get the last N candles
                cursor.execute("""
                    SELECT timestamp, open, high, low, close, volume
                    FROM (
                        SELECT * FROM market_candles
                        WHERE symbol = ? AND timeframe = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                    )
                    ORDER BY timestamp ASC
                """, (symbol, timeframe, limit))
                
                rows = cursor.fetchall()
                
                return [{
                    'time': row['timestamp'] // 1000, # Return as seconds for consistency with existing app
                    'open': row['open'],
                    'high': row['high'],
                    'low': row['low'],
                    'close': row['close'],
                    'volume': row['volume']
                } for row in rows]
        except Exception as e:
            self.logger.error(f"Error retrieving candles for {symbol}: {e}")
            return []
