# Trading Bot for Hyperliquid

An automated multi-strategy trading bot for Hyperliquid perpetual futures exchange with real-time monitoring dashboard, adaptive risk management, and strategy-specific position controls.

## Features

- **Multi-Strategy Architecture**: Four distinct quantitative strategies running concurrently
- **Real-Time Dashboard**: Web-based monitoring at `localhost:5050` with positions, PnL, trade history
- **Dynamic Pair Selection**: Automatically trades all assets meeting liquidity/volume/open interest thresholds
- **Strategy-Specific Risk Management**: Custom TP/SL logic and trailing stops per strategy
- **Capital Rotation**: Automatically closes weaker positions for stronger signals
- **Graceful Shutdown**: Ensures positions are closed on Ctrl+C, kill signals, or crashes
- **SQLite Performance Tracking**: Persistent trade history and per-strategy analytics

## Trading Strategies

| Strategy | Description | Exit Logic |
|----------|-------------|------------|
| `ou_mean_reversion` | Ornstein-Uhlenbeck mean reversion on Z-score deviations | Z-score reverts or overshoots mean |
| `momentum_factor` | Cross-sectional momentum ranking top/bottom performers | Asset falls out of top N rankings |
| `stat_arb` | Statistical arbitrage on correlated pairs | Spread Z-score normalizes |
| `funding_rate_arbitrage` | Delta-neutral funding rate capture | Funding rate threshold breach |

Each strategy has:
- Custom stop loss calculation (volatility-adjusted, capped 3-12%)
- Trailing stop loss (activates after profit threshold)
- Take profit targets based on strategy mechanics

## Dashboard

Access the real-time dashboard at **http://localhost:5050** when the bot is running.

### Dashboard Features
- **Account Summary**: Equity, available capital, total PnL (realized + unrealized)
- **Open Positions**: Symbol, side, size, entry/current price, notional, margin, leverage, liquidation price, PnL
- **Strategy Performance**: Per-strategy trade count, win rate, realized PnL
- **Trade History**: Complete log of closed trades with exit reasons
- **Visual Alerts**: Color-coded liquidation warnings (red <10%, yellow <20%)

## Project Structure

```text
TradeBot/
├── README.md
├── requirements.txt
├── .env                    # API credentials and optional overrides
├── env.example
├── docs/
│   └── setup.md            # Extended setup & risk notes
├── logs/
│   └── trading_bot.log     # Runtime logs
├── data/
│   └── trades.db           # SQLite trade history database
├── positions.json          # Open position snapshot for recovery
├── scripts/
│   └── close_all_positions.py  # Emergency position closure script
└── src/
    ├── main.py             # Entry point
    ├── config/
    │   └── settings.py     # Configuration loader
    ├── api/
    │   └── hyperliquid_api.py  # Exchange API client
    ├── strategies/
    │   ├── strategy_manager.py         # Orchestrates all strategies
    │   ├── base_strategy.py            # Abstract base class
    │   ├── ou_mean_reversion_strategy.py
    │   ├── momentum_factor_strategy.py
    │   ├── statistical_arbitrage_strategy.py
    │   └── funding_rate_arbitrage_strategy.py
    ├── dashboard/
    │   ├── app.py          # Flask dashboard server
    │   └── templates/
    │       └── dashboard.html
    ├── utils/
    │   ├── pair_selector.py        # Dynamic pair selection
    │   ├── portfolio_manager.py    # Capital and equity management
    │   ├── performance_tracker.py  # Analytics and metrics
    │   └── trade_database.py       # SQLite storage
    └── models/
        └── trade.py        # Position and Trade dataclasses
```

## Requirements

- Python 3.8+
- Hyperliquid perpetuals account with API access
- Private key and wallet address
- USDC balance for margin

```bash
pip install -r requirements.txt
```

## Quick Start

