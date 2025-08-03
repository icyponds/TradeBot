# Trading Bot Setup Guide

## Overview

This trading bot is designed for Hyperliquid perpetual futures trading with real-time data collection via WebSocket connections. The bot uses dynamic pair selection based on market conditions and implements multiple trading strategies optimized for scalping timeframes.

## Features

- **Real-time Data Collection**: WebSocket-based data collection for live market data
- **Dynamic Pair Selection**: Automatically selects trading pairs based on open interest and liquidity
- **Multiple Strategies**: Moving Average Crossover and RSI strategies
- **Scalping Optimized**: Configured for 1-minute timeframes with rapid execution
- **Risk Management**: Position sizing and stop-loss/take-profit mechanisms
- **Performance Tracking**: Comprehensive trade and performance analytics

## Prerequisites

- Python 3.8 or higher
- Hyperliquid account with API access
- Private key and wallet address for authentication

## Installation

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd TradeBot
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables**:
   ```bash
   cp env.example .env
   ```

4. **Configure your `.env` file**:
   ```bash
   # Hyperliquid API Configuration
   HYPERLIQUID_API_URL=https://api.hyperliquid.xyz
   HYPERLIQUID_WS_URL=wss://api.hyperliquid.xyz/ws
   HYPERLIQUID_PRIVATE_KEY=your_private_key_here
   HYPERLIQUID_WALLET_ADDRESS=your_wallet_address_here
   
   # Trading Configuration
   BASE_CURRENCY=USDC
   MAX_POSITION_SIZE=50
   
   # Strategy Configuration
   STRATEGY_TIMEFRAME=1m
   MA_SHORT_PERIOD=5
   MA_LONG_PERIOD=10
   RSI_PERIOD=14
   RSI_OVERBOUGHT=70
   RSI_OVERSOLD=30
   
   # Dynamic Pair Selection
   DYNAMIC_PAIR_SELECTION=true
   MIN_OPEN_INTEREST=1000000
   MAX_OPEN_INTEREST=100000000
   MAX_PAIRS_TO_TRADE=5
   SCAN_INTERVAL_MINUTES=5
   
   # Asset Filtering (leave empty for no restrictions)
   EXCLUDED_ASSETS=
   INCLUDED_ASSETS=
   
   # Risk Management
   STOP_LOSS_PERCENTAGE=5
   TAKE_PROFIT_PERCENTAGE=10
   
   # Logging
   LOG_LEVEL=INFO
   ```

## Hyperliquid Integration

### Authentication Setup

1. **Generate Private Key**: Create a private key for your wallet
2. **Get Wallet Address**: Your Hyperliquid wallet address
3. **Configure API**: Add credentials to `.env` file

### WebSocket Data Collection

The bot uses WebSocket connections to collect real-time market data:

- **Real-time Price Updates**: Live price feeds for all trading pairs
- **OHLCV Candle Building**: Automatic construction of 1-minute candles
- **Data Buffering**: Maintains rolling window of historical data
- **Automatic Reconnection**: Handles connection drops gracefully

### Supported Assets

The bot dynamically discovers and trades available assets based on:

- **Open Interest**: Minimum and maximum thresholds
- **Volume**: 24-hour trading volume
- **Liquidity**: Bid-ask spread analysis
- **Market Conditions**: Real-time market data

## Dynamic Trading Pair Selection

The bot automatically selects trading pairs based on market conditions:

### Configuration Parameters

- `DYNAMIC_PAIR_SELECTION`: Enable/disable dynamic selection
- `MIN_OPEN_INTEREST`: Minimum open interest threshold
- `MAX_OPEN_INTEREST`: Maximum open interest threshold
- `MAX_PAIRS_TO_TRADE`: Maximum number of pairs to trade simultaneously
- `SCAN_INTERVAL_MINUTES`: How often to rescan for new pairs

### Selection Criteria

1. **Open Interest**: Pairs with sufficient liquidity
2. **Volume**: Active trading volume
3. **Spread**: Tight bid-ask spreads
4. **Performance**: Historical performance tracking

### Asset Filtering

- `EXCLUDED_ASSETS`: Comma-separated list of assets to exclude
- `INCLUDED_ASSETS`: Comma-separated list of assets to include only

## Scalping Configuration

The bot is optimized for scalping with the following settings:

### Timeframe Settings
- **Strategy Timeframe**: 1-minute candles
- **Execution Interval**: 30 seconds between analysis cycles
- **Data Collection**: Real-time WebSocket feeds

### Strategy Parameters
- **Moving Average**: 5-period short, 10-period long
- **RSI**: 14-period with 70/30 thresholds
- **Position Size**: Maximum 50 USDC per trade

### Risk Management
- **Stop Loss**: 5% of position value
- **Take Profit**: 10% of position value
- **Position Limits**: Maximum 50 USDC per position

## Usage

### Running the Bot

1. **Start the bot**:
   ```bash
   python src/main.py
   ```

2. **Test WebSocket data collection**:
   ```bash
   python test_websocket.py
   ```

3. **Monitor logs**:
   ```bash
   tail -f logs/trading_bot.log
   ```

### Monitoring

The bot provides comprehensive monitoring:

- **Real-time Data**: WebSocket connection status
- **Trade Execution**: Buy/sell signals and executions
- **Performance Metrics**: P&L, win rate, total trades
- **Pair Performance**: Individual asset performance tracking

### Stopping the Bot

Use `Ctrl+C` to gracefully stop the bot. The bot will:

- Close all open positions
- Stop WebSocket connections
- Save performance data
- Log shutdown information

## Testing

### WebSocket Data Test

Test the WebSocket data collection:

```bash
python test_websocket.py
```

This will:
- Connect to Hyperliquid WebSocket
- Collect real-time data for 30 seconds
- Verify OHLCV data availability
- Test current price retrieval

### Strategy Testing

Test individual strategies:

```bash
python -m pytest tests/test_strategies.py
```

## Troubleshooting

### Common Issues

1. **WebSocket Connection Failed**:
   - Check network connectivity
   - Verify WebSocket URL in configuration
   - Check firewall settings

2. **No Data Available**:
   - Wait for initial data collection (10-30 seconds)
   - Check symbol availability
   - Verify API credentials

3. **Authentication Errors**:
   - Verify private key format
   - Check wallet address
   - Ensure API permissions

### Log Analysis

Check logs for detailed information:

```bash
grep "ERROR" logs/trading_bot.log
grep "WebSocket" logs/trading_bot.log
grep "Signal" logs/trading_bot.log
```

## Performance Optimization

### For Scalping

- **Execution Speed**: 30-second intervals for rapid signals
- **Data Quality**: Real-time WebSocket feeds
- **Memory Usage**: Rolling data buffers
- **CPU Usage**: Efficient OHLCV calculations

### Monitoring Performance

- **Data Collection**: WebSocket connection status
- **Signal Generation**: Strategy execution frequency
- **Trade Execution**: Order placement success rate
- **Risk Management**: Stop-loss and take-profit effectiveness

## Security Considerations

- **Private Keys**: Store securely, never commit to version control
- **API Permissions**: Use minimal required permissions
- **Network Security**: Use secure connections
- **Position Limits**: Set appropriate position size limits

## Disclaimer

This trading bot is for educational and research purposes. Trading involves substantial risk of loss. Always:

- Test thoroughly with small amounts
- Monitor performance continuously
- Understand the strategies being used
- Never risk more than you can afford to lose

## Support

For issues and questions:

1. Check the logs for error messages
2. Review the configuration settings
3. Test WebSocket connectivity
4. Verify API credentials and permissions 