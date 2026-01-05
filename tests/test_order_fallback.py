import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestOrderFallback:
    
    @pytest.fixture
    def mock_config(self):
        return {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'wallet_address': '0x123',
                'private_key': '0xabc'
            },
            'trading': {
                'leverage': 1
            }
        }

    @pytest.fixture
    def api_client(self, mock_config):
        with patch('src.api.hyperliquid_api.Account'):
            client = HyperliquidAPI(mock_config)
            client.exchange = MagicMock()
            client.info = MagicMock()
            # Mock precision info check
            client.meta = {'universe': [{'name': 'BTC', 'szDecimals': 3}]}
            return client

    def test_market_order_fallback_triggered(self, api_client):
        """Test that market order fallback is triggered for reduce_only orders after limit failures."""
        symbol = "BTC"
        side = "sell" # Closing a long
        size = 0.1
        
        # Mock asset info
        with patch.object(api_client, '_get_asset_info_for_symbol', return_value={'name': 'BTC', 'szDecimals': 3}):
            # Mock get_current_price needed for limit price calc
            with patch.object(api_client, 'get_current_price', return_value=10000.0):
                
                # Simulate limit orders completing but not filling (e.g. price mismatch or invalid)
                # We return a response that allows the loop to continue but results in 0 filled.
                failure_response = {
                    'status': 'ok',
                    'response': {
                        'type': 'order',
                        'data': {
                            'statuses': [{'error': 'Post only maker'}]
                        }
                    }
                }
                api_client.exchange.order.return_value = failure_response
                api_client.exchange.order.side_effect = None # Clear previous side_effect
                
                # Mock market_open to succeed
                expected_response = {
                    'status': 'ok',
                    'response': {
                        'type': 'order',
                        'data': {
                            'statuses': [{'filled': {'totalSz': '0.1', 'avgPx': '9900', 'oid': 123}}]
                        }
                    }
                }
                api_client.exchange.market_open.return_value = expected_response
                
                # Execute with reduce_only=True
                result = api_client.execute_order(
                    symbol=symbol, side=side, size=size, reduce_only=True
                )
                
                # Verify
                # Should have tried limit orders 5 times (max attempts)
                assert api_client.exchange.order.call_count == 5
                
                # Should have called market_open once
                assert api_client.exchange.market_open.call_count == 1
                
                # Check args: asset, is_buy, size, px(slippage), slippage_pct
                # For SELL, is_buy=False
                # Slippage logic in execute_order uses 5% for fallback:
                # worst_price = price * 0.95 (for sell) -> 9500.0
                api_client.exchange.market_open.assert_called_with(
                    'BTC', False, 0.1, None, 0.05
                )
                
                # Result should reflect the market fill
                assert result['filled_size'] == 0.1
                assert result['avg_fill_price'] == 9900.0

    def test_no_fallback_for_normal_entry(self, api_client):
        """Test that fallback is NOT triggered for non-reduce-only orders."""
        symbol = "BTC"
        side = "buy" 
        size = 0.1
        
        with patch.object(api_client, '_get_asset_info_for_symbol', return_value={'name': 'BTC', 'szDecimals': 3}):
            with patch.object(api_client, 'get_current_price', return_value=10000.0):
                
                # Fail limit orders
                failure_response = {
                    'status': 'ok',
                    'response': {
                        'type': 'order',
                        'data': {
                            'statuses': [{'error': 'Some error'}]
                        }
                    }
                }
                api_client.exchange.order.return_value = failure_response
                
                # Execute with reduce_only=False
                result = api_client.execute_order(
                    symbol=symbol, side=side, size=size, reduce_only=False
                )
                
                # Verify limit attempts
                assert api_client.exchange.order.call_count == 5
                
                # Verify NO market fallback
                api_client.exchange.market_open.assert_not_called()
                
                # Result should indicate failure (returns structure with status='not_filled', not None)
                assert result['status'] == 'not_filled'
                assert result['filled_size'] == 0