1. **Clone & Install**
   ```bash
   git clone <repo-url>
   cd TradeBot
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure Environment**
   ```bash
   cp env.example .env
   # Edit .env with your credentials
   ```

   **Required variables:**
   ```bash
   HYPERLIQUID_PRIVATE_KEY=your_private_key
   HYPERLIQUID_WALLET_ADDRESS=your_wallet_address
   HYPERLIQUID_PUBLIC_ACCOUNT_ADDRESS=your_public_address
   ```

3. **Run the Bot**
   ```bash
   python -m src.main
   ```

4. **Access Dashboard**
   Open http://localhost:5050 in your browser

5. **Monitor Logs**
   ```bash
   tail -f logs/trading_bot.log
   ```

## Risk Management

### Position Sizing
| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_POSITION_SIZE_PERCENTAGE` | 20% | Max size per position (% of equity) |
| `MAX_POSITIONS_PERCENTAGE` | 80% | Max total capital deployed |
| `MAX_ACCOUNT_LOSS_PER_TRADE` | 3% | Max account loss from any single trade |

### Leverage
| Parameter | Default | Description |
|-----------|---------|-------------|
| `LEVERAGE_BASE` | 3x | Default leverage |
| `LEVERAGE_MIN` | 1.5x | Minimum leverage |
| `LEVERAGE_MAX` | 5x | Maximum leverage |

### Stop Loss Hierarchy
1. **Strategy-Specific SL**: Each strategy calculates its own stop loss (3-12% range)
2. **Trailing Stop**: Activates after profit threshold, locks in gains
3. **Global Fallback**: 3% max account loss per trade (safety net)

### Capital Rotation
When portfolio limits are reached and a stronger signal appears:
- Bot evaluates profitability scores of all positions
- Closes least profitable position if new signal is 20%+ stronger
- Enables capturing better opportunities without over-allocating

## Configuration

Most settings have sensible defaults in `src/config/settings.py`. Only override in `.env` if needed:

```bash
# === REQUIRED ===
HYPERLIQUID_PRIVATE_KEY=
HYPERLIQUID_WALLET_ADDRESS=
HYPERLIQUID_PUBLIC_ACCOUNT_ADDRESS=

# === OPTIONAL OVERRIDES ===
# Strategies (comma-separated)
ENABLED_STRATEGIES=ou_mean_reversion,momentum_factor,stat_arb,funding_rate_arbitrage

# Pair Selection
MIN_OPEN_INTEREST=500000
MIN_VOLUME=1000000
EXCLUDED_ASSETS=PURR,HFUN

# Position Sizing (override defaults)
# MAX_POSITION_SIZE_PERCENTAGE=20.0
# MAX_POSITIONS_PERCENTAGE=80.0

# Leverage (override defaults)
# LEVERAGE_BASE=3.0
# LEVERAGE_MIN=1.5
# LEVERAGE_MAX=5.0

# Dashboard
DASHBOARD_PORT=5050

# Logging
LOG_LEVEL=INFO
```

## Emergency Position Closure

If the bot crashes hard (kill -9) and positions remain open:

```bash
python scripts/close_all_positions.py
```

This standalone script loads your credentials and closes all open positions.

## Shutdown Behavior

The bot handles shutdown gracefully in these scenarios:

| Signal | Behavior |
|--------|----------|
| `Ctrl+C` | Closes all positions, cancels orders, exits cleanly |
| `kill <pid>` | Same as Ctrl+C |
| Terminal closed | SIGHUP handler triggers graceful shutdown |
| Python crash | atexit handler attempts position closure |
| `kill -9` | Use `close_all_positions.py` script |

## Monitoring & Logs

- **Dashboard**: Real-time web UI at localhost:5050
- **Logs**: `logs/trading_bot.log` with execution traces, signals, and alerts
- **Position Snapshot**: `positions.json` for restart recovery
- **Trade Database**: `data/trades.db` SQLite with full history

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No pairs selected | Lower `MIN_OPEN_INTEREST`, check `EXCLUDED_ASSETS` |
| API rate limits | Bot has built-in circuit breaker, will auto-recover |
| Dashboard not loading | Ensure bot is running, check port 5050 is free |
| Positions not closing | Check logs for errors, use emergency script |
| Wrong leverage | Verify `LEVERAGE_*` settings in .env |

## Disclaimer

This bot executes leveraged trades on Hyperliquid perpetual futures. Trading involves significant risk of loss. Features include:

- Automated position entry/exit
- Leveraged exposure (up to 5x default)
- 24/7 operation without supervision

**Never deploy capital you cannot afford to lose. Test thoroughly with small amounts first. Monitor continuously during initial deployment.**
