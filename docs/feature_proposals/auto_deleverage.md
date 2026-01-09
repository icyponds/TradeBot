# Feature Proposal: Auto-Deleveraging Logic

**Status**: Proposed
**Date**: 2026-01-08
**Objective**: Implement active risk management to automatically reduce exposure when account equity drops below a critical safety threshold.

## Context
Currently, the bot sizes positions based on capital *at entry*. If capital is withdrawn or significant losses occur, existing positions may become "oversized" relative to the new reduced equity, leading to dangerous leverage levels.

## Proposed Strategy: "Cut the Losers" Priority
We propose a **Margin Safety Monitor** that runs periodically. If `Used Margin` exceeds a `Safety Threshold`, the bot will iteratively close positions—starting with the worst performing ones—until margin usage returns to a safe level.

## Implementation Details

### 1. Configuration (`config.yaml`)
Add new parameters to the `risk_management` section:

```yaml
risk_management:
  auto_deleverage:
    enabled: true
    safety_threshold_pct: 0.95  # Trigger if Used Margin > 95% of Equity
    target_safety_pct: 0.90     # Deleverage until Used Margin <= 90% of Equity
    priority: "pnl_asc"         # "pnl_asc" (Cut Losers) or "size_desc" (Free Capital)
```

### 2. Monitoring Logic (`StrategyManager._update_account_balance`)
Integrate a check into the existing balance refresh loop:

```python
def _update_account_balance(self):
    # ... existing update logic ...
    
    # New Deleverage Check
    if self.config['risk_management']['auto_deleverage']['enabled']:
        self._check_margin_safety()
```

### 3. Execution Logic (`StrategyManager._check_margin_safety`)

```python
def _check_margin_safety(self):
    equity = self.portfolio_manager.total_equity
    used_margin = self.portfolio_manager.used_margin
    
    threshold_pct = self.config['risk_management']['auto_deleverage']['safety_threshold_pct']
    
    # Check basic trigger
    if used_margin > (equity * threshold_pct):
        self.logger.warning(f"⚠️ MARGIN DANGER: Used {used_margin} > {threshold_pct*100}% of {equity}. Initiating Auto-Deleverage.")
        self._execute_deleverage(equity, used_margin)

def _execute_deleverage(self, equity, current_used_margin):
    target_pct = self.config['risk_management']['auto_deleverage']['target_safety_pct']
    target_margin = equity * target_pct
    
    margin_to_free = current_used_margin - target_margin
    
    # Sort positions by PnL % (Ascending = Worst Losers First)
    sorted_positions = sorted(
        self.positions.values(), 
        key=lambda p: p.unrealized_pnl_percentage
    )
    
    freed_margin = 0.0
    
    for pos in sorted_positions:
        if freed_margin >= margin_to_free:
            break
            
        self.logger.warning(f"Deleverage: Closing {pos.symbol} (PnL: {pos.unrealized_pnl_percentage:.2f}%) to free ${pos.margin} margin.")
        
        # Close the position
        self.close_position(pos.symbol, reason="auto_deleverage")
        
        # Track progress
        freed_margin += pos.margin  # Approximate, assuming 1:1 margin release
        
    self.logger.info(f"Deleverage Complete. Freed approx ${freed_margin}.")
```

## Benefits
1.  **Safety**: Prevents liquidation by actively managing margin usage.
2.  **Efficiency**: "Cut Losers" strategy preserves winning trades while pruning dragging positions.
3.  **Automation**: Handles deposit/withdrawal scenarios without manual intervention.
