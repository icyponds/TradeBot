# Conflict Resolution Findings

## 1. Opposing Direction: "Weighted Priority"
Instead of blind betting or blocking, we implement a **Strength Delta Check**.

*   **Logic**:
    *   Current Position has a `entry_signal_strength` (0.0 - 1.0).
    *   New Signal has `new_signal_strength`.
    *   **Rule**: Flip position ONLY IF `new_signal_strength > (current_strength * 1.1)`.
*   **Why**: Prevents "churning" (flipping back and forth) on weak signals while allowing strong reversals to take precedence.

## 2. Cross-Timeframe: "Ensemble Voting"
Instead of tracking "virtual positions" (complex, capital inefficient), we treat multiple strategies on the same symbol as a **Vote**.

*   **Logic**:
    *   Strategy A (4h) says Long (Strength 0.6).
    *   Strategy B (1h) says Short (Strength 0.8).
    *   **Result**: Short wins (0.8 > 0.6). The 4h Long is closed, 1h Short is opened.
*   **Why**: Capital efficiency. You cannot be net Long and net Short on the same instrument in the same subaccount. The system should align with the strongest timeline.

## 3. Multi-Leg Safety: "Nuclear Switch" Option ☢️
*   **Logic**:
    *   **Arb Open -> Single-Leg Signal**: If Single-Leg Signal weighted strength > (Arb Strength * 2.0):
        *   **Action**: Fully CLOSE the entire Arb position (both legs).
        *   **Then**: OPEN the new Single-Leg position.
    *   **Reverse (Single Open -> Arb Signal)**: If Arb weighted strength > (Single-Leg Strength * 2.0):
        *   **Action**: Fully CLOSE the Single-Leg position.
        *   **Then**: OPEN the new Multi-Leg Arb position.
*   **Prerequisite: Signal Normalization** (Critical)
    *   Currently, strategies return placeholder strengths (0.5 or 0.8).
    *   We must update `calculate_signal_strength` in all strategies to return a dynamic `0.0 - 1.0` score based on conviction (e.g., Z-score magnitude, RSI divergence).
    *   Without this, the "2.0x" check is meaningless.
*   **Pros**:
    *   **Safety**: Never leaves a naked leg.
    *   **Clarity**: You are either fully in Mode A or Mode B. No messy hybrid states.
    *   **Performance**: Shifts capital to the highest conviction strategy.
*   **Cons**: Higher turnover/fees if calibration is wrong (requires strong 2.0x threshold).

## Alternatives Considered

### Option A: Global Lock
*   **Logic**: If `ETH` is in a hedge, **NO** other strategy can touch it.
*   **Pros**: Guarantees delta neutrality. Zero risk.
*   **Cons**: Misses out on potentially profitable single-leg trends.

### Option C: Virtual Sub-Accounts (Netting)
*   **Logic**: Bot tracks `-1` for StatArb and `+2` for RSI separately; Exchange holds net `+1`.
*   **Pros**: Independent strategies.
*   **Cons**: Extremely complex accounting and PnL attribution.

### Option D: Cooperative Unlocking (Hybrid)
*   **Logic**: Allow same-direction or hedge-increasing trades; block only risk-increasing flips.
*   **Pros**: Balanced.
*   **Cons**: Still blocks profitable reversals. Confusing to debug.
