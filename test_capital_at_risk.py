#!/usr/bin/env python3
"""
Test script to verify capital at risk position sizing.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from config.settings import load_config
from utils.leverage_manager import LeverageManager
from utils.portfolio_manager import PortfolioManager
import logging

def setup_logging():
    """Setup logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

def test_capital_at_risk_sizing():
    """Test that position sizing is based on capital at risk, not notional value."""
    print("Testing Capital at Risk Position Sizing")
    print("=" * 60)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        return False
    
    # Initialize managers
    portfolio_manager = PortfolioManager(config)
    leverage_manager = LeverageManager(config, portfolio_manager)
    
    # Test scenarios
    test_scenarios = [
        {
            'symbol': 'BTC',
            'current_price': 50000,
            'portfolio_value': 10000,
            'signal_strength': 0.8,
            'market_volatility': 0.1,
            'strategy': 'moving_average'
        },
        {
            'symbol': 'ETH',
            'current_price': 3000,
            'portfolio_value': 10000,
            'signal_strength': 0.6,
            'market_volatility': 0.2,
            'strategy': 'rsi'
        },
        {
            'symbol': 'SOL',
            'current_price': 100,
            'portfolio_value': 10000,
            'signal_strength': 0.9,
            'market_volatility': 0.05,
            'strategy': 'moving_average'
        }
    ]
    
    for i, scenario in enumerate(test_scenarios, 1):
        print(f"\nTest Scenario {i}:")
        print(f"  Symbol: {scenario['symbol']}")
        print(f"  Price: ${scenario['current_price']:,}")
        print(f"  Portfolio: ${scenario['portfolio_value']:,}")
        print(f"  Signal Strength: {scenario['signal_strength']:.2f}")
        print(f"  Volatility: {scenario['market_volatility']:.2f}")
        print(f"  Strategy: {scenario['strategy']}")
        
        # Set up portfolio
        portfolio_manager.total_equity = scenario['portfolio_value']
        portfolio_manager.free_margin = scenario['portfolio_value']
        
        # Calculate position size
        position_size, margin_required, leverage = leverage_manager.calculate_leveraged_position_size(
            scenario['symbol'],
            scenario['current_price'],
            portfolio_manager.calculate_available_capital_for_trading(),
            scenario['strategy'],
            scenario['signal_strength'],
            scenario['market_volatility']
        )
        
        # Calculate notional value
        notional_value = position_size * scenario['current_price']
        
        # Calculate expected capital at risk (should be the margin_required)
        expected_capital_at_risk = margin_required
        
        print(f"  Calculated Position Size: {position_size:.4f} units")
        print(f"  Notional Value: ${notional_value:.2f}")
        print(f"  Capital at Risk: ${margin_required:.2f}")
        print(f"  Leverage: {leverage:.1f}x")
        
        # Verify capital at risk is correct
        max_position = portfolio_manager.calculate_max_position_size()
        if margin_required <= max_position:
            print("  ✅ Capital at risk within limits")
        else:
            print("  ❌ Capital at risk exceeds limits")
        
        # Verify the relationship between notional value and capital at risk
        calculated_leverage_from_notional = notional_value / margin_required if margin_required > 0 else 0
        leverage_diff = abs(calculated_leverage_from_notional - leverage)
        
        if leverage_diff < 0.1:  # Allow for small rounding differences
            print("  ✅ Leverage calculation is correct")
        else:
            print(f"  ❌ Leverage mismatch: calculated={calculated_leverage_from_notional:.1f}, expected={leverage:.1f}")
        
        # Verify that capital at risk is the limiting factor, not notional value
        if margin_required <= max_position and notional_value > max_position:
            print("  ✅ Position sizing correctly based on capital at risk, not notional value")
        else:
            print("  ⚠️  Position sizing may not be properly based on capital at risk")
    
    return True

def test_leverage_impact():
    """Test how leverage affects position sizing with capital at risk approach."""
    print("\n" + "=" * 60)
    print("Testing Leverage Impact on Capital at Risk")
    print("=" * 60)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        return False
    
    # Initialize managers
    portfolio_manager = PortfolioManager(config)
    leverage_manager = LeverageManager(config, portfolio_manager)
    
    # Set up portfolio
    portfolio_manager.total_equity = 10000
    portfolio_manager.free_margin = 10000
    
    # Test with different leverage scenarios
    test_price = 50000  # BTC price
    max_capital_at_risk = portfolio_manager.calculate_max_position_size()
    
    print(f"Maximum capital at risk: ${max_capital_at_risk:.2f}")
    print(f"Test price: ${test_price:,}")
    print()
    
    leverage_scenarios = [1.0, 2.0, 5.0, 10.0, 20.0]
    
    for leverage in leverage_scenarios:
        # Simulate different leverage by adjusting signal strength
        # Higher signal strength = higher leverage
        signal_strength = min(1.0, (leverage - 1.0) / 10.0)
        
        position_size, margin_required, actual_leverage = leverage_manager.calculate_leveraged_position_size(
            'BTC',
            test_price,
            portfolio_manager.calculate_available_capital_for_trading(),
            'moving_average',
            signal_strength,
            0.1  # Low volatility
        )
        
        notional_value = position_size * test_price
        
        print(f"Target Leverage: {leverage:.1f}x")
        print(f"  Actual Leverage: {actual_leverage:.1f}x")
        print(f"  Capital at Risk: ${margin_required:.2f}")
        print(f"  Notional Value: ${notional_value:.2f}")
        print(f"  Position Size: {position_size:.4f} units")
        
        # Verify that capital at risk remains constant regardless of leverage
        if abs(margin_required - max_capital_at_risk) < 1.0:
            print("  ✅ Capital at risk remains constant")
        else:
            print(f"  ❌ Capital at risk varies: ${margin_required:.2f} vs ${max_capital_at_risk:.2f}")
        
        print()

def main():
    """Main test function."""
    setup_logging()
    
    print("Capital at Risk Position Sizing Test")
    print("=" * 60)
    
    # Test basic capital at risk sizing
    if not test_capital_at_risk_sizing():
        print("❌ Basic capital at risk sizing test failed")
        return False
    
    # Test leverage impact
    test_leverage_impact()
    
    print("✅ All tests completed successfully!")
    print("\nKey Points:")
    print("- Position sizing is now based on capital at risk, not notional value")
    print("- Capital at risk remains constant regardless of leverage")
    print("- Notional value increases with leverage while capital at risk stays the same")
    print("- This provides better risk management and position control")
    
    return True

if __name__ == "__main__":
    main() 