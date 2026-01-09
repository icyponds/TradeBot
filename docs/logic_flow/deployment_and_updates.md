# Deployment & Update Logic

This document summarizes the "Hybrid Docker" deployment strategy chosen for TradeBot, explaining how code updates are delivered and applied without downtime.

## 1. The Core Problem
In algorithmic trading, **Uptime is Critical**.
*   **Standard Docker Deployment** (Immutable Containers) requires restarting the container to apply updates.
*   **Restart Cost**: 
    1.  Websocket disconnection (loss of market data for 10-30s).
    2.  Cache clearing (loss of in-memory OHLCV history).
    3.  Ghost Position risk (orders filling while the bot is offline).

## 2. The Solution: Hybrid Hot-Reload
We strictly prioritize **Uptime** over "Container Immutability".

### Architecture
*   **Runtime**: Docker (ensures stable OS/Python environment).
*   **Filesystem**: **Bind Mount** (`./src:/app/src`). The container sees the *actual files* on the VPS host, not a frozen copy inside the image.

### The Update Pipeline
1.  **Push**: You commit and push code to GitHub `main`.
2.  **Sync (CI/CD)**: GitHub Actions uses `rsync` to mirror the `src/` folder to the VPS host (`~/tradebot/src`).
    *   *Note*: The `data/` folder (database) is explicitly **excluded** to prevent overwriting live data.
3.  **Reflect**: Because of the Bind Mount, the running Docker container sees the file changes immediately.

## 3. Applying Changes (Hot Reload)
The bot detects and applies changes *in-memory* without restarting the process.

*   **Mechanism**: `StrategyManager.reconcile_strategies()`
*   **Trigger**: Runs periodically (every 60s).
*   **Action**:
    1.  Reloads `src.config.settings` to check for parameter updates.
    2.  If strategy logic has changed, it uses `importlib.reload(module)` to swap the Python class definitions in memory.
*   **Result**: The "Brain" is updated, but the "Body" (Websockets, Cache, Connection) stays online.

## 4. The Safety Net: DB Persistence
While Hot Reload allows us to avoid restarts, we **must** survive them (crashes, server reboots).
*   **`live_positions` Table**: Every trade execution is immediately committed to SQLite.
*   **Startup Logic**: On boot, the bot reads `live_positions` and hydrates its memory. It knows what it owns.
*   **Ghost Reconciliation**: It compares DB state vs. Exchange state to catch any discrepancies.

**Summary**: 
*   **Hot Reload**: Defines *how we update* (Zero Downtime).
*   **DB Persistence**: Defines *how we survive* failures (Crash Recovery).
