# TradeBot Profitability Improvement Research + Recommendations (2026-01-04)

## Executive summary
Your 90-day backtest produced **~$105 PnL on ~$60K starting equity (~0.175% total return, ~0.71% annualized)**, which looks bad **on total equity**.

However, the bot’s **risk deployment is extremely low**:
- Logs show **capital at risk ~$615** with ~13 positions (≈ **~1%** of $60K) and **$50 max position size** per position in practice.
- If you only deploy ~1% of equity, even a decent “edge” will look tiny on total equity.

So the main levers are:
1) **Fix/validate backtest realism + accounting** (so we can trust results),
2) **Stop running losing strategy variants** (the backtest already shows large negative contributors),
3) **Increase capital utilization intelligently** (vol targeting / risk parity / Kelly-like sizing),
4) Add **new strategies that diversify return streams**, not just more mean reversion variants.

---

## What the current backtest is telling us (high signal)
From the backtest performance report (end of run):
- Total trades: **2657**
- Total PnL: **$105.05**
- Win rate: **~45.1%**
- Profit factor: **~1.01** (barely above break-even)
- Biggest per-strategy drivers (from the printed breakdown):
  - **adaptive_grid_15m**: strongly positive PnL but very high turnover (many trades)
  - **stat_arb_15m**: **large negative PnL** (dominant loser)
  - **stat_arb_1h**: positive PnL
  - **vol_breakout_1h**: positive PnL
  - **momentum_4h**: negative but tiny sample (3 trades)
  - **ou_mean_reversion**: tiny sample

Interpretation:
- You already have at least **one losing “fast stat-arb” variant** that likely needs higher hurdles / different pair selection / different timeframe.
- Your “edge” is currently too small **net of churn** (PF ~ 1.01).
- The bot is likely under-allocated (low $ at risk), making total-return look terrible.

---

## Critical: backtest realism issues to fix BEFORE believing annualized returns
These directly affect whether 0.71% annualized is “real”:
- **Fees & slippage model**: if it’s too optimistic, returns are overstated; if too punitive/incorrect, returns can be understated. Ensure:
  - maker/taker fees (or a reasonable proxy),
  - realistic slippage by liquidity/volatility,
  - no “free fills” at mid.
- **Funding PnL modeling**: funding strategies require explicit funding cashflows per hour/interval, not only price PnL.
- **Equity curve / drawdown**: the printed drawdown % looked nonsensical earlier (a sign initial equity / equity snapshots may not be initialized for backtest).
- **Capital constraints**:
  - Ensure spot/perp transfers, margin constraints, and leverage are modeled consistently,
  - Ensure “capital at risk” matches the true margin used.
- **Survivorship / selection bias**:
  - If pair selection uses “today’s” universe, it can leak information.

Recommendation:
- Treat current backtest as a **directional smoke test**, not an investable performance estimate, until the above are validated.

---

# Improvements to existing strategies (highest ROI first)

## 1) Disable or overhaul `stat_arb_15m` (biggest immediate PnL lift)
Observed: `stat_arb_15m` is a major loser in the backtest.

Why it likely loses:
- Too much turnover on 15m → fees/spread kill the edge.
- Pair selection may be correlation-based, not cointegration-based.
- Hedge ratio drift (static beta) breaks neutrality.

Fix options (in priority order):
- **Raise entry hurdle**: z-score threshold + minimum expected edge after fees (“fee hurdle”).
- **Reduce trading frequency**: move this variant to 1h+ only; keep 15m only if you add stronger filters.
- **Cointegration-based pair selection**:
  - Engle–Granger test to select stable residual relationships.
  - Drop pairs when stationarity breaks.
- **Dynamic hedge ratio**:
  - Kalman filter beta to adapt to drift.
- **Regime gate**:
  - Block MR/stat-arb during trending/high-vol regimes (you already implemented change-point + HMM gating—use it more aggressively here).

Expected outcome:
- Simply removing/pausing this losing variant often improves portfolio returns immediately.

---

## 2) Upgrade `adaptive_grid_15m` (it’s positive but high churn)
Grid systems often look good in chop and get hurt in trends.

Improvements:
- **Regime-aware grid**:
  - In trend regime: widen grid spacing, reduce size, or disable.
  - In low-vol chop: tighten and allocate more.
- **Volatility targeting**:
  - Size grid notional so the strategy contributes a stable risk amount (e.g., target daily vol).
- **Hard fee-aware spacing**:
  - Ensure grid step > (fees + slippage + expected adverse selection buffer).
- **Inventory/risk limits**:
  - Cap inventory accumulation in one direction; enforce max exposure per symbol.

---

## 3) Make `volatility_breakout_1h` more selective and larger when it works
Breakouts can work in crypto, but false breakouts are common.

Improvements:
- **Compression filter**:
  - Only trade breakouts after clear compression (ATR percentile / BB width percentile).
- **Trend confirmation**:
  - Trade breakouts in the direction of higher-timeframe trend (e.g., 4h/1d EMA).
- **Vol targeting**:
  - Increase size when volatility is lower (cheaper risk), reduce when high.
