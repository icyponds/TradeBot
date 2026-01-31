"""
Trading Bot Dashboard

A real-time web dashboard for monitoring positions, strategies, and performance.

Usage:
    # Integrated with the bot (shares live data):
    from src.dashboard import run_dashboard
    run_dashboard(strategy_manager, port=5050)
"""

import os
import sys
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from threading import Thread

from flask import Flask, render_template, jsonify, request

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

logger = logging.getLogger(__name__)

# Global reference to strategy manager (set when integrated with bot)
_strategy_manager = None
_market_api = None


def create_dashboard_app() -> Flask:
    """Create and configure the Flask dashboard app."""
    
    template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    
    app = Flask(__name__, 
                template_folder=template_dir,
                static_folder=static_dir)
    
    app.config['SECRET_KEY'] = os.urandom(24)
    
    @app.route('/')
    def index():
        """Main dashboard page."""
        return render_template('dashboard.html')
    
    @app.route('/api/positions')
    def get_positions():
        """Get all open positions with current metrics."""
        try:
            positions_data = _get_positions_data()
            return jsonify({
                'success': True,
                'timestamp': datetime.now().isoformat(),
                'data': positions_data
            })
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
    
    @app.route('/api/summary')
    def get_summary():
        """Get account and performance summary."""
        try:
            timeframe = request.args.get('timeframe', '7d')
            logger.info(f"DASHBOARD: Summary request for timeframe='{timeframe}'")
            summary = _get_summary_data(timeframe)
            return jsonify({
                'success': True,
                'timestamp': datetime.now().isoformat(),
                'data': summary
            })
        except Exception as e:
            logger.error(f"Error getting summary: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
    
    @app.route('/api/strategies')
    def get_strategies():
        """Get strategy status and allocation."""
        try:
            strategies = _get_strategies_data()
            return jsonify({
                'success': True,
                'timestamp': datetime.now().isoformat(),
                'data': strategies
            })
        except Exception as e:
            logger.error(f"Error getting strategies: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    @app.route('/api/close_position', methods=['POST'])
    def close_position():
        """Close a specific position (single or multi-leg)."""
        if not _strategy_manager:
            return jsonify({
                'success': False,
                'error': 'Strategy manager not connected (readonly mode)'
            }), 400

        try:
            data = request.json
            identifier = data.get('identifier')
            pos_type = data.get('type')  # 'single' or 'multi'
            
            if not identifier or not pos_type:
                return jsonify({'success': False, 'error': 'Missing identifier or type'}), 400
                
            logger.info(f"Received close request for {pos_type} position: {identifier}")
            
            if pos_type == 'single':
                # Single-leg position (identifier = symbol)
                symbol = identifier
                if symbol not in _strategy_manager.positions:
                    return jsonify({'success': False, 'error': f'Position {symbol} not found'}), 404
                    
                success, message = _strategy_manager.close_position(symbol, reason="manual_dashboard")
                
                if success:
                    return jsonify({'success': True, 'message': f'Closed position {symbol}'})
                else:
                    return jsonify({'success': False, 'error': f'Failed: {message}'}), 500

            elif pos_type == 'multi':
                # Multi-leg position (identifier = position_id)
                pos_id = identifier
                if pos_id not in _strategy_manager.multi_leg_positions:
                     return jsonify({'success': False, 'error': f'Multi-leg position {pos_id} not found'}), 404
                     
                position = _strategy_manager.multi_leg_positions[pos_id]
                symbol = position.primary_symbol
                strategy_name = position.strategy
                
                # Construct exit signal
                signal = {
                    'action': 'exit',
                    'reason': 'manual_dashboard',
                    'urgency': 'high',
                    'is_multi_leg': True
                }
                
                # Fetch recent OHLCV for context (optional but good practice)
                ohlcv = {} # Can retrieve if strictly needed, but exit logic mainly needs symbol/size
                
                # Trigger the handler
                # Note: We access the internal handler. Ideally StrategyManager should expose a public 
                # wrapper, but consistent with our plan we rely on _handle_multi_leg_signal 
                # which validates and calls execution engine.
                
                # We need current_price... we can fetch it from API or pass 0 if exit doesn't strictly depend on it for 'market' close
                current_price = 0.0
                if _market_api:
                    current_price = _market_api.get_current_price(symbol) or 0.0

                _strategy_manager._handle_multi_leg_signal(
                    symbol, 
                    signal, 
                    current_price, 
                    strategy_name, 
                    ohlcv, 
                    timestamp=datetime.now()
                )
                
                return jsonify({'success': True, 'message': f'Initiated close for {pos_id}'})
                
            else:
                return jsonify({'success': False, 'error': 'Invalid position type'}), 400

        except Exception as e:
            logger.error(f"Error closing position: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
    
    @app.route('/api/trades')
    def get_trades():
        """Get recent trade history with pagination."""
        try:
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 25))
            timeframe = request.args.get('timeframe', '7d')
            
            result = _get_trades_data(page=page, limit=limit, timeframe=timeframe)
            return jsonify({
                'success': True,
                'timestamp': datetime.now().isoformat(),
                'data': result['trades'],
                'pagination': result['pagination']
            })
        except Exception as e:
            logger.error(f"Error getting trades: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
    
    @app.route('/api/health')
    def health_check():
        """Health check endpoint."""
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'bot_connected': _strategy_manager is not None
        })
    
    return app


