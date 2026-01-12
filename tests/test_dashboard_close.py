
import pytest
from unittest.mock import MagicMock, patch
from flask import Flask
import json
import sys
import os

# Ensure src is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dashboard.app import create_dashboard_app, run_dashboard

@pytest.fixture
def mock_strategy_manager():
    sm = MagicMock()
    # Mock positions and multi_leg_positions
    sm.positions = {'BTC-PERP': {'symbol': 'BTC-PERP'}}
    
    # Mock multi-leg position object
    ml_pos = MagicMock()
    ml_pos.primary_symbol = 'BTC'
    ml_pos.strategy = 'stat_arb'
    sm.multi_leg_positions = {'pos_123': ml_pos}
    
    return sm

@pytest.fixture
def client(mock_strategy_manager):
    # Init global vars in app (hacky but needed for the module-level globals)
    import src.dashboard.app as app_module
    app_module._strategy_manager = mock_strategy_manager
    app_module._market_api = MagicMock()
    
    app = create_dashboard_app()
    app.config['TESTING'] = True
    
    with app.test_client() as client:
        yield client

def test_close_single_leg_success(client, mock_strategy_manager):
    mock_strategy_manager.close_position.return_value = True
    
    response = client.post('/api/close_position', json={
        'identifier': 'BTC-PERP',
        'type': 'single'
    })
    
    assert response.status_code == 200
    assert response.json['success'] is True
    mock_strategy_manager.close_position.assert_called_with('BTC-PERP', reason="manual_dashboard")

def test_close_single_leg_not_found(client, mock_strategy_manager):
    response = client.post('/api/close_position', json={
        'identifier': 'ETH-PERP',
        'type': 'single'
    })
    
    assert response.status_code == 404
    assert response.json['success'] is False

def test_close_multi_leg_success(client, mock_strategy_manager):
    response = client.post('/api/close_position', json={
        'identifier': 'pos_123',
        'type': 'multi'
    })
    
    assert response.status_code == 200
    assert response.json['success'] is True
    
    # Check if _handle_multi_leg_signal was called correctly
    args, kwargs = mock_strategy_manager._handle_multi_leg_signal.call_args
    assert args[0] == 'BTC' # symbol
    assert args[1]['action'] == 'exit' # signal
    assert args[1]['reason'] == 'manual_dashboard'
    assert args[3] == 'stat_arb' # strategy_name

def test_close_multi_leg_not_found(client, mock_strategy_manager):
    response = client.post('/api/close_position', json={
        'identifier': 'pos_999',
        'type': 'multi'
    })
    
    assert response.status_code == 404
    assert response.json['success'] is False

def test_invalid_input(client):
    response = client.post('/api/close_position', json={})
    assert response.status_code == 400
