# Agent Instructions & Guidelines

This document outlines critical development rules and patterns for the TradeBot codebase. All agents modifying this repo MUST adhere to these rules.

## 1. Market API Parity

The system architecture relies on the **Mock API** being a functional mirror of the **Real API** to ensure backtests remain valid.

*   **Real API**: `src/api/hyperliquid_api.py` (Class `HyperliquidAPI`)
*   **Mock API**: `src/backtesting/mock_market_api.py` (Class `MockMarketAPI`)
*   **Contract**: `src/api/interface.py` (Class `MarketInterface`)

### Rule: Synchronized Updates
Whenever you add, modify, delete, or rename a public method in `HyperliquidAPI`:
1.  **Update the Interface**: Add the method signature to `MarketInterface` in `src/api/interface.py`.
2.  **Update the Mock**: Implement the corresponding method in `MockMarketAPI` in `src/backtesting/mock_market_api.py`.
    *   The Mock implementation MUST simulate the behavior (return dummy data, update internal state, or raise appropriate errors), NOT just `pass`.
    *   Do NOT leave abstract methods unimplemented in the Mock.

### Validation
Both `HyperliquidAPI` and `MockMarketAPI` inherit from `MarketInterface`. Python will raise a `TypeError` at instantiation if ANY abstract method is missing implementation.

## 2. Configuration Management
(Configuration section omitted for brevity but preserved in file)

## 3. Position Logic Parity

### Rule: Single-Leg vs Multi-Leg Symmetry
When implementing or modifying logic for position management (e.g., `close_position`, `unwind`, `exit_strategy`, `stop_loss`, `take_profit`, or database persistence):
1.  **Check Both Types**: You MUST explicitly check if the logic needs to apply to both `SingleLegPosition` and `MultiLegPosition`.
2.  **Apply to Both**: If a logic change (e.g., "Close all positions on shutdown") applies to one, it almost certainly applies to the other. Ensure you implement the handling for both dictionaries (`self.positions` and `self.multi_leg_positions`).
3.  **Verify**: Verify that your changes affect both position types correctly.

**Example Failure Mode**:
*   Implementing a "Close All" feature that iterates `self.positions` but forgets `self.multi_leg_positions`.
*   Implementing a Ghost Position check that syncs single-leg positions but ignores multi-leg ones.

### Implementation Pattern
When iterating positions, always consider:
```python
# 1. Single-Leg
for symbol, pos in self.positions.items():
    check_logic(pos)

# 2. Multi-Leg
for pos_id, pos in self.multi_leg_positions.items():
    check_logic(pos)
```
