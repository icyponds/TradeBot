
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

@pytest.fixture
def mock_config():
    """Returns a standard config dictionary."""
    return {
        "api": {
            "base_url": "https://api.hyperliquid.xyz",
            "wallet_address": "0x123",
            "private_key": "0xabc"
        },
        "trading": {
            "base_currency": "USDC",
            "max_position_size_percentage": 20.0,
            "max_positions_percentage": 100.0,
            "max_account_loss_per_trade": 3.0,
            "use_portfolio_based_sizing": True,
            "symbols": ["BTC", "ETH", "SOL"]
        },
        "risk_management": {
            "strategy_exploration": {
                "reserve_capital_pct": 0.10
            },
            "max_drawdown_percentage": 15.0,
            "max_leverage": 20.0,
            "margin_buffer_percentage": 0.05,
            "liquidation_risk_threshold": 0.10
        },
        "strategies": {
            "ohlcv_limit": 100,
            "cross_sectional_momentum": {
                "lookback_period": 12,     # 4H settings
                "top_n_percent": 0.15,
                "bottom_n_percent": 0.15,
                "rebalance_interval": 4
            },
            "adaptive_grid": {
                "ema_period": 50,
                "atr_period": 14,
                "grid_spacing_atr": 2.5,
                "adx_threshold": 30
            }
        }
    }

@pytest.fixture
def sample_ohlcv_data():
    """Generates 200 hours of synthetic OHLCV data."""
    dates = pd.date_range(end=datetime.now(), periods=200, freq='1h')
    
    # Create a trend
    t = np.linspace(0, 10, 200)
    prices = 100 + t * 5 + np.sin(t * 2) * 2  # Uptrend with sine wave
    
    df = pd.DataFrame(index=dates)
    df['open'] = prices
    df['high'] = prices + 1
    df['low'] = prices - 1
    df['close'] = prices
    df['volume'] = 100000 + np.random.normal(0, 10000, 200)
    
    return df

@pytest.fixture
def mock_market_api():
    """Mocks the HyperliquidAPI."""
    from unittest.mock import MagicMock
    mock = MagicMock()
    # Setup default return values
    mock.get_position.return_value = None
    mock.get_account_summary.return_value = {"equity": 10000, "asset_value": 10000}
    return mock
