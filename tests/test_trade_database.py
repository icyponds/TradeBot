import pytest
import sqlite3
import os
from datetime import datetime
from src.utils.trade_database import TradeDatabase

class TestTradeDatabase:
    
    @pytest.fixture
    def db_path(self, tmp_path):
        """Temp DB path."""
        return str(tmp_path / "test_trades.db")
        
    @pytest.fixture
    def db(self, db_path):
        """Database instance."""
        return TradeDatabase(db_path)
        
    def test_initialization(self, db, db_path):
        """Test DB init and schema creation."""
        assert os.path.exists(db_path)
        
        # Verify table existence
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
            assert cursor.fetchone() is not None
            
    def test_insert_and_retrieve_trade(self, db):
        """Test inserting and fetching a trade."""
        trade_data = {
            'symbol': 'BTC',
            'strategy': 'test_strat',
            'side': 'buy',
            'entry_price': 50000.0,
            'exit_price': 51000.0,
            'size': 0.1,
            'entry_time': datetime.now().isoformat(),
            'exit_time': datetime.now().isoformat(),
            'pnl': 100.0,
            'pnl_percentage': 2.0,
            'capital_at_risk': 5000.0,
            'exit_reason': 'take_profit',
            'limit': 10
        }
        
        trade_id = db.insert_trade(trade_data)
        assert trade_id is not None
        
        # Retrieve
        trades = db.get_all_trades()
        assert len(trades) == 1
        assert trades[0]['symbol'] == 'BTC'
        assert trades[0]['pnl'] == 100.0

    def test_get_strategy_stats(self, db):
        """Test aggregation query."""
        # Insert 2 trades
        t1 = {
            'symbol': 'BTC', 'strategy': 's1', 'side': 'buy',
            'entry_price': 100, 'exit_price': 110, 'size': 1,
            'entry_time': '2024-01-01', 'exit_time': '2024-01-02',
            'pnl': 10, 'pnl_percentage': 10, 'capital_at_risk': 100,
            'exit_reason': 'tp'
        }
        t2 = {
            'symbol': 'BTC', 'strategy': 's1', 'side': 'buy',
            'entry_price': 100, 'exit_price': 90, 'size': 1,
            'entry_time': '2024-01-03', 'exit_time': '2024-01-04',
            'pnl': -10, 'pnl_percentage': -10, 'capital_at_risk': 100,
            'exit_reason': 'sl'
        }
        
        db.insert_trade(t1)
        db.insert_trade(t2)
        
        # Test stats
        stats = db.get_strategy_stats('s1')
        assert stats['total_trades'] == 2
        assert stats['total_pnl'] == 0.0
        assert stats['win_rate'] == 50.0  # 1 win, 1 loss

    def test_save_and_get_candles(self, db):
        """Test market candle persistence."""
        candles = [
            {'time': 1700000000, 'open': 100.0, 'high': 105.0, 'low': 99.0, 'close': 104.0, 'volume': 1000.0},
            {'time': 1700003600, 'open': 104.0, 'high': 110.0, 'low': 103.0, 'close': 108.0, 'volume': 1200.0},
            {'time': 1700007200, 'open': 108.0, 'high': 112.0, 'low': 107.0, 'close': 111.0, 'volume': 800.0},
        ]
        
        db.save_candles('BTC', '1h', candles)
        
        retrieved = db.get_candles('BTC', '1h', limit=10)
        assert len(retrieved) == 3
        assert retrieved[0]['close'] == 104.0
        assert retrieved[2]['close'] == 111.0
        
    def test_get_latest_candle_time(self, db):
        """Test latest candle timestamp retrieval."""
        # No data initially
        latest = db.get_latest_candle_time('ETH', '1h')
        assert latest is None
        
        # Add some candles
        candles = [
            {'time': 1700000000, 'open': 2000.0, 'high': 2050.0, 'low': 1990.0, 'close': 2040.0, 'volume': 500.0},
            {'time': 1700003600, 'open': 2040.0, 'high': 2100.0, 'low': 2030.0, 'close': 2080.0, 'volume': 600.0},
        ]
        db.save_candles('ETH', '1h', candles)
        
        latest = db.get_latest_candle_time('ETH', '1h')
        # Time is stored as ms internally
        assert latest == 1700003600 * 1000
        
    def test_candle_gap_fill(self, db):
        """Test incremental candle loading (gap-fill scenario)."""
        # Initial load
        initial = [
            {'time': 1700000000, 'open': 100.0, 'high': 105.0, 'low': 99.0, 'close': 104.0, 'volume': 1000.0},
        ]
        db.save_candles('SOL', '1h', initial)
        assert len(db.get_candles('SOL', '1h')) == 1
        
        # Gap fill (add new candles)
        new_candles = [
            {'time': 1700003600, 'open': 104.0, 'high': 110.0, 'low': 103.0, 'close': 108.0, 'volume': 1200.0},
            {'time': 1700007200, 'open': 108.0, 'high': 112.0, 'low': 107.0, 'close': 111.0, 'volume': 800.0},
        ]
        db.save_candles('SOL', '1h', new_candles)
        
        all_candles = db.get_candles('SOL', '1h')
        assert len(all_candles) == 3
        assert db.get_latest_candle_time('SOL', '1h') == 1700007200 * 1000
