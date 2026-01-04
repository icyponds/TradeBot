import pytest
import numpy as np
from src.utils.regime_hmm import GaussianHMM3, RegimeAllocator, logsumexp

class TestRegimeHMM:
    
    def test_logsumexp(self):
        """Test numeric stability of logsumexp."""
        a = np.array([1000, 1000, 1000])
        # log(exp(1000) * 3) = 1000 + log(3) ~= 1001.0986
        result = logsumexp(a)
        assert result == pytest.approx(1000 + np.log(3))
        
    def test_gaussian_hmm_fit(self):
        """Test HMM fitting on simple synthetic data."""
        # Create clear 2-state data: low variance vs high variance
        rng = np.random.default_rng(42)
        X1 = rng.normal(0, 0.1, (100, 2))  # Stable
        X2 = rng.normal(0, 2.0, (100, 2))  # Volatile
        X = np.concatenate([X1, X2])
        
        hmm = GaussianHMM3(n_states=2, n_iter=10, seed=42)
        hmm.fit(X)
        
        # Check if means and vars are populated
        assert hmm.means is not None
        assert hmm.vars is not None
        
        # Check dimensions
        assert hmm.means.shape == (2, 2)
        
    def test_regime_allocator_update(self):
        """Test RegimeAllocator update cycle."""
        alloc = RegimeAllocator(lookback=50, retrain_minutes=0) # 0 to force retrain check
        
        # Generate dummy OHLCV-like features
        # 50 rows, 2 columns (trend, vol)
        X = np.random.randn(50, 2)
        
        result = alloc.update(X, now_ts=1000)
        
        assert result.regime in ["range", "trend", "high_vol"]
        assert len(result.probs) == 3
        
    def test_feature_builder(self):
        """Test building features from dataframe."""
        import pandas as pd
        
        # Create dummy df
        prices = [100.0 + i*0.1 for i in range(100)] # Trending up
        df = pd.DataFrame({'close': prices})
        
        X = RegimeAllocator.build_features_from_ohlcv(df)
        
        assert X.shape[0] > 0
        assert X.shape[1] == 2 # trend, vol
