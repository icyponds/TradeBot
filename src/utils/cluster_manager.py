import numpy as np
import pandas as pd
import logging
from typing import List, Dict, Tuple, Optional

class ClusterManager:
    """
    Manages asset clustering for diversified portfolio construction.
    Uses correlation-based clustering to group assets with similar price movements.
    Implements custom K-Means using NumPy since sklearn/scipy are unavailable.
    """
    
    def __init__(self, market_api):
        self.market_api = market_api
        self.logger = logging.getLogger(__name__)
        
    def cluster_assets(self, symbols: List[str], n_clusters: int = 5, lookback_days: int = 30) -> Dict[int, List[str]]:
        """
        Group assets into n_clusters based on price correlation.
        
        Args:
            symbols: List of candidate symbols
            n_clusters: Number of clusters to create
            lookback_days: Days of history to use for correlation
            
        Returns:
            Dict mapping cluster_id -> list of symbols
        """
        if len(symbols) < n_clusters:
            # Not enough symbols to cluster, return single cluster or trivial mapping
            return {0: symbols}
            
        # 1. Fetch History & Build Price Matrix
        price_df = self._build_price_matrix(symbols, lookback_days)
        if price_df.empty or len(price_df.columns) < 2:
            self.logger.warning("Insufficient data for clustering. Returning single cluster.")
            return {0: symbols}
            
        # 2. Calculate Correlation Matrix
        # Transpose so rows are assets, columns are time steps (returns) for correlation
        returns_df = price_df.pct_change().dropna()
        if returns_df.empty:
             return {0: symbols}

        # We actually want to cluster based on the behavior (correlation features)
        # One way is to use the correlation matrix itself as the feature set for each asset.
        # Asset A's features = [Corr(A, A), Corr(A, B), Corr(A, C)...]
        correlation_matrix = returns_df.corr().fillna(0)
        
        # Convert to numpy array for our manual K-Means
        # Rows = samples (assets), Cols = features (correlations with other assets)
        data = correlation_matrix.values
        asset_names = correlation_matrix.index.tolist()
        
        # 3. Apply K-Means
        try:
            labels = self._kmeans(data, k=n_clusters)
            
            # 4. Map back to symbols
            clusters = {}
            for asset, label in zip(asset_names, labels):
                if label not in clusters:
                    clusters[label] = []
                clusters[label].append(asset)
                
            # Add back any symbols that were dropped due to data issues
            all_clustered = set(asset_names)
            missing = [s for s in symbols if s not in all_clustered]
            if missing:
                # Add to a new or existing cluster
                clusters[0].extend(missing)
                
            self.logger.info(f"Clustered {len(asset_names)} assets into {len(clusters)} groups")
            return clusters
            
        except Exception as e:
            self.logger.error(f"Clustering failed: {e}")
            return {0: symbols}
            
    def _build_price_matrix(self, symbols: List[str], days: int) -> pd.DataFrame:
        """Fetch historical close prices for all symbols."""
        prices = {}
        
        # We need a common index. Let's fetch one valid symbol first to get index?
        # Or just fetch all and concat.
        
        for symbol in symbols:
            # Use 4h data for clustering (good balance of granularity and noise reduction)
            # Assuming get_ohlcv returns a DataFrame with 'close' column
            df = self.market_api.get_ohlcv(symbol, '4h', limit=days*6) # 6 candles per day
            if df is not None and not df.empty:
                prices[symbol] = df['close']
                
        if not prices:
            return pd.DataFrame()
            
        # Combine into single DF, aligned by timestamp
        df = pd.concat(prices, axis=1)
        # Forward fill gaps (some assets might be illiquid or missing candles)
        df.fillna(method='ffill', inplace=True)
        # Drop leading NaNs
        df.dropna(how='any', axis=0, inplace=True) 
        
        return df

    def _kmeans(self, data: np.ndarray, k: int, max_iters: int = 100) -> np.ndarray:
        """
        Simple K-Means implementation using NumPy.
        
        Args:
            data: shape (n_samples, n_features)
            k: number of clusters
            
        Returns:
            labels: shape (n_samples,) with cluster indices
        """
        n_samples, _ = data.shape
        
        # Randomly initialize centroids
        # Pick k unique random indices
        random_indices = np.random.choice(n_samples, size=k, replace=False)
        centroids = data[random_indices]
        
        labels = np.zeros(n_samples, dtype=int)
        
        for _ in range(max_iters):
            # Calculate distances from each sample to each centroid
            # data: (N, F), centroids: (K, F)
            # dists: (N, K)
            
            # Expand dims to broadcast: (N, 1, F) - (1, K, F)
            dists = np.linalg.norm(data[:, np.newaxis, :] - centroids[np.newaxis, :, :], axis=2)
            
            # Assign labels
            new_labels = np.argmin(dists, axis=1)
            
            # Check convergence
            if np.array_equal(labels, new_labels):
                break
                
            labels = new_labels
            
            # Update centroids
            for i in range(k):
                # Points in this cluster
                points = data[labels == i]
                if len(points) > 0:
                    centroids[i] = points.mean(axis=0)
                # If a cluster is empty, we leave the centroid where it is (or could re-initialize)
                
        return labels
