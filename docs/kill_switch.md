# Kill Switch Documentation

## Overview

The trading bot includes a kill switch mechanism that allows you to immediately close all open positions in case of emergency or when you need to stop trading.

## Kill Switch Methods

### 1. Built-in Signal Handler (Recommended)

The bot automatically sets up signal handlers for emergency stops:

- **Ctrl+C**: Closes all positions and exits gracefully
- **SIGTERM**: Closes all positions and exits gracefully

When you press Ctrl+C while the bot is running, it will:
1. Close all open positions
2. Save the final state
3. Exit gracefully

### 2. Independent Kill Switch Script

You can also use the independent kill switch script:

```bash
python kill_switch.py
```

This script will:
1. Load saved positions from `positions.json`
2. Ask for confirmation (type 'KILL' to confirm)
3. Close all positions via API
4. Show summary of closed positions and PnL

## How It Works

### Position Tracking

The bot automatically saves all positions to `positions.json` whenever:
- A new position is opened
- A position is closed
- The bot starts up

### Kill Switch Process

1. **Signal Detection**: The bot listens for SIGINT (Ctrl+C) and SIGTERM signals
2. **Position Loading**: Loads current positions from `positions.json`
3. **Order Execution**: Places market orders to close all positions
4. **State Cleanup**: Clears saved positions and exits

## Usage Examples

### Emergency Stop While Bot is Running

```bash
# In the terminal where the bot is running
Ctrl+C
```

### Independent Kill Switch

```bash
# In a separate terminal
python kill_switch.py
```

You'll see output like:
```
🚨 TRADING BOT KILL SWITCH 🚨
========================================
📊 Found 3 saved positions:
   BTC: long @ 45000.0
   ETH: short @ 3200.0
   SOL: long @ 150.0

⚠️  WARNING: This will close ALL open positions!
⚠️  This action cannot be undone!

Type 'KILL' to confirm closing all positions: KILL

🔄 Closing all positions...
✅ Closed BTC: sell 0.1 @ 45100.0
✅ Closed ETH: buy 2.5 @ 3180.0
✅ Closed SOL: sell 10.0 @ 152.0

✅ Kill switch completed!
📊 Summary: Closed 3 positions
💰 Total PnL: $125.50
🎯 All positions closed successfully!
```

## Safety Features

1. **Confirmation Required**: The kill switch requires explicit confirmation
2. **Position Validation**: Checks current prices before closing
3. **Error Handling**: Continues even if some positions fail to close
4. **State Persistence**: Positions are saved to file for recovery

## Configuration

The kill switch uses the same configuration as the main bot:
- API credentials from `.env` file
- Same risk management settings
- Same order execution logic

## Recovery

If the kill switch fails or is interrupted:
1. Check `positions.json` for remaining positions
2. Run the kill switch again
3. Or manually close positions via the exchange interface

## Best Practices

1. **Test First**: Test the kill switch in a demo environment
2. **Monitor**: Always monitor the bot when running
3. **Backup**: Keep backup of position data
4. **Documentation**: Document your emergency procedures

## Troubleshooting

### Kill Switch Not Working

1. Check API connection: `python -c "from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI; api = HyperliquidSDKAPI(load_config()); print(api.test_connection())"`
2. Check positions file: `cat positions.json`
3. Check logs for errors

### Positions Not Closing

1. Verify API credentials in `.env`
2. Check if positions exist on exchange
3. Verify sufficient balance for closing orders
4. Check exchange status and maintenance windows 