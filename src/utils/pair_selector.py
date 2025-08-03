"""
Dynamic pair selector for trading based on open interest thresholds.
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import pandas as pd


class DynamicPairSelector:
    """Selects trading pairs based on open interest and other criteria."""
    
    def __init__(self, config: Dict[str, Any], market_api):
        """
        Initialize the dynamic pair selector.
        
        Args:
            config: Configuration dictionary
            market_api: Market data API instance
        """
        self.config = config
        self.market_api = market_api
        self.logger = logging.getLogger(__name__)
        
        # Trading configuration
        self.dynamic_selection = config['trading']['dynamic_pair_selection']
        self.min_open_interest = config['trading']['min_open_interest']
        self.max_open_interest = config['trading']['max_open_interest']
        self.max_pairs_to_trade = config['trading']['max_pairs_to_trade']
        self.scan_interval_minutes = config['trading']['scan_interval_minutes']
        self.excluded_assets = config['trading']['excluded_assets']
        self.included_assets = config['trading']['included_assets']
        
        # State tracking
        self.selected_pairs = []
        self.last_scan_time = None
        self.pair_history = {}  # Track pair performance
        
        self.logger.info(f"Initialized DynamicPairSelector with OI range: ${self.min_open_interest:,} - ${self.max_open_interest:,}")
    
    def scan_and_select_pairs(self) -> List[str]:
        """
        Scan available assets and select trading pairs based on criteria.
        
        Returns:
            List of selected trading pairs
        """
        if not self.dynamic_selection:
            self.logger.info("Dynamic pair selection is disabled")
            return []
        
        try:
            # Get asset information from Hyperliquid
            asset_info = self.market_api.get_asset_info()
            if not asset_info:
                self.logger.error("Failed to get asset information")
                return []
            
            universe = asset_info.get('universe', [])
            self.logger.info(f"Scanning {len(universe)} available assets")
            
            # Filter and rank assets
            eligible_pairs = self._filter_assets(universe)
            selected_pairs = self._rank_and_select_pairs(eligible_pairs)
            
            # Update state
            self.selected_pairs = selected_pairs
            self.last_scan_time = datetime.now()
            
            self.logger.info(f"Selected {len(selected_pairs)} pairs for trading: {selected_pairs}")
            return selected_pairs
            
        except Exception as e:
            self.logger.error(f"Error scanning and selecting pairs: {e}")
            return []
    
    def _filter_assets(self, universe: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter assets based on criteria.
        
        Args:
            universe: List of all available assets
            
        Returns:
            List of eligible assets
        """
        eligible_assets = []
        
        for asset in universe:
            asset_name = asset.get('name', '')
            
            # Skip if explicitly excluded
            if asset_name in self.excluded_assets:
                continue
            
            # Skip if included assets are specified and this asset is not in the list
            if self.included_assets and asset_name not in self.included_assets:
                continue
            
            # Get open interest
            open_interest = float(asset.get('openInterest', 0))
            
            # Check open interest thresholds
            if open_interest < self.min_open_interest:
                continue
            
            if open_interest > self.max_open_interest:
                continue
            
            # Additional filtering criteria
            if self._is_asset_eligible(asset):
                eligible_assets.append(asset)
        
        self.logger.info(f"Found {len(eligible_assets)} eligible assets after filtering")
        return eligible_assets
    
    def _is_asset_eligible(self, asset: Dict[str, Any]) -> bool:
        """
        Check if an asset meets additional eligibility criteria.
        
        Args:
            asset: Asset information dictionary
            
        Returns:
            True if asset is eligible, False otherwise
        """
        try:
            # Check for minimum volume
            volume_24h = float(asset.get('volume24h', 0))
            if volume_24h < 100000:  # $100k minimum daily volume
                return False
            
            # Check for valid price
            mark_price = float(asset.get('markPrice', 0))
            if mark_price <= 0:
                return False
            
            # Check for reasonable bid-ask spread
            bid = float(asset.get('bid', 0))
            ask = float(asset.get('ask', 0))
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / bid * 100
                if spread_pct > 5:  # 5% maximum spread
                    return False
            
            return True
            
        except (ValueError, TypeError):
            return False
    
    def _rank_and_select_pairs(self, eligible_assets: List[Dict[str, Any]]) -> List[str]:
        """
        Rank eligible assets and select the best pairs.
        
        Args:
            eligible_assets: List of eligible assets
            
        Returns:
            List of selected pair symbols
        """
        if not eligible_assets:
            return []
        
        # Create DataFrame for ranking
        df = pd.DataFrame(eligible_assets)
        
        # Calculate ranking score
        df['open_interest_score'] = df['openInterest'].astype(float) / 1000000  # Normalize to millions
        df['volume_score'] = df['volume24h'].astype(float) / 1000000  # Normalize to millions
        
        # Calculate volatility (if available)
        if 'markPrice' in df.columns:
            df['price'] = df['markPrice'].astype(float)
            df['price_score'] = df['price'].rank(pct=True)  # Higher price = higher score
        
        # Calculate composite score
        df['composite_score'] = (
            df['open_interest_score'] * 0.5 +
            df['volume_score'] * 0.3 +
            df.get('price_score', 0) * 0.2
        )
        
        # Sort by composite score
        df_sorted = df.sort_values('composite_score', ascending=False)
        
        # Select top pairs
        selected_pairs = df_sorted.head(self.max_pairs_to_trade)['name'].tolist()
        
        # Log selection details
        for i, (_, row) in enumerate(df_sorted.head(self.max_pairs_to_trade).iterrows()):
            self.logger.info(
                f"Rank {i+1}: {row['name']} - "
                f"OI: ${float(row['openInterest']):,.0f}, "
                f"Volume: ${float(row['volume24h']):,.0f}, "
                f"Score: {row['composite_score']:.3f}"
            )
        
        return selected_pairs
    
    def should_rescan(self) -> bool:
        """
        Check if it's time to rescan for new pairs.
        
        Returns:
            True if should rescan, False otherwise
        """
        if not self.last_scan_time:
            return True
        
        time_since_scan = datetime.now() - self.last_scan_time
        scan_interval = timedelta(minutes=self.scan_interval_minutes)
        
        return time_since_scan >= scan_interval
    
    def get_current_pairs(self) -> List[str]:
        """
        Get currently selected trading pairs.
        
        Returns:
            List of current trading pairs
        """
        if self.should_rescan():
            return self.scan_and_select_pairs()
        return self.selected_pairs
    
    def update_pair_performance(self, symbol: str, pnl: float):
        """
        Update performance tracking for a pair.
        
        Args:
            symbol: Trading symbol
            pnl: Profit/loss for the pair
        """
        if symbol not in self.pair_history:
            self.pair_history[symbol] = {
                'total_pnl': 0,
                'trade_count': 0,
                'last_trade': None,
            }
        
        self.pair_history[symbol]['total_pnl'] += pnl
        self.pair_history[symbol]['trade_count'] += 1
        self.pair_history[symbol]['last_trade'] = datetime.now()
        
        self.logger.info(f"Updated performance for {symbol}: PnL={pnl:.2f}, Total={self.pair_history[symbol]['total_pnl']:.2f}")
    
    def get_pair_performance_summary(self) -> Dict[str, Any]:
        """
        Get performance summary for all pairs.
        
        Returns:
            Performance summary dictionary
        """
        if not self.pair_history:
            return {}
        
        summary = {}
        for symbol, data in self.pair_history.items():
            summary[symbol] = {
                'total_pnl': data['total_pnl'],
                'trade_count': data['trade_count'],
                'avg_pnl': data['total_pnl'] / data['trade_count'] if data['trade_count'] > 0 else 0,
                'last_trade': data['last_trade'].isoformat() if data['last_trade'] else None,
            }
        
        return summary
    
    def force_rescan(self):
        """Force a rescan of available pairs."""
        self.logger.info("Forcing pair rescan")
        self.last_scan_time = None
        self.scan_and_select_pairs() 