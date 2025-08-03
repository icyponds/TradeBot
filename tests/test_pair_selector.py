"""
Tests for DynamicPairSelector.
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta
import pandas as pd

from src.utils.pair_selector import DynamicPairSelector


class TestDynamicPairSelector:
    """Test cases for DynamicPairSelector."""
    
    @pytest.fixture
    def config(self):
        """Sample configuration."""
        return {
            'trading': {
                'dynamic_pair_selection': True,
                'min_open_interest': 1000000,
                'max_open_interest': 100000000,
                'max_pairs_to_trade': 5,
                'scan_interval_minutes': 60,
                'excluded_assets': ['SHIB', 'DOGE'],
                'included_assets': ['BTC', 'ETH', 'SOL'],
            },
        }
    
    @pytest.fixture
    def market_api(self):
        """Mock market API."""
        return Mock()
    
    @pytest.fixture
    def pair_selector(self, config, market_api):
        """DynamicPairSelector instance."""
        return DynamicPairSelector(config, market_api)
    
    def test_initialization(self, pair_selector, config):
        """Test selector initialization."""
        assert pair_selector.dynamic_selection == True
        assert pair_selector.min_open_interest == 1000000
        assert pair_selector.max_open_interest == 100000000
        assert pair_selector.max_pairs_to_trade == 5
        assert pair_selector.excluded_assets == ['SHIB', 'DOGE']
        assert pair_selector.included_assets == ['BTC', 'ETH', 'SOL']
    
    def test_filter_assets_basic(self, pair_selector):
        """Test basic asset filtering."""
        universe = [
            {'name': 'BTC', 'openInterest': '5000000', 'volume24h': '1000000', 'markPrice': '50000', 'bid': '49900', 'ask': '50100'},
            {'name': 'ETH', 'openInterest': '2000000', 'volume24h': '800000', 'markPrice': '3000', 'bid': '2990', 'ask': '3010'},
            {'name': 'SHIB', 'openInterest': '100000', 'volume24h': '50000', 'markPrice': '0.0001', 'bid': '0.00009', 'ask': '0.00011'},
            {'name': 'SOL', 'openInterest': '1500000', 'volume24h': '600000', 'markPrice': '100', 'bid': '99', 'ask': '101'},
        ]
        
        eligible = pair_selector._filter_assets(universe)
        
        # Should exclude SHIB (too low OI and in excluded list)
        # Should include BTC, ETH, SOL (meet criteria)
        assert len(eligible) == 3
        asset_names = [asset['name'] for asset in eligible]
        assert 'BTC' in asset_names
        assert 'ETH' in asset_names
        assert 'SOL' in asset_names
        assert 'SHIB' not in asset_names
    
    def test_filter_assets_open_interest_limits(self, pair_selector):
        """Test open interest filtering."""
        universe = [
            {'name': 'LOW', 'openInterest': '500000', 'volume24h': '100000', 'markPrice': '10', 'bid': '9', 'ask': '11'},
            {'name': 'GOOD', 'openInterest': '2000000', 'volume24h': '800000', 'markPrice': '50', 'bid': '49', 'ask': '51'},
            {'name': 'HIGH', 'openInterest': '200000000', 'volume24h': '5000000', 'markPrice': '100', 'bid': '99', 'ask': '101'},
        ]
        
        eligible = pair_selector._filter_assets(universe)
        
        # Should only include 'GOOD' (within OI range)
        assert len(eligible) == 1
        assert eligible[0]['name'] == 'GOOD'
    
    def test_is_asset_eligible_valid(self, pair_selector):
        """Test asset eligibility with valid data."""
        asset = {
            'name': 'BTC',
            'volume24h': '1000000',
            'markPrice': '50000',
            'bid': '49900',
            'ask': '50100',
        }
        
        assert pair_selector._is_asset_eligible(asset) == True
    
    def test_is_asset_eligible_low_volume(self, pair_selector):
        """Test asset eligibility with low volume."""
        asset = {
            'name': 'BTC',
            'volume24h': '50000',  # Below 100k threshold
            'markPrice': '50000',
            'bid': '49900',
            'ask': '50100',
        }
        
        assert pair_selector._is_asset_eligible(asset) == False
    
    def test_is_asset_eligible_invalid_price(self, pair_selector):
        """Test asset eligibility with invalid price."""
        asset = {
            'name': 'BTC',
            'volume24h': '1000000',
            'markPrice': '0',  # Invalid price
            'bid': '49900',
            'ask': '50100',
        }
        
        assert pair_selector._is_asset_eligible(asset) == False
    
    def test_is_asset_eligible_high_spread(self, pair_selector):
        """Test asset eligibility with high bid-ask spread."""
        asset = {
            'name': 'BTC',
            'volume24h': '1000000',
            'markPrice': '50000',
            'bid': '40000',  # 20% spread
            'ask': '50000',
        }
        
        assert pair_selector._is_asset_eligible(asset) == False
    
    def test_rank_and_select_pairs(self, pair_selector):
        """Test pair ranking and selection."""
        eligible_assets = [
            {'name': 'BTC', 'openInterest': '5000000', 'volume24h': '1000000', 'markPrice': '50000'},
            {'name': 'ETH', 'openInterest': '2000000', 'volume24h': '800000', 'markPrice': '3000'},
            {'name': 'SOL', 'openInterest': '1500000', 'volume24h': '600000', 'markPrice': '100'},
            {'name': 'MATIC', 'openInterest': '1000000', 'volume24h': '400000', 'markPrice': '1'},
            {'name': 'ADA', 'openInterest': '800000', 'volume24h': '300000', 'markPrice': '0.5'},
        ]
        
        selected = pair_selector._rank_and_select_pairs(eligible_assets)
        
        # Should select top 5 pairs (max_pairs_to_trade = 5)
        assert len(selected) == 5
        # Should be ranked by composite score (BTC should be first due to highest OI and volume)
        assert selected[0] == 'BTC'
    
    def test_should_rescan_fresh(self, pair_selector):
        """Test rescan decision for fresh selector."""
        pair_selector.last_scan_time = None
        assert pair_selector.should_rescan() == True
    
    def test_should_rescan_old(self, pair_selector):
        """Test rescan decision for old scan."""
        pair_selector.last_scan_time = datetime.now() - timedelta(hours=2)
        assert pair_selector.should_rescan() == True
    
    def test_should_rescan_recent(self, pair_selector):
        """Test rescan decision for recent scan."""
        pair_selector.last_scan_time = datetime.now() - timedelta(minutes=30)
        assert pair_selector.should_rescan() == False
    
    def test_update_pair_performance(self, pair_selector):
        """Test pair performance tracking."""
        pair_selector.update_pair_performance('BTC', 100.0)
        pair_selector.update_pair_performance('BTC', 50.0)
        pair_selector.update_pair_performance('ETH', -25.0)
        
        summary = pair_selector.get_pair_performance_summary()
        
        assert 'BTC' in summary
        assert 'ETH' in summary
        assert summary['BTC']['total_pnl'] == 150.0
        assert summary['BTC']['trade_count'] == 2
        assert summary['ETH']['total_pnl'] == -25.0
        assert summary['ETH']['trade_count'] == 1
    
    @patch('src.utils.pair_selector.requests.get')
    def test_scan_and_select_pairs_success(self, mock_get, pair_selector):
        """Test successful pair scanning and selection."""
        # Mock API response
        mock_response = Mock()
        mock_response.json.return_value = {
            'universe': [
                {'name': 'BTC', 'openInterest': '5000000', 'volume24h': '1000000', 'markPrice': '50000', 'bid': '49900', 'ask': '50100'},
                {'name': 'ETH', 'openInterest': '2000000', 'volume24h': '800000', 'markPrice': '3000', 'bid': '2990', 'ask': '3010'},
                {'name': 'SOL', 'openInterest': '1500000', 'volume24h': '600000', 'markPrice': '100', 'bid': '99', 'ask': '101'},
            ]
        }
        mock_response.raise_for_status.return_value = None
        pair_selector.market_api.get_asset_info.return_value = mock_response.json.return_value
        
        selected = pair_selector.scan_and_select_pairs()
        
        assert len(selected) == 3
        assert 'BTC' in selected
        assert 'ETH' in selected
        assert 'SOL' in selected
    
    def test_scan_and_select_pairs_disabled(self, pair_selector):
        """Test pair scanning when disabled."""
        pair_selector.dynamic_selection = False
        
        selected = pair_selector.scan_and_select_pairs()
        
        assert selected == []
    
    def test_force_rescan(self, pair_selector):
        """Test forced rescan."""
        pair_selector.last_scan_time = datetime.now()
        pair_selector.selected_pairs = ['BTC', 'ETH']
        
        pair_selector.force_rescan()
        
        assert pair_selector.last_scan_time is None 