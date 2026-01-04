# TradeBot Profitability Improvement Research + Recommendations (2026-01-04)

## Executive summary
With percentage-based sizing (no USD caps), a 90-day backtest run produced **-$8,460.38 PnL on 192 trades** with profit factor **~0.887**.

So the main levers are:
1) **Fix/validate backtest realism + accounting** (so we can trust results),
2) **Stop running losing strategy variants** (the backtest already shows large negative contributors),
3) **Improve strategy edge net of costs** (reduce churn, add cost-aware hurdles, improve pair selection/hedging),
4) **Tune portfolio sizing intelligently** (risk/vol targeting, correlation-aware allocation),
5) Add **new strategies that diversify return streams**, not just more mean reversion variants.

---

## What the current backtest is telling us (high signal)
From the percentage-sized backtest run:
- Total trades: **192**
- Total PnL: **-$8,460.38**
- Profit factor: **~0.887**

Interpretation (high-level):
- You already have at least **one losing “fast stat-arb” variant** that likely needs higher hurdles / different pair selection / different timeframe.
- The overall strategy mix does **not** have a robust positive edge at realistic sizing yet.
- The trade count can drop sharply when sizing increases because portfolio/position limits bind sooner and more entries are skipped; this is expected behavior but must be managed intentionally.

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
Position sizing is now **percentage-based**:
- `MAX_POSITION_SIZE_PERCENTAGE` caps **margin-at-risk per position** as a % of equity.
- `MAX_POSITIONS_PERCENTAGE` caps **total margin-at-risk** across all positions as a % of equity.

This is the correct direction (no brittle USD caps), but it also means strategy weaknesses show up immediately at scale.

## Key recommendation: make “average position size” smaller than the cap
Right now, a common failure mode is sizing that often hits the **max cap** per trade. This reduces the tradeable universe and can amplify drawdowns.

The cap should be a **safety ceiling**, not the default trade size. The default trade size should be a separate “budget”, and only rare high-confidence trades should approach the ceiling.

### Implement “budget vs ceiling” sizing
Use three layers:
1) **Global ceilings (safety)**:
   - `MAX_POSITION_SIZE_PERCENTAGE` (per-position ceiling)
   - `MAX_POSITIONS_PERCENTAGE` (portfolio ceiling)
2) **Default budget (average trade size)**:
   - Add a *base risk per trade* percentage (e.g., `BASE_RISK_PER_TRADE_PCT`), which defines the typical margin-at-risk per trade.
3) **Confidence/edge scaling (strategy-driven)**:
   - Strategies should output a calibrated confidence / signal strength.
   - Convert it into a multiplier so most trades are small, and only high-confidence trades get larger (still under the ceiling).

### Suggested sizing formula (conceptual)
Let equity be \(E\). Compute:
\[
\text{margin} = \min\Big(E \cdot \text{MAX\_POS\_PCT},\ E \cdot \text{BASE\_RISK\_PCT} \cdot f(\text{confidence}) \cdot g(\text{vol}) \cdot h(\text{regime}) \cdot k(\text{corr})\Big)
\]
Then enforce per-position ceiling:
\[
\text{margin} \le E \cdot \text{MAX\_POSITION\_SIZE\_PERCENTAGE}
\]

Practical defaults:
- `BASE_RISK_PER_TRADE_PCT`: **0.25%–1.0%**
- `MAX_RISK_MULTIPLIER`: **2×–5×**
- Confidence mapping: convex (e.g., \(f(c)=0.25 + (c^2)\cdot(\text{MAX\_MULT}-0.25)\)) so most trades stay small.
- Vol targeting: reduce size in high volatility (to stabilize risk contribution).
- Regime/correlation overlays: reduce size when the portfolio is already concentrated or when regime uncertainty is high.

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
- ✅ **Disable `stat_arb_15m` by default** (can be re-enabled for research via config/env).
- ✅ Add a basic **entry hurdle** for MR/stat-arb entries (require an additional Z-score buffer over the strategy’s entry threshold to reduce churn near the boundary where costs dominate).
- ✅ Implement **“budget vs ceiling” position sizing** (base risk per trade + confidence scaling; caps remain as safety ceilings).
- ✅ Fix **backtest equity baseline initialization** so drawdown metrics are based on a sensible starting equity (BacktestEngine now sets initial equity without requiring `StrategyManager.start()`).

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


