import pytest
import os
from unittest.mock import patch, MagicMock
from src.config.settings import load_config, validate_config

class TestConfig:
    
    @pytest.fixture
    def mock_env(self):
        """Mock environment variables."""
        return {
            "HYPERLIQUID_API_URL": "https://api.hyperliquid.xyz",
            "HYPERLIQUID_PRIVATE_KEY": "0x123",
            "HYPERLIQUID_WALLET_ADDRESS": "0xabc",
            "MAX_POSITION_SIZE_PERCENTAGE": "15.0",
            "ENABLED_STRATEGIES": "stat_arb,csm"
        }

    def test_load_config_basic(self, mock_env):
        """Test loading configuration from environment."""
        with patch.dict(os.environ, mock_env):
            config = load_config()
            
            assert config['api']['base_url'] == "https://api.hyperliquid.xyz"
            assert config['trading']['max_position_size_percentage'] == 15.0
            # Check instances list logic
            instances = config['strategies']['instances']
            assert any(s['type'] == 'volatility_breakout' for s in instances)
            assert len([s for s in instances if s['type'] == 'volatility_breakout']) > 0

    def test_validate_config_success(self, mock_env):
        """Test validation with valid config."""
        with patch.dict(os.environ, mock_env):
            config = load_config()
            assert validate_config(config) is True

    def test_validate_config_missing_api(self, mock_env):
        """Test validation fails when API URL is missing."""
        mock_env_broken = mock_env.copy()
        del mock_env_broken["HYPERLIQUID_API_URL"]
        
        with patch.dict(os.environ, mock_env_broken):
            config = load_config()
            # load_config might return None for missing key depending on impl, 
            # let's verify what validate_config does
            assert validate_config(config) is False

    def test_validate_config_invalid_percentage(self, mock_env):
        """Test validation fails with invalid values."""
        mock_env_broken = mock_env.copy()
        mock_env_broken["MAX_POSITION_SIZE_PERCENTAGE"] = "150.0" # Invalid > 100
        
        with patch.dict(os.environ, mock_env_broken):
            config = load_config()
            assert validate_config(config) is False
