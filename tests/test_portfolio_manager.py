import pytest
from unittest.mock import MagicMock
from src.utils.portfolio_manager import PortfolioManager

class TestPortfolioManager:

    @pytest.fixture
    def portfolio_manager(self, mock_config):
        """Creates a PortfolioManager instance."""
        return PortfolioManager(mock_config)

    def test_initialization(self, portfolio_manager):
        """Test initialization."""
        assert portfolio_manager.total_equity == 0.0
        assert portfolio_manager.use_portfolio_based_sizing is True

    def test_update_portfolio_info(self, portfolio_manager):
        """Test updating portfolio info from API."""
        mock_api = MagicMock()
        mock_api.get_account_balance.return_value = {
            'total_equity': 10000.0,
            'free_margin': 5000.0,
            'used_margin': 5000.0
        }
        
        success = portfolio_manager.update_portfolio_info(mock_api)
        
        assert success is True
        assert portfolio_manager.total_equity == 10000.0
        assert portfolio_manager.free_margin == 5000.0

    def test_calculate_max_position_size(self, portfolio_manager):
        """Test max position size calculation."""
        portfolio_manager.total_equity = 10000.0
        # Config has max_position_size_percentage = 20.0
        
        size = portfolio_manager.calculate_max_position_size("BTC")
        # 20% of 10000 = 2000
        assert size == 2000.0

    def test_calculate_available_capital(self, portfolio_manager):
        """Test available capital calculation."""
        portfolio_manager.total_equity = 10000.0
        portfolio_manager.free_margin = 8000.0
        # max_positions_percentage = 100.0 -> max capital = 10000
        
        available = portfolio_manager.calculate_available_capital_for_trading()
        assert available == 8000.0  # Limited by free margin
        
        # Test limit by max_positions_percentage
        portfolio_manager.max_positions_percentage = 50.0
        # max capital = 5000
        available = portfolio_manager.calculate_available_capital_for_trading()
        assert available == 5000.0

    def test_can_open_position(self, portfolio_manager):
        """Test position opening check."""
        portfolio_manager.total_equity = 10000.0
        portfolio_manager.free_margin = 5000.0
        portfolio_manager.max_positions_percentage = 100.0
        
        assert portfolio_manager.can_open_position(4000.0) is True
        assert portfolio_manager.can_open_position(6000.0) is False
