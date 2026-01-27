# Position Sizing & Risk Management Logic

## Overview
This document outlines the bot's logic for determining Position Size and Leverage. The system uses a **Risk-Based Model** combined with **Volatility Targeting** to ensure consistent risk exposure across diverse market conditions.

---

## 1. Risk-Based Position Sizing
Unlike simple "Allocation Models" (e.g., "Always put $1000 per trade"), this bot calculates size based on **Acceptable Loss**.

### Formula
$$
\text{Position Value (Notional)} = \frac{\text{Risk Budget (\$)}}{\text{Stop Loss \%}}
$$

### Logic Flow
1.  **Determine Risk Budget**:
    *   **Risk Per Trade**: Set to **3.0%** of Portfolio Equity.
    *   *Example*: On a $10,000 account, Risk Budget = **$300**.
2.  **Determine Stop Loss**:
    *   Strategy provides a specific Stop Loss (e.g., 5%).
    *   *Fallback*: If no SL is provided, one is implied from leverage (approx `5% / Leverage`).
3.  **Calculate Required Size**:
    *   To lose exactly $300 on a 5% drop, we need a position of:
        $$ \$300 / 0.05 = \$6,000 $$

---

## 2. Dynamic Leverage (Volatility Targeting)
Leverage is not fixed (e.g., "Always 5x"). It adapts to the asset's risk.

### Formula
$$
\text{Leverage} = \frac{\text{Target Annual Volatility (40\%)}}{\text{Asset Annual Volatility}}
$$

### Logic Flow
1.  **Calculate Asset Volatility**:
    *   Bot calculates annualized volatility (e.g., Bitcoin = 40%, Memecoin = 150%).
2.  **Derive Leverage**:
    *   **Bitcoin (40% Vol)**: $0.40 / 0.40 = \mathbf{1.0x}$
    *   **Stable Altcoin (20% Vol)**: $0.40 / 0.20 = \mathbf{2.0x}$
    *   **Memecoin (160% Vol)**: $0.40 / 1.60 = \mathbf{0.25x}$ (De-leveraging)
3.  **Safety Clamps**:
    *   Leverage is capped at asset-specific limits.
    *   Minimum volatility assumption (10%) prevents infinite leverage on stablecoins.

---

## 3. The "Balanced Approach" Configuration
We utilize a **Balanced Configuration** to enable high-conviction trades while preventing catastrophic concentration.

### Context: The Conflict
*   **Goal**: Risk 3% per trade ($300 on $10k).
*   **Constraint**: With a standard 5% Stop Loss, this requires a **$6,000 position** (60% of account).
*   **Problem**: A 10% Max Position Size cap would force this trade down to $1,000, reducing actual risk to only 0.5% ($50).

### Solution: 40% Cap
We set `MAX_POSITION_SIZE_PERCENTAGE` to **40%**.

| Scenario | Equity | Risk Target | Stop Loss | Required Size | 10% Cap Limit | 40% Cap Limit |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Standard Trade** | $10,000 | 3% ($300) | 5% | **$6,000** | $1,000 (Risk $50) ❌ | **$4,000 (Risk $200) ✅** |
| **Tight Stop** | $10,000 | 3% ($300) | 2% | **$15,000** | $1,000 (Risk $20) ❌ | **$4,000 (Risk $80) ✅** |
| **Volatile Trade** | $10,000 | 3% ($300) | 15% | **$2,000** | $1,000 (Risk $150) ⚠️ | **$2,000 (Risk $300) ✅** |

### Why 40%?
*   **Capital Efficiency**: Allows sufficiently large positions to realize ~2% - 3% risk on most standard setups.
*   **Safety**: Prevents a single trade from consuming >40% of the portfolio (Concentration Risk), leaving room for parallel strategies.

---

## 4. Strategy Allocation & Capacity Constraints
The bot allocates capital to strategies based on **Dynamic Weighting** (performance-based) but constrains them with **Strategy-Specific Caps**.

### Design Decision: Granular vs. Global Control
We intentionally use granular, strategy-specific caps rather than a single global cap.

```python
"strategy_weight_caps": {
    "vol_breakout_1h": {"min": 0.05, "max": 0.20},  # Fragile strategy, strictly capped
    "stat_arb_1h":     {"min": 0.05, "max": 0.40},  # Proven strategy, moderate cap
    "csm_4h":          {"min": 0.10, "max": 1.00},  # Robust strategy, high capacity
}
```

### Rationale
Not all strategies are created equal. We differentiate based on:

1.  **Capacity Variance**: 
    *   **High Capacity**: Strategies like `Cross-Sectional Momentum` on major pairs can handle 100% of equity without slippage.
    *   **Low Capacity**: Strategies like `Liquidation Hunter` on altcoins face severe slippage if sized too large. A global cap would starve the robust ones or oversize the fragile ones.

2.  **Risk Profile (Luck vs. Skill)**:
    *   Short-term strategies can have "lucky streaks" in specific regimes.
    *   Strategy-specific caps act as a "sandbox" to prevent a fragile strategy from dominating the portfolio during a temporary lucky streak, protecting against the inevitable drawdown.

3.  **Fee Churn**:
    *   High-turnover strategies have thinner edges and higher fee drag. We explicitly cap them lower than low-turnover strategies.
