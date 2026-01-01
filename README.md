# Trading Bot for Hyperliquid

An automated trading bot that connects to Hyperliquid perpetual futures exchange and executes automated trading strategies.

## Features

- Real-time market data fetching from Hyperliquid API
- WebSocket connection for live price updates
- Configurable trading strategies for perpetual futures
- Risk management and position sizing
- Performance tracking and analytics
- **Accurate position and order monitoring**
- **Real-time position synchronization with exchange**
- **Stale order cleanup and validation**
- Modular architecture for easy strategy development

## Position & Order Monitoring

The bot includes comprehensive monitoring to ensure accurate tracking of positions and orders:

### Position Monitoring
- **Real-time synchronization** with exchange positions
- **Automatic detection** of closed positions
- **Size and price discrepancy** validation
- **Position integrity checks** to prevent data corruption
- **Continuous monitoring** with automatic position closure
- **Stop-loss and take-profit enforcement**
- **Position timeout management**
- **Emergency stop capabilities**

### Order Monitoring
- **Open order tracking** and status monitoring
- **Stale order detection** and automatic cleanup
- **Order timeout management** (configurable)
- **Order validation** to prevent invalid orders

### Monitoring Components
- `src/strategies/strategy_manager.py` - Runs the live trading loop, cleans up stale orders, and enforces position/risk rules every cycle
- `src/utils/portfolio_manager.py` - Keeps the real account equity and available capital in sync with Hyperliquid
- `positions.json` - Persisted snapshot of open positions so a kill switch or restart can immediately reconcile state
- `logs/trading_bot.log` - Structured log stream with validation results, emergency-stop alerts, and execution traces

### Integrated Position Monitoring

Position monitoring is now **automatically integrated** into the main trading bot and runs continuously:

```bash
# Start the trading bot (position monitoring runs automatically)
python src/main.py
```

#### Automatic Position Monitoring Features:
- **Real-time position tracking** every 10 seconds (configurable)
- **Automatic position closure** based on:
  - Stop-loss and take-profit levels
  - Maximum loss percentage (default: 5%)
  - Maximum profit percentage (default: 20%)
  - Position timeout (default: 24 hours)
- **Emergency stop** when portfolio loss exceeds threshold (default: 10%)
- **Position synchronization** with exchange data
- **Comprehensive logging** and status display

The position monitoring runs as part of the main trading loop, ensuring positions are constantly monitored and closed when needed without requiring separate services or manual intervention.

### Configuration
```bash
# Order monitoring settings (optimized for scalping)
ORDER_TIMEOUT_MINUTES=0.5          # 30 seconds before order is considered stale
ENABLE_STALE_ORDER_CLEANUP=true    # Enable automatic stale order cleanup
POSITION_SYNC_INTERVAL=10          # 10 seconds between position syncs
ENABLE_POSITION_VALIDATION=true    # Enable position integrity checks

# Integrated position monitoring settings
POSITION_MONITORING_INTERVAL=10    # Check positions every 10 seconds
POSITION_TIMEOUT_HOURS=24          # Close positions after 24 hours
MAX_LOSS_PERCENTAGE=5.0            # Close if loss > 5%
MAX_PROFIT_PERCENTAGE=20.0         # Close if profit > 20%
EMERGENCY_LOSS_THRESHOLD=10.0      # Emergency stop at 10% loss
```

## Project Structure

```text
TradeBot/
├── README.md
├── requirements.txt
├── env.example
├── docs/
│   └── setup.md                # Extended setup & risk notes
├── logs/
│   └── trading_bot.log         # Default runtime log file
├── positions.json              # Auto-generated open-position snapshot
└── src/
    ├── main.py                 # Entry point that wires config + strategies
    ├── config/
    │   └── settings.py         # Loads .env and builds the config dictionary
    ├── api/                    # Hyperliquid REST / WebSocket / hybrid clients
    ├── strategies/             # Strategy implementations + manager
    ├── utils/                  # Portfolio, leverage, correlation, logging helpers
    └── models/                 # Trade & position dataclasses
```

## Requirements

- Python 3.8+ (aligned with `docs/setup.md`)
- Hyperliquid perpetuals account with API access, private key, and wallet address
- USDC balance on Hyperliquid for margin requirements
- macOS/Linux recommended for long-running processes (Windows via WSL is fine)

Install project dependencies with:

```bash
python -m pip install -r requirements.txt
```

## Quick Start

1. **Clone & install**
   ```bash
   git clone <repo-url>
   cd TradeBot
   python -m venv .venv && source .venv/bin/activate  # optional but recommended
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```
2. **Configure environment**
   ```bash
   cp env.example .env
   # edit .env with your Hyperliquid credentials and preferred risk limits
   ```
3. **Run the bot**
   ```bash
   python src/main.py
   ```
   The bot validates connectivity, starts the WebSocket feed, syncs open positions, and then executes strategies on the configured interval.
4. **Monitor in real-time**
   ```bash
   tail -f logs/trading_bot.log
   ```
   Watch for execution summaries, position monitoring output, and any emergency-stop alerts.

> For a longer, opinionated walkthrough (including leverage assumptions and capital-at-risk math) see `docs/setup.md`.

## Configuration Overview

All runtime behavior is controlled through `.env` and loaded by `src/config/settings.py`. The defaults in `env.example` mirror the hard-coded fallbacks in `settings.py`, so you only need to override what differs from your preferred risk posture.

