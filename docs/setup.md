# Trading Bot Setup Guide

## Overview

This trading bot is designed for Hyperliquid perpetual futures trading with **aggressive leverage** (10-20x) and real-time data collection via WebSocket connections. The bot uses dynamic pair selection based on market conditions and implements multiple trading strategies optimized for scalping timeframes.

## Features

- **Real-time Data Collection**: WebSocket-based data collection for live market data
- **Dynamic Pair Selection**: Automatically selects trading pairs based on open interest and liquidity
- **Multiple Strategies**: Moving Average Crossover and RSI strategies
- **Aggressive Leverage**: 10-20x leverage for maximum profit potential
- **Scalping Optimized**: Configured for 1-minute timeframes with rapid execution
- **Advanced Risk Management**: Leverage-adjusted stop losses and take profits
- **Performance Tracking**: Comprehensive trade and performance analytics

## Prerequisites

- Python 3.8 or higher
- Hyperliquid account with API access
- Private key and wallet address for authentication
- **Risk Tolerance**: This bot uses aggressive leverage (10-20x) - only for experienced traders

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

## Configuration

### Environment Variables

Copy the `env.example` file to `.env` and configure the following variables:

#### API Configuration
- `HYPERLIQUID_PRIVATE_KEY`: Your private key for trading
- `HYPERLIQUID_WALLET_ADDRESS`: Your wallet address
- `HYPERLIQUID_API_URL`: Hyperliquid API URL (default: https://api.hyperliquid.xyz)

#### Portfolio-based Position Sizing
- `USE_PORTFOLIO_BASED_SIZING`: Enable portfolio-based position sizing (default: true)
- `MAX_POSITION_SIZE_USD`: Maximum USD per position (fallback, default: 50)
- `MAX_POSITION_SIZE_PERCENTAGE`: Maximum percentage of portfolio per position (default: 2.0%)
- `MAX_POSITIONS_PERCENTAGE`: Maximum percentage of portfolio in all positions (default: 33.33%)
- `RISK_PERCENTAGE`: Risk percentage per trade (default: 2.0%)
- `STOP_LOSS_PERCENTAGE`: Stop loss percentage (default: 2.0%)

#### Trading Configuration
- `DYNAMIC_PAIR_SELECTION`: Enable dynamic pair selection (default: true)
- `MIN_OPEN_INTEREST`: Minimum open interest for pair selection (default: 1,000,000)
- `MAX_OPEN_INTEREST`: Maximum open interest for pair selection (optional)
- `SCAN_INTERVAL_MINUTES`: Interval for scanning new pairs (default: 60)

#### Strategy Configuration
- `ENABLED_STRATEGIES`: Comma-separated list of enabled strategies (default: moving_average,rsi)
- `STRATEGY_TIMEFRAME`: Timeframe for strategy analysis (default: 1m)
- `OHLCV_LIMIT`: Number of OHLCV candles to analyze (default: 100)

### Position Sizing

The bot uses **capital at risk-based position sizing** instead of notional position sizing:

1. **Dynamic Portfolio Fetching**: The bot fetches your real portfolio balance from Hyperliquid
2. **Capital at Risk Sizing**: Position sizes are calculated based on the maximum capital at risk, not the total position value
3. **Risk Management**: Maximum 2% of portfolio capital at risk per position (configurable)
4. **Portfolio Limits**: Maximum 33.33% of portfolio capital at risk in all positions combined

**Example**: If your portfolio is $10,000:
- Maximum capital at risk per position: $200 (2% of $10,000)
- Maximum total capital at risk: $3,333 (33.33% of $10,000)
- With 10x leverage, a $200 capital at risk position would have a $2,000 notional value
- The bot will automatically adjust position sizes as your portfolio grows or shrinks

### Risk Management

The bot implements comprehensive risk management based on capital at risk:

1. **Position Limits**: Maximum 2% of portfolio capital at risk per position
2. **Portfolio Limits**: Maximum 33.33% of portfolio capital at risk in all positions
3. **Dynamic Leverage**: Leverage adjusts based on signal strength and market volatility
4. **Stop Losses**: Automatic stop losses calculated based on capital at risk (configurable)
5. **Margin Buffer**: 20% margin buffer to prevent liquidation
6. **Capital at Risk Tracking**: Real-time monitoring of actual capital exposed to risk

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

## Aggressive Leverage Configuration

### ⚠️ **HIGH RISK WARNING**

This bot uses **aggressive leverage (10-20x)** which carries significant risk:

- **Liquidation Risk**: Positions can be liquidated quickly
- **Amplified Losses**: Losses are multiplied by leverage
- **Margin Calls**: May require additional capital
- **Volatility Risk**: High leverage amplifies price volatility

### Leverage Settings

- **MAX_LEVERAGE=20**: Maximum 20x leverage
- **DEFAULT_LEVERAGE=15**: Default 15x leverage
- **MIN_LEVERAGE=10**: Minimum 10x leverage
- **LEVERAGE_PER_SYMBOL**: Symbol-specific leverage (BTC:20, ETH:18, etc.)

### Risk Management

- **Tighter Stops**: 2% stop loss (adjusted for leverage)
- **Higher Targets**: 6% take profit (adjusted for leverage)
- **Margin Buffer**: 20% margin buffer for safety
- **Liquidation Monitoring**: Real-time liquidation risk tracking

### Position Sizing

The bot calculates position sizes based on:

1. **Available Capital**: Total capital available
2. **Risk Percentage**: 2% risk per trade
3. **Leverage**: Symbol-specific leverage (10-20x)
4. **Margin Requirements**: Adjusted for leverage
5. **Position Limits**: Maximum 50 USDC per position

### Example Position Calculation

For a $1000 account with 15x leverage on BTC:

- **Risk Amount**: $20 (2% of $1000)
- **Position Value**: $600 (20 × 15x leverage)
- **Margin Required**: $40 (600 ÷ 15 + 20% buffer)
- **Stop Loss**: 1.3% (2% ÷ √15/10)
- **Take Profit**: 7.3% (6% × √15/10)

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
- **Stop Loss**: 2% of position value (leverage-adjusted)
- **Take Profit**: 6% of position value (leverage-adjusted)
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
- **Margin Usage**: Real-time margin utilization
- **Leverage Risk**: Liquidation risk monitoring

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

4. **Liquidation Risk**:
   - Reduce leverage settings
   - Increase margin buffer
   - Monitor position sizes

### Log Analysis

Check logs for detailed information:

```bash
grep "ERROR" logs/trading_bot.log
grep "WebSocket" logs/trading_bot.log
grep "Signal" logs/trading_bot.log
grep "Leverage" logs/trading_bot.log
```

## Performance Optimization

### For Aggressive Scalping

- **Execution Speed**: 30-second intervals for rapid signals
- **Data Quality**: Real-time WebSocket feeds
- **Memory Usage**: Rolling data buffers
- **CPU Usage**: Efficient OHLCV calculations
- **Leverage Management**: Real-time margin monitoring

### Monitoring Performance

- **Data Collection**: WebSocket connection status
- **Signal Generation**: Strategy execution frequency
- **Trade Execution**: Order placement success rate
- **Risk Management**: Stop-loss and take-profit effectiveness
- **Margin Utilization**: Real-time margin usage tracking

## Security Considerations

- **Private Keys**: Store securely, never commit to version control
- **API Permissions**: Use minimal required permissions
- **Network Security**: Use secure connections
- **Position Limits**: Set appropriate position size limits
- **Leverage Limits**: Monitor leverage usage carefully

## ⚠️ **CRITICAL RISK WARNINGS**

### Leverage Risks

- **Liquidation**: Positions can be liquidated at any time
- **Margin Calls**: May require additional capital
- **Amplified Losses**: Losses are multiplied by leverage
- **Volatility**: High leverage amplifies price movements

### Recommended Usage

- **Start Small**: Begin with small position sizes
- **Monitor Closely**: Watch margin usage and liquidation risk
- **Test First**: Use paper trading before live trading
- **Understand Risks**: Only trade what you can afford to lose

## Disclaimer

This trading bot uses **aggressive leverage (10-20x)** and is for **experienced traders only**. Trading involves substantial risk of loss. Always:

- Test thoroughly with small amounts
- Monitor performance continuously
- Understand the strategies being used
- Never risk more than you can afford to lose
- Be prepared for liquidation events
- Monitor margin requirements closely

## Support

For issues and questions:

1. Check the logs for error messages
2. Review the configuration settings
3. Test WebSocket connectivity
4. Verify API credentials and permissions
5. Monitor leverage and margin usage 