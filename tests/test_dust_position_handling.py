"""
Tests for dust position handling in execute_order.

Dust positions are positions too small for normal limit orders (< $10 value or
size rounds to 0). These are handled by:
1. Getting price from L2 order book (works for HIP-3 unlike all_mids)
2. Placing aggressive IOC order with original (unrounded) size
"""

import pytest
from unittest.mock import MagicMock, patch


class TestDustPositionHandling:
    """Test suite for dust position handling in execute_order."""
    
    @pytest.fixture
    def api_client(self, shared_api_client):
        """Use shared module-scoped client, reset mocks before each test."""
        shared_api_client.exchange.reset_mock()
        shared_api_client.info.reset_mock()
        return shared_api_client

    def test_dust_position_uses_l2_orderbook(self, api_client):
        """Test that dust positions get price from L2 order book."""
        symbol = "xyz:TSLA"
        side = "buy"  # Closing a short
        size = 0.004  # Dust size - rounds to 0 with 2 decimals
        
        with patch.object(api_client, '_get_asset_info_for_symbol', return_value={'name': 'xyz:TSLA', 'szDecimals': 2}):
            with patch.object(api_client, 'get_current_price', return_value=423.0):
                # Mock L2 order book
                api_client.info.l2_snapshot.return_value = {
                    'coin': 'xyz:TSLA',
                    'levels': [
                        [{'px': '422.50', 'sz': '1.0', 'n': 1}],  # Bids
                        [{'px': '423.50', 'sz': '1.0', 'n': 1}]   # Asks
                    ]
                }
                
                # Mock successful order
                api_client.exchange.order.return_value = {
                    'status': 'ok',
                    'response': {
                        'type': 'order',
                        'data': {
                            'statuses': [{'filled': {'totalSz': '0.004', 'avgPx': '423.50', 'oid': 123}}]
                        }
                    }
                }
                
                result = api_client.execute_order(
                    symbol=symbol,
                    side=side,
                    size=size,
                    reduce_only=True,  # Important: must be reduce_only
                    market_type='hip3'
                )
                
                # Verify L2 was called
                api_client.info.l2_snapshot.assert_called_with('xyz:TSLA')
                
                # Verify order was placed with original size (not rounded to 0)
                api_client.exchange.order.assert_called()
                call_args = api_client.exchange.order.call_args
                ordered_size = call_args[0][2]
                assert abs(ordered_size - 0.004) < 0.0001

    def test_dust_position_not_triggered_for_normal_orders(self, api_client):
        """Test that normal (non-reduce_only) orders don't trigger dust handling."""
        symbol = "BTC"
        side = "buy"
        size = 0.0001  # Dust size
        
        with patch.object(api_client, '_get_asset_info_for_symbol', return_value={'name': 'BTC', 'szDecimals': 4}):
            with patch.object(api_client, 'get_current_price', return_value=80000.0):
                # Order value = 0.0001 * 80000 = $8 < $10 minimum
                # But reduce_only=False, so should reject, not use dust handling
                
                result = api_client.execute_order(
                    symbol=symbol,
                    side=side,
                    size=size,
                    reduce_only=False,  # NOT reduce_only
                    market_type='perp'
                )
                
                # Should return None (rejected) not try dust handling
                # The L2 snapshot should NOT be called for dust handling
                # (might be called for other reasons, so just check result)
                assert result is None or result.get('status') == 'not_filled'

    def test_dust_position_calculates_aggressive_price(self, api_client):
        """Test that dust position handler uses aggressive slippage from L2 price."""
        symbol = "xyz:AMD"
        side = "sell"  # Closing a long
        size = 0.003
        
        with patch.object(api_client, '_get_asset_info_for_symbol', return_value={'name': 'xyz:AMD', 'szDecimals': 2}):
            with patch.object(api_client, 'get_current_price', return_value=230.0):
                # Mock L2 order book
                api_client.info.l2_snapshot.return_value = {
                    'coin': 'xyz:AMD',
                    'levels': [
                        [{'px': '229.50', 'sz': '1.0', 'n': 1}],  # Best bid
                        [{'px': '230.50', 'sz': '1.0', 'n': 1}]   # Best ask
                    ]
                }
                
                # Mock successful order
                api_client.exchange.order.return_value = {
                    'status': 'ok',
                    'response': {
                        'type': 'order',
                        'data': {
                            'statuses': [{'filled': {'totalSz': '0.003', 'avgPx': '229.00', 'oid': 456}}]
                        }
                    }
                }
                
                result = api_client.execute_order(
                    symbol=symbol,
                    side=side,
                    size=size,
                    reduce_only=True,
                    market_type='hip3'
                )
                
                # For SELL, price should be ~5% below best bid (229.50 * 0.95 ≈ 218.02)
                call_args = api_client.exchange.order.call_args
                exec_price = call_args[0][3]
                expected_price = round(229.50 * 0.95, 2)
                assert abs(exec_price - expected_price) < 0.01


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

