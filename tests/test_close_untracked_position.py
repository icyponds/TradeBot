"""
Tests for close_untracked_position method in ExecutionEngine.
Handles ghost positions that exist on exchange but aren't tracked locally.
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


@pytest.fixture
def mock_execution_engine():
    """Create a mock ExecutionEngine with required dependencies."""
    with patch('src.strategies.execution_engine.ExecutionEngine.__init__', lambda x: None):
        from src.strategies.execution_engine import ExecutionEngine
        engine = ExecutionEngine()
        
        # Mock dependencies
        engine.market_api = MagicMock()
        engine.logger = MagicMock()
        engine.positions = {}
        engine.multi_leg_positions = {}
        
        return engine


class TestCloseUntrackedPosition:
    """Tests for closing ghost/untracked positions."""
    
    def test_close_native_perp_long(self, mock_execution_engine):
        """Test closing a native perp long position."""
        engine = mock_execution_engine
        engine.market_api.execute_order.return_value = {
            'filled_size': 1.0,
            'avg_fill_price': 50000.0,
            'status': 'filled'
        }
        
        success, msg = engine.close_untracked_position(
            symbol='BTC',
            size=1.0,
            side='long'
        )
        
        assert success is True
        assert '50000' in msg
        
        # Should sell to close long
        engine.market_api.execute_order.assert_called_with(
            symbol='BTC',
            side='sell',
            size=1.0,
            reduce_only=True,
            urgency='high',
            market_type='perp'
        )
    
    def test_close_native_perp_short(self, mock_execution_engine):
        """Test closing a native perp short position."""
        engine = mock_execution_engine
        engine.market_api.execute_order.return_value = {
            'filled_size': 0.5,
            'avg_fill_price': 2500.0,
            'status': 'filled'
        }
        
        success, msg = engine.close_untracked_position(
            symbol='ETH',
            size=-0.5,  # Negative for short
            side='short'
        )
        
        assert success is True
        
        # Should buy to close short, using abs(size)
        engine.market_api.execute_order.assert_called_with(
            symbol='ETH',
            side='buy',
            size=0.5,  # Absolute value
            reduce_only=True,
            urgency='high',
            market_type='perp'
        )
    
    def test_close_hip3_position(self, mock_execution_engine):
        """Test closing a HIP-3 perp position (symbol with colon prefix)."""
        engine = mock_execution_engine
        engine.market_api.execute_order.return_value = {
            'filled_size': 0.394,
            'avg_fill_price': 425.0,
            'status': 'filled'
        }
        
        success, msg = engine.close_untracked_position(
            symbol='xyz:TSLA',
            size=-0.394,
            side='short'
        )
        
        assert success is True
        
        # Should detect HIP-3 market type from colon
        engine.market_api.execute_order.assert_called_with(
            symbol='xyz:TSLA',
            side='buy',
            size=0.394,
            reduce_only=True,
            urgency='high',
            market_type='hip3'
        )
    
    def test_close_spot_position(self, mock_execution_engine):
        """Test closing a spot position."""
        engine = mock_execution_engine
        engine.market_api.execute_order.return_value = {
            'filled_size': 10.0,
            'avg_fill_price': 100.0,
            'status': 'filled'
        }
        
        success, msg = engine.close_untracked_position(
            symbol='SOL_SPOT',
            size=10.0,
            side='long'
        )
        
        assert success is True
        
        # Should detect spot market type from _SPOT suffix
        engine.market_api.execute_order.assert_called_with(
            symbol='SOL_SPOT',
            side='sell',
            size=10.0,
            reduce_only=True,
            urgency='high',
            market_type='spot'
        )
    
    def test_retry_on_failure(self, mock_execution_engine):
        """Test that retries happen on order failure."""
        engine = mock_execution_engine
        
        # First two attempts fail, third succeeds
        engine.market_api.execute_order.side_effect = [
            {'filled_size': 0},  # Not filled
            {'filled_size': 0},  # Not filled
            {'filled_size': 1.0, 'avg_fill_price': 100.0}  # Success
        ]
        
        with patch('time.sleep'):  # Don't actually sleep in tests
            success, msg = engine.close_untracked_position(
                symbol='BTC',
                size=1.0,
                side='long'
            )
        
        assert success is True
        assert engine.market_api.execute_order.call_count == 3
    
    def test_fails_after_max_attempts(self, mock_execution_engine):
        """Test that failure is reported after max attempts."""
        engine = mock_execution_engine
        
        # All attempts fail
        engine.market_api.execute_order.return_value = {'filled_size': 0}
        
        with patch('time.sleep'):  # Don't actually sleep in tests
            success, msg = engine.close_untracked_position(
                symbol='BTC',
                size=1.0,
                side='long'
            )
        
        assert success is False
        assert 'Failed after 5 attempts' in msg
        assert engine.market_api.execute_order.call_count == 5
    
    def test_handles_exception(self, mock_execution_engine):
        """Test that exceptions are caught and reported."""
        engine = mock_execution_engine
        engine.market_api.execute_order.side_effect = Exception("API error")
        
        with patch('time.sleep'):
            success, msg = engine.close_untracked_position(
                symbol='BTC',
                size=1.0,
                side='long'
            )
        
        assert success is False
        assert 'API error' in msg or 'Failed after' in msg

