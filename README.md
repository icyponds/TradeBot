# Trading Bot for Hyperliquid

An automated multi-strategy trading bot for Hyperliquid perpetual futures exchange with real-time monitoring dashboard, adaptive risk management, and strategy-specific position controls.

## Features

- **Multi-Strategy Architecture**: Dynamic orchestration of multiple concurrent strategies.
- **Real-Time Dashboard**: Web-based monitoring at `localhost:5050` with positions, PnL, trade history.
- **Dynamic Asset Discovery**: Automatically discovers and trades new assets (including HIP-3) every 60 minutes.
- **Hybrid Environment Support**: 
    - **Native Perps**: Standard Hyperliquid markets.
    - **HIP-3 (HyperEVM)**: Seamlessly supports builder-deployed assets (e.g., PURR/USDC).
    - **Spot Trading**: Support for spot assets and balances.
- **Cointegration Filtering**: Uses Engle-Granger tests to ensure StatArb pairs are statistically robust.
- **Regime-Aware Allocation**: 3-state HMM regime classifier adjusts strategy weights (e.g., favors Trend in High Vol, Mean Reversion in Low Vol).
- **Graceful Shutdown**: Ensures positions are managed on exit.
- **SQLite Database**: Persistent trade history and analytics (`data/trades.db`).

## Trading Strategies

| Strategy | Description | Key Mechanism |
|----------|-------------|---------------|
| `structural_arbitrage` | **Statistical Arbitrage** | Cointegration-based mean reversion. Uses Kalman Filter for dynamic hedge ratios and Z-score triggers. Supports Multi-Leg execution. |
| `funding_arbitrage` | **Funding Rate Arbitrage** | Delta-neutral capture of high funding rates (Perp Long/Short + Spot Hedge). |
| `momentum_csm` | **Cross-Sectional Momentum** | Ranks assets by returns (7-day lookback), goes Long winners / Short losers. Uses EMA(200) trend filter. |
| `adaptive_grid` | **Adaptive Grid** | Mean reversion grid centered on EMA. Spacing adapts to ATR. Regulated by ADX filter (pauses in strong trends). |
| `sentiment_ml` | **Sentiment / Volatility** | Hypersensitive volume/price reaction logic. |
| `volatility_breakout` | **Volatility Breakout** | Captures explosive moves from low volatility squeezes (Bollinger Band Squeeze + Hurst Exponent filter). |
| `ou_mean_reversion` | **OU Mean Reversion** | Models price as Ornstein-Uhlenbeck process. Estimates Mean/Theta/Sigma to trade deviations. |

## Dashboard

Access the real-time dashboard at **http://localhost:5050** when the bot is running.
features:
- **Account Summary**: Equity, available capital, total PnL.
- **Open Positions**: Live PnL, leverage, margin.
- **Strategy Performance**: Win rates, trade counts.
- **Visual Alerts**: Liquidation risk warnings.

## Project Structure

