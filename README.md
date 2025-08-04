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

### Monitoring Tools
- `position_monitor.py` - Comprehensive position and order validation script with continuous monitoring
- `test_position_monitoring.py` - Test suite for monitoring functionality
- `check_pnl.py` - Quick PnL checking with position validation

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

```