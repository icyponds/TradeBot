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
    
    def __init__(self, db_path: str = "data/trades.db", table_prefix: str = ""):
        """
        Initialize the trade database.
        
        Args:
            db_path: Path to SQLite database file
            table_prefix: Prefix for mutable tables (trades, snapshots, pnl).
                          Useful for separating backtest results (e.g. 'backtest_').
                          Market data tables remain shared.
        """
        self.logger = logging.getLogger(__name__)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.table_prefix = table_prefix
        
        # Initialize database
        self._init_database()
        
        self.logger.info(f"TradeDatabase initialized at {self.db_path} (prefix='{self.table_prefix}')")
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row  # Return rows as dict-like objects
        
        # Optimize performance and concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            # Checkpoint WAL to ensure data persists to main database file
            # PASSIVE mode: checkpoints without waiting, non-blocking
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass  # Ignore checkpoint errors (e.g., if other connections exist)
            conn.close()
    
    def _init_database(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            self._init_core_tables(conn)
            self._init_live_positions_tables(conn)

    def _init_core_tables(self, conn):
        """Initialize core trade history and market data tables."""
        cursor = conn.cursor()
        
        # Mutable Tables (Prefixed)
        trades_table = f"{self.table_prefix}trades"
        equity_table = f"{self.table_prefix}equity_snapshots"
        pnl_table = f"{self.table_prefix}daily_pnl"
        
        # Create trades table
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {trades_table} (
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
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_prefix}trades_strategy ON {trades_table}(strategy)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_prefix}trades_symbol ON {trades_table}(symbol)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_prefix}trades_exit_time ON {trades_table}(exit_time)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_prefix}trades_entry_time ON {trades_table}(entry_time)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_prefix}trades_strategy_exit ON {trades_table}(strategy, exit_time)")
        
        # Create equity snapshots table
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {equity_table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                equity REAL NOT NULL,
                pnl REAL NOT NULL,
                trade_id INTEGER,
                trade_symbol TEXT,
                FOREIGN KEY (trade_id) REFERENCES {trades_table}(id)
            )
        """)
        
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_prefix}equity_timestamp ON {equity_table}(timestamp)")
        
        # Create daily PnL table (materialized for fast access)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {pnl_table} (
                date TEXT PRIMARY KEY,
                pnl REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create metadata table for schema version and settings (Shared)
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

        # Market Data Table (Shared - Always Source of Truth)
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
        
        # Funding Rates Table (Shared - Always Source of Truth)
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

    def _init_live_positions_tables(self, conn):
        """Initialize tables for live position persistence."""
        cursor = conn.cursor()
        
        # 1. Live Positions (High-level container)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_prefix}live_positions (
                position_id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,  -- 'long', 'short', 'neutral'
                size REAL NOT NULL,
                leverage REAL,
                entry_price REAL,
                entry_time TIMESTAMP NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                order_id TEXT,  -- Exchange OID for single-leg positions
                metadata TEXT,  -- JSON string
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 2. Live Position Legs (Individual executions)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_prefix}live_position_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market_type TEXT NOT NULL, -- 'spot', 'perp'
                side TEXT NOT NULL, -- 'buy', 'sell'
                size REAL NOT NULL,
                entry_price REAL,
                order_id TEXT,  -- Exchange OID for this leg
                FOREIGN KEY (position_id) REFERENCES {self.table_prefix}live_positions(position_id) ON DELETE CASCADE
            )
        """)
        
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_prefix}live_pos_strat ON {self.table_prefix}live_positions(strategy)")
        
        # Migration: Add order_id column to existing tables (backwards compatibility)
        try:
            cursor.execute(f"ALTER TABLE {self.table_prefix}live_positions ADD COLUMN order_id TEXT")
        except Exception:
            pass  # Column already exists
        
        try:
            cursor.execute(f"ALTER TABLE {self.table_prefix}live_position_legs ADD COLUMN order_id TEXT")
        except Exception:
            pass  # Column already exists
    
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
            
            cursor.execute(f"""
                INSERT INTO {self.table_prefix}trades (
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
            cursor.execute(f"""
                INSERT INTO {self.table_prefix}daily_pnl (date, pnl, trade_count)
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
            cursor.execute(f"""
                INSERT INTO {self.table_prefix}equity_snapshots (timestamp, equity, pnl, trade_id, trade_symbol)
                VALUES (?, ?, ?, ?, ?)
            """, (datetime.now().isoformat(), equity, pnl, trade_id, trade_symbol))
    
    def get_all_trades(self, limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        """Get all trades, optionally limited and paginated."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = f"SELECT * FROM {self.table_prefix}trades ORDER BY exit_time DESC"
            
            if limit:
                query += f" LIMIT {limit}"
                if offset > 0:
                     query += f" OFFSET {offset}"
            
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_trades_by_strategy(self, strategy: str, 
                               limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get trades for a specific strategy."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = f"SELECT * FROM {self.table_prefix}trades WHERE strategy = ? ORDER BY exit_time DESC"
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, (strategy,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_trades_by_symbol(self, symbol: str,
                             limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get trades for a specific symbol."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = f"SELECT * FROM {self.table_prefix}trades WHERE symbol = ? ORDER BY exit_time DESC"
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, (symbol,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_trades_in_range(self, start_time: datetime, 
                            end_time: datetime) -> List[Dict[str, Any]]:
        """Get trades within a time range using exit_time."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT * FROM {self.table_prefix}trades 
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
                    SUM(fees) as total_fees,
                    SUM(pnl) - SUM(fees) as net_pnl,
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
                FROM {self.table_prefix}trades
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
            cursor.execute(f"SELECT DISTINCT strategy FROM {self.table_prefix}trades")
            strategies = [row[0] for row in cursor.fetchall()]
        
        return {strategy: self.get_strategy_stats(strategy) for strategy in strategies}
    
    def get_symbol_stats(self, symbol: str) -> Dict[str, Any]:
        """Get aggregate statistics for a symbol."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(pnl) as total_pnl,
                    AVG(pnl) as avg_pnl,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades
                FROM {self.table_prefix}trades
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
            cursor.execute(f"""
                SELECT date, pnl FROM {self.table_prefix}daily_pnl
                ORDER BY date DESC
                LIMIT ?
            """, (days,))
            
            return {row['date']: row['pnl'] for row in cursor.fetchall()}
    
    def get_monthly_pnl(self, months: int = 12) -> Dict[str, float]:
        """Get monthly PnL."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT 
                    strftime('%Y-%m', exit_time) as month,
                    SUM(pnl) as pnl
                FROM {self.table_prefix}trades
                GROUP BY month
                ORDER BY month DESC
                LIMIT ?
            """, (months,))
            
            return {row['month']: row['pnl'] for row in cursor.fetchall()}
    
    def get_equity_curve(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get equity curve data."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = f"SELECT * FROM {self.table_prefix}equity_snapshots ORDER BY timestamp"
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
            cursor.execute(f"SELECT COUNT(*) FROM {self.table_prefix}trades")
            return cursor.fetchone()[0]

    def delete_all_trades(self) -> None:
        """
        Delete all trade/performance rows.

        This is primarily used for backtesting runs that should not be mixed with
        production data. The market_data table is NOT cleared.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM {self.table_prefix}equity_snapshots")
            cursor.execute(f"DELETE FROM {self.table_prefix}trades")
            conn.commit()

    def get_all_symbols(self) -> List[str]:
        """Get list of all symbols present in market_data table."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT symbol FROM market_data ORDER BY symbol")
            rows = cursor.fetchall()
            return [r[0] for r in rows]

    
    def get_strategies(self) -> List[str]:
        """Get list of all strategies with trades."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT DISTINCT strategy FROM {self.table_prefix}trades ORDER BY strategy")
            return [row[0] for row in cursor.fetchall()]
    
    def get_symbols(self) -> List[str]:
        """Get list of all symbols traded."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT DISTINCT symbol FROM {self.table_prefix}trades ORDER BY symbol")
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

    def get_all_timestamps(self, symbol: str, timeframe: str) -> List[datetime]:
        """Get all timestamps to identify gaps."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp 
                FROM market_data 
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp ASC
            """, (symbol, timeframe))
            
            timestamps = []
            for row in cursor.fetchall():
                try:
                    timestamps.append(datetime.fromisoformat(row[0]))
                except ValueError:
                    continue
            return timestamps

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
            # DB stores naive UTC timestamps, strip tzinfo for comparison
            ts_str = start_date.replace(tzinfo=None).isoformat() if start_date.tzinfo else start_date.isoformat()
            params.append(ts_str)
        if end_date:
            query += " AND timestamp <= ?"
            ts_str = end_date.replace(tzinfo=None).isoformat() if end_date.tzinfo else end_date.isoformat()
            params.append(ts_str)
            
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

    def get_available_symbols_for_timeframes(self, timeframes: List[str], start_date: datetime, end_date: datetime) -> List[str]:
        """
        Get symbols that have data for ALL specified timeframes within the date range.
        Returns intersection of symbols.
        """
        if not timeframes:
            return []
            
        accepted_symbols = None
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            for tf in timeframes:
                # Get symbols with ANY data in this range for this timeframe
                # Note: This is an existence check, not a completeness check (which is expensive)
                # We assume if data exists in range, it's usable.
                query = """
                    SELECT DISTINCT symbol FROM market_data 
                    WHERE timeframe = ? 
                    AND timestamp >= ? 
                    AND timestamp <= ?
                """
                cursor.execute(query, (tf, start_date, end_date))
                tf_symbols = set(row[0] for row in cursor.fetchall())
                
                if accepted_symbols is None:
                    accepted_symbols = tf_symbols
                else:
                    accepted_symbols = accepted_symbols.intersection(tf_symbols)
                
                if not accepted_symbols:
                    break
                    
        return sorted(list(accepted_symbols or []))   

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

    # ==================== LIVE POSITION PERSISTENCE ====================

    def save_position(self, position_data: Dict[str, Any]):
        """
        Save an active position to the database (Upsert).
        
        Handles both single-leg and multi-leg positions.
        Replaces any existing position with the same position_id.
        """
        import json
        
        cols = [
            'position_id', 'strategy', 'symbol', 'side', 'size', 
            'leverage', 'entry_price', 'entry_time', 'stop_loss', 
            'take_profit', 'order_id', 'metadata', 'updated_at'
        ]
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Delete existing (clean slate for legs)
            cursor.execute(f"DELETE FROM {self.table_prefix}live_positions WHERE position_id = ?", (position_data['position_id'],))
            cursor.execute(f"DELETE FROM {self.table_prefix}live_position_legs WHERE position_id = ?", (position_data['position_id'],))
            
            # 2. Insert Head
            metadata_json = json.dumps(position_data.get('metadata', {}))
            
            vals = (
                position_data['position_id'],
                position_data['strategy'],
                position_data['symbol'],
                position_data['side'],
                position_data['size'],
                position_data.get('leverage'),
                position_data.get('entry_price'),
                position_data['entry_time'],
                position_data.get('stop_loss'),
                position_data.get('take_profit'),
                position_data.get('order_id'),  # Exchange OID
                metadata_json,
                datetime.now().isoformat()
            )
            
            placeholders = ','.join(['?'] * len(cols))
            cursor.execute(f"""
                INSERT INTO {self.table_prefix}live_positions ({','.join(cols)})
                VALUES ({placeholders})
            """, vals)
            
            # 3. Insert Legs (if any)
            legs = position_data.get('legs', [])
            if legs:
                leg_data = []
                for leg in legs:
                    leg_data.append((
                        position_data['position_id'],
                        leg['symbol'],
                        leg['market_type'],
                        leg['side'],
                        leg['size'],
                        leg.get('entry_price'),
                        leg.get('order_id')  # Exchange OID for this leg
                    ))
                
                cursor.executemany(f"""
                    INSERT INTO {self.table_prefix}live_position_legs 
                    (position_id, symbol, market_type, side, size, entry_price, order_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, leg_data)

    def get_all_active_positions(self) -> List[Dict[str, Any]]:
        """
        Retrieve all active positions with their legs.
        """
        import json
        
        positions = {}
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Get Heads
            cursor.execute(f"SELECT * FROM {self.table_prefix}live_positions")
            for row in cursor.fetchall():
                pos = dict(row)
                if pos['metadata']:
                    try:
                        pos['metadata'] = json.loads(pos['metadata'])
                    except:
                        pos['metadata'] = {}
                pos['legs'] = []
                positions[pos['position_id']] = pos
                
            # Get Legs
            if positions:
                placeholders = ','.join(['?'] * len(positions))
                cursor.execute(f"""
                    SELECT * FROM {self.table_prefix}live_position_legs 
                    WHERE position_id IN ({placeholders})
                """, list(positions.keys()))
                
                for row in cursor.fetchall():
                    leg = dict(row)
                    pid = leg['position_id']
                    if pid in positions:
                        # Remove redundant FK from leg object if desired, keeping for now
                        positions[pid]['legs'].append(leg)
        
        return list(positions.values())

    def delete_position(self, position_id: str):
        """Delete a position and its legs."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM {self.table_prefix}live_position_legs WHERE position_id = ?", (position_id,))
            cursor.execute(f"DELETE FROM {self.table_prefix}live_positions WHERE position_id = ?", (position_id,))

            return pd.DataFrame()

    def clear_open_positions(self):
        """Clear all open positions (Teardown/Reset)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM {self.table_prefix}live_position_legs")
            cursor.execute(f"DELETE FROM {self.table_prefix}live_positions")