```text
TradeBot/
├── README.md
├── requirements.txt
├── .env                    # API credentials
├── data/
│   ├── trades.db           # Persistent Trade History
│   └── market_data.db      # Cached Historical Data
├── logs/
│   └── trading_bot.log     # Runtime logs
├── scripts/
│   ├── run_backtest.py         # Main Backtesting Engine
│   ├── ingest_historical_data.py # Data Ingestion Utility
│   ├── analyze_backtest_db.py  # Analysis Tool
│   ├── close_all_positions.py  # Emergency Kill Switch
│   └── test_pair_selector.py   # Component Test
└── src/
    ├── main.py             # Entry point
    ├── config/             # Configuration Settings
    ├── strategies/         # Strategy Implementations
    │   ├── strategy_manager.py # The Brain (Orchestrator)
    │   ├── statistical_arbitrage_strategy.py
    │   ├── cross_sectional_momentum_strategy.py
    │   ├── adaptive_grid_strategy.py
    │   └── ...
    ├── utils/              # Math & Logic
    │   ├── statistics.py       # Cointegration / ADF Tests
    │   ├── correlation_manager.py # Pair Correlation Logic
    │   └── regime_hmm.py       # Market Regime Detection
    └── dashboard/          # Flask UI

## Utility Scripts Reference
The `scripts/` directory contains essential tools for maintenance and analysis:

| Script | Description |
|--------|-------------|
| `analyze_backtest_db.py` | **Analyze Backtest Performance**: Connects to `data/trades.db` to generate performance metrics (Win Rate, Sharpes, Drawdowns) from backtest runs. |
| `close_all_positions.py` | **Emergency Kill Switch**: Safely unwinds and closes all open positions. Run this if the bot fails to shut down gracefully. |
| `fill_data_gaps.py` | **Data Maintenance**: Multi-purpose tool to ensure data integrity.<br>Modes:<br>`fill`: Finds and fills missing candle ranges.<br>`repair`: Mismatches check & overwrite against API.<br>`verify`: Read-only integrity check. |
| `ingest_historical_data.py` | **Data Ingestion**: Downloads ~90 days of historical OHLCV and funding data for all active assets to prime the database for backtesting. |
| `run_backtest.py` | **Backtest Engine**: Runs the trading simulation over historical data using the configured strategies. |
| `run_timed_session.py` | **Timed Execution**: Runs the live trading bot for a specified duration (in seconds), useful for controlled live tests. |
```

## Quick Start

### 1. Installation
```bash
git clone <repo-url>
cd TradeBot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuration
Copy `env.example` to `.env` and fill in your Hyperliquid credentials.
```bash
cp env.example .env
```
Required:
 - `HYPERLIQUID_PRIVATE_KEY`: Your Arbitrum private key.
 - `HYPERLIQUID_WALLET_ADDRESS`: Your wallet address.
 
 Optional (HIP-3 / Spot):
 - `HIP3_ENABLED`: Set to `true` to enable HyperEVM assets.
 - `HIP3_PERP_DEXS`: Comma-separated list of DEX names (or leave empty for auto-discovery).
 - `SPOT_ENABLED`: Set to `true` to enable Spot trading.


### 3. Run Backtest (Validation)
Before live trading, verify the logic with a backtest.
```bash
# Ingest data (required for backtesting)
python3 scripts/ingest_historical_data.py --days 90

# Run simulation
python3 scripts/run_backtest.py --days 30
```

### 4. Run Live (Paper/Production)
```bash
python3 -m src.main
```

## Risk Management

- **Position Sizing**: Configurable per strategy (default ~20% max per position).
- **Leverage**: Dynamic (1.5x - 5x).
- **Per-Strategy Weights**: StrategySelector weights are performance-based and now honor per-strategy caps/floors (configurable in `risk_management.strategy_weight_caps`) so high-churn strategies stay small while strong performers can scale.
- **Cooldowns & Pair Controls**: Per-strategy cooldowns, pair blacklist/penalties, and optional cost/edge hurdles (bps) are configurable to throttle high-churn strategies and avoid poor pairs.
- **Stop Losses**:
  - **Hard Stop**: Fixed percentage (e.g., 5-10%).
  - **Trailing Stop**: Activates after profit target to lock gains.
  - **Time-Based Stop**: StatArb positions auto-close after 5 days if flat.

## Troubleshooting

- **Rate Limits**: The bot handles 429s automatically with exponential backoff.
- **Stuck Positions**: Use `python3 scripts/close_all_positions.py` to liquidate all open positions immediately.
- **Database**: If `trades.db` gets corrupted, delete it; the bot will recreate it (stats will reset).

## Disclaimer

**USE AT YOUR OWN RISK.**
This software is for educational purposes. Cryptocurrency trading involves significant risk of loss. The authors are not responsible for financial losses generated by this bot.
