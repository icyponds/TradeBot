import numpy as np
import pandas as pd
from typing import Tuple, Union, Optional

def adfuller(series: Union[pd.Series, np.ndarray], lag: int = 1) -> Tuple[float, float]:
    """
    Simplified Augmented Dickey-Fuller test for stationarity.
    
    Tests the null hypothesis that a unit root is present in a time series sample.
    This is a simplified implementation using OLS on the AR(1) process:
    dy_t = rho * y_{t-1} + error_t
    
    If t-stat of rho is < critical value, we reject null (it is stationary).
    
    Args:
        series: Time series data
        lag: Lag order (simplified to 1 by default or auto-selected in full libraries)
        
    Returns:
        t_stat: The computed t-statistic
        p_value: Approximate p-value (interpolated from MacKinnon tables)
    """
    if isinstance(series, pd.Series):
        series = series.values
        
    series = series[~np.isnan(series)]
    if len(series) < 10:
        return 0.0, 1.0
        
    # Create y (series) and dy (diff)
    y = series[:-1]
    dy = np.diff(series)
    
    # We want to regress dy on y_lagged (+ optionally lags of dy)
    # Simplified Model: dy_t = beta * y_{t-1} + alpha
    
    X = np.vstack([y, np.ones(len(y))]).T
    
    if len(X) != len(dy):
        # Alignment check
        min_len = min(len(X), len(dy))
        X = X[:min_len]
        dy = dy[:min_len]
        
    # OLS: beta = (X'X)^-1 X'y
    try:
        # beta_hat = (X.T @ X)^-1 @ X.T @ dy
        # But lstsq is more stable
        beta_hat, residuals, rank, s = np.linalg.lstsq(X, dy, rcond=None)
        
        # beta_hat[0] is the coefficient for y_{t-1} (rho)
        # beta_hat[1] is intercept
        rho = beta_hat[0]
        
        # Calculate standard error of rho
        mse = np.sum((dy - X @ beta_hat)**2) / (len(dy) - 2)
        cov = mse * np.linalg.inv(X.T @ X)
        std_err_rho = np.sqrt(cov[0, 0])
        
        t_stat = rho / std_err_rho
        
        # Approximate P-value from MacKinnon (1994) for "No Trend, Constant"
        # Critical values: 1%: -3.43, 5%: -2.86, 10%: -2.57
        # Very rough approximation for trading signals
        if t_stat < -3.43:
            p_val = 0.01 + (t_stat + 3.43) * 0.005 # Arbitrary smoothing tail
        elif t_stat < -2.86:
            p_val = 0.05 - (0.04 * (abs(t_stat) - 2.86) / (3.43 - 2.86))
        elif t_stat < -2.57:
            p_val = 0.10 - (0.05 * (abs(t_stat) - 2.57) / (2.86 - 2.57))
        else:
            p_val = 1.0 # Not significant
            
        return t_stat, max(0.0, min(1.0, p_val))
        
    except Exception:
        return 0.0, 1.0

def engle_granger(series_x: Union[pd.Series, np.ndarray], series_y: Union[pd.Series, np.ndarray]) -> Tuple[float, float, float]:
    """
    Simplified Engle-Granger test for Cointegration.
    
    Step 1: OLS Regression y = beta * x + alpha
    Step 2: Check stationarity of residuals (ADF test)
    
    Args:
        series_x: Independent variable (e.g. BTC)
        series_y: Dependent variable (e.g. ETH)
        
    Returns:
        t_stat: ADF t-stat of residuals
        p_value: ADF p-value of residuals
        hedge_ratio: Calculated beta
    """
    if isinstance(series_x, pd.Series):
        series_x = series_x.values
    if isinstance(series_y, pd.Series):
        series_y = series_y.values
        
    # Length check
    min_len = min(len(series_x), len(series_y))
    x = series_x[:min_len]
    y = series_y[:min_len]
    
    # 1. OLS
    # X_mat = [x, 1]
    X_mat = np.vstack([x, np.ones(len(x))]).T
    try:
        beta_hat, _, _, _ = np.linalg.lstsq(X_mat, y, rcond=None)
        beta = beta_hat[0]
        alpha = beta_hat[1]
        
        # 2. Residuals
        # residuals = y - (beta*x + alpha)
        predictions = X_mat @ beta_hat
        residuals = y - predictions
        
        # 3. ADF on Residuals
        t_stat, p_val = adfuller(residuals)
        
        return t_stat, p_val, beta
        
    except Exception:
        return 0.0, 1.0, 1.0

def hurst_exponent(ts: Union[pd.Series, np.ndarray]) -> float:
    """
    Calculate Hurst Exponent (R/S Analysis).
    """
    if isinstance(ts, pd.Series):
        ts = ts.values
        
    series = ts[~np.isnan(ts)]
    if len(series) < 100:
        return 0.5
        
    lags = range(2, 20)
    tau = []
    
    for lag in lags:
        diff = np.subtract(series[lag:], series[:-lag])
        tau.append(np.std(diff))
        
    try:
        m = np.polyfit(np.log(lags), np.log(tau), 1)
        return m[0]
    except:
        return 0.5
