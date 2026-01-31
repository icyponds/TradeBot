import pytest
import json
from unittest.mock import MagicMock, patch
from flask import Flask
from src.dashboard.app import create_dashboard_app
from src.dashboard import app as dashboard_module
from src.models.trade import Position, MultiLegPosition, PositionLeg
from datetime import datetime

class TestDashboardCloseRepro:
    
    @pytest.fixture
    def mock_strategy_manager(self):
        sm = MagicMock()
        sm.positions = {}
        sm.multi_leg_positions = {}
        sm.is_running = True
        return sm
        
    @pytest.fixture
    def client(self, mock_strategy_manager):
        # Inject mock strategy manager into app module global
        dashboard_module._strategy_manager = mock_strategy_manager
        
        # Create app
        app = create_dashboard_app()
        app.config['TESTING'] = True
        
        with app.test_client() as client:
            yield client
            
    def test_close_single_leg_position_success(self, client, mock_strategy_manager):
        """Test happy path for closing single leg position."""
        # Setup position
        mock_strategy_manager.positions = {
            'BTC': MagicMock()
        }
        # close_position returns (bool, str) tuple
        mock_strategy_manager.close_position.return_value = (True, "Success")
        
        # Call endpoint
        response = client.post('/api/close_position', json={
            'identifier': 'BTC',
            'type': 'single'
        })
        
        # Verify
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        
        # Verify method call
        mock_strategy_manager.close_position.assert_called_with('BTC', reason='manual_dashboard')
        
    def test_close_single_leg_position_not_found(self, client, mock_strategy_manager):
        """Test closing non-existent position."""
        mock_strategy_manager.positions = {}
        
        response = client.post('/api/close_position', json={
            'identifier': 'ETH',
            'type': 'single'
        })
        
        assert response.status_code == 404
        
    def test_close_single_leg_position_failure(self, client, mock_strategy_manager):
        """Test close failure (e.g. execution error)."""
        mock_strategy_manager.positions = {'BTC': MagicMock()}
        # close_position returns (bool, str) tuple
        mock_strategy_manager.close_position.return_value = (False, "Order rejected")
        
        response = client.post('/api/close_position', json={
            'identifier': 'BTC',
            'type': 'single'
        })
        
        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'Order rejected' in data['error']

    def test_close_multi_leg_position(self, client, mock_strategy_manager):
        """Test multi-leg close."""
        pos_id = 'pos_arb_123'
        pos = MagicMock()
        pos.primary_symbol = 'BTC'
        pos.strategy = 'stat_arb'
        
        mock_strategy_manager.multi_leg_positions = {
            pos_id: pos
        }
        
        response = client.post('/api/close_position', json={
            'identifier': pos_id,
            'type': 'multi'
        })
        
        assert response.status_code == 200
        
        # Verify signal handler call
        mock_strategy_manager._handle_multi_leg_signal.assert_called_once()
        args, kwargs = mock_strategy_manager._handle_multi_leg_signal.call_args
        
        # Check arguments (symbol, signal, current_price, strategy_name...)
        assert args[0] == 'BTC'
        assert args[1]['action'] == 'exit'
        assert args[1]['reason'] == 'manual_dashboard'
        assert args[1]['urgency'] == 'high'
        assert args[3] == 'stat_arb'
