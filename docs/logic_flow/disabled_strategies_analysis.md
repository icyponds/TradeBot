# Disabled Strategies Analysis
**Date**: 2026-01-10

This document summarizes the analysis and backtesting results that led to the disabling of the `cross_sectional_momentum` (CSM) and `sentiment_ml` strategies.

## 1. Cross-Sectional Momentum (CSM)
**Logic**: "Buy Winners, Sell Losers." Ranks assets by 24h return, goes Long top N% and Short bottom N%. Rebalances periodically.

### Performance Issues
*   **Massive Fee Churn**: In a 7-day backtest, `csm_4h` executed **309 trades**, incurring **$2,139 in fees** on a $50k account.
*   **Market Regime Mismatch**: The strategy performs well in strong trending markets but suffers "whipsaw" losses in choppy/sideways markets. The backtest period (Jan 2026) was characterized by volatility without sustained extensive trends, causing the strategy to buy near tops and sell near bottoms.
*   **Optimization Failure**: We attempted to optimize by:
    *   Tightening selection to Top 5% (from 15%).
    *   Slowing rebalancing to 12h (from 4h).
    *   Adding an ADX filter (>25).
    *   **Result**: Trade count dropped 83%, but PnL remained negative (-$1,210). The strategy consistently selected assets that mean-reverted immediately after entry.

**Status**: **DISABLED**.
**Future Activation Condition**: Explicit regime detection finding a "Strong Trend" environment (ADX > 35 on Daily).

---

## 2. Sentiment ML
**Logic**: Uses Price * Volume as a proxy for social sentiment (Hype). High Score -> Long, Low Score -> Short.

### Performance Issues
*   **"Chasing the Pump"**: The strategy essentially buys high-volume breakouts. In the backtest, it had a **3% Win Rate** (buying the absolute top).
*   **Failed Inversion Experiment**: We hypothesized that if it consistently buys tops, we should *invert* the signal (Short Hype).
    *   **Result**: Catastrophic failure (**-$10,500** PnL).
    *   **Analysis**: Shorting high-volume momentum in a generally bullish/volatile market is extremely dangerous (stepping in front of a freight train). The "Proxy" (Volume*Price) is a lagging indicator of interest, not a predictive indicator of reversal.

**Status**: **DISABLED**.
**Future Activation Condition**: Integration with a *real* external sentiment API (e.g., LunarCrush, Santiment, X/Twitter firehose) rather than using price/volume proxies.

---

## 3. Retained Strategies (The "Winner" Portfolio)
The following strategies demonstrated robust profitability even after fees:
1.  **Volatility Breakout** (`volatility_breakout`): Captures expansion from low-volatility compression. (PnL: +$1,000 range).
2.  **Liquidation Hunter** (`liquidation_hunter`): Exploits cascading liquidations and mean reversion. (PnL: +$500 range).
