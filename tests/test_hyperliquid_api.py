"""
Tests for Hyperliquid API.
"""

import pytest
from unittest.mock import Mock, patch
import pandas as pd
from datetime import datetime

from src.api.hyperliquid_api import HyperliquidAPI


class TestHyperliquidAPI:
    """Test cases for HyperliquidAPI."""
    
    @pytest.fixture
    def config(self):
        """Sample configuration."""
        return {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'ws_url': 'wss://api.hyperliquid.xyz/ws',
                'private_key': 'test_private_key',
                'wallet_address': 'test_wallet_address',
                'timeout': 30,
            },
            'trading': {
                'symbols': ['BTC', 'ETH', 'SOL'],
                'base_currency': 'USDC',
            },
        }
    
    @pytest.fixture
    def hyperliquid_api(self, config):
        """Hyperliquid API instance."""
        return HyperliquidAPI(config)
    
    def test_initialization(self, hyperliquid_api, config):
        """Test API initialization."""
        assert hyperliquid_api.config == config
        assert hyperliquid_api.symbols == config['trading']['symbols']
        assert hyperliquid_api.base_currency == config['trading']['base_currency']
        assert hyperliquid_api.private_key == config['api']['private_key']
        assert hyperliquid_api.wallet_address == config['api']['wallet_address']
    
    @patch('src.api.hyperliquid_api.requests.get')
    def test_get_asset_info_success(self, mock_get, hyperliquid_api):
        """Test successful asset info fetching."""
        # Mock response
        mock_response = Mock()
        mock_response.json.return_value = {
            'universe': [
                {'name': 'BTC', 'markPrice': '50000', 'bid': '49900', 'ask': '50100'},
                {'name': 'ETH', 'markPrice': '3000', 'bid': '2990', 'ask': '3010'},
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        
        asset_info = hyperliquid_api.get_asset_info()
        assert asset_info is not None
        assert len(asset_info['universe']) == 2
    
    @patch('src.api.hyperliquid_api.requests.get')
    def test_get_market_data_success(self, mock_get, hyperliquid_api):
        """Test successful market data fetching."""
        # Mock response
        mock_response = Mock()
        mock_response.json.return_value = {
            'universe': [
                {
                    'name': 'BTC',
                    'markPrice': '50000',
                    'bid': '49900',
                    'ask': '50100',
                    'volume24h': '1000000',
                    'openInterest': '500000',
                    'fundingRate': '0.0001'
                }
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        
        market_data = hyperliquid_api.get_market_data('BTC')
        assert market_data is not None
        assert market_data['symbol'] == 'BTC'
        assert market_data['current_price'] == 50000.0
        assert market_data['bid'] == 49900.0
        assert market_data['ask'] == 50100.0
    
    @patch('src.api.hyperliquid_api.requests.get')
    def test_get_current_price_success(self, mock_get, hyperliquid_api):
        """Test successful price fetching."""
        # Mock response
        mock_response = Mock()
        mock_response.json.return_value = {
            'universe': [
                {'name': 'BTC', 'markPrice': '50000', 'bid': '49900', 'ask': '50100'}
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        
        price = hyperliquid_api.get_current_price('BTC')
        assert price == 50000.0
    
    @patch('src.api.hyperliquid_api.requests.get')
    def test_get_current_price_error(self, mock_get, hyperliquid_api):
        """Test price fetching with error."""
        # Mock error response
        mock_get.side_effect = Exception("API Error")
        
        price = hyperliquid_api.get_current_price('BTC')
        assert price is None
    
    @patch('src.api.hyperliquid_api.requests.get')
    def test_get_all_prices(self, mock_get, hyperliquid_api):
        """Test getting all prices."""
        # Mock response
        mock_response = Mock()
        mock_response.json.return_value = {
            'universe': [
                {'name': 'BTC', 'markPrice': '50000', 'bid': '49900', 'ask': '50100'},
                {'name': 'ETH', 'markPrice': '3000', 'bid': '2990', 'ask': '3010'},
                {'name': 'SOL', 'markPrice': '100', 'bid': '99', 'ask': '101'},
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        
        prices = hyperliquid_api.get_all_prices()
        assert prices == {
            'BTC': 50000.0,
            'ETH': 3000.0,
            'SOL': 100.0,
        }
    
    def test_place_order_without_credentials(self, hyperliquid_api):
        """Test placing order without credentials."""
        # Set empty credentials
        hyperliquid_api.private_key = ""
        hyperliquid_api.wallet_address = ""
        
        result = hyperliquid_api.place_order('BTC', 'buy', 1.0, 50000.0)
        assert result is None
    
    def test_place_order_invalid_side(self, hyperliquid_api):
        """Test placing order with invalid side."""
        result = hyperliquid_api.place_order('BTC', 'invalid', 1.0, 50000.0)
        assert result is None
    
    @patch('src.api.hyperliquid_api.requests.get')
    def test_test_connection_success(self, mock_get, hyperliquid_api):
        """Test successful connection test."""
        # Mock successful response
        mock_response = Mock()
        mock_response.json.return_value = {'universe': []}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        
        result = hyperliquid_api.test_connection()
        assert result is True
    
    @patch('src.api.hyperliquid_api.requests.get')
    def test_test_connection_failure(self, mock_get, hyperliquid_api):
        """Test failed connection test."""
        # Mock error response
        mock_get.side_effect = Exception("Connection failed")
        
        result = hyperliquid_api.test_connection()
        assert result is False
    
    def test_get_signature(self, hyperliquid_api):
        """Test signature generation."""
        test_data = '{"test": "data"}'
        signature = hyperliquid_api._get_signature(test_data)
        
        # Signature should be a hex string
        assert isinstance(signature, str)
        assert len(signature) == 64  # SHA256 hex length 