### API & Authentication
- `HYPERLIQUID_API_URL`, `HYPERLIQUID_WS_URL`, `HYPERLIQUID_WS_ALTERNATIVE_URLS`
- `HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_WALLET_ADDRESS`, `HYPERLIQUID_PUBLIC_ACCOUNT_ADDRESS`
- `API_TIMEOUT` for REST calls

### Trading & Risk
- `DYNAMIC_PAIR_SELECTION`, `MIN_OPEN_INTEREST`, `SCAN_INTERVAL_MINUTES`, `EXCLUDED_ASSETS`, `INCLUDED_ASSETS`
- Portfolio sizing: `USE_PORTFOLIO_BASED_SIZING`, `MAX_POSITION_SIZE_USD`, `MAX_POSITION_SIZE_PERCENTAGE`, `MAX_POSITIONS_PERCENTAGE`, `RISK_PERCENTAGE`, `STOP_LOSS_PERCENTAGE`
- Kill-switch & monitoring: `POSITION_MONITORING_INTERVAL`, `POSITION_TIMEOUT_HOURS`, `MAX_LOSS_PERCENTAGE`, `MAX_PROFIT_PERCENTAGE`, `EMERGENCY_LOSS_THRESHOLD`
- Order health: `ORDER_TIMEOUT_MINUTES`, `ENABLE_STALE_ORDER_CLEANUP`, `POSITION_SYNC_INTERVAL`, `ENABLE_POSITION_VALIDATION`

### Strategy Controls
- `ENABLED_STRATEGIES` (comma-separated)
- Timeframes are auto-selected per strategy (e.g., stat_arb=15m, funding_rate=1h)
- Per-strategy knobs such as `MA_SHORT_PERIOD`, `RSI_PERIOD`, `BB_STD_DEV`, `SUPERTREND_MULTIPLIER`, `VWAP_STD_DEV_MULT`, `STAT_ARB_Z_SCORE_THRESHOLD`

### Leverage & Execution
- `LEVERAGE_BASE`, `LEVERAGE_MIN`, `LEVERAGE_MAX`, and strategy-specific leverage multipliers
- Smart order execution with automatic price walking (no configuration needed)
- Logging lifecycle: `LOG_LEVEL`, `LOG_FILE`, `PURGE_LOGS_ON_STARTUP`, `CLEAR_CURRENT_LOG_ON_STARTUP`, `MAX_LOG_FILES`, `MAX_LOG_FILE_SIZE_MB`

## Built-in Strategies

- `MovingAverageStrategy` – short/long MA crossover with trend strength + volatility caps
- `RSIStrategy` – configurable RSI thresholds with volatility-adjusted position sizing
- `BollingerBandSqueezeStrategy` – detects low-volatility squeezes ahead of breakouts
- `SupertrendStrategy` – ATR-based trailing trend follower
- `VWAPStrategy` – VWAP-centered mean reversion with RSI confirmation
- `StatisticalArbitrageStrategy` – correlation-aware spread trades powered by `CorrelationManager`

Enable or disable individual strategies via `ENABLED_STRATEGIES` and tailor their parameters in `.env`. The `StrategyManager` will still apply centralized risk limits and kill-switch logic regardless of the strategy mix.

## Logging, Monitoring & Kill Switch

- **Logs**: `logs/trading_bot.log` captures startup checks, pair selection output, signal decisions, stale-order cleanups, and emergency-stop triggers. Rotate behavior is governed by the logging block in `settings.py`.
- **Position snapshot**: `positions.json` is continuously updated so that a restart or kill switch immediately reconciles with on-exchange positions.
- **Emergency stop**: Portfolio-level drawdowns beyond `EMERGENCY_LOSS_THRESHOLD` automatically close all positions via `StrategyManager.close_all_positions`.
- **Graceful shutdown**: `Ctrl+C` or `kill` signals trigger `StrategyManager.stop(close_positions=True)` so orders are cancelled and positions are synced before exit.

## Testing & Developer Tooling

- Unit/strategy tests (if present) can be executed with:
  ```bash
  pytest
  ```
- Format and lint before committing:
  ```bash
  black src
  flake8 src
  ```
- Use `docs/setup.md` for deeper guidance on leverage assumptions, troubleshooting WebSocket connectivity, and operational checklists.

## Troubleshooting

- **API/WebSocket failures**: verify credentials in `.env`, ensure network connectivity, and confirm the configured URLs respond with `curl`.
- **No pairs selected**: relax `MIN_OPEN_INTEREST`, whitelist symbols via `INCLUDED_ASSETS`, or shorten `SCAN_INTERVAL_MINUTES`.
- **Orders stuck pending**: lower `ORDER_TIMEOUT_MINUTES` so stale orders are cancelled more aggressively, and watch the logs for the cleanup summary.
- **Unexpected allocation**: tail the log for “Portfolio Allocation” lines and adjust `MAX_POSITION_SIZE_PERCENTAGE` or `MAX_POSITIONS_PERCENTAGE`.

For a longer FAQ—including scalping presets, leverage math, and security guidance—see `docs/setup.md`.

## Disclaimer

This bot is built for high-frequency, leveraged trading on Hyperliquid. Running it with real capital implies significant risk of loss, rapid liquidation, and API-related failure modes. Test with paper-sized capital, monitor continuously, and never deploy funds you cannot afford to lose.