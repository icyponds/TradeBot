
import os
import sys
import sqlite3
import pandas as pd
import numpy as np
from itertools import combinations
import logging

# Add project root to path
sys.path.append(os.getcwd())
from src.utils.statistics import engle_granger

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = 'data/trades.db'

def fetch_historical_data(timeframe='1h'):
    """Fetch close prices from local DB."""
    if not os.path.exists(DB_PATH):
        logger.error(f"Database not found at {DB_PATH}")
        return None

    conn = sqlite3.connect(DB_PATH)
    try:
        # Get all symbols with sufficient data
        query = f"""
            SELECT symbol, timestamp, close 
            FROM market_data 
            WHERE timeframe = '{timeframe}'
            ORDER BY timestamp ASC
        """
        logger.info(f"Fetching {timeframe} data from {DB_PATH}...")
        df = pd.read_sql_query(query, conn)
        
        # Pivot to format: Index=Timestamp, Columns=Symbols, Values=Close
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        pivoted = df.pivot(index='timestamp', columns='symbol', values='close')
        
        # Filter columns with sufficient data (e.g., > 500 points)
        min_points = 500
        valid_cols = pivoted.dropna(axis=1, thresh=min_points).columns
        pivoted = pivoted[valid_cols]
        
        # Forward fill small gaps, then drop remaining NaNs
        pivoted = pivoted.ffill().dropna()
        
        return pivoted
    except Exception as e:
        logger.error(f"Error fetching data: {e}")
        return None
    finally:
        conn.close()

def main():
    logger.info("Starting Hedge Ratio Research (Local DB)...")
    
    # 1. Load Data
    df_prices = fetch_historical_data('1h')
    if df_prices is None or df_prices.empty:
        logger.error("No data available.")
        return

    assets = df_prices.columns.tolist()
    logger.info(f"Loaded {len(assets)} assets with sufficient history: {assets[:10]}...")
    
    # Limit to last 500 periods (approx 20 days) for relevance
    df_prices = df_prices.iloc[-500:]
    
    # 2. Analyze Pairs
    valid_hedge_ratios = []
    pairs_found = 0
    
    # Filter stablecoins/special assets if needed, but let's check everything
    excludes = ['USDC', 'USDT', 'HYPE_SPOT']
    assets = [a for a in assets if a not in excludes and not a.endswith('_SPOT')]
    
    # Random sample or top volume? 
    # Since we have data, let's just use the assets we have.
    # To avoid huge combinatorial explosion (if 100 assets -> 5000 pairs), let's limit to top 30 by price volume proxy if needed.
    # But 30 assets is 435 pairs. Doable.
    if len(assets) > 40:
        assets = assets[:40] # Arbitrary cut to keep script fast
        logger.info(f"Limiting to first 40 assets for speed.")
    
    combos = list(combinations(assets, 2))
    logger.info(f"Scanning {len(combos)} combinations for cointegration...")
    
    for a, b in combos:
        s1 = df_prices[a]
        s2 = df_prices[b]
        
        # Check Cointegration
        try:
            # engle_granger(dependent=y, independent=x) -> returns beta for y = beta*x + alpha
            # Strategy assumes: spread = prices_a - hedge_ratio * prices_b
            # So prices_a = hedge_ratio * prices_b
            # Let y = prices_a (s1), x = prices_b (s2)
            
            t_stat, p_value, beta = engle_granger(s2.values, s1.values)
            
            # Using looser p-value for research visibility, but 0.05 is standard
            if p_value < 0.05:
                valid_hedge_ratios.append({
                    'pair': f"{a}/{b}",
                    'p_value': p_value,
                    'price_a': s1.iloc[-1],
                    'price_b': s2.iloc[-1],
                    'ratio': beta,
                    'abs_ratio': abs(beta)
                })
                pairs_found += 1
                
        except Exception:
            continue
            
    # 3. Statistics
    if not valid_hedge_ratios:
        logger.warning("No cointegrated pairs found.")
        return

    df_ratios = pd.DataFrame(valid_hedge_ratios)
    
    logger.info("="*50)
    logger.info(f"RESULTS: {pairs_found} Cointegrated Pairs found")
    logger.info("="*50)
    
    ratios = df_ratios['abs_ratio']
    
    print(f"\nTimeframe: 1h (Last 500 candles)")
    print(f"Total Pairs: {len(ratios)}")
    print(f"Mean Ratio: {ratios.mean():.4f}")
    print(f"Median Ratio: {ratios.median():.4f}")
    print(f"Min Ratio: {ratios.min():.4f}")
    print(f"Max Ratio: {ratios.max():.4f}")
    print(f"90th Percentile: {ratios.quantile(0.90):.4f}")
    print(f"95th Percentile: {ratios.quantile(0.95):.4f}")
    
    print("\n--- Pairs Exceeding 3.0 Cap ---")
    high_ratios = df_ratios[df_ratios['abs_ratio'] > 3.0].sort_values('abs_ratio', ascending=False)
    if not high_ratios.empty:
        print(high_ratios[['pair', 'price_a', 'price_b', 'abs_ratio']].head(15).to_string(index=False))
        print(f"\nTotal > 3.0: {len(high_ratios)} pairs ({len(high_ratios)/len(ratios)*100:.1f}%)")
    else:
        print("None.")

    # Also check the inverse (0.33) which corresponds to flipping the pair
    # If A/B has ratio 10, then B/A has ratio 0.1
    # We should exclude these extremes too if we want "matchable" pairs
    print("\n--- Pairs Under 0.33 (Inverse of 3.0) ---")
    low_ratios = df_ratios[df_ratios['abs_ratio'] < 0.33].sort_values('abs_ratio', ascending=True)
    if not low_ratios.empty:
        print(low_ratios[['pair', 'price_a', 'price_b', 'abs_ratio']].head(15).to_string(index=False))
        print(f"\nTotal < 0.33: {len(low_ratios)} pairs ({len(low_ratios)/len(ratios)*100:.1f}%)")

if __name__ == "__main__":
    main()
