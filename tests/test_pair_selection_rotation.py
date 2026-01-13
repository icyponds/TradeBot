"""
Unit tests for rotational scouting in DynamicPairSelector.

Tests:
1. Core Pool Cap: Ensure selected_pairs never exceeds max_pairs_to_trade.
2. Rotation Logic: Verify swap behavior when a higher-scoring asset arrives.
3. Subscription State: Verify subscribe/unsubscribe calls for swap operations.
"""

import pytest
from unittest.mock import MagicMock, patch
from src.utils.pair_selector import DynamicPairSelector, AssetMetrics


@pytest.fixture
def mock_config():
    """Create a minimal config for testing."""
    return {
        'trading': {
            'dynamic_pair_selection': True,
            'min_open_interest': 1000,
            'scan_interval_minutes': 60,
            'max_pairs_to_trade': 3,  # Small pool for testing
            'excluded_assets': [],
            'included_assets': [],
        },
        'hip3': {'enabled': False},
        'spot': {'enabled': False},
        'pair_selection': {
            'mode': 'sophisticated',
            'weights': {
                'liquidity': 0.25,
                'volatility': 0.20,
                'strategy_fit': 0.25,
                'diversification': 0.15,
                'historical_performance': 0.15,
            },
            'volatility': {
                'optimal_min': 0.40,
                'optimal_max': 1.50,
                'lookback_days': 14,
            },
            'diversification': {
                'max_correlation': 0.7,
                'penalty_factor': 0.5,
            },
        },
    }


@pytest.fixture
def mock_market_api():
    """Create a mock market API."""
    api = MagicMock()
    api.subscribe_symbol = MagicMock()
    api.unsubscribe_symbol = MagicMock()
    api._subscribed_symbols = set()
    
    def subscribe_side_effect(symbol):
        api._subscribed_symbols.add(symbol)
    
    def unsubscribe_side_effect(symbol):
        api._subscribed_symbols.discard(symbol)
    
    api.subscribe_symbol.side_effect = subscribe_side_effect
    api.unsubscribe_symbol.side_effect = unsubscribe_side_effect
    return api


@pytest.fixture
def pair_selector(mock_config, mock_market_api):
    """Create a DynamicPairSelector for testing."""
    selector = DynamicPairSelector(
        config=mock_config,
        market_api=mock_market_api,
        strategy_manager=None,
        performance_tracker=None,
        correlation_manager=None,
    )
    return selector


class TestCorePoolCap:
    """Test that the Core Pool never exceeds max_pairs_to_trade."""
    
    def test_pool_fills_to_max(self, pair_selector, mock_market_api):
        """Test that pool fills up to max_pairs_to_trade."""
        max_pairs = pair_selector._get_max_pairs_to_trade()
        assert max_pairs == 3
        
        # Simulate adding assets
        for i in range(5):
            asset = {
                'name': f'ASSET{i}',
                'market_type': 'perp',
                'openInterest': 1000000,
                'volume_24h': 500000,
                'maxLeverage': 20,
            }
            # Mock the metrics calculation
            with patch.object(pair_selector, '_calculate_asset_metrics') as mock_metrics:
                mock_metrics.return_value = AssetMetrics(
                    symbol=f'ASSET{i}',
                    market_type='perp',
                    is_hip3=False,
                    composite_score=0.5 + i * 0.1,  # Increasing scores
                )
                with patch.object(pair_selector, '_calculate_composite_score') as mock_score:
                    mock_score.return_value = 0.5 + i * 0.1
                    pair_selector._try_add_to_trading_pairs(asset)
        
        # Pool should be capped at 3
        assert len(pair_selector.selected_pairs) == 3
    
    def test_pool_never_exceeds_max(self, pair_selector):
        """Test that trying to add more assets doesn't exceed the cap."""
        max_pairs = pair_selector._get_max_pairs_to_trade()
        
        # Pre-fill the pool
        for i in range(max_pairs):
            pair_selector.selected_pairs.append(f'EXISTING{i}')
            pair_selector.selected_pairs_metadata[f'EXISTING{i}'] = {
                'composite_score': 0.5,
            }
        
        # Try to add a low-scoring asset
        asset = {'name': 'NEWASSET', 'market_type': 'perp'}
        with patch.object(pair_selector, '_calculate_asset_metrics') as mock_metrics:
            mock_metrics.return_value = AssetMetrics(
                symbol='NEWASSET',
                market_type='perp',
                is_hip3=False,
                composite_score=0.3,  # Lower than existing
            )
            with patch.object(pair_selector, '_calculate_composite_score') as mock_score:
                mock_score.return_value = 0.3
                pair_selector._try_add_to_trading_pairs(asset)
        
        # Pool should still be at max
        assert len(pair_selector.selected_pairs) == max_pairs
        assert 'NEWASSET' not in pair_selector.selected_pairs


