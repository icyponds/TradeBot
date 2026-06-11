# Agent Instructions & Guidelines

This document outlines critical development rules and patterns for the TradeBot codebase. All agents modifying this repo MUST adhere to these rules.

## 0. Unit Tests for Every Change

**Every code change MUST ship with unit tests in the same commit** — new
features, bug fixes, and behavior changes alike.

*   **Bug fixes**: write a test that reproduces the failure mode being fixed
    (it should fail on the pre-fix code). Reference the incident in the test
    docstring when one exists (e.g. "live failure 2026-06-11: ...").
*   **New features/parameters**: cover the enabled path, the default/disabled
    path, and failure handling.
*   **Run the FULL suite before committing and verify pytest's exit code
    directly.** Never gate a commit on `pytest ... | tail` or any pipeline —
    the pipe masks pytest's exit status and has shipped broken code to main.
*   Exceptions (comment-only edits, log-message wording) should be rare and
    called out explicitly in the commit message.

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

### Best Practices (Logic vs. Secrets)

*   **`.env` File**: 
    *   **Purpose**: Credentials and Infrastructure endpoints ONLY.
    *   **Content**: Private Keys, API URLs, Wallet Addresses.
    *   **Rule**: Never commit `.env`. Use `env.example` as a template. ABSOLUTELY NO strategy logic, risk parameters, or feature flags should go here.
*   **`src/config/settings.py`**: 
    *   **Purpose**: Strategy Logic and Algorithms.
    *   **Content**: Indicators (RSI/EMA), Timeframes, Thresholds (Z-Score), Lookbacks.
    *   **Rule**: These parameters define "How the bot thinks". They should be version-controlled in code. Do NOT expose them in `.env`.

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

## 4. API Retry Logic

### Rule: All API Calls Must Use `_rate_limited_call`

All calls to `self.info.*` and `self.exchange.*` in `HyperliquidAPI` **MUST** be wrapped in `_rate_limited_call` to handle 429 rate limits and transient errors.

### Backoff Pattern
Use the **aggressive backoff** pattern consistently across all retry logic:
```python
backoff_steps = [2, 10, 30, 60]  # seconds
```

This provides ~2 minutes of total retry time for transient issues.

### Implementation Pattern
```python
def some_api_method(self):
    def _fetch():
        return self.info.some_call()
    
    return self._rate_limited_call(_fetch)
```

### Do NOT:
- Call `self.info.*` or `self.exchange.*` directly without retry wrapper
- Use `2 ** attempt` exponential backoff (inconsistent timing)
- Create new retry decorators - use `_rate_limited_call` only

### Validation
When adding new API methods, verify:
1. All `self.info.*` and `self.exchange.*` calls are wrapped in `_rate_limited_call`
2. Any local retry loops use `[2, 10, 30, 60]` backoff pattern
