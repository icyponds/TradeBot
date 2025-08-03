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

### Dynamic Trading Pair Selection

The bot now automatically selects trading pairs based on open interest and other criteria:

#### **Open Interest Thresholds**
- `MIN_OPEN_INTEREST`: Minimum open interest in USD (default: $1,000,000)
- `MAX_OPEN_INTEREST`: Maximum open interest in USD (default: $100,000,000)
- `MAX_PAIRS_TO_TRADE`: Maximum number of pairs to trade simultaneously (default: 10)

#### **Scanning Configuration**
- `DYNAMIC_PAIR_SELECTION`: Enable/disable dynamic selection (default: true)
- `SCAN_INTERVAL_MINUTES`: How often to rescan for new pairs (default: 60 minutes)

#### **Asset Filtering**
- `EXCLUDED_ASSETS`: Comma-separated list of assets to exclude (e.g., SHIB,DOGE)
- `INCLUDED_ASSETS`: Comma-separated list of assets to include (optional, if empty all assets are considered)

#### **Example Configurations**

**Conservative (High liquidity only):**
```bash
MIN_OPEN_INTEREST=5000000
MAX_OPEN_INTEREST=50000000
MAX_PAIRS_TO_TRADE=5
```

**Aggressive (More opportunities):**
```bash
MIN_OPEN_INTEREST=500000
MAX_OPEN_INTEREST=200000000
MAX_PAIRS_TO_TRADE=15
```

**Crypto-only:**
```bash
INCLUDED_ASSETS=BTC,ETH,SOL,MATIC,ADA,DOT,LINK,UNI,AAVE,COMP
EXCLUDED_ASSETS=SPY,QQQ,GLD,SLV
```

### Trading Configuration

Edit the following variables in your `.env` file:

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

## Dynamic Pair Selection Features

### **Automatic Asset Discovery**
- Scans all available assets on Hyperliquid
- Ranks assets by open interest, volume, and price
- Automatically adapts to new assets added by Hyperliquid

### **Smart Filtering**
- **Liquidity Filter**: Only trades assets with sufficient open interest
- **Volume Filter**: Minimum daily volume requirements
- **Spread Filter**: Avoids assets with excessive bid-ask spreads
- **Price Filter**: Ensures valid pricing data

### **Performance Tracking**
- Tracks PnL for each trading pair
- Uses performance data to optimize future selections
- Logs detailed selection criteria and rankings

### **Flexible Configuration**
- Adjustable open interest thresholds
- Configurable scan intervals
- Asset inclusion/exclusion lists
- Maximum pair limits

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
- **Dynamic Pair Selection**: Focuses on liquid assets only

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
- **Pair selection criteria and rankings**
- **Performance by trading pair**

## Safety Notes

⚠️ **Important**: This trading bot is for educational purposes. Perpetual futures trading involves significant risk:

- **Leverage Risk**: Leverage can amplify both gains and losses
- **Liquidation Risk**: Positions can be liquidated if margin requirements aren't met
- **Funding Rate Risk**: Perpetual futures have funding rates that affect PnL
- **Dynamic Selection Risk**: New pairs may have different risk profiles
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

3. **No Trading Pairs Selected**
   - Check open interest thresholds
   - Verify asset inclusion/exclusion lists
   - Check minimum volume requirements

4. **Strategy Errors**
   - Verify strategy parameters
   - Check log files for details

### Getting Help

- Check the logs in `logs/` directory
- Review the configuration in `.env`
- Test individual components
- Consult the Hyperliquid documentation
- Join Hyperliquid community channels 