---
name: hyperliquid-api
description: Guidelines and best practices for working with the Hyperliquid API and the Python SDK within the TradeBot codebase. Use this when modifying API interactions, mocked market data, or hyperliquid-python-sdk functions.
---

# Hyperliquid API & Python SDK Guidelines

This skill defines how to interact with the Hyperliquid API in this repository. 
While the base SDK is `hyperliquid-python-sdk`, this codebase implements specific wrappers and rules for resilience, rate-limiting, and backtesting parity.

## 1. Core Architecture

The system uses an interface pattern to ensure the real and mock APIs remain perfectly mirrored for valid backtesting.

- **Interface Contract**: `src/api/interface.py` (`MarketInterface`)
- **Real API Implementation**: `src/api/hyperliquid_api.py` (`HyperliquidAPI`)
- **Mock API Implementation**: `src/backtesting/mock_market_api.py` (`MockMarketAPI`)

## 2. API Method Rules (CRITICAL)

### Synchronized Updates
Whenever you add, modify, delete, or rename a public method in `HyperliquidAPI`:
1.  **Update the Interface**: Add the method signature to `MarketInterface`.
2.  **Update the Mock**: Implement the exact same method signature in `MockMarketAPI`.
    - **Important**: The Mock implementation MUST simulate the behavior (return dummy data, update internal state, or raise expected errors), NOT just `pass`.
    - Both classes inherit from `MarketInterface`. A `TypeError` will be raised if any abstract method is missing.

### Rate Limiting and Retry Wrappers
All direct calls to `self.info.*` and `self.exchange.*` (the underlying SDK components) in `HyperliquidAPI` **MUST** be wrapped in `self._rate_limited_call()`.

*Example Pattern:*
```python
def fetch_some_data(self):
    def _fetch():
        return self.info.some_sdk_call()
    
    return self._rate_limited_call(_fetch)
```

**Do NOT**: 
- Call the SDK methods directly without the `_rate_limited_call` wrapper.
- Implement custom retry loops inside the API client methods (the wrapper uses an aggressive backoff pattern `[2, 10, 30, 60]`).

## 3. Position Logic Parity (Single-Leg vs Multi-Leg)

When implementing or modifying logic involving positions (e.g., fetching open orders, closing positions):
1. **Always Handle Both Types**: Ensure that your logic iterates through BOTH `self.positions` (single-leg) and `self.multi_leg_positions` (multi-leg) if the scope applies globally to the user's portfolio.
2. **Symmetry**: Features meant for one usually apply to the other unless strictly defined otherwise.

## 4. Documentation References

- [Python SDK Repository](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
- [Official Hyperliquid API Documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api)