- **Time stop / invalidation**:
  - If breakout doesn’t follow through within N bars, exit.

---

## 4) Fix sampling / effectiveness of `momentum_factor` and `ou_mean_reversion`
Backtest shows tiny samples (few trades), so they’re not meaningfully evaluated.

Actions:
- Confirm they are actually being enabled/trading under realistic conditions.
- Reduce friction (but don’t increase churn) by:
  - loosening overly-strict entry conditions slightly,
  - ensuring universe selection provides candidates,
  - using exploration capital (already implemented) but with a minimum number of trades.

---

# New strategies to add (diversifying return streams)

## A) Cross-sectional momentum long/short (market-neutral factor)
What:
- Rank assets by trailing returns (e.g., 7d/14d/30d), go long top quantile and short bottom quantile, dollar-neutral.

Why:
- Diversifies away from pure mean reversion and single-name trading.

Data needed:
- OHLCV only (already available).

Key design choices:
- Universe filter: liquidity/vol/spread proxies.
- Rebalance: daily/4h.
- Risk control: cap per-name, sector-like clustering to avoid concentration.

---

## B) “Carry” / funding-based factor portfolio (not just threshold arbitrage)
What:
- Treat funding as a carry signal; build a portfolio tilted to receive carry when it is persistent/compensated.

Why:
- Funding regimes can persist; naive thresholding often churns.

Data needed:
- Funding time series (already stored) + a persistence/term filter.

Key design choices:
- Persistence filter: only trade if funding stays extreme for N intervals.
- Risk overlay: avoid carry when market is in crash/trend reversal regimes.

---

## C) Factor-neutral residual mean reversion (PCA/stat-arb as portfolio)
What:
- Build a low-rank factor model on returns across the universe, trade mean reversion in residuals rather than raw pair spreads.

Why:
- More robust than hand-picked pairs; naturally diversified.

Data needed:
- OHLCV across many assets.

---

## D) Intraday seasonality (small, diversifying edge)
What:
- Some hours/days have predictable return distributions (weekend effects, funding times, etc.).

Why:
- Often low correlation with other signals.

Data needed:
- Hourly candles; careful to avoid overfitting.

Implementation:
- Simple: per-hour average return conditional on regime + vol, then small tilt.

---

## E) Volatility-managed trend (time-series momentum)
What:
- Simple time-series momentum per asset (e.g., sign of 20/60-day return), sized by volatility.

Why:
- Crypto trends can be strong; this complements mean reversion.

Data needed:
- OHLCV.

---

# Capital utilization: why return looks “terrible” and how to improve it safely
Given current sizing (~$50 per position), the bot is effectively running at **very low exposure** relative to equity.

Recommendations:
- **Add a portfolio-level target risk**:
  - e.g., “target 10–20% of equity as margin used” (or target daily volatility).
- **Size positions by risk, not by fixed dollars**:
  - per-position risk budget = equity × risk_pct / (ATR × multiplier)
- **Scale winners and shrink losers**:
  - strategy-level risk parity (allocate more risk to strategies with better Sharpe and low correlation).
- **Use leverage intentionally**:
  - if you’re already trading perps, explicitly model leverage and margin usage so the backtest matches reality.

---

# Process changes: how to iterate toward real profitability
This is as important as adding strategies.

## Backtest methodology upgrades
- Walk-forward evaluation (train/tune on earlier window, test on later window).
- Multiple market regimes: at least 2021 bull, 2022 bear, 2023-2024 chop/trend transitions.
- Add transaction cost sensitivity:
  - run with low/medium/high slippage and fee assumptions.
- Report *risk-adjusted metrics*:
  - Sharpe/Sortino, max drawdown, turnover, exposure, average holding time.

## Live trading “guardrails”
- Strategy kill switch: disable a strategy if rolling PF < 1 for N trades or drawdown threshold breached.
- Exposure caps by correlated clusters (avoid being “long beta” across all positions).
- Monitoring: PnL attribution by strategy and by regime.

---

# Prioritized roadmap (what I’d do next)

## Phase 1 (fast wins, 1–2 days)
- Disable/raise hurdle for **stat_arb_15m** (largest negative contributor).
- Add basic **fee hurdle** to all MR/stat-arb entries.
- Fix backtest equity curve initialization so drawdown metrics are trustworthy.

## Phase 2 (core alpha improvements, ~1 week)
- Implement **cointegration + Kalman hedge ratio** for stat-arb (replace correlation pairs).
- Implement **cross-sectional momentum long/short** with volatility targeting.
- Improve `adaptive_grid` with regime gating + spacing > costs.

## Phase 3 (portfolio sophistication, 1–2 weeks)
- Implement **factor-neutral residual MR (PCA)**.
- Add **carry/funding factor portfolio** with persistence filters.
- Add strategy-level **risk parity** and correlation-aware exposure caps.

---

## Notes / assumptions
- This document assumes current data availability: multi-asset OHLCV + funding series + position/account state.
- If you add a true trade-print stream (with size), you can unlock higher quality volume/flow signals and better execution models; that can materially improve results but requires more infra.


