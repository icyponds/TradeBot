import pytest
from unittest.mock import MagicMock, patch, ANY
from src.strategies.strategy_manager import StrategyManager
from src.models.trade import Position
from datetime import datetime

class TestStrategyManager:
    
    @pytest.fixture
    def strategy_manager(self, mock_config, mock_market_api):
        """Creates a StrategyManager instance with mocked dependencies."""
        # Ensure strategies key exists
        if 'strategies' not in mock_config:
            mock_config['strategies'] = {}
            
        # Add required strategy config keys if missing
        mock_config['strategies']['enabled'] = ['cross_sectional_momentum']
        mock_config['strategies']['ohlcv_limit'] = 100
        mock_config['trading']['position_monitoring_interval'] = 10
        mock_config['trading']['enable_stale_order_cleanup'] = True
        mock_config['trading']['position_sync_interval'] = 300
        mock_config['trading']['enable_position_validation'] = True
        mock_config['trading']['order_timeout_minutes'] = 5

        # Mock StrategySelector and ExecutionEngine to avoid complex dependencies
        with patch('src.strategies.strategy_manager.StrategySelector'), \
             patch('src.strategies.strategy_manager.ExecutionEngine'), \
             patch('src.strategies.strategy_manager.DynamicPairSelector'), \
             patch('src.strategies.strategy_manager.PerformanceTracker'):
             
             manager = StrategyManager(mock_config, mock_market_api)
             return manager

    def test_initialization(self, strategy_manager):
        """Test that StrategyManager initializes correctly."""
        assert strategy_manager.is_running is False
        assert isinstance(strategy_manager.strategies, dict)
        # Check if CSM strategy was initialized (legacy path simulation)
        # Note: In real app, it loads from settings.py, here we use mock_config
        # We need to verify if _initialize_strategies picked it up
        assert strategy_manager.market_api is not None

    def test_should_execute_signal_basic(self, strategy_manager):
        """Test basic signal execution logic."""
        symbol = "BTC"
        signal = {
            'signal': 'buy',
            'confidence': 0.9,
            'price': 50000
        }
        current_price = 50000
        ohlcv = {} # Mock OHLCV
        
        # Mock checks
        strategy_manager.max_positions_percentage = 100.0
        strategy_manager.execution_engine.positions = {} # No positions
        
        # Assuming _should_execute_signal returns True for valid signal in simple case
        # We need to mock internal checks called by _should_execute_signal 
        # (like _check_position_limit, etc.) if they are complex.
        
        # For now, let's test a direct method if possible or mock the heavy lifters
        with patch.object(strategy_manager, '_check_position_limit', return_value=False):
             result = strategy_manager._should_execute_with_position_limit(symbol, signal, 0.9)
             assert result is True

    def test_position_limit_check(self, strategy_manager):
        """Test position limit enforcement."""
        strategy_manager.max_positions_percentage = 50.0
        
        # Mock portfolio allocation return
        # Case 1: Under limit
        with patch.object(strategy_manager, '_check_portfolio_allocation', 
                          return_value={'allocation_percentage': 10.0, 'max_allocation': 50.0}):
            assert strategy_manager._check_position_limit() is False
            
        # Case 2: Over limit
        with patch.object(strategy_manager, '_check_portfolio_allocation', 
                          return_value={'allocation_percentage': 60.0, 'max_allocation': 50.0}):
            assert strategy_manager._check_position_limit() is True

    def test_monitor_and_close_positions_unbound_local_fix(self, strategy_manager):
        """Verify the fix for UnboundLocalError in _monitor_and_close_positions."""
        # Setup state
        strategy_manager.execution_engine.positions = {
            'BTC': Position(symbol='BTC', side='long', size=1.0, entry_price=50000, 
                           current_price=51000, entry_time=datetime.now(), strategy='test')
        }
        strategy_manager.total_positions_closed = 0
        strategy_manager.last_emergency_check = 0 # force check
        
        # Mock dependencies
        strategy_manager._should_close_position = MagicMock(return_value="take_profit")
        strategy_manager.close_position = MagicMock(return_value=True)
        strategy_manager._check_emergency_stop = MagicMock(return_value=False)
        
        # Run method
        strategy_manager._monitor_and_close_positions(
            emergency_portfolio_loss_pct=10.0,
            timestamp=datetime.now()
        )
        
        # Verify side effects
        assert strategy_manager.total_positions_closed == 1
        strategy_manager.close_position.assert_called_with('BTC', 'take_profit', timestamp=ANY)

    def test_close_all_positions(self, strategy_manager):
        """Test close_all_positions iterates correctly."""
        # Setup positions on the execution engine directly
        strategy_manager.execution_engine.positions = {
            'BTC': MagicMock(),
            'ETH': MagicMock()
        }
        
        # Test 1: Normal closure
        strategy_manager.close_all_positions(reason="shutdown")
        
        assert strategy_manager.execution_engine.close_position.call_count == 2
        strategy_manager.execution_engine.close_position.assert_any_call('BTC', reason='shutdown')
        strategy_manager.execution_engine.close_position.assert_any_call('ETH', reason='shutdown')
        
        # Test 2: Error handling during loop
        strategy_manager.execution_engine.close_position.side_effect = [Exception("Error closing BTC"), True]
        strategy_manager.close_all_positions(reason="emergency")
        
        # Should still try to close ETH after BTC fails
        assert strategy_manager.execution_engine.close_position.call_count == 4 # 2 from prev + 2 new

    def test_get_position_timeframe(self, strategy_manager):
        """Test extraction of timeframe from strategy name."""
        # Mock positions property
        strategy_manager.execution_engine.positions = {
            'BTC': {'strategy': 'stat_arb_15m', 'side': 'long'},
            'ETH': {'strategy': 'csm_4h', 'side': 'long'},
            'SOL': {'strategy': 'vol_breakout_1h', 'side': 'short'},
            'DOGE': {'strategy': 'unknown_strategy', 'side': 'long'},
        }
        
        assert strategy_manager._get_position_timeframe('BTC') == '15m'
        assert strategy_manager._get_position_timeframe('ETH') == '4h'
        assert strategy_manager._get_position_timeframe('SOL') == '1h'
        assert strategy_manager._get_position_timeframe('DOGE') == '1h'  # default fallback
        assert strategy_manager._get_position_timeframe('NONEXISTENT') == '1h'  # no position

    def test_check_exit_conditions_with_price_stop_loss(self, strategy_manager):
        """Test stop loss triggers during stale data."""
        position = {
            'side': 'long',
            'entry_price': 50000,
            'stop_loss': 48000,
            'take_profit': 55000,
        }
        
        # Mock close_position
        strategy_manager.execution_engine.close_position = MagicMock()
        
        # Price above stop loss - should NOT close
        strategy_manager._check_exit_conditions_with_price('BTC', position, 49000)
        strategy_manager.execution_engine.close_position.assert_not_called()
        
        # Price at stop loss - should close
        strategy_manager._check_exit_conditions_with_price('BTC', position, 48000)
        strategy_manager.execution_engine.close_position.assert_called_once_with('BTC', reason='stop_loss_stale_data')

    def test_check_exit_conditions_with_price_take_profit(self, strategy_manager):
        """Test take profit triggers during stale data."""
        position = {
            'side': 'long',
            'entry_price': 50000,
            'stop_loss': 48000,
            'take_profit': 55000,
        }
        
        strategy_manager.execution_engine.close_position = MagicMock()
        
        # Price at take profit - should close
        strategy_manager._check_exit_conditions_with_price('BTC', position, 55000)
        strategy_manager.execution_engine.close_position.assert_called_once_with('BTC', reason='take_profit_stale_data')

    def test_handle_stale_data_force_close(self, strategy_manager):
        """Test force close when stale duration exceeds threshold."""
        import time
        
        # Mock position with 15m strategy (threshold = 180 seconds)
        strategy_manager.execution_engine.positions = {
            'BTC': {'strategy': 'stat_arb_15m', 'side': 'long', 'entry_price': 50000}
        }
        strategy_manager.execution_engine.close_position = MagicMock()
        
        # Mock stale data for 200 seconds (exceeds 180s threshold for 15m)
        strategy_manager.market_api._symbol_last_tick = {'BTC': time.time() - 200}
        
        strategy_manager._handle_stale_data_for_symbol('BTC')
        
        # Should force close
        strategy_manager.execution_engine.close_position.assert_called_once()
        call_args = strategy_manager.execution_engine.close_position.call_args
        assert 'stale_data_15m_timeout' in call_args[1]['reason']

    def test_handle_stale_data_no_position(self, strategy_manager):
        """Test that handler does nothing when no position exists."""
        strategy_manager.execution_engine.positions = {}
        strategy_manager.execution_engine.close_position = MagicMock()
        
        strategy_manager._handle_stale_data_for_symbol('BTC')
        
        # Should not call close
        strategy_manager.execution_engine.close_position.assert_not_called()

    def test_per_strategy_weight_clamp(self, strategy_manager, mocker):
        """Ensure weights are clamped by per-strategy caps/floors."""
        cfg_caps = {
            "adaptive_grid_5m": {"min": 0.05, "max": 0.25},
            "csm_4h": {"min": 0.10, "max": 1.00},
        }
        strategy_manager.config["risk_management"]["strategy_weight_caps"] = cfg_caps
        # Mock selector and regime multiplier
        strategy_manager.strategy_selector.get_strategy_weight = mocker.Mock(return_value=0.8)
        strategy_manager._regime_allocator = mocker.Mock()
        strategy_manager._regime_result = {"regime": "range"}
        strategy_manager._regime_allocator.get_multiplier = mocker.Mock(return_value=2.0)  # doubles to 1.6

        # Clamped to max 0.25
        w_grid = strategy_manager._get_effective_strategy_weight("adaptive_grid_5m")
        assert abs(w_grid - 0.25) < 1e-9

        # Clamped to min 0.10 (base*mult=1.6 -> clamp to 1.0 cap, but min=0.10)
        w_csm = strategy_manager._get_effective_strategy_weight("csm_4h")
        assert abs(w_csm - 1.0) < 1e-9

    def test_cooldown_blocks_then_allows(self, strategy_manager, mocker):
        """Per-strategy cooldown prevents rapid re-entry."""
        strategy_manager.config["trading"]["strategy_cooldowns"] = {"adaptive_grid_15m": 300}
        strategy_manager._last_trade_ts_by_strategy["adaptive_grid_15m"] = datetime.now()

        signal = {"signal": "buy"}
        with patch.object(strategy_manager, "_resolve_conflict", return_value="proceed"), \
             patch.object(strategy_manager, "_calculate_market_volatility", return_value=0.2), \
             patch.object(strategy_manager, "_should_execute_with_position_limit", return_value=True), \
             patch.object(strategy_manager, "_count_positions_for_strategy", return_value=0), \
             patch.object(strategy_manager.leverage_manager, "calculate_leveraged_position_size", return_value=(1.0, 10.0, 1.0)), \
             patch.object(strategy_manager.leverage_manager, "can_open_position", return_value=True):
            res = strategy_manager._should_execute_signal("BTC", signal, 100.0, {}, "adaptive_grid_15m")
            assert res is False

        # Advance time beyond cooldown
        strategy_manager._last_trade_ts_by_strategy["adaptive_grid_15m"] = datetime.now() - timedelta(seconds=400)
        with patch.object(strategy_manager, "_resolve_conflict", return_value="proceed"), \
             patch.object(strategy_manager, "_calculate_market_volatility", return_value=0.2), \
             patch.object(strategy_manager, "_should_execute_with_position_limit", return_value=True), \
             patch.object(strategy_manager, "_count_positions_for_strategy", return_value=0), \
             patch.object(strategy_manager.leverage_manager, "calculate_leveraged_position_size", return_value=(1.0, 10.0, 1.0)), \
             patch.object(strategy_manager.leverage_manager, "can_open_position", return_value=True):
            res = strategy_manager._should_execute_signal("BTC", signal, 100.0, {}, "adaptive_grid_15m")
            assert res is True

    def test_pair_controls_blacklist_and_penalty(self, strategy_manager, mocker):
        """Blacklist skips; penalty scales signal strength."""
        strategy_manager.config["trading"]["pair_blacklist"] = ["BADPAIR"]
        strategy_manager.config["trading"]["pair_penalties"] = {"PENALTY": 0.5}
        signal = {"signal": "buy"}

        with patch.object(strategy_manager, "_resolve_conflict", return_value="proceed"), \
             patch.object(strategy_manager, "_calculate_market_volatility", return_value=0.2), \
             patch.object(strategy_manager, "_should_execute_with_position_limit", return_value=True), \
             patch.object(strategy_manager, "_count_positions_for_strategy", return_value=0), \
             patch.object(strategy_manager.leverage_manager, "calculate_leveraged_position_size", return_value=(1.0, 10.0, 1.0)), \
             patch.object(strategy_manager.leverage_manager, "can_open_position", return_value=True):
            # Blacklisted
            res = strategy_manager._should_execute_signal("BADPAIR", signal.copy(), 100.0, {}, "adaptive_grid_15m")
            assert res is False

            # Penalty scales strength; still allowed
            res2 = strategy_manager._should_execute_signal("PENALTY", signal.copy(), 100.0, {}, "adaptive_grid_15m")
            assert res2 is True

    def test_cost_hurdle_blocks_low_edge(self, strategy_manager, mocker):
        """Skip trades when expected edge is below hurdle."""
        strategy_manager.config["risk_management"]["cost_hurdles"] = {"adaptive_grid_15m": 6.0}
        strategy_manager.config["trading"]["pair_blacklist"] = []
        signal_low = {"signal": "buy", "expected_edge_bps": 4.0}
        signal_high = {"signal": "buy", "expected_edge_bps": 7.0}

        common_patches = dict(
            _resolve_conflict="proceed",
            _calculate_market_volatility=0.2,
            _should_execute_with_position_limit=True,
            _count_positions_for_strategy=0,
        )

        with patch.object(strategy_manager, "_resolve_conflict", return_value="proceed"), \
             patch.object(strategy_manager, "_calculate_market_volatility", return_value=common_patches["_calculate_market_volatility"]), \
             patch.object(strategy_manager, "_should_execute_with_position_limit", return_value=common_patches["_should_execute_with_position_limit"]), \
             patch.object(strategy_manager, "_count_positions_for_strategy", return_value=common_patches["_count_positions_for_strategy"]), \
             patch.object(strategy_manager.leverage_manager, "calculate_leveraged_position_size", return_value=(1.0, 10.0, 1.0)), \
             patch.object(strategy_manager.leverage_manager, "can_open_position", return_value=True):
            res_low = strategy_manager._should_execute_signal("BTC", signal_low.copy(), 100.0, {}, "adaptive_grid_15m")
            assert res_low is False

        with patch.object(strategy_manager, "_resolve_conflict", return_value="proceed"), \
             patch.object(strategy_manager, "_calculate_market_volatility", return_value=common_patches["_calculate_market_volatility"]), \
             patch.object(strategy_manager, "_should_execute_with_position_limit", return_value=common_patches["_should_execute_with_position_limit"]), \
             patch.object(strategy_manager, "_count_positions_for_strategy", return_value=common_patches["_count_positions_for_strategy"]), \
             patch.object(strategy_manager.leverage_manager, "calculate_leveraged_position_size", return_value=(1.0, 10.0, 1.0)), \
             patch.object(strategy_manager.leverage_manager, "can_open_position", return_value=True):
            res_high = strategy_manager._should_execute_signal("BTC", signal_high.copy(), 100.0, {}, "adaptive_grid_15m")
            assert res_high is True

    def test_run_trading_cycle_prioritizes_positions(self, strategy_manager):
        """Test that symbols with open positions are analyzed first."""
        # Setup: Track the order of symbol analysis
        analyzed_symbols = []
        original_analyze = strategy_manager._analyze_symbol
        
        def mock_analyze(symbol, **kwargs):
            analyzed_symbols.append(symbol)
        
        strategy_manager._analyze_symbol = mock_analyze
        strategy_manager.is_running = True
        
        # Setup positions for SOL and ETH
        strategy_manager.execution_engine.positions = {
            'SOL': MagicMock(strategy='csm_4h'),
            'ETH': MagicMock(strategy='ou_mean_reversion_1h'),
        }
        
        # Mock pair_selector to return mixed order (positions NOT first)
        strategy_manager.pair_selector.get_current_pairs = MagicMock(
            return_value=['BTC', 'SOL', 'DOGE', 'ETH', 'XRP']
        )
        
        # Mock other dependencies to allow cycle to run
        strategy_manager._maybe_update_regime_and_changepoint = MagicMock()
        strategy_manager.sync_positions_with_exchange = MagicMock()
        strategy_manager._check_liquidation_risks = MagicMock()
        strategy_manager._monitor_and_close_positions = MagicMock()
        strategy_manager._monitor_pending_orders = MagicMock()
        strategy_manager._cleanup_stale_orders = MagicMock()
        strategy_manager.correlation_manager = MagicMock()
        strategy_manager.correlation_manager.should_update = MagicMock(return_value=False)
        strategy_manager.update_position_prices = MagicMock()
        strategy_manager.display_positions_pnl = MagicMock()
        strategy_manager.last_position_sync = 0
        strategy_manager.last_position_monitoring = 0
        strategy_manager.last_performance_report = 0
        
        # Run the trading cycle
        strategy_manager.run_trading_cycle()
        
        # Verify: Position symbols (SOL, ETH) should be in first two positions
        assert set(analyzed_symbols[:2]) == {'SOL', 'ETH'}, \
            f"Position symbols should be first, but got {analyzed_symbols}"
        # Verify: Non-position symbols should follow
        assert set(analyzed_symbols[2:]) == {'BTC', 'DOGE', 'XRP'}, \
            f"Other symbols should follow, but got {analyzed_symbols}"

    def test_count_positions_for_strategy(self, strategy_manager):
        """Test that _count_positions_for_strategy correctly counts positions."""
        # Setup positions with different strategies (use execution_engine.positions)
        strategy_manager.execution_engine.positions = {
            'SOL': MagicMock(strategy='csm_4h'),
            'ETH': MagicMock(strategy='csm_4h'),
            'BTC': MagicMock(strategy='ou_mean_reversion_1h'),
            'DOGE': MagicMock(strategy='csm_4h'),
            'XRP': MagicMock(strategy='vol_breakout_1h'),
        }
        
        # Test counting
        assert strategy_manager._count_positions_for_strategy('csm_4h') == 3
        assert strategy_manager._count_positions_for_strategy('ou_mean_reversion_1h') == 1
        assert strategy_manager._count_positions_for_strategy('vol_breakout_1h') == 1
        assert strategy_manager._count_positions_for_strategy('nonexistent') == 0

    def test_is_data_ready_for_symbol_nested_cache(self, strategy_manager):
        """
        Verify that _is_data_ready_for_symbol correctly handles the nested dictionary structure
        of ohlcv_cache (Symbol -> Timeframe -> DataFrame) and checks DataFrame length,
        not the number of keys in the inner dictionary.
        """
        import pandas as pd
        import numpy as np

        symbol = 'BTC/USD'
        required_timeframes = ['5m', '15m', '1h', '4h']

        # Setup mock cache
        # Create a DataFrame with enough rows (>= 20 to pass historical check)
        df_valid = pd.DataFrame(np.random.randn(25, 5), columns=['open', 'high', 'low', 'close', 'volume'])
        
        # Scenario: Cache exists but is a nested dict. 
        # Crucially, we manipulate the inner dict to have FEWER keys than the required length threshold (5)
        # to reproduce the bug conditions (if it were checking len(dict) < 5).
        # Inner dict has 4 keys (the timeframes), which is < 5.
        
        mock_cache = {}
        for tf in required_timeframes:
            mock_cache[tf] = df_valid
            
        # Set the cache on the market_api
        # Structure: { 'BTC/USD': { '5m': df, '1h': df, ... } }
        strategy_manager.market_api.ohlcv_cache = {symbol: mock_cache}
        
        # Mock get_ohlcv to return valid data so the historical check passes
        strategy_manager.market_api.get_ohlcv.return_value = df_valid
        
        # Mock WebSocket checks
        strategy_manager.market_api._subscribed_symbols = {symbol}
        # Assuming health_monitor might be accessed, verify checks pass or don't exist
        # If hasattr returns True, we need is_ws_data_fresh to return True
        # Since mock_market_api is a MagicMock, attributes exist by default
        strategy_manager.market_api.health_monitor.is_ws_data_fresh.return_value = True

        # Act
        is_ready = strategy_manager._is_data_ready_for_symbol(symbol)

        # Assert
        assert is_ready is True

        # Counter-test: Verify it fails if DataFrame is actually too short
        df_short = pd.DataFrame(np.random.randn(2, 5), columns=['open', 'high', 'low', 'close', 'volume'])
        mock_cache_short = {tf: df_short for tf in required_timeframes}
        strategy_manager.market_api.ohlcv_cache = {symbol: mock_cache_short}
        
        assert strategy_manager._is_data_ready_for_symbol(symbol) is False