def _get_positions_data() -> Dict[str, Any]:
    """Get position data from strategy manager or file."""
    single_leg = []
    multi_leg = []
    
    if _strategy_manager is not None:
        # Live data from strategy manager
        _strategy_manager.update_position_prices()
        
        # Single-leg positions
        for symbol, position in list(_strategy_manager.positions.items()):
            pos_data = _format_position(position)
            single_leg.append(pos_data)
        
        # Multi-leg positions (funding arb, etc.)
        for pos_id, multi_pos in list(_strategy_manager.multi_leg_positions.items()):
            multi_data = _format_multi_leg_position(multi_pos)
            multi_leg.append(multi_data)
    else:
        # Standalone mode: No strategy manager connected
        # In production, the dashboard always runs alongside the bot.
        # Return empty positions rather than reading from deprecated JSON file.
        pass
    
    # Calculate totals
    total_pnl = sum(p.get('unrealized_pnl', 0) or 0 for p in single_leg)
    total_pnl += sum(p.get('unrealized_pnl', 0) or 0 for p in multi_leg)
    
    total_notional = sum(
        (p.get('current_price', 0) or p.get('entry_price', 0)) * p.get('size', 0)
        for p in single_leg
    )
    
    return {
        'single_leg': single_leg,
        'multi_leg': multi_leg,
        'totals': {
            'position_count': len(single_leg) + len(multi_leg),
            'single_leg_count': len(single_leg),
            'multi_leg_count': len(multi_leg),
            'total_unrealized_pnl': total_pnl,
            'total_notional': total_notional,
        }
    }


