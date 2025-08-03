# Trading Bot Setup Guide for Hyperliquid

## Prerequisites

- Python 3.8 or higher
- pip (Python package installer)
- Git
- Hyperliquid wallet with private key

## Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd TradeBot
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp env.example .env
   # Edit .env with your Hyperliquid credentials and settings
   ```

## Configuration

### Hyperliquid Setup

1. **Get Hyperliquid credentials**
   - Create a Hyperliquid account at https://hyperliquid.xyz
   - Export your wallet private key
   - Note your wallet address

2. **Update .env file**
   ```bash
   HYPERLIQUID_PRIVATE_KEY=your_private_key_here
   HYPERLIQUID_WALLET_ADDRESS=your_wallet_address_here
   ```

### Trading Configuration

Edit the following variables in your `.env` file:

- `TRADING_SYMBOLS`: Comma-separated list of trading pairs (e.g., BTC,ETH,SOL)
- `MAX_POSITION_SIZE`: Maximum position size in USDC
- `RISK_PERCENTAGE`: Risk per trade as percentage
- `STOP_LOSS_PERCENTAGE`: Stop loss percentage
- `TAKE_PROFIT_PERCENTAGE`: Take profit percentage

### Strategy Configuration

Enable/disable strategies and configure parameters:

- `ENABLED_STRATEGIES`: Comma-separated list of strategies to use
- `MA_SHORT_PERIOD`: Short moving average period
- `MA_LONG_PERIOD`: Long moving average period
- `RSI_PERIOD`: RSI calculation period
- `RSI_OVERBOUGHT`: RSI overbought threshold
- `RSI_OVERSOLD`: RSI oversold threshold

## Usage

### Running the Bot

```bash
python src/main.py
```

### Testing

```bash
pytest tests/
```

### Code Formatting

```bash
black src/
flake8 src/
```

## Hyperliquid Features

### Perpetual Futures Trading

- Trade with leverage on various assets
- Long and short positions
- Real-time price updates via WebSocket
- Funding rate considerations

### Available Assets

The bot supports all assets available on Hyperliquid, including:
- Cryptocurrencies: BTC, ETH, SOL, MATIC, etc.
- Traditional assets: SPY, QQQ, etc.
- Commodities: GOLD, SILVER, etc.

### WebSocket Integration

- Real-time market data streaming
- Live price updates
- Order book updates
- Position updates

## Strategies

### Moving Average Crossover

- Uses two moving averages (short and long period)
- Generates buy signals when short MA crosses above long MA
- Generates sell signals when short MA crosses below long MA
- Suitable for trending markets

### RSI (Relative Strength Index)

- Uses RSI oscillator to identify overbought/oversold conditions
- Generates buy signals when RSI is oversold
- Generates sell signals when RSI is overbought
- Suitable for ranging markets

## Risk Management

The bot includes several risk management features:

- **Position Sizing**: Calculates position size based on risk percentage
- **Stop Loss**: Automatic stop loss orders
- **Take Profit**: Automatic take profit orders
- **Maximum Position Size**: Limits total position exposure
- **Leverage Management**: Configurable leverage limits

## Monitoring

The bot logs all activities to:
- Console output
- Log files in `logs/` directory

Key metrics tracked:
- Trade execution
- PnL calculations
- Strategy performance
- Error handling
- WebSocket connection status

## Safety Notes

⚠️ **Important**: This trading bot is for educational purposes. Perpetual futures trading involves significant risk:

- **Leverage Risk**: Leverage can amplify both gains and losses
- **Liquidation Risk**: Positions can be liquidated if margin requirements aren't met
- **Funding Rate Risk**: Perpetual futures have funding rates that affect PnL
- **Never invest more than you can afford to lose**
- **Test thoroughly with small amounts first**
- **Monitor the bot regularly**
- **Understand the strategies before using real money**
- **Consider paper trading first**

## Troubleshooting

### Common Issues

1. **API Connection Failed**
   - Check your wallet credentials
   - Verify wallet has sufficient balance
   - Check internet connection

2. **WebSocket Connection Issues**
   - Verify WebSocket URL in configuration
   - Check firewall settings
   - Monitor connection logs

3. **Insufficient Data**
   - Ensure you have enough historical data
   - Check symbol availability on Hyperliquid

4. **Strategy Errors**
   - Verify strategy parameters
   - Check log files for details

### Getting Help

- Check the logs in `logs/` directory
- Review the configuration in `.env`
- Test individual components
- Consult the Hyperliquid documentation
- Join Hyperliquid community channels 