class TestRotationLogic:
    """Test the swap/eviction logic."""
    
    def test_higher_scorer_evicts_lowest(self, pair_selector, mock_market_api):
        """Test that a higher-scoring asset evicts the lowest scorer."""
        # Pre-fill pool with known scores
        scores = {'A': 0.5, 'B': 0.6, 'C': 0.7}
        for sym, score in scores.items():
            pair_selector.selected_pairs.append(sym)
            pair_selector.selected_pairs_metadata[sym] = {'composite_score': score}
        
        assert len(pair_selector.selected_pairs) == 3
        
        # Try to add asset with score 0.65 (higher than A's 0.5)
        asset = {'name': 'NEWCOMER', 'market_type': 'perp'}
        with patch.object(pair_selector, '_calculate_asset_metrics') as mock_metrics:
            mock_metrics.return_value = AssetMetrics(
                symbol='NEWCOMER',
                market_type='perp',
                is_hip3=False,
                composite_score=0.65,
            )
            with patch.object(pair_selector, '_calculate_composite_score') as mock_score:
                mock_score.return_value = 0.65
                pair_selector._try_add_to_trading_pairs(asset)
        
        # NEWCOMER should be in, A should be out
        assert 'NEWCOMER' in pair_selector.selected_pairs
        assert 'A' not in pair_selector.selected_pairs
        assert len(pair_selector.selected_pairs) == 3
    
    def test_lower_scorer_does_not_evict(self, pair_selector):
        """Test that a lower-scoring asset doesn't evict anyone."""
        # Pre-fill pool with known scores
        scores = {'X': 0.6, 'Y': 0.7, 'Z': 0.8}
        for sym, score in scores.items():
            pair_selector.selected_pairs.append(sym)
            pair_selector.selected_pairs_metadata[sym] = {'composite_score': score}
        
        # Try to add asset with score 0.55 (lower than lowest X=0.6)
        asset = {'name': 'LOWSCORER', 'market_type': 'perp'}
        with patch.object(pair_selector, '_calculate_asset_metrics') as mock_metrics:
            mock_metrics.return_value = AssetMetrics(
                symbol='LOWSCORER',
                market_type='perp',
                is_hip3=False,
                composite_score=0.55,
            )
            with patch.object(pair_selector, '_calculate_composite_score') as mock_score:
                mock_score.return_value = 0.55
                pair_selector._try_add_to_trading_pairs(asset)
        
        # Pool should be unchanged
        assert 'LOWSCORER' not in pair_selector.selected_pairs
        assert set(pair_selector.selected_pairs) == {'X', 'Y', 'Z'}


class TestSubscriptionState:
    """Test that subscribe/unsubscribe are called correctly during swaps."""
    
    def test_subscribe_called_on_add(self, pair_selector, mock_market_api):
        """Test that subscribe_symbol is called when adding to pool."""
        asset = {'name': 'NEWASSET', 'market_type': 'perp'}
        with patch.object(pair_selector, '_calculate_asset_metrics') as mock_metrics:
            mock_metrics.return_value = AssetMetrics(
                symbol='NEWASSET',
                market_type='perp',
                is_hip3=False,
                composite_score=0.7,
            )
            with patch.object(pair_selector, '_calculate_composite_score') as mock_score:
                mock_score.return_value = 0.7
                pair_selector._try_add_to_trading_pairs(asset)
        
        mock_market_api.subscribe_symbol.assert_called_with('NEWASSET')
        assert 'NEWASSET' in mock_market_api._subscribed_symbols
    
    def test_unsubscribe_called_on_evict(self, pair_selector, mock_market_api):
        """Test that unsubscribe_symbol is called when evicting from pool."""
        # Pre-fill pool
        scores = {'OLD1': 0.5, 'OLD2': 0.6, 'OLD3': 0.7}
        for sym, score in scores.items():
            pair_selector.selected_pairs.append(sym)
            pair_selector.selected_pairs_metadata[sym] = {'composite_score': score}
            mock_market_api._subscribed_symbols.add(sym)
        
        # Add higher-scoring asset to trigger eviction
        asset = {'name': 'BETTER', 'market_type': 'perp'}
        with patch.object(pair_selector, '_calculate_asset_metrics') as mock_metrics:
            mock_metrics.return_value = AssetMetrics(
                symbol='BETTER',
                market_type='perp',
                is_hip3=False,
                composite_score=0.8,
            )
            with patch.object(pair_selector, '_calculate_composite_score') as mock_score:
                mock_score.return_value = 0.8
                pair_selector._try_add_to_trading_pairs(asset)
        
        # OLD1 (lowest scorer) should be unsubscribed
        mock_market_api.unsubscribe_symbol.assert_called_with('OLD1')
        assert 'OLD1' not in mock_market_api._subscribed_symbols
        
        # BETTER should be subscribed
        mock_market_api.subscribe_symbol.assert_called_with('BETTER')
        assert 'BETTER' in mock_market_api._subscribed_symbols
