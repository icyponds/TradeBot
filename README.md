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

### Order Monitoring
- **Open order tracking** and status monitoring
- **Stale order detection** and automatic cleanup
- **Order timeout management** (configurable)
- **Order validation** to prevent invalid orders

### Monitoring Tools
- `position_monitor.py` - Comprehensive position and order validation script
- `test_position_monitoring.py` - Test suite for monitoring functionality
- `check_pnl.py` - Quick PnL checking with position validation

### Configuration
```bash
# Order monitoring settings (optimized for scalping)
ORDER_TIMEOUT_MINUTES=0.5          # 30 seconds before order is considered stale
ENABLE_STALE_ORDER_CLEANUP=true    # Enable automatic stale order cleanup
POSITION_SYNC_INTERVAL=10          # 10 seconds between position syncs
ENABLE_POSITION_VALIDATION=true    # Enable position integrity checks
```

## Project Structure

```