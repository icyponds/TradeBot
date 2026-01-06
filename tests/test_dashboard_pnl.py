import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import MagicMock, patch
from src.dashboard.app import _format_multi_leg_position, _get_leg_current_price
from src.models.trade import PositionLeg
from datetime import datetime, timedelta

@pytest.fixture
def mock_market_api():
    with patch('src.dashboard.app._market_api') as mock_api:
        yield mock_api

def test_get_leg_current_price_perp(mock_market_api):
    mock_market_api.get_current_price.return_value = 100.0
    price = _get_leg_current_price('BTC', 'perp')
    assert price == 100.0
    mock_market_api.get_current_price.assert_called_with('BTC')

def test_get_leg_current_price_spot(mock_market_api):
    # Case 1: Resolves via get_spot_api_name
    mock_market_api.get_spot_api_name.return_value = '@109' # UBTC(Spot)
    mock_market_api.get_current_price.return_value = 100.0
    
    price = _get_leg_current_price('UBTC', 'spot')
    assert price == 100.0
    mock_market_api.get_spot_api_name.assert_called_with('UBTC')
    mock_market_api.get_current_price.assert_called_with('@109')

def test_format_multi_leg_position_pnl_calculation(mock_market_api):
    # Setup mock legs
    leg1 = PositionLeg(
        symbol='BTC',
        side='long', 
        size=1.0,
        entry_price=50000.0,
        market_type='perp'
    )
    
    leg2 = PositionLeg(
        symbol='UBTC',
        side='short',
        size=10.0, # Spot size usually larger
        entry_price=5000.0,
        market_type='spot'
    )
    
    # Mock position object
    mock_pos = MagicMock()
    mock_pos.position_id = 'pos_123'
    mock_pos.strategy = 'StatArb'
    mock_pos.primary_symbol = 'BTC'
    mock_pos.entry_time = datetime.now() - timedelta(hours=1)
    mock_pos.legs = [leg1, leg2]
    mock_pos.metadata = {'unrealized_pnl': 0.0} # Should be overridden
    mock_pos.net_delta = 0.0
    mock_pos.capital_at_risk = 10000.0
    
    # Mock prices
    # Leg 1 (Long): 50000 -> 51000 (+1000 PnL)
    # Leg 2 (Short): 5000 -> 4900 (+1000 PnL, assuming spot short for example sake, or just tracking price delta) 
    # Note: Spot legs in StatArb are usually Long/Short hedge. 
    # Let's say Leg 2 is Short Spot (borrowed and sold). 
    
    def side_effect_get_price(symbol, market_type):
        if symbol == 'BTC': return 51000.0
        if symbol == 'UBTC': return 4900.0
        return None
        
    with patch('src.dashboard.app._get_leg_current_price', side_effect=side_effect_get_price):
        formatted = _format_multi_leg_position(mock_pos)
        
        # Verify Leg 1 PnL
        leg1_data = formatted['legs'][0]
        assert leg1_data['symbol'] == 'BTC'
        assert leg1_data['current_price'] == 51000.0
        assert leg1_data['unrealized_pnl'] == 1000.0 # (51000 - 50000) * 1
        
        # Verify Leg 2 PnL
        leg2_data = formatted['legs'][1]
        assert leg2_data['symbol'] == 'UBTC'
        assert leg2_data['current_price'] == 4900.0
        assert leg2_data['unrealized_pnl'] == 1000.0 # (5000 - 4900) * 10
        
        # Verify Net PnL
        assert formatted['unrealized_pnl'] == 2000.0
        
        # Verify Spot Flag
        assert leg1_data['is_spot'] == False
        assert leg2_data['is_spot'] == True
