import numpy as np
from typing import Tuple

class KalmanFilter1D:
    """
    A simple 1D Kalman Filter for estimating a time-varying hedge ratio (beta) 
    and intercept (alpha) between two assets.
    
    Model:
    y_t = alpha_t + beta_t * x_t + epsilon_t  (Observation Equation)
    [alpha_t, beta_t] = [alpha_{t-1}, beta_{t-1}] + eta_t  (State transition - Random Walk)
    """
    
    def __init__(self, delta: float = 1e-4, R: float = 1e-3):
        """
        Args:
            delta: State noise covariance (process noise). Tuning parameter for how fast beta adapts.
            R: Measurement noise covariance. Tuning parameter for observation noise.
        """
        self.n_states = 2  # [alpha, beta]
        
        # State Vector [alpha, beta]
        self.x = np.zeros(self.n_states)
        
        # State Covariance Matrix
        self.P = np.zeros((self.n_states, self.n_states))
        
        # Measurement Noise Covariance
        self.R = R
        
        # Process Noise Covariance (assumed diagonal)
        self.Q = np.eye(self.n_states) * delta
        
        # Identity matrix
        self.eye = np.eye(self.n_states)
        
        self.initialized = False
        
    def initialize(self, intercept: float, slope: float):
        """Initialize state estimates."""
        self.x = np.array([intercept, slope])
        self.P = np.eye(self.n_states) # Initial uncertainty
        self.initialized = True
        
    def update(self, price_y: float, price_x: float) -> Tuple[float, float, float]:
        """
        Update state (alpha, beta) with new observation.
        
        Args:
            price_y: Dependent variable (e.g. Price A)
            price_x: Independent variable (e.g. Price B)
            
        Returns:
            Tuple containing:
            - error: The prediction error (residuals)
            - variance: The variance of the prediction error
            - beta: The updated slope estimate
        """
        if not self.initialized:
            # First observation heuristic if not initialized
            # (In a real system, we'd need a warm-up period or batch init)
            # For simplicity, we assume initialization happens externally or defaults 0
            self.x = np.array([0.0, 1.0])
            self.initialized = True

        # 1. Predict Step
        # State prediction: x_t|t-1 = x_{t-1} (Random walk assumption)
        x_pred = self.x 
        # Covariance prediction: P_t|t-1 = P_{t-1} + Q
        P_pred = self.P + self.Q
        
        # 2. Update Step
        # Observation matrix H = [1, price_x]
        H = np.array([1.0, price_x])
        
        # Innovation/Measurement Residual: y - H * x_pred
        # estimated_y = alpha + beta * price_x
        y_pred = np.dot(H, x_pred)
        error = price_y - y_pred
        
        # Innovation Covariance: S = H * P * H.T + R
        S = np.dot(H, np.dot(P_pred, H.T)) + self.R
        
        # Kalman Gain: K = P * H.T * S^-1
        K = np.dot(P_pred, H.T) / S
        
        # Update State: x_new = x_pred + K * error
        self.x = x_pred + K * error
        
        # Update Covariance: P_new = (I - K * H) * P_pred
        self.P = np.dot((self.eye - np.outer(K, H)), P_pred)
        
        # Extract estimates
        estimated_alpha = self.x[0]
        estimated_beta = self.x[1]
        
        return error, S, estimated_beta
