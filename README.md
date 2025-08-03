# Trading Bot for Hyperliquid

An automated trading bot that connects to Hyperliquid perpetual futures exchange and executes automated trading strategies.

## Features

- Real-time market data fetching from Hyperliquid API
- WebSocket connection for live price updates
- Configurable trading strategies for perpetual futures
- Risk management and position sizing
- Performance tracking and analytics
- Modular architecture for easy strategy development

## Project Structure

```
TradeBot/
├── src/
│   ├── api/           # API clients for market data
│   │   ├── hyperliquid_sdk_api.py  # Hyperliquid SDK API client
│   │   └── websocket_collector.py  # WebSocket data collection
│   ├── strategies/    # Trading strategy implementations
│   ├── models/        # Data models and schemas
│   ├── utils/         # Utility functions
│   └── config/        # Configuration management
├── tests/             # Test files
├── docs/              # Documentation
├── requirements.txt   # Python dependencies
└── README.md         # This file
```

## Setup

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your Hyperliquid credentials
4. Configure your trading parameters in `src/config/`

## Usage

```bash
python src/main.py
```

## Development

- Run tests: `pytest`
- Format code: `black src/`
- Lint code: `flake8 src/`

## Hyperliquid Integration

This bot is specifically designed for Hyperliquid, a decentralized perpetual futures exchange. Key features:

- **Perpetual Futures Trading**: Trade with leverage on various assets
- **WebSocket Real-time Data**: Live price updates and market data
- **Wallet-based Authentication**: Uses private keys for secure trading
- **Asset Universe**: Supports all assets available on Hyperliquid

## Disclaimer

This trading bot is for educational purposes. Trading perpetual futures involves significant risk and you should never invest more than you can afford to lose. Leverage can amplify both gains and losses. 