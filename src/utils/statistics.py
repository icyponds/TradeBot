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

def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """
    Calculate Average True Range (ATR).
    
    Args:
        high: High price series
        low: Low price series
        close: Close price series
        length: Window length (default 14)
        
    Returns:
        ATR series
    """
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=length).mean()

def calculate_bollinger_bands(series: pd.Series, length: int = 20, std: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculate Bollinger Bands.
    
    Args:
        series: Price series
        length: Rolling window length (default 20)
        std: Standard deviations (default 2.0)
        
    Returns:
        Tuple of (upper, middle, lower) series
    """
    middle = series.rolling(window=length).mean()
    std_dev = series.rolling(window=length).std()
    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)
    return upper, middle, lower

def calculate_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Calculate ADX (Average Directional Index).
    
    Args:
        high: High price series
        low: Low price series
        close: Close price series
        period: Smoothing period (default 14)
        
    Returns:
        ADX series
    """
    # +DM, -DM
    up = high - high.shift(1)
    down = low.shift(1) - low
    
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)
    
    # TR (using simplified calc reused from ATR logic internal or just inline)
    # Using inline to be self-contained or call calculate_atr if we wanted TR.. 
    # But ADX typically uses smoothed TR
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Smooth (Wilder's)
    # Using EWM with com = period - 1 to approximate Wilder's MMA
    tr_smooth = tr.ewm(com=period-1, min_periods=period).mean()
    plus_dm_smooth = plus_dm.ewm(com=period-1, min_periods=period).mean()
    minus_dm_smooth = minus_dm.ewm(com=period-1, min_periods=period).mean()
    
    # Handle division by zero
    tr_smooth = tr_smooth.replace(0, np.nan)
    
    # +DI, -DI
    plus_di = 100 * (plus_dm_smooth / tr_smooth)
    minus_di = 100 * (minus_dm_smooth / tr_smooth)
    
    # DX
    sum_di = plus_di + minus_di
    dx = 100 * abs(plus_di - minus_di) / sum_di
    
    # ADX
    adx = dx.ewm(com=period-1, min_periods=period).mean()
    
    return adx

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Calculate RSI (Relative Strength Index).
    
    Args:
        series: Price series
        period: RSI period (default 14)
        
    Returns:
        RSI series
    """
    delta = series.diff()
    
    up = delta.where(delta > 0, 0.0)
    down = -delta.where(delta < 0, 0.0)
    
    # Use Wilder's Smoothing
    ma_up = up.ewm(com=period-1, min_periods=period).mean()
    ma_down = down.ewm(com=period-1, min_periods=period).mean()
    
    rs = ma_up / ma_down
    rs = rs.replace([np.inf, -np.inf], np.nan)
    
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_annualized_volatility(prices: Union[pd.Series, np.ndarray], window: int = 30) -> float:
    """
    Calculate annualized volatility for a price series.
    
    Assumes daily data or resamples appropriately.
    Standard crypto convention: Vol * sqrt(365) for daily data.
    
    Args:
        prices: Price series
        window: Lookback window (default 30)
        
    Returns:
        Annualized volatility (decimal, e.g. 0.40 for 40%)
    """
    if isinstance(prices, np.ndarray):
        prices = pd.Series(prices)
        
    if len(prices) < 2:
        return 0.0
        
    returns = prices.pct_change().dropna()
    
    if len(returns) == 0:
        return 0.0
    
    # Calculate volatility (std dev of returns)
    if len(returns) > window:
        vol = returns.rolling(window=window).std().iloc[-1]
    else:
        vol = returns.std()
        
    # Scale to annualized (assuming daily returns)
    annualized_vol = vol * np.sqrt(365)
    
    return float(annualized_vol) if not np.isnan(annualized_vol) else 0.0

