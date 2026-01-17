# Trading Bot for Hyperliquid

An automated multi-strategy trading bot for Hyperliquid perpetual futures exchange with real-time monitoring dashboard, adaptive risk management, and self-healing connectivity.

## Key Features

### 🛡️ Robust & Self-Healing
- **Auto-Reconnecting WebSockets**: Intelligent watchdog detects stale connections (>60s silence) and automatically reconnects, preventing signal generation stoppages.
- **Graceful Shutdown**: Ensures positions are managed on exit.
- **Rate Limit Handling**: Exponential backoff for 429 errors.

### 🧠 Intelligent Trading Engine
- **Multi-Strategy Architecture**: Dynamic orchestration of multiple concurrent strategies.
- **Regime-Aware Volatility Gating**: 
    - **VolatilityGate**: Per-asset blocking mechanism that prevents entry during extreme volatility spikes using Z-score and correlation propagation.
    - **VolatilityScaler**: Dynamically adjusts strategy thresholds (e.g., Z-scores) based on market volatility regimes (ATR-based).
- **Cointegration Filtering**: Uses Engle-Granger tests to ensure StatArb pairs are statistically robust.
- **Dynamic Asset Discovery**: Automatically discovers and trades new assets (including HIP-3) every 60 minutes.

### 📊 Real-Time Dashboard
Access via **http://localhost:5050**
- **Live Equity Updates**: Real-time account value tracking (Snapshot + Live PnL Delta).
- **Active Management**: "Close Position" button for immediate manual intervention.
- **Visual Analytics**: Win rates, PnL curves, and liquidation risk warnings.

## Trading Strategies

| Strategy | Description | Key Mechanism |
|----------|-------------|---------------|
| `structural_arbitrage` | **Statistical Arbitrage** | Cointegration-based mean reversion. Uses Kalman Filter for dynamic hedge ratios. Regime-adaptive Z-score thresholds. |
| `funding_arbitrage` | **Funding Rate Arbitrage** | Delta-neutral capture of high funding rates (Perp Long/Short + Spot Hedge). |
| `momentum_csm` | **Cross-Sectional Momentum** | Ranks assets by returns (7-day lookback), goes Long winners / Short losers. Uses EMA(200) trend filter. |
| `adaptive_grid` | **Adaptive Grid** | Mean reversion grid centered on EMA. Spacing adapts to ATR. Regulated by ADX filter (pauses in strong trends). |
| `sentiment_ml` | **Sentiment / Volatility** | Hypersensitive volume/price reaction logic. |
| `volatility_breakout` | **Volatility Breakout** | Captures explosive moves from low volatility squeezes (Bollinger Band Squeeze + Hurst Exponent filter). |
| `ou_mean_reversion` | **OU Mean Reversion** | Models price as Ornstein-Uhlenbeck process. Estimates Mean/Theta/Sigma. Regime-adaptive entry/exit thresholds. |

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
    │   └── ...
    ├── utils/              # Math & Logic
    │   ├── volatility_gate.py  # Volatility Blocking Logic
    │   ├── volatility_scaler.py # Threshold Scaling Logic
    │   └── portfolio_manager.py # Equity & Margin Management
    └── dashboard/          # Flask UI
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

### 3. Run Live
```bash
python3 -m src.main
```
The dashboard will be available at `http://localhost:5050` (or configured port).

## Risk Management

- **Position Sizing**: Configurable per strategy (default ~20% max per position).
- **Leverage**: Dynamic (1.5x - 5x).
- **Volatility Gating**: Automatic suspension of trading on assets with >3-sigma volatility spikes.
- **Stop Losses**:
  - **Hard Stop**: Fixed percentage (e.g., 5-10%).
  - **Trailing Stop**: Activates after profit target to lock gains.
  - **Time-Based Stop**: StatArb positions auto-close if flat for extended periods.

## Troubleshooting

- **"WebSocket stale for Xs"**: The bot detected a frozen connection and is auto-reconnecting. No action needed.
- **Stuck Positions**: Use `python3 scripts/close_all_positions.py` to liquidate all open positions immediately.
- **Rate Limits**: The bot handles 429s automatically with exponential backoff.

## Disclaimer

**USE AT YOUR OWN RISK.**
This software is for educational purposes. Cryptocurrency trading involves significant risk of loss. The authors are not responsible for financial losses generated by this bot.
