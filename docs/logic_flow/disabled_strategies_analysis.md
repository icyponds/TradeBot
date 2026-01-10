# Disabled Strategies Analysis
**Date**: 2026-01-10

This document summarizes the analysis and backtesting results that led to the disabling of specific strategies to optimize portfolio performance.

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

## 3. Volatility Breakout (15m Timeframe)
**Logic**: Enters trades when price expands outside Bollinger Bands (squeeze breakout).

### Performance Issues
*   **Timeframe Noise**: The 15m timeframe is too noisy. Breakouts on 15m often fail or reverse within the hour ("Fakeouts").
*   **Chop Losses**: In the 7-day backtest, `vol_breakout_15m` was the worst performer (**-$3,135**), consistently getting stopped out of false breaks.
*   **Comparative Analysis**: The **1h version** (`vol_breakout_1h`) was profitable (**+$1,482**). The higher timeframe filters out the noise and captures legitimate trend initiations.

**Status**: **DISABLED (15m Only)**.
**Future Activation Condition**: None. 1h is superior.

---

## 4. Liquidation Hunter (5m Timeframe)
**Logic**: Contra-trend mean reversion. Buys "waterfall" crashes (3+ sigma deviation) anticipating a snap-back.

### Performance Issues
*   **"Falling Knives"**: On the 5m timeframe, a 3-sigma crash often continues into a 5-10 sigma crash within minutes. The strategy bought early and was stopped out immediately at the bottom of the candle.
*   **Performance**: Even with a **Wick Validation Fix** (requiring 15% bounce), the 5m strategy lost **-$2,149**. The slippage on rapid 5m candles eats any edge.
*   **Comparative Analysis**: The strategy works better on higher timeframes (1h) or needs much deeper deviation thresholds for 5m scalping.

**Status**: **DISABLED (5m Only)**.
**Future Activation Condition**: Significant tuning of thresholds (e.g., 5-sigma) or execution speed improvements (HFT).

---

## 5. Retained Strategies (The "Winner" Portfolio)
The following strategies demonstrated robust profitability and carry the portfolio:
1.  **Stat Arb** (`stat_arb_1h`): **The Heavy Lifter**. (PnL: +$30k+). Dominant mean-reversion strategy.
2.  **Volatility Breakout** (`vol_breakout_1h`): Captures legitimate trend expansions. (PnL: +$1,482).
3.  **Liquidation Hunter** (`liquidation_hunter_1h`): Occasional high-quality reversal trades.
