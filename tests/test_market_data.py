"""
Tests for market data API.
"""

import pytest
from unittest.mock import Mock, patch
import pandas as pd
from datetime import datetime

from src.api.market_data import MarketDataAPI


class TestMarketDataAPI:
    """Test cases for MarketDataAPI."""
    
    @pytest.fixture
    def config(self):
        """Sample configuration."""
        return {
            'api': {
                'api_key': 'test_key',
                'api_secret': 'test_secret',
                'timeout': 30,
            },
            'trading': {
                'symbols': ['BTC/USD', 'ETH/USD'],
                'base_currency': 'USD',
            },
        }
    
    @pytest.fixture
    def market_api(self, config):
        """Market data API instance."""
        return MarketDataAPI(config)
    
    def test_initialization(self, market_api, config):
        """Test API initialization."""
        assert market_api.config == config
        assert market_api.symbols == config['trading']['symbols']
        assert market_api.base_currency == config['trading']['base_currency']
    
    @patch('src.api.market_data.ccxt.binance')
    def test_get_current_price_success(self, mock_exchange, market_api):
        """Test successful price fetching."""
        # Mock exchange response
        mock_ticker = {'last': 50000.0}
        mock_exchange.return_value.fetch_ticker.return_value = mock_ticker
        
        price = market_api.get_current_price('BTC/USD')
        assert price == 50000.0
    
    @patch('src.api.market_data.ccxt.binance')
    def test_get_current_price_error(self, mock_exchange, market_api):
        """Test price fetching with error."""
        # Mock exchange error
        mock_exchange.return_value.fetch_ticker.side_effect = Exception("API Error")
        
        price = market_api.get_current_price('BTC/USD')
        assert price is None
    
    @patch('src.api.market_data.ccxt.binance')
    def test_get_ohlcv_success(self, mock_exchange, market_api):
        """Test successful OHLCV fetching."""
        # Mock OHLCV data
        mock_ohlcv = [
            [1640995200000, 50000, 51000, 49000, 50500, 1000],  # timestamp, open, high, low, close, volume
            [1640995260000, 50500, 51500, 50000, 51000, 1200],
        ]
        mock_exchange.return_value.fetch_ohlcv.return_value = mock_ohlcv
        
        ohlcv = market_api.get_ohlcv('BTC/USD')
        assert isinstance(ohlcv, pd.DataFrame)
        assert len(ohlcv) == 2
        assert 'open' in ohlcv.columns
        assert 'high' in ohlcv.columns
        assert 'low' in ohlcv.columns
        assert 'close' in ohlcv.columns
        assert 'volume' in ohlcv.columns
    
    @patch('src.api.market_data.ccxt.binance')
    def test_get_all_prices(self, mock_exchange, market_api):
        """Test getting all prices."""
        # Mock exchange responses
        mock_exchange.return_value.fetch_ticker.side_effect = [
            {'last': 50000.0},  # BTC/USD
            {'last': 3000.0},   # ETH/USD
        ]
        
        prices = market_api.get_all_prices()
        assert prices == {
            'BTC/USD': 50000.0,
            'ETH/USD': 3000.0,
        }
    
    @patch('src.api.market_data.ccxt.binance')
    def test_test_connection_success(self, mock_exchange, market_api):
        """Test successful connection test."""
        mock_exchange.return_value.fetch_ticker.return_value = {'last': 50000.0}
        
        result = market_api.test_connection()
        assert result is True
    
    @patch('src.api.market_data.ccxt.binance')
    def test_test_connection_failure(self, mock_exchange, market_api):
        """Test failed connection test."""
        mock_exchange.return_value.fetch_ticker.side_effect = Exception("Connection failed")
        
        result = market_api.test_connection()
        assert result is False 