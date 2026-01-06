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
...
