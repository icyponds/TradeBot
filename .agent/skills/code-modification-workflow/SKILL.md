---
name: code-modification-workflow
description: Mandatory 4-step checklist to follow whenever code changes are made in the repository, ensuring logic parity, testing, and version control.
---

# Code Modification Workflow

Whenever you make code changes to this repository, you **must** proactively follow this 4-step workflow.

## 1. Single and Multi-Leg Trade Parity
- **Requirement**: Check that any changes made apply equally to both single-leg (`live_positions`) and multi-leg (`live_position_legs`) trade logic.
- **Action**: Review methods handling positions in files like `src/strategies/strategy_manager.py` or `src/api/hyperliquid_api.py`. Ensure that if you update how a single-leg position is tracked or exited, you also update the corresponding logic for multi-leg strategies (e.g. `_handle_multi_leg_signal()`).

## 2. Mock API Parity
- **Requirement**: The backtesting mock API must perfectly mirror the live exchange API.
- **Action**: If you modified `src/api/hyperliquid_api.py` (Real API), you **must** update `src/api/mock_hyperliquid_api.py` (Mock API) and ensure the method signatures in `src/api/interface.py` match perfectly. The Mock API must simulate the expected behavior.

## 3. Unit Testing
- **Requirement**: All new logic must be tested, and existing tests must pass.
- **Action**: 
  - Check whether new unit tests are needed for the code you just wrote. If yes, write the tests (usually in `tests/`).
  - Run the entire test suite (e.g., `python -m pytest tests/`).
  - If errors arise, **work through them** continuously until the entire test suite passes successfully.

## 4. Version Control (Git)
- **Requirement**: All verified changes must be committed and pushed.
- **Action**: Once steps 1-3 are complete and tests pass, run the following commands:
  ```bash
  git add .
  git commit -m "Brief descriptive message of the changes"
  git push
  ```
