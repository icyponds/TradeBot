import pytest
import numpy as np
from src.utils.kalman_filter import KalmanFilter1D

class TestKalmanFilter:
    
    def test_initialization(self):
        """Test initialization."""
        kf = KalmanFilter1D()
        assert kf.initialized is False
        
        kf.initialize(0.0, 1.0)
        assert kf.initialized is True
        assert kf.x[1] == 1.0
        
    def test_update_convergence(self):
        """Test that KF converges to known relationship."""
        kf = KalmanFilter1D(delta=1e-2, R=1e-3)
        kf.initialize(0.0, 1.0)
        
        # True relationship: y = 2x + 0
        true_beta = 2.0
        
        betas = []
        for i in range(50):
            x = float(i)
            y = true_beta * x # perfect correlation
            
            _, _, beta = kf.update(y, x)
            betas.append(beta)
            
        # Should converge towards 2.0
        final_beta = betas[-1]
        assert final_beta == pytest.approx(2.0, rel=0.1)
        
    def test_state_noise(self):
        """Test KF adjusting to changing beta."""
        kf = KalmanFilter1D(delta=0.1, R=1e-3)
        kf.initialize(0.0, 1.0)
        
        # Phase 1: y = x (beta=1)
        for i in range(20):
            kf.update(float(i), float(i))
            
        beta_phase1 = kf.x[1]
        assert beta_phase1 == pytest.approx(1.0, rel=0.1)
        
        # Phase 2: y = 2x (beta=2)
        for i in range(20, 50):
            kf.update(2.0 * float(i), float(i))
            
        beta_phase2 = kf.x[1]
        assert beta_phase2 > 1.5 # Should have moved significantly towards 2.0