def _format_holding_time(seconds: float) -> str:
    """Format holding time as days, hours, minutes."""
    days = int(seconds // 86400)
    remaining = seconds % 86400
    hours = int(remaining // 3600)
    remaining = remaining % 3600
    minutes = int(remaining // 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    
    return " ".join(parts)


def _format_position(position) -> Dict[str, Any]:
    """Format a Position object for the dashboard."""
    entry_time = position.entry_time
    if isinstance(entry_time, str):
        entry_time = datetime.fromisoformat(entry_time)
    
    holding_time = datetime.now() - entry_time
    hours = holding_time.total_seconds() / 3600
    
    notional = (position.current_price or position.entry_price) * position.size
    
    # Calculate leverage from notional / margin
    leverage = 1.0
    if position.capital_at_risk and position.capital_at_risk > 0:
        leverage = notional / position.capital_at_risk
    
    # Get trailing stop info
    trailing_active = getattr(position, 'trailing_stop_active', False)
    trailing_enabled = getattr(position, 'trailing_stop_enabled', False)
    highest_price = getattr(position, 'highest_price', None)
    lowest_price = getattr(position, 'lowest_price', None)
    
    # Get liquidation price and margin from cached position attributes (no API calls)
    liquidation_price = getattr(position, 'liquidation_price', None)
    margin_used = getattr(position, 'margin_used', None)
    
    return {
        'symbol': position.symbol,
        'side': position.side,
        'size': position.size,
        'entry_price': position.entry_price,
        'current_price': position.current_price,
        'stop_loss': position.stop_loss,
        'take_profit': position.take_profit,
        'strategy': position.strategy,
        'entry_time': entry_time.isoformat(),
        'holding_time': _format_holding_time(holding_time.total_seconds()),
        'holding_hours': hours,
        'notional': notional,
        'margin': margin_used or position.capital_at_risk,
        'leverage': round(leverage, 2),
        'liquidation_price': liquidation_price,
        'capital_at_risk': position.capital_at_risk,
        'unrealized_pnl': position.unrealized_pnl,
        'unrealized_pnl_pct': position.unrealized_pnl_percentage,
        'capital_pnl_pct': position.capital_at_risk_pnl_percentage,
        'trailing_stop_enabled': trailing_enabled,
        'trailing_stop_active': trailing_active,
        'highest_price': highest_price,
        'lowest_price': lowest_price,
    }


def _format_multi_leg_position(multi_pos) -> Dict[str, Any]:
    """Format a MultiLegPosition for the dashboard."""
    entry_time = multi_pos.entry_time
    if isinstance(entry_time, str):
        entry_time = datetime.fromisoformat(entry_time)
    
    holding_time = datetime.now() - entry_time
    hours = holding_time.total_seconds() / 3600
    
    legs_data = []
    total_net_pnl = 0.0
    
    for leg in multi_pos.legs:
        # Get current price
        current_price = _get_leg_current_price(leg.symbol, leg.market_type)
        
        # Calculate leg PnL
        leg_pnl = 0.0
        if current_price:
            if leg.side == 'long':
                leg_pnl = (current_price - leg.entry_price) * leg.size
            else:
                leg_pnl = (leg.entry_price - current_price) * leg.size
        
        total_net_pnl += leg_pnl

        # Get margin/liquidation info
        margins = None
        liquidation_price = None
        leg_margin_used = 0.0

        if _strategy_manager and _strategy_manager.market_api:
            try:
                if leg.market_type == 'spot':
                    # For spot, margin is effectively the full notional value
                    if current_price:
                        leg_margin_used = leg.size * current_price
                else:
                    # For perp, fetch actual margin used
                    margin_info = _strategy_manager.market_api.get_position_margin_info(leg.symbol)
                    if margin_info:
                        liquidation_price = margin_info.get('liquidation_price')
                        parts_margin = margin_info.get('margin_used', 0)
                        # If the API returns 0 (cross margin sometimes?), fallback to approximate
                        leg_margin_used = parts_margin if parts_margin > 0 else (current_price * leg.size / 10.0) # Assume 10x if missing
            except Exception as e:
                logger.debug(f"Error getting margin info for leg {leg.symbol}: {e}")

        legs_data.append({
            'symbol': leg.symbol,
            'market_type': leg.market_type,
            'side': leg.side,
            'size': leg.size,
            'entry_price': leg.entry_price,
            'current_price': current_price,
            'unrealized_pnl': leg_pnl,
            'is_spot': leg.market_type == 'spot',
            'margin': leg_margin_used,
            'liquidation_price': liquidation_price
        })
    
    # Get metadata (funding rate info, etc.)
    metadata = multi_pos.metadata or {}

    # Fallback for Capital at Risk if missing (sum of margins)
    capital_at_risk = multi_pos.capital_at_risk
    if not capital_at_risk or capital_at_risk == 0:
        # Sum margins from legs
        capital_at_risk = sum(l.get('margin', 0) for l in legs_data)
    
    return {
        'position_id': multi_pos.position_id,
        'strategy': multi_pos.strategy,
        'primary_symbol': multi_pos.primary_symbol,
        'entry_time': entry_time.isoformat(),
        'holding_time': _format_holding_time(holding_time.total_seconds()),
        'holding_hours': hours,
        'legs': legs_data,
        'leg_count': len(legs_data),
        'net_delta': multi_pos.net_delta,
        'capital_at_risk': capital_at_risk,
        # Prefer calculated net PnL, fallback to metadata
        'unrealized_pnl': total_net_pnl if total_net_pnl != 0 else metadata.get('unrealized_pnl'),
        'funding_collected': metadata.get('funding_collected', 0),
        'entry_funding_rate': metadata.get('entry_funding_rate'),
        'current_funding_rate': metadata.get('current_funding_rate'),
        'stop_loss': getattr(multi_pos, 'stop_loss', None),
        'take_profit': getattr(multi_pos, 'take_profit', None),
    }


from datetime import datetime, timedelta

def _get_start_time_for_frame(frame: str) -> Optional[datetime]:
    """Calculate start time for a given timeframe."""
    now = datetime.now()
    
    if frame == 'session':
        # Handled by caller or special case
        return None
    elif frame == '1d':
        return now - timedelta(days=1)
    elif frame == '7d':
        return now - timedelta(days=7)
    elif frame == '14d':
        return now - timedelta(days=14)
    elif frame == '30d':
        return now - timedelta(days=30)
    elif frame == '90d':
        return now - timedelta(days=90)
    elif frame == '365d':
        return now - timedelta(days=365)
    elif frame == 'ytd':
        return datetime(now.year, 1, 1)
    elif frame == 'lifetime':
        # Return a very old date to capture everything
        return datetime(2020, 1, 1)
    
    # Default fallback
    return now - timedelta(days=7)


def _get_summary_data(timeframe: str = '7d') -> Dict[str, Any]:
    """Get account summary data."""
    if _strategy_manager is not None:
        portfolio = _strategy_manager.portfolio_manager
        summary = portfolio.get_portfolio_summary()
        
        # Determine start time based on timeframe
        start_time = None
        if timeframe == 'session':
            start_time = getattr(_strategy_manager, "session_start_time", None)
            logger.info(f"DASHBOARD: Session start time resolved to: {start_time}")
        else:
            start_time = _get_start_time_for_frame(timeframe)
            logger.info(f"DASHBOARD: Timeframe '{timeframe}' resolved to start_time: {start_time}")

        # Get realized and unrealized PnL
        total_realized_pnl = _get_total_realized_pnl(start_time)
        logger.info(f"DASHBOARD: Realized PnL calculated: {total_realized_pnl}")

        total_unrealized_pnl = _get_total_unrealized_pnl()
        
        # Use API equity value directly (matches exchange UI "Total Balance")
        # The portfolio is updated frequently, so this is near-real-time
        snapshot_equity = summary.get('total_equity', 0)
        live_equity = snapshot_equity
        
        # Get trade stats
        trade_stats = _get_trade_stats(start_time)

        # Session start time (if available)
        session_start = getattr(_strategy_manager, "session_start_time", None)
        
        return {
            'account': {
                'total_equity': live_equity,
                'available_margin': summary.get('available_margin', 0),
                'used_margin': summary.get('used_margin', 0),
                'margin_usage_pct': summary.get('margin_usage_percentage', 0),
                'available_capital': summary.get('available_capital', 0),
            },
            'performance': {
                'total_trades': trade_stats.get('total_trades', 0),
                'winning_trades': trade_stats.get('winning_trades', 0),
                'losing_trades': trade_stats.get('losing_trades', 0),
                'win_rate': trade_stats.get('win_rate', 0),
                'session_start_time': session_start.isoformat() if session_start else None,
                'total_realized_pnl': total_realized_pnl,
                'total_unrealized_pnl': total_unrealized_pnl,
                'total_pnl': total_realized_pnl + total_unrealized_pnl,
                'timeframe': timeframe  # Echo back for UI
            },
            'bot_status': {
                'is_running': _strategy_manager.is_running,
                'selected_pairs': len(_strategy_manager.pair_selector.get_current_pairs(trigger_rescan=False)) if _strategy_manager.pair_selector else 0,
                'ready_pairs': len(_strategy_manager.pair_selector.get_ready_pairs()) if _strategy_manager.pair_selector else 0,
                'active_strategies': len([s for s in _strategy_manager.strategies.keys()]),
            }
        }
    else:
        return {
            'account': {},
            'performance': {},
            'bot_status': {'is_running': False}
        }


def _get_strategies_data() -> List[Dict[str, Any]]:
    """Get strategy status and allocations."""
    strategies = []
    
    if _strategy_manager is not None:
        selector = _strategy_manager.strategy_selector
        perf_tracker = getattr(_strategy_manager, 'performance_tracker', None)
        
        for name, strategy in _strategy_manager.strategies.items():
            ranking = selector.strategy_rankings.get(name) if selector else None
            
            # Get actual trade metrics from the database
            trade_count = 0
            total_pnl = 0.0
            win_rate = 0.0
            
            if perf_tracker and hasattr(perf_tracker, 'db'):
                try:
                    # Get strategy-specific stats from the database
                    strategy_stats = perf_tracker.db.get_strategy_stats(name)
                    trade_count = strategy_stats.get('total_trades', 0) or 0
                    total_pnl = strategy_stats.get('total_pnl', 0) or 0.0
                    # Calculate win rate from winning/total trades
                    winning_trades = strategy_stats.get('winning_trades', 0) or 0
                    if trade_count > 0:
                        win_rate = (winning_trades / trade_count) * 100
                except Exception as e:
                    logger.debug(f"Could not get stats for {name}: {e}")
            
            strategies.append({
                'name': name,
                'enabled': selector.is_strategy_enabled(name) if selector else True,
                'weight': ranking.weight if ranking else 1.0,
                'effective_weight': _strategy_manager._get_effective_strategy_weight(name) if hasattr(_strategy_manager, '_get_effective_strategy_weight') else (ranking.weight if ranking else 1.0),
                'recent_pnl': total_pnl,
                'sharpe_ratio': 0,  # Could calculate from trades if needed
                'win_rate': win_rate,
                'trade_count': trade_count,
                'trade_confidence': (ranking.metrics.get('trade_confidence') if ranking and ranking.metrics else None),
                'adj_win_rate': (ranking.metrics.get('adj_win_rate') if ranking and ranking.metrics else None),
                'adj_profit_factor': (ranking.metrics.get('adj_profit_factor') if ranking and ranking.metrics else None),
                'on_probation': selector.on_probation.get(name, False) if selector and hasattr(selector, 'on_probation') else False,
                'in_cooling_off': name in selector.cooling_off_until if selector else False,
            })
    
    return strategies


def _get_current_prices(symbols: List[str]) -> Dict[str, float]:
    """Get current prices from local cache only (no API calls)."""
    prices = {}
    if _market_api is not None:
        for symbol in symbols:
            try:
                price = _market_api.get_cached_price(symbol)
                if price:
                    prices[symbol] = price
            except Exception as e:
                logger.debug(f"Error getting cached price for {symbol}: {e}")
    return prices


def _calculate_pnl(pos: Dict) -> Optional[float]:
    """Calculate unrealized PnL."""
    current = pos.get('current_price')
    entry = pos.get('entry_price')
    size = pos.get('size', 0)
    side = pos.get('side', 'long')
    
    if not current or not entry:
        return None
    
    if side == 'long':
        return (current - entry) * size
    else:
        return (entry - current) * size


def _get_leg_current_price(symbol: str, market_type: str) -> Optional[float]:
    """Get current price for a leg from cache (no API calls)."""
    if _market_api is None:
        return None
        
    try:
        if market_type == 'spot':
            # For spot, try the API name first
            base = symbol.split('/')[0] if '/' in symbol else symbol
            spot_api_name = _market_api.get_spot_api_name(base)
            if spot_api_name:
                return _market_api.get_cached_price(spot_api_name)
            return _market_api.get_cached_price(symbol)
        else:
            # Perp/Hip3
            return _market_api.get_cached_price(symbol)
            
    except Exception as e:
        logger.debug(f"Error getting cached price for {symbol}: {e}")
        return None


def _calculate_pnl_pct(pos: Dict) -> Optional[float]:
    """Calculate unrealized PnL percentage."""
    current = pos.get('current_price')
    entry = pos.get('entry_price')
    side = pos.get('side', 'long')
    
    if not current or not entry:
        return None
    
    if side == 'long':
        return ((current - entry) / entry) * 100
    else:
        return ((entry - current) / entry) * 100


def _calculate_holding_time(entry_time_str: Optional[str]) -> str:
    """Calculate holding time from entry time string."""
    if not entry_time_str:
        return "?"
    
    try:
        entry_time = datetime.fromisoformat(entry_time_str)
        holding = datetime.now() - entry_time
        return _format_holding_time(holding.total_seconds())
    except Exception:
        return "?"


def _get_win_rate() -> float:
    """Get overall win rate from trade database."""
    if _strategy_manager is not None and hasattr(_strategy_manager, 'performance_tracker'):
        try:
            start_time = getattr(_strategy_manager, "session_start_time", None)
            if start_time:
                trades = _strategy_manager.performance_tracker.db.get_trades_in_range(start_time, datetime.now())
            else:
                # Fallback: last 7 days (TradeDatabase.get_recent_trades takes days, not limit)
                trades = _strategy_manager.performance_tracker.db.get_recent_trades(7)
            if trades:
                wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
                return (wins / len(trades)) * 100
        except Exception as e:
            logger.debug(f"Error getting win rate: {e}")
    return 0


def _get_total_realized_pnl(start_time: Optional[datetime] = None) -> float:
    """Get realized PnL since start_time."""
    if _strategy_manager is not None and hasattr(_strategy_manager, 'performance_tracker'):
        try:
            # If no start time (e.g. session requested but not set), fallback to recent
            if not start_time:
                # Default logic if session has n/a start time
                 start_time = getattr(_strategy_manager, "session_start_time", None)
            
            if not start_time:
                 # Last resort fallback
                 logger.warning("DASHBOARD: No start_time available for PnL, falling back to 7d recent trades")
                 trades = _strategy_manager.performance_tracker.db.get_recent_trades(7)
                 return sum(t.get('pnl', 0) or 0 for t in trades)

            trades = _strategy_manager.performance_tracker.db.get_trades_in_range(start_time, datetime.now())
            logger.info(f"DASHBOARD: Found {len(trades)} trades since {start_time}")
            return sum(t.get('pnl', 0) or 0 for t in trades)
        except Exception as e:
            logger.debug(f"Error getting total realized PnL: {e}")
    return 0


def _get_total_unrealized_pnl() -> float:
    """Get total unrealized PnL from position objects (no API calls)."""
    total = 0.0
    if _strategy_manager is not None:
        # Single-leg: use pre-calculated PnL
        for symbol, position in _strategy_manager.positions.items():
            if position.unrealized_pnl is not None:
                total += position.unrealized_pnl
        
        # Multi-leg: use pre-calculated PnL from position object or cached leg prices
        for pos_id, multi_pos in _strategy_manager.multi_leg_positions.items():
            # First try the position's own unrealized_pnl if available
            if hasattr(multi_pos, 'unrealized_pnl') and multi_pos.unrealized_pnl is not None:
                total += multi_pos.unrealized_pnl
            else:
                # Fallback: calculate from cached prices (still no API calls)
                for leg in multi_pos.legs:
                    current_price = getattr(leg, 'current_price', None) or _get_leg_current_price(leg.symbol, leg.market_type)
                    if current_price:
                        if leg.side == 'long':
                            total += (current_price - leg.entry_price) * leg.size
                        else:
                            total += (leg.entry_price - current_price) * leg.size
    return total


def _get_trade_stats(start_time: Optional[datetime] = None) -> Dict[str, Any]:
    """Get comprehensive trade statistics since start_time."""
    stats = {
        'total_trades': 0,
        'winning_trades': 0,
        'losing_trades': 0,
        'win_rate': 0.0,
    }
    
    if _strategy_manager is not None and hasattr(_strategy_manager, 'performance_tracker'):
        try:
            if not start_time:
                 start_time = getattr(_strategy_manager, "session_start_time", None)
            
            if not start_time:
                trades = _strategy_manager.performance_tracker.db.get_recent_trades(7)
                logger.info(f"DASHBOARD: WinRate Fallback to 7d (No start_time): {len(trades)} trades")
            else:
                trades = _strategy_manager.performance_tracker.db.get_trades_in_range(start_time, datetime.now())
                logger.info(f"DASHBOARD: WinRate Trades since {start_time}: {len(trades)} trades")
                
            if trades:
                stats['total_trades'] = len(trades)
                stats['winning_trades'] = sum(1 for t in trades if (t.get('pnl') or 0) > 0)
                stats['losing_trades'] = sum(1 for t in trades if (t.get('pnl') or 0) < 0)
                if stats['total_trades'] > 0:
                    stats['win_rate'] = (stats['winning_trades'] / stats['total_trades']) * 100
        except Exception as e:
            logger.debug(f"Error getting trade stats: {e}")
    
    return stats


def _get_trades_data(page: int = 1, limit: int = 25, timeframe: str = '7d') -> Dict[str, Any]:
    """Get recent trade history with pagination, filtered by timeframe."""
    trades = []
    total_count = 0
    total_pages = 0
    
    if _strategy_manager is not None and hasattr(_strategy_manager, 'performance_tracker'):
        try:
            db = _strategy_manager.performance_tracker.db
            
            # Get start time for the timeframe filter
            start_time = _get_start_time_for_frame(timeframe)
            
            # Get trades in range (or all trades if no start time)
            if start_time:
                raw_trades = db.get_trades_in_range(start_time, datetime.now())
            else:
                raw_trades = db.get_all_trades(limit=1000, offset=0)  # Fallback
            
            # Apply pagination to filtered results
            total_count = len(raw_trades)
            total_pages = (total_count + limit - 1) // limit if limit > 0 else 1
            offset = (page - 1) * limit
            paginated_trades = raw_trades[offset:offset + limit]

            for trade in paginated_trades:
                # Format trade data for display
                exit_time = trade.get('exit_time') or trade.get('timestamp')
                entry_time = trade.get('entry_time')
                
                # Calculate holding time if both times are available
                holding_time = None
                if entry_time and exit_time:
                    try:
                        if isinstance(entry_time, str):
                            entry_dt = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
                        else:
                            entry_dt = entry_time
                        if isinstance(exit_time, str):
                            exit_dt = datetime.fromisoformat(exit_time.replace('Z', '+00:00'))
                        else:
                            exit_dt = exit_time
                        holding_seconds = (exit_dt - entry_dt).total_seconds()
                        holding_hours = holding_seconds / 3600
                        holding_time = _format_holding_time(holding_seconds)
                    except Exception as e:
                        logger.debug(f"Error calculating holding time for trade {trade.get('symbol')}: {e}")
                
                trades.append({
                    'symbol': trade.get('symbol', 'Unknown'),
                    'side': trade.get('side', 'unknown'),
                    'strategy': trade.get('strategy', 'unknown'),
                    'entry_price': trade.get('entry_price'),
                    'exit_price': trade.get('exit_price'),
                    'size': trade.get('size'),
                    'realized_pnl': trade.get('pnl', 0),  # DB uses 'pnl' not 'realized_pnl'
                    'realized_pnl_pct': trade.get('pnl_percentage'),
                    'exit_reason': trade.get('exit_reason', 'unknown'),
                    'entry_time': entry_time,
                    'exit_time': exit_time,
                    'holding_time': holding_time,
                })
        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
    
    # Sort by exit time (most recent first) - though DB usually handles ordering
    # trades.sort(key=lambda x: x.get('exit_time') or '', reverse=True)
    
    return {
        'trades': trades,
        'pagination': {
            'page': page,
            'limit': limit,
            'total_trades': total_count,
            'total_pages': total_pages, 
            'has_next': page < total_pages,
            'has_prev': page > 1
        }
    }


def run_dashboard(strategy_manager=None, market_api=None, port: int = 5050, debug: bool = False):
    """
    Run the dashboard server.
    
    Args:
        strategy_manager: Optional StrategyManager instance for live data
        market_api: Optional HyperliquidAPI instance for price data
        port: Port to run on (default 5050)
        debug: Enable Flask debug mode
    """
    global _strategy_manager, _market_api
    _strategy_manager = strategy_manager
    _market_api = market_api or (strategy_manager.market_api if strategy_manager else None)
    
    app = create_dashboard_app()
    
    # Run in a separate thread if integrated with bot
    if strategy_manager is not None:
        thread = Thread(target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False))
        thread.daemon = True
        thread.start()
        logger.info(f"Dashboard started at http://localhost:{port}")
        return thread
    else:
        # Standalone mode
        print(f"\n{'='*60}")
        print(f"TradeBot Dashboard")
        print(f"{'='*60}")
        print(f"Open in browser: http://localhost:{port}")
        print(f"Press Ctrl+C to stop")
        print(f"{'='*60}\n")
        app.run(host='0.0.0.0', port=port, debug=debug)


if __name__ == '__main__':
    # Standalone mode - read from files
    from src.config.settings import load_config
    from src.api.hyperliquid_api import HyperliquidAPI
    
    config = load_config()
    if config:
        _market_api = HyperliquidAPI(config)
    
    run_dashboard(port=5050, debug=True)

