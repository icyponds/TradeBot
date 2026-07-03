"""
Unified Hyperliquid API client using the official SDK with built-in WebSocket support.

Features:
- Single API for REST and real-time WebSocket data
- Rate limiting with configurable limits
- Automatic retry with exponential backoff
- Request caching with TTL
- Circuit breaker for resilience
- Proper order status tracking
"""

import logging
import time
import threading
import math
from typing import Dict, List, Optional, Any, Callable, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict, deque
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
import pandas as pd
from src.utils.trade_database import TradeDatabase

# Lazy imports for SDK modules - these are expensive and should only be
# imported when actually needed. This speeds up test setup significantly.
# The actual imports happen in _init_sdk_clients() and _get_account_class()
if TYPE_CHECKING:
    from hyperliquid.info import Info
    from src.utils.market_data_repair import MarketDataRepairer
    from hyperliquid.exchange import Exchange
    from eth_account import Account

from .interface import MarketInterface

# =============================================================================
# RATE LIMITER
# =============================================================================

class RateLimiter:
    """
    Token bucket rate limiter for API calls.

    Tokens are denominated in Hyperliquid request-weight units. The REST API
    allows 1200 weight/minute per IP (cheap info requests like allMids and
    clearinghouseState cost 2, most other info requests cost 20).
    `calls_per_second` is the refill rate in weight units per second and
    `burst_size` is the bucket capacity in weight units.
    """

    def __init__(self, calls_per_second: float = 15, burst_size: int = 60):
        """
        Initialize rate limiter.

        Args:
            calls_per_second: Sustained refill rate (weight units per second)
            burst_size: Maximum burst capacity (weight units)
        """
        self.calls_per_second = calls_per_second
        self.burst_size = burst_size
        self.tokens = burst_size
        self.last_update = time.time()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0, weight: float = 1.0) -> bool:
        """
        Acquire `weight` tokens, blocking if necessary.

        Args:
            timeout: Maximum time to wait for tokens
            weight: Request weight to consume (Hyperliquid weight units)

        Returns:
            True if tokens acquired, False if timeout
        """
        # A weight larger than the bucket could never be satisfied
        weight = min(weight, self.burst_size)
        deadline = time.time() + timeout

        while True:
            with self._lock:
                self._refill()
                if self.tokens >= weight:
                    self.tokens -= weight
                    return True
                # Time until enough tokens accumulate (other threads may consume
                # in the meantime, so we re-check after sleeping)
                wait = (weight - self.tokens) / self.calls_per_second

            now = time.time()
            if now >= deadline:
                return False
            time.sleep(min(wait, deadline - now, 0.5))

    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_update
        self.tokens = min(self.burst_size, self.tokens + elapsed * self.calls_per_second)
        self.last_update = now


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """Circuit breaker for API resilience."""
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3
    ):
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Failures before opening circuit
            recovery_timeout: Seconds before trying half-open
            half_open_max_calls: Test calls in half-open state
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        self.half_open_calls = 0
        self._lock = threading.Lock()
        self.logger = logging.getLogger(__name__)
    
    def can_execute(self) -> bool:
        """Check if request can be executed."""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            
            if self.state == CircuitState.OPEN:
                # Check if recovery timeout passed
                if self.last_failure_time and \
                   time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    self.logger.info("Circuit breaker: OPEN -> HALF_OPEN")
                    return True
                return False
            
            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls < self.half_open_max_calls:
                    self.half_open_calls += 1
                    return True
                return False
            
            return False
    
    def record_success(self):
        """Record a successful call."""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.logger.info("Circuit breaker: HALF_OPEN -> CLOSED (recovered)")
            elif self.state == CircuitState.CLOSED:
                self.failure_count = max(0, self.failure_count - 1)
    
    def record_failure(self):
        """Record a failed call."""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.logger.warning("Circuit breaker: HALF_OPEN -> OPEN (failed)")
            elif self.state == CircuitState.CLOSED and \
                 self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.logger.warning(f"Circuit breaker: CLOSED -> OPEN (failures: {self.failure_count})")


# =============================================================================
# CACHE WITH TTL
# =============================================================================

@dataclass
class CacheEntry:
    """Cache entry with TTL."""
    value: Any
    expires_at: float
    
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class TTLCache:
    """Thread-safe cache with time-to-live."""
    
    def __init__(self, default_ttl: float = 1.0):
        """
        Initialize cache.
        
        Args:
            default_ttl: Default TTL in seconds
        """
        self.default_ttl = default_ttl
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        with self._lock:
            entry = self._cache.get(key)
            if entry and not entry.is_expired():
                return entry.value
            elif entry:
                del self._cache[key]
            return None
    
    def set(self, key: str, value: Any, ttl: Optional[float] = None):
        """Set value in cache with TTL."""
        with self._lock:
            expires_at = time.time() + (ttl if ttl is not None else self.default_ttl)
            self._cache[key] = CacheEntry(value=value, expires_at=expires_at)
    
    def invalidate(self, key: str):
        """Remove entry from cache."""
        with self._lock:
            self._cache.pop(key, None)
    
    def clear(self):
        """Clear all entries."""
        with self._lock:
            self._cache.clear()
    
    def cleanup(self):
        """Remove expired entries."""
        with self._lock:
            expired = [k for k, v in self._cache.items() if v.is_expired()]
            for k in expired:
                del self._cache[k]


class OhlcvCache:
    """
    In-memory rolling OHLCV cache per symbol/timeframe.
    Supports seeding from API fetch and incremental updates from ticks.
    """
    def __init__(self):
        self.cache = defaultdict(lambda: defaultdict(lambda: deque()))
        self.timeframe_seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
        }
        self.maxlen = defaultdict(lambda: defaultdict(lambda: 300))
        # Track last seeded message time to prevent duplicate callbacks on startup
        self.last_seeded_keys = defaultdict(lambda: defaultdict(lambda: None))
        # Callback for when a bar is complete (boundary crossed)
        self.on_bar_complete_callback: Optional[Callable] = None
    
    def seed(self, symbol: str, timeframe: str, bars: list, maxlen: int = 300):
        if timeframe not in self.timeframe_seconds:
            return
        dq = deque(maxlen=maxlen)
        for bar in bars:
            dq.append(bar)
        self.cache[symbol][timeframe] = dq
        self.maxlen[symbol][timeframe] = maxlen
        
        # Record the timestamp of the last seeded bar
        # CRITICAL: Only set this on the FIRST seed (startup/initialization).
        # Subsequent seeds (e.g. from PairSelector or Repairer) should NOT suppress callbacks
        # for new bars, otherwise we lose live persistence.
        if bars:
            last_bar = bars[-1]
            if isinstance(last_bar, dict) and 'time' in last_bar:
                current_seed = self.last_seeded_keys[symbol][timeframe]
                if current_seed is None:
                    self.last_seeded_keys[symbol][timeframe] = last_bar['time']
    
    def get(self, symbol: str, timeframe: str):
        if timeframe not in self.cache.get(symbol, {}):
            return None
        return list(self.cache[symbol][timeframe])
    
    def ensure_timeframe(self, symbol: str, timeframe: str, maxlen: int):
        # Initialize if not exists
        if timeframe not in self.cache[symbol]:
            self.cache[symbol][timeframe] = deque(maxlen=maxlen)
        else:
            # Check if maxlen needs update (deque.maxlen is read-only)
            dq = self.cache[symbol][timeframe]
            if dq.maxlen != maxlen:
                new_dq = deque(dq, maxlen=maxlen)
                self.cache[symbol][timeframe] = new_dq
        
        self.maxlen[symbol][timeframe] = maxlen
    
    def _get_bar_key(self, ts: float, timeframe: str) -> Optional[float]:
        secs = self.timeframe_seconds.get(timeframe)
        if not secs:
            return None
        return math.floor(ts / secs) * secs
    
    def update_from_tick(self, symbol: str, price: float, volume: float, ts: float):
        if symbol not in self.cache:
            return
            
        for timeframe, dq in self.cache[symbol].items():
            self._update_bar_for_timeframe(symbol, timeframe, dq, price, volume, ts)
    
    def _update_bar_for_timeframe(self, symbol: str, timeframe: str, dq: deque, price: float, volume: float, ts: float):
        key = self._get_bar_key(ts, timeframe)
        if key is None:
            return
            
        if dq and dq[-1].get("time") == key:
            bar = dq[-1]
        else:
            # BOUNDARY CROSSED: Previous bar is complete
            if dq and self.on_bar_complete_callback:
                completed_bar = dq[-1]
                
                # Check if this bar was just seeded (prevent optimistic write loop)
                seed_key = self.last_seeded_keys[symbol][timeframe]
                should_skip = (seed_key is not None and completed_bar.get("time") == seed_key)
                
                if not should_skip:
                    try:
                        self.on_bar_complete_callback(symbol, timeframe, completed_bar)
                    except Exception as e:
                        logging.getLogger(__name__).error(f"Callback error in cache: {e}")
            
            # Create new bar
            bar = {"time": key, "open": price, "high": price, "low": price, "close": price, "volume": 0.0}
            dq.append(bar)
        bar["close"] = price
        bar["high"] = max(bar["high"], price)
        bar["low"] = min(bar["low"], price)
        bar["volume"] = bar.get("volume", 0.0) + (volume or 0.0)


# =============================================================================
# ORDER TRACKER
# =============================================================================

@dataclass
class TrackedOrder:
    """Tracked order with status history."""
    order_id: int
    symbol: str
    side: str
    size: float
    price: Optional[float]
    status: str
    created_at: datetime
    updated_at: datetime
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    
    def is_terminal(self) -> bool:
        return self.status in ('filled', 'cancelled', 'rejected', 'expired')


class OrderTracker:
    """Track order status and history."""
    
    def __init__(self):
        self.orders: Dict[int, TrackedOrder] = {}
        self._lock = threading.Lock()
        self.logger = logging.getLogger(__name__)
    
    def track(self, order: TrackedOrder):
        """Start tracking an order."""
        with self._lock:
            self.orders[order.order_id] = order
    
    def update(self, order_id: int, **kwargs):
        """Update order status."""
        with self._lock:
            if order_id in self.orders:
                order = self.orders[order_id]
                for key, value in kwargs.items():
                    if hasattr(order, key):
                        setattr(order, key, value)
                order.updated_at = datetime.now()
    
    def get(self, order_id: int) -> Optional[TrackedOrder]:
        """Get tracked order."""
        with self._lock:
            return self.orders.get(order_id)
    
    def get_open_orders(self) -> List[TrackedOrder]:
        """Get all non-terminal orders."""
        with self._lock:
            return [o for o in self.orders.values() if not o.is_terminal()]
    
    def cleanup_old(self, max_age_hours: int = 24):
        """Remove old terminal orders."""
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        with self._lock:
            old_ids = [
                oid for oid, order in self.orders.items()
                if order.is_terminal() and order.updated_at < cutoff
            ]
            for oid in old_ids:
                del self.orders[oid]


# =============================================================================
# CONNECTION HEALTH MONITOR
# =============================================================================

class HealthStatus(Enum):
    """Health status levels."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    status: HealthStatus
    timestamp: datetime
    latency_ms: float
    details: Dict[str, Any] = field(default_factory=dict)


class ConnectionHealthMonitor:
    """
    Monitors API connection health with periodic checks.
    
    Features:
    - Periodic health checks (configurable interval)
    - WebSocket data freshness monitoring
    - REST API latency tracking
    - Automatic status degradation/recovery
    - Health callbacks for alerting
    """
    
    def __init__(
        self,
        check_interval: float = 30.0,
        unhealthy_threshold: int = 3,
        ws_stale_threshold: float = 10.0,
        latency_warning_ms: float = 1000.0
    ):
        """
        Initialize health monitor.
        
        Args:
            check_interval: Seconds between health checks
            unhealthy_threshold: Consecutive failures before unhealthy
            ws_stale_threshold: Seconds before WebSocket data considered stale
            latency_warning_ms: Latency threshold for degraded status
        """
        self.check_interval = check_interval
        self.unhealthy_threshold = unhealthy_threshold
        self.ws_stale_threshold = ws_stale_threshold
        self.latency_warning_ms = latency_warning_ms
        
        # State
        self.status = HealthStatus.UNKNOWN
        self.consecutive_failures = 0
        self.last_check: Optional[HealthCheckResult] = None
        self.check_history: deque = deque(maxlen=100)
        
        # WebSocket tracking
        self.ws_last_message_time: Optional[float] = None
        self.ws_message_count = 0
        
        # REST latency tracking
        self.latency_history: deque = deque(maxlen=50)
        
        # WebSocket reconnection tracking
        self._ws_stale_since: Optional[float] = None  # When WS first became stale
        self._reconnect_requested = False             # Flag to request reconnection
        self.ws_reconnect_threshold = 60.0            # Seconds before triggering reconnect
        
        # Threading
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()  # Reentrant lock to allow nested acquisitions
        self._api = None  # Set when attached to API
        
        # Callbacks
        self._status_callbacks: List[Callable[[HealthStatus, HealthStatus], None]] = []
        
        self.logger = logging.getLogger(__name__)
    
    def attach(self, api: 'HyperliquidAPI'):
        """Attach to an API instance."""
        self._api = api
    
    def start(self):
        """Start the health monitoring thread."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        self.logger.info("Health monitor started")
    
    def stop(self):
        """Stop the health monitoring thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        self.logger.info("Health monitor stopped")
    
    def add_status_callback(self, callback: Callable[[HealthStatus, HealthStatus], None]):
        """Add callback for status changes. Callback receives (old_status, new_status)."""
        self._status_callbacks.append(callback)
    
    def record_ws_message(self):
        """Record that a WebSocket message was received."""
        with self._lock:
            self.ws_last_message_time = time.time()
            self.ws_message_count += 1
    
    def record_rest_latency(self, latency_ms: float):
        """Record REST API call latency."""
        with self._lock:
            self.latency_history.append(latency_ms)
    
    def is_healthy(self) -> bool:
        """Quick check if API is healthy."""
        return self.status == HealthStatus.HEALTHY
    
    def is_ws_connected(self) -> bool:
        """Check if WebSocket is receiving data."""
        with self._lock:
            if self.ws_last_message_time is None:
                return False
            return time.time() - self.ws_last_message_time < self.ws_stale_threshold
    
    def is_ws_data_fresh(self, symbol: str = None) -> bool:
        """
        Check if WebSocket data is fresh.
        
        Args:
            symbol: If provided, check freshness for this specific symbol.
                   If None, check global connection freshness.
        
        Returns:
            True if data is fresh (within threshold), False otherwise.
        """
        if symbol is None:
            return self.is_ws_connected()
        
        # Per-symbol freshness check
        # Access the API's per-symbol tracking if available
        if self._api and hasattr(self._api, '_symbol_last_tick'):
            with self._lock:
                last_tick = self._api._symbol_last_tick.get(symbol)
                if last_tick is None:
                    return False
                return time.time() - last_tick < self.ws_stale_threshold
        
        # Fallback to global check
        return self.is_ws_connected()
    
    def check_and_request_reconnect(self) -> bool:
        """
        Check if WebSocket is stale long enough to warrant reconnection.
        
        Returns:
            True if reconnection was requested, False otherwise.
        """
        with self._lock:
            if not self.is_ws_connected():
                if self._ws_stale_since is None:
                    self._ws_stale_since = time.time()
                    self.logger.debug("WebSocket became stale, starting timer")
                else:
                    stale_duration = time.time() - self._ws_stale_since
                    if stale_duration > self.ws_reconnect_threshold:
                        if not self._reconnect_requested:
                            self._reconnect_requested = True
                            self.logger.warning(
                                f"WebSocket stale for {stale_duration:.0f}s (threshold: {self.ws_reconnect_threshold}s), "
                                f"requesting reconnect"
                            )
                        return True
            else:
                # WebSocket is fresh - reset tracking
                if self._ws_stale_since is not None:
                    self.logger.info("WebSocket connection restored")
                self._ws_stale_since = None
                self._reconnect_requested = False
            return False
    
    def get_avg_latency_ms(self) -> float:
        """Get average REST latency in milliseconds."""
        with self._lock:
            if not self.latency_history:
                return 0.0
            return sum(self.latency_history) / len(self.latency_history)
    
    def get_health_summary(self) -> Dict[str, Any]:
        """Get comprehensive health summary."""
        with self._lock:
            ws_age = None
            if self.ws_last_message_time:
                ws_age = time.time() - self.ws_last_message_time
            
            return {
                'status': self.status.value,
                'consecutive_failures': self.consecutive_failures,
                'last_check': self.last_check.timestamp.isoformat() if self.last_check else None,
                'last_check_latency_ms': self.last_check.latency_ms if self.last_check else None,
                'websocket': {
                    'connected': self.is_ws_connected(),
                    'last_message_age_seconds': ws_age,
                    'message_count': self.ws_message_count,
                },
                'rest': {
                    'avg_latency_ms': self.get_avg_latency_ms(),
                    'latency_samples': len(self.latency_history),
                },
                'checks_in_history': len(self.check_history),
            }
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                result = self._perform_health_check()
                self._process_check_result(result)
            except Exception as e:
                self.logger.error(f"Health check error: {e}")
                self._record_failure()

            # Evict expired TTL cache entries (otherwise they linger until
            # the next get() for the same key, which may never come)
            try:
                if self._api is not None:
                    self._api.cache.cleanup()
            except Exception:
                pass
            
            # Sleep in small increments to allow quick shutdown
            sleep_remaining = self.check_interval
            while sleep_remaining > 0 and self._running:
                time.sleep(min(1.0, sleep_remaining))
                sleep_remaining -= 1.0
    
    def _perform_health_check(self) -> HealthCheckResult:
        """Perform a single health check."""
        if not self._api:
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN,
                timestamp=datetime.now(),
                latency_ms=0,
                details={'error': 'API not attached'}
            )
        
        start_time = time.time()
        details = {}
        
        try:
            # Test REST API with a simple call
            meta = self._api.info.meta()
            latency_ms = (time.time() - start_time) * 1000
            
            if meta and 'universe' in meta:
                details['rest_ok'] = True
                details['assets_count'] = len(meta.get('universe', []))
            else:
                details['rest_ok'] = False
                details['error'] = 'Invalid meta response'
            
            # Check WebSocket status
            details['ws_connected'] = self.is_ws_connected()
            details['ws_message_count'] = self.ws_message_count
            
            # Determine status
            if not details['rest_ok']:
                status = HealthStatus.UNHEALTHY
            elif not details['ws_connected']:
                status = HealthStatus.DEGRADED
            elif latency_ms > self.latency_warning_ms:
                status = HealthStatus.DEGRADED
            else:
                status = HealthStatus.HEALTHY
            
            self.record_rest_latency(latency_ms)
            
            return HealthCheckResult(
                status=status,
                timestamp=datetime.now(),
                latency_ms=latency_ms,
                details=details
            )
            
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                timestamp=datetime.now(),
                latency_ms=latency_ms,
                details={'error': str(e)}
            )
    
    def _process_check_result(self, result: HealthCheckResult):
        """Process health check result and update status."""
        with self._lock:
            old_status = self.status
            
            if result.status == HealthStatus.HEALTHY:
                self.consecutive_failures = 0
                self.status = HealthStatus.HEALTHY
            elif result.status == HealthStatus.DEGRADED:
                self.consecutive_failures = 0
                self.status = HealthStatus.DEGRADED
            else:
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.unhealthy_threshold:
                    self.status = HealthStatus.UNHEALTHY
                else:
                    self.status = HealthStatus.DEGRADED
            
            self.last_check = result
            self.check_history.append(result)
            
            # Log status changes
            if old_status != self.status:
                self.logger.warning(f"Health status changed: {old_status.value} -> {self.status.value}")
                
                # Notify callbacks
                for callback in self._status_callbacks:
                    try:
                        callback(old_status, self.status)
                    except Exception as e:
                        self.logger.error(f"Status callback error: {e}")
    
    def _record_failure(self):
        """Record a health check failure."""
        with self._lock:
            old_status = self.status
            self.consecutive_failures += 1
            
            if self.consecutive_failures >= self.unhealthy_threshold:
                self.status = HealthStatus.UNHEALTHY
            else:
                self.status = HealthStatus.DEGRADED
            
            if old_status != self.status:
                self.logger.warning(f"Health status changed: {old_status.value} -> {self.status.value}")


# =============================================================================
# MAIN API CLASS
# =============================================================================

class HyperliquidAPI(MarketInterface):
    """
    Unified Hyperliquid API client using SDK with built-in WebSocket.
    
    Features:
    - Real-time data via SDK's WebSocket
    - REST operations via SDK
    - Rate limiting
    - Automatic retry with exponential backoff
    - Request caching with TTL
    - Circuit breaker for resilience
    - Proper order status tracking
    - Connection health monitoring
    """
    
    # Asset index ranges
    NATIVE_PERP_MAX = 9999
    HIP3_START = 110000
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the unified Hyperliquid API.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # API configuration
        self.base_url = config['api']['base_url']
        self.private_key = config['api']['private_key']
        self.wallet_address = config['api']['wallet_address']
        # Public account address: the account to read positions from and trade on behalf of
        # If not specified or empty, defaults to wallet_address
        _public_addr = config['api'].get('public_account_address', '')
        self.public_account_address = _public_addr if _public_addr else config['api']['wallet_address']
        
        self.logger.info(f"Wallet Address: {self.wallet_address}")
        self.logger.info(f"Public Account Address: {self.public_account_address}")
        
        # HIP-3 configuration (Hypurr/Spot-linked assets)
        # Enabled by default to ensure all assets are visible.
        # Calling code can still restrict using internal logic if needed, 
        # but the API layer should be permissive.
        self.hip3_enabled = True 
        
        # Determine PERP DEX indices (Native is 0, others are HIP-3)
        # We will auto-discover these in _init_sdk_clients unless checking config for overrides
        hip3_config = config.get('hip3', {})
        self.perp_dexs = hip3_config.get('perp_dexs', None)
        
        # Rate limiter configuration (weight units: Hyperliquid allows
        # 1200 weight/min per IP; we default to 15/s = 900/min for headroom)
        rate_config = config.get('api', {}).get('rate_limit', {})
        self.rate_limiter = RateLimiter(
            calls_per_second=rate_config.get('calls_per_second', 15),
            burst_size=rate_config.get('burst_size', 60)
        )
        
        # Circuit breaker configuration
        cb_config = config.get('api', {}).get('circuit_breaker', {})
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=cb_config.get('failure_threshold', 5),
            recovery_timeout=cb_config.get('recovery_timeout', 30.0)
        )
        
        # Cache configuration
        cache_config = config.get('api', {}).get('cache', {})
        self.cache = TTLCache(default_ttl=cache_config.get('default_ttl', 1.0))
        self.cache_ttl_market_data = cache_config.get('market_data_ttl', 0.5)
        self.cache_ttl_asset_info = cache_config.get('asset_info_ttl', 5.0)
        self.cache_ttl_positions = cache_config.get('positions_ttl', 1.0)
        self.ohlcv_cache = OhlcvCache()
        self.ohlcv_cache.on_bar_complete_callback = self._on_bar_complete
        
        # Backfill state to prevent infinite loops on pre-genesis data
        # Structure: {(symbol, timeframe): min_timestamp_checked_ms}
        self._backfill_state = {}
        
        # Health monitoring configuration
        health_config = config.get('api', {}).get('health_monitor', {})
        self.health_monitor = ConnectionHealthMonitor(
            check_interval=health_config.get('check_interval', 30.0),
            unhealthy_threshold=health_config.get('unhealthy_threshold', 3),
            ws_stale_threshold=health_config.get('ws_stale_threshold', 60.0),
            latency_warning_ms=health_config.get('latency_warning_ms', 1000.0)
        )
        self.health_monitor.attach(self)
        
        # Order tracking
        self.order_tracker = OrderTracker()
        
        # Market data persistence (optional, for incremental loading)
        self.market_db = None  # Set externally via set_market_db()
        
        # Queue for symbols that failed initialization
        self._pending_init_symbols: set = set()
        self._initializing_symbols: set = set() # Track in-flight async inits
        
        # Flight Cache for Request Coalescing
        self._flight_cache: Dict[Tuple[str, str], threading.Event] = {}
        self._flight_lock = threading.Lock()
        # Reuse built OHLCV DataFrames while the underlying bars are unchanged
        # (pd.DataFrame construction per symbol/timeframe/cycle is expensive)
        self._ohlcv_df_cache: Dict[Tuple[str, str], Tuple[tuple, pd.DataFrame]] = {}
        self._df_cache_lock = threading.Lock()
        # Async persistence worker
        # Async persistence worker (SINGLE worker to serialize API calls and prevent rate limit bursts)
        self._persistence_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db_persist")
        
        # Wire up OhlcvCache callback for DB persistence on boundary crossing
        self.ohlcv_cache.on_bar_complete_callback = self._on_bar_complete
        
        # Real-time data storage
        self._price_data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._symbol_last_tick: Dict[str, float] = {}  # Per-symbol last tick timestamp
        self._subscribed_symbols: set = set()
        self._callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._data_lock = threading.Lock()
        
        
        # Periodic Data Integrity components
        self.repairer: Optional['MarketDataRepairer'] = None
        self._integrity_thread = None
        self._stop_integrity_event = threading.Event()
        
        # User state cache (to reduce redundant API calls for margin info)
        self._user_state_cache: Optional[Dict[str, Any]] = None
        self._user_state_cache_time: float = 0.0
        self._user_state_cache_ttl: float = 2.0  # 2 second TTL
        
        # Initialize SDK clients
        self._init_sdk_clients()
        
        self.logger.info(f"Initialized HyperliquidAPI (HIP-3: {self.hip3_enabled})")
        # Initialize state for periodic checks
        self._last_expiration_check = 0.0
        
        # Check API key expiration
        self.check_api_key_expiration()
    
    def _apply_http_timeout(self, client) -> None:
        """
        Apply a default HTTP timeout to an SDK client's requests.Session.

        The hyperliquid SDK calls session.post() without a timeout, so a hung
        TCP connection would block the calling thread forever (and with it the
        serial trading loop). requests has no session-level timeout setting,
        so we wrap session.request to inject a default.
        """
        try:
            session = getattr(client, 'session', None)
            if session is None or getattr(session, '_default_timeout_applied', False):
                return

            read_timeout = float(self.config.get('api', {}).get('timeout', 30) or 30)
            original_request = session.request

            def request_with_timeout(*args, **kwargs):
                kwargs.setdefault('timeout', (5, read_timeout))
                return original_request(*args, **kwargs)

            session.request = request_with_timeout
            session._default_timeout_applied = True
            self.logger.debug(f"Applied default HTTP timeout (5s connect, {read_timeout}s read) to SDK session")
        except Exception as e:
            self.logger.warning(f"Could not apply HTTP timeout to SDK session: {e}")

    def _init_sdk_clients(self):
        """Initialize SDK Info and Exchange clients."""
        # Lazy imports - these are expensive SDK modules
        from hyperliquid.info import Info
        from hyperliquid.exchange import Exchange
        from eth_account import Account
        
        try:
            from hyperliquid.info import Info
            from hyperliquid.exchange import Exchange
            from eth_account import Account

            # Auto-discover HIP-3 dexes if enabled (always) but not specified
            if self.perp_dexs is None:
                try:
                    self.perp_dexs = self._discover_perp_dexs()
                    # Note: _discover_perp_dexs() already logs the discovered dexes
                except Exception as e:
                    self.logger.warning(f"Failed to auto-discover PERP DEXs: {e}. Defaulting to native [\"\"].")
                    self.perp_dexs = [""]  # Empty string = native dex
            
            # Initialize Info client WITHOUT WebSocket first (fast, non-blocking)
            # WebSocket will be enabled when start() is called
            # Retry loop for 429s during startup
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    self.info = Info(
                        self.base_url,
                        skip_ws=True,  # Start without WebSocket for fast init
                        perp_dexs=self.perp_dexs
                    )
                    self._apply_http_timeout(self.info)
                    break
                except Exception as e:
                    if "429" in str(e) and attempt < max_retries - 1:
                        # Aggressive backoff: 2s, 10s, 30s, 60s
                        backoff_steps = [2, 10, 30, 60]
                        wait_time = backoff_steps[min(attempt, len(backoff_steps)-1)]
                        self.logger.warning(f"SDK Init 429 (attempt {attempt+1}/{max_retries}). Waiting {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        raise e
            self._ws_enabled = False
            
            # Initialize Exchange client if credentials provided (with retry for 429)
            self.exchange = None
            if self.private_key and self.wallet_address:
                max_retries = 5
                backoff_steps = [2, 10, 30, 60]
                for attempt in range(max_retries):
                    try:
                        wallet = Account.from_key(self.private_key)
                        
                        # API Wallet Configuration:
                        # When using an API wallet created on Hyperliquid, the agent is already
                        # authorized to trade on behalf of the main account. We do NOT use
                        # vault_address (that's for Hyperliquid Vaults, a different concept).
                        #
                        # The agent signs with its own key, and the exchange recognizes the
                        # pre-existing authorization from when the API wallet was created.
                        #
                        # account_address is used for position lookups (e.g., market_close)
                        if self.public_account_address.lower() != self.wallet_address.lower():
                            self.logger.info(f"API wallet {self.wallet_address} trading on behalf of {self.public_account_address}")
                        else:
                            self.logger.info("Trading with own wallet (no separate account)")
                        
                        self.exchange = Exchange(
                            wallet=wallet,
                            base_url=self.base_url,
                            # NO vault_address - API wallets don't use vault mechanism
                            account_address=self.public_account_address,  # For position lookups
                            perp_dexs=self.perp_dexs if self.hip3_enabled else None
                        )
                        self._apply_http_timeout(self.exchange)
                        self.logger.info(f"Exchange client initialized")
                        break
                    except Exception as e:
                        if "429" in str(e) and attempt < max_retries - 1:
                            wait_time = backoff_steps[min(attempt, len(backoff_steps)-1)]
                            self.logger.warning(f"Exchange Init 429 (attempt {attempt+1}/{max_retries}). Waiting {wait_time}s...")
                            time.sleep(wait_time)
                        else:
                            self.logger.warning(f"Exchange client init failed: {e}")
            
        except Exception as e:
            self.logger.error(f"SDK initialization failed: {e}")
            raise

    def check_api_key_expiration(self):
        """
        Check if the API key is nearing its 6-month expiration date.
        Checks 'key_expiration_date' first, then falls back to 'key_created_at'.
        
        Dynamic Cooldowns:
        - Critical (<7 days): 1 hour
        - Warning (<30 days): 6 hours
        - Normal (>30 days): 24 hours
        """
        expiration_date = None
        
        # 1. Check for explicit expiration date (YYYY-MM-DD)
        expiry_str = self.config.get('api', {}).get('key_expiration_date')
        if expiry_str:
            try:
                expiration_date = datetime.strptime(expiry_str.strip(), "%Y-%m-%d")
            except ValueError:
                self.logger.warning(f"Invalid format for TRADING_API_KEY_EXPIRATION_DATE: '{expiry_str}'. Use YYYY-MM-DD.")

        # 2. Check for creation date fallback (YYYY-MM-DD) -> +6 months
        if not expiration_date:
            created_at_str = self.config.get('api', {}).get('key_created_at')
            if created_at_str:
                try:
                    created_at = datetime.strptime(created_at_str.strip(), "%Y-%m-%d")
                    expiration_date = created_at + timedelta(days=180)
                except ValueError:
                    self.logger.warning(f"Invalid format for TRADING_API_KEY_CREATED_AT: '{created_at_str}'. Use YYYY-MM-DD.")
        
        if not expiration_date:
             # If no date is configured, we can't check. 
             # We still apply a long cooldown to avoid spamming the "No dates configured" log.
             now = time.time()
             if now - self._last_expiration_check < 86400:
                 return
             self._last_expiration_check = now
             self.logger.info("ℹ️ API Key Expiration: No dates configured (TRADING_API_KEY_EXPIRATION_DATE or TRADING_API_KEY_CREATED_AT). Cannot track expiration.")
             return

        try:
            # Determine urgency
            days_remaining = (expiration_date - datetime.now()).days
            
            # cooldown in seconds
            if days_remaining <= 7:
                cooldown = 3600      # 1 hour for Critical
            elif days_remaining <= 30:
                cooldown = 21600     # 6 hours for Warning
            else:
                cooldown = 86400     # 24 hours for Normal
            
            # Cooldown Check
            now = time.time()
            if now - self._last_expiration_check < cooldown:
                return
                
            self._last_expiration_check = now
            
            # Log based on urgency
            if days_remaining <= 7:
                self.logger.critical(f"🚨 API KEY EXPIRATION IMMINENT! Key expires in {days_remaining} days (on {expiration_date.strftime('%Y-%m-%d')}). UPDATE IMMEDIATELY.")
            elif days_remaining <= 30:
                self.logger.warning(f"⚠️ API Key expires in {days_remaining} days (on {expiration_date.strftime('%Y-%m-%d')}). Plan to rotate keys soon.")
            else:
                self.logger.info(f"✅ API Key valid. Expires in {days_remaining} days ({expiration_date.strftime('%Y-%m-%d')}).")
                
        except Exception as e:
            self.logger.error(f"Failed to check API key expiration: {e}")
    
    def _enable_websocket(self, timeout: float = 10.0, force_reconnect: bool = False):
        """Enable WebSocket connection (called from start() or for reconnection)."""
        if self._ws_enabled and not force_reconnect:
            return
        
        if force_reconnect:
            self.logger.info("Force reconnecting WebSocket...")
            self._ws_enabled = False
        
        self.logger.info("Enabling WebSocket connection...")
        
        # Initialize WebSocket in background thread to avoid blocking
        ws_info = [None]  # Use list to allow modification in thread
        ws_error = [None]
        
        def init_ws():
            try:
                from hyperliquid.info import Info  # Lazy import
                ws_info[0] = Info(
                    self.base_url,
                    skip_ws=False,
                    perp_dexs=self.perp_dexs if self.hip3_enabled else None
                )
            except Exception as e:
                ws_error[0] = e
        
        ws_thread = threading.Thread(target=init_ws)
        ws_thread.daemon = True
        ws_thread.start()
        ws_thread.join(timeout=timeout)
        
        if ws_thread.is_alive():
            self.logger.warning(f"WebSocket initialization timed out after {timeout}s. Continuing with REST-only mode.")
            return
        
        if ws_error[0]:
            self.logger.warning(f"WebSocket initialization failed: {ws_error[0]}. Continuing with REST-only mode.")
            return
        
        if ws_info[0]:
            self.info = ws_info[0]
            self._apply_http_timeout(self.info)
            self._ws_enabled = True
            self._setup_websocket_subscriptions()
            self.logger.info("WebSocket enabled successfully")
    
    
    def check_connection_status(self) -> bool:
        """
        Active heartbeat to check WebSocket health and trigger non-blocking reconnection if needed.
        Designed to be called from the main trading loop without blocking.
        
        Returns:
            True if connection is healthy or reconnecting, False if critical error.
        """
        # 1. Check health monitor
        if self.health_monitor.check_and_request_reconnect():
            self.logger.warning("Main Loop Heartbeat: WebSocket is stale. Triggering background reconnection...")
            
            # Spawn non-blocking reconnection thread
            # We use a lock to ensure only one reconnect thread runs at a time
            if getattr(self, '_reconnect_thread', None) and self._reconnect_thread.is_alive():
                self.logger.debug("Reconnection thread already running, skipping dispatch.")
                return True
                
            self._reconnect_thread = threading.Thread(
                target=self.attempt_ws_reconnect,
                name="BackgroundWSReconnect",
                daemon=True
            )
            self._reconnect_thread.start()
            
        return True

    def attempt_ws_reconnect(self) -> bool:
        """
        Attempt to reconnect WebSocket if stale for too long.
        Now designed to run in a background thread.
        
        Returns:
            True if reconnection was attempted, False otherwise.
        """
        # Double check inside the thread (race condition handling)
        if not self.health_monitor.check_and_request_reconnect():
            return False

        self.logger.warning("Attempting WebSocket reconnection (Background Thread)...")
        try:
            # Force close existing connection first to ensure clean slate
            # (Note: _enable_websocket with force_reconnect=True handles logical reset, 
            # but we want to ensure the socket is truly closed at the network level if possible)
            
            self._enable_websocket(timeout=10.0, force_reconnect=True)
            
            # Reset health monitor staleness tracking on success
            self.health_monitor._reconnect_requested = False
            self.health_monitor._ws_stale_since = None
            self.logger.info("WebSocket reconnection completed successfully")
            return True
        except Exception as e:
            self.logger.error(f"WebSocket reconnection failed: {e}")
            return False
    
    def _discover_perp_dexs(self) -> List[str]:
        """Discover available perp dexes.
        
        Returns a list of dex name strings:
        - "" (empty string) represents the native/original dex
        - HIP-3 dexes are represented by their names (e.g., "Hypurr")
        """
        try:
            from hyperliquid.info import Info
            temp_info = Info(self.base_url, skip_ws=True)
            dexs = temp_info.perp_dexs()
            
            # SDK expects dex NAMES as strings, not indices
            # Native dex is "" (empty string), HIP-3 dexes have names
            # The API returns: [native_dex_obj, hip3_dex_1, hip3_dex_2, ...]
            # where hip3 dexes have a "name" field
            dex_names = [""]  # Always include native dex
            if dexs and len(dexs) > 1:
                # Add HIP-3 dex names (skip first entry which is native)
                for dex in dexs[1:]:
                    if isinstance(dex, dict) and "name" in dex:
                        dex_names.append(dex["name"])
            
            self.logger.info(f"Discovered perp dexes (names): {dex_names}")
            return dex_names
        except Exception as e:
            self.logger.warning(f"Perp dex discovery failed: {e}")
            return [""]  # Default to native dex only
    
    def _setup_websocket_subscriptions(self):
        """Setup WebSocket subscriptions using SDK."""
        try:
            # Subscribe to all mid prices
            self.info.subscribe(
                {"type": "allMids"},
                self._on_all_mids_update
            )
            self.logger.info("Subscribed to allMids WebSocket feed")
            
        except Exception as e:
            self.logger.warning(f"WebSocket subscription failed: {e}")
    
    def _on_all_mids_update(self, data: Dict[str, Any]):
        """Handle allMids WebSocket updates."""
        try:
            # Record WebSocket message for health monitoring
            self.health_monitor.record_ws_message()
            
            mids = data.get('data', {}).get('mids', {}) if 'data' in data else data.get('mids', {})
            timestamp = time.time()
            
            # Diagnostic logging: Log tick count every 60 seconds to track WS health
            if not hasattr(self, '_ws_diag_last_log'):
                self._ws_diag_last_log = 0
                self._ws_diag_tick_count = 0
            self._ws_diag_tick_count += len(mids)
            if timestamp - self._ws_diag_last_log >= 60:
                self.logger.info(f"[WS DIAG] Processed {self._ws_diag_tick_count} price updates in last 60s ({len(mids)} symbols this tick)")
                self._ws_diag_tick_count = 0
                self._ws_diag_last_log = timestamp
            
            with self._data_lock:
                for symbol, price_str in mids.items():
                    try:
                        price = float(price_str)
                        self._symbol_last_tick[symbol] = timestamp  # Track per-symbol
                        self._price_data[symbol].append({
                            'price': price,
                            'timestamp': timestamp
                        })
                        
                        # Notify callbacks
                        for callback in self._callbacks.get('price', []):
                            try:
                                callback(symbol, price, timestamp)
                            except Exception as e:
                                self.logger.error(f"Price callback error: {e}")
                        
                        # Update rolling OHLCV cache from tick
                        try:
                            self.update_ohlcv_from_tick(symbol, price, volume=0.0, ts=timestamp)
                        except Exception as e:
                            self.logger.debug(f"OHLCV tick update error for {symbol}: {e}")
                                
                    except (ValueError, TypeError):
                        # Ignore malformed ticks; continue processing other symbols.
                        continue
                        
        except Exception as e:
            self.logger.error(f"Error handling allMids update: {e}")
    
    def _is_retryable_error(self, e: Exception) -> bool:
        """
        Classify whether an API error is transient and worth retrying.

        Prefers the SDK's typed errors (which carry real HTTP status codes)
        over string matching; falls back to substring checks for errors raised
        by other layers (e.g. wrapped connection failures).
        """
        # 1. Typed SDK errors with real status codes
        try:
            from hyperliquid.utils.error import ClientError, ServerError
            if isinstance(e, ClientError):
                return getattr(e, 'status_code', None) == 429
            if isinstance(e, ServerError):
                # (500, 'null') typically means "Symbol not found" or invalid
                # request - not transient, do not retry
                return "(500, 'null')" not in str(e)
        except ImportError:
            pass

        # 2. Typed requests-layer network errors
        try:
            import requests
            if isinstance(e, (requests.exceptions.Timeout,
                              requests.exceptions.ConnectionError)):
                return True
        except ImportError:
            pass

        # 3. Fallback: substring matching for errors from other layers
        err_str = str(e)
        if "429" in err_str:  # Rate Limit
            return True
        if "500" in err_str or "502" in err_str or "503" in err_str:  # Server Error
            return "(500, 'null')" not in err_str
        if any(pattern in err_str.lower() for pattern in [
            "connectionerror", "connection refused", "connection reset",
            "connection failed", "networkerror", "network unreachable",
            "timeout", "timed out"
        ]):
            # Network errors - but NOT HTTP headers like "'Connection': 'keep-alive'"
            return True
        return False

    def _rate_limited_call(self, func: Callable, *args, weight: float = 10.0, **kwargs) -> Any:
        """
        Execute a rate-limited API call with retry logic, circuit breaker and latency tracking.
        Retries on 429 (Too Many Requests) and 5xx (Server Errors).

        Args:
            weight: Hyperliquid request weight of this call (cheap info requests
                    like allMids/user_state = 2, most info requests = 20).
                    Consumed from the token bucket on every attempt.
        """
        # Check circuit breaker
        if not self.circuit_breaker.can_execute():
            raise RuntimeError("Circuit breaker is open - API temporarily unavailable")

        # Aggressive Backoff Strategy: 2s, 10s, 30s, 60s
        # This ensures we persist through temporary outages (total ~2 mins)
        backoff_steps = [2, 10, 30, 60]
        max_retries = len(backoff_steps)

        for attempt in range(max_retries + 1):
            # Acquire tokens on EVERY attempt so retry traffic is also throttled
            # (retries are exactly when the API is telling us to slow down)
            if not self.rate_limiter.acquire(timeout=30.0, weight=weight):
                raise RuntimeError("Rate limit timeout - too many requests")

            start_time = time.time()
            try:
                result = func(*args, **kwargs)

                # Record latency for health monitoring
                latency_ms = (time.time() - start_time) * 1000
                self.health_monitor.record_rest_latency(latency_ms)

                self.circuit_breaker.record_success()
                return result

            except Exception as e:
                if self._is_retryable_error(e) and attempt < max_retries:
                    wait_time = backoff_steps[attempt]
                    self.logger.warning(f"API Error (attempt {attempt+1}/{max_retries+1}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                # Known benign server hiccup: Hyperliquid sporadically answers
                # HTTP 500 with body 'null'; the identical request succeeds on
                # the next cycle. Fail fast and quietly — no ERROR noise, no
                # circuit-breaker pollution from a server-side blip.
                if "(500, 'null')" in str(e):
                    self.logger.warning(f"Hyperliquid transient 500-null (benign, not retried): {e}")
                    raise

                # If not retryable or max retries reached:
                self.circuit_breaker.record_failure()
                self.logger.error(f"API Call Failed (attempt {attempt+1}/{max_retries+1}): {e}")
                raise
    
    # =========================================================================
    # CONNECTION & LIFECYCLE
    # =========================================================================
    
    def _run_integrity_check_loop(self):
        """
        Background loop to verify and repair market data integrity.
        Runs every 60 minutes.
        """
        self.logger.info("Integrity Check Loop started")
        
        # Initial sleep to let the system stabilize
        if self._stop_integrity_event.wait(60):
            return

        while not self._stop_integrity_event.is_set():
            try:
                if self.repairer:
                    self.logger.info("Running periodic data integrity check...")
                    # 2 days lookback is sufficient for periodic checks
                    self.repairer.repair_all(days_back=2) 
                    self.logger.info("Periodic integrity check completed")
            except Exception as e:
                self.logger.error(f"Error in integrity check loop: {e}")
                
            # Wait 60 minutes or until stopped
            # Check every second to respond to stop event quickly
            # But simple wait is fine
            if self._stop_integrity_event.wait(3600):
                break
                
        self.logger.info("Integrity Check Loop stopped")

    
    def start(self) -> bool:
        """Start the API, enable WebSocket, and start health monitoring."""
        self.logger.info("Starting HyperliquidAPI...")
        
        # Test connection first (uses REST, fast)
        if not self.test_connection():
            return False
        
        # Enable WebSocket for real-time data
        self._enable_websocket()
        
        # Start health monitoring
        self.health_monitor.start()
        
        # Start periodic integrity check (daemon thread)
        try:
            # Re-initialize here to ensure fresh DB connection if needed
            from src.utils.market_data_repair import MarketDataRepairer
            
            # Use a separate DB instance for this thread to avoid contention/thread-safety issues
            # if TradeDatabase isn't fully thread-safe (sqlite is picky about threads)
            db_path = self.config.get('persistence', {}).get('db_path', 'data/trades.db') or 'data/trades.db'
            repair_db = TradeDatabase(db_path)
            
            self.repairer = MarketDataRepairer(self, repair_db)
            
            # Only start if explicitly enabled (defaults to False to prevent startup API contention)
            if self.config.get('data_collection', {}).get('enable_integrity_check', False):
                self._stop_integrity_event.clear()
                self._integrity_thread = threading.Thread(
                    target=self._run_integrity_check_loop,
                    name="IntegrityCheck",
                    daemon=True
                )
                self._integrity_thread.start()
                self.logger.info("Integrity Check thread started")
            else:
                self.logger.info("Integrity Check thread disabled (config)")
        except Exception as e:
            self.logger.error(f"Failed to start Integrity Check thread: {e}")
        
        self.logger.info("HyperliquidAPI started successfully")
        return True
    
    def stop(self):
        """Stop the API, health monitor, and cleanup."""
        self.logger.info("Stopping HyperliquidAPI...")
        
        # Stop health monitor
        self.health_monitor.stop()
        
        # Stop integrity thread
        self._stop_integrity_event.set()
        if self._integrity_thread and self._integrity_thread.is_alive():
            self._integrity_thread.join(timeout=2.0)
            
        # Stop persistence executor (wait=False to avoid hanging on pending tasks)
        self._persistence_executor.shutdown(wait=False, cancel_futures=True)
        
        # Cleanup
        self.cache.clear()
        self.order_tracker.cleanup_old(max_age_hours=0)
        
        self.logger.info("HyperliquidAPI stopped")

    
    def test_connection(self) -> bool:
        """Test API connection with retry logic."""
        def _fetch():
            return self.info.meta()
        
        try:
            meta = self._rate_limited_call(_fetch, weight=20)
            if meta and 'universe' in meta:
                self.logger.info("API connection test successful")
                return True
            return False
        except Exception as e:
            self.logger.error(f"API connection test failed: {e}")
            return False
    
    # =========================================================================
    # MARKET DATA
    # =========================================================================
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current price for a symbol.
        
        Uses WebSocket data if available, otherwise falls back to REST.
        """
        # Try WebSocket data first (real-time)
        # Handle HIP-3 prefix (e.g. "1:TSLA" -> "TSLA")
        clean_symbol = symbol
        if ":" in symbol:
            parts = symbol.split(":", 1)
            if parts[0].isdigit():
                clean_symbol = parts[1]

        with self._data_lock:
            # Check for clean symbol (standard)
            if clean_symbol in self._price_data and self._price_data[clean_symbol]:
                latest = self._price_data[clean_symbol][-1]
                # Only use if fresh (< 5 seconds old)
                if time.time() - latest['timestamp'] < 5.0:
                    return latest['price']
            
            # Fallback: check raw symbol if different (just in case)
            if clean_symbol != symbol and symbol in self._price_data and self._price_data[symbol]:
                latest = self._price_data[symbol][-1]
                if time.time() - latest['timestamp'] < 5.0:
                    return latest['price']
        
        # Fall back to SDK's allMids (also uses WebSocket internally)
        def _fetch():
            return self.info.all_mids()
        
        try:
            all_mids = self._rate_limited_call(_fetch, weight=2)
            if clean_symbol in all_mids:
                return float(all_mids[clean_symbol])
            if clean_symbol != symbol and symbol in all_mids:
                return float(all_mids[symbol])
        except Exception as e:
            self.logger.debug(f"all_mids failed: {e}")
        
        # Last resort: REST call
        return self._get_price_from_rest(symbol)
    
    def get_cached_price(self, symbol: str) -> Optional[float]:
        """
        Get price from local cache only (no API fallback).
        
        Used by dashboard to avoid API rate limits. Returns None if not cached.
        Prices come from:
        1. WebSocket allMids subscription (updated every tick)
        2. Last known price from _price_data
        
        Returns:
            Current price if cached, None otherwise
        """
        # Try WebSocket data (no staleness check - dashboard accepts slightly stale data)
        with self._data_lock:
            if symbol in self._price_data and self._price_data[symbol]:
                latest = self._price_data[symbol][-1]
                return latest['price']
        
        # Try allMids cache (from WebSocket subscription)
        if hasattr(self, '_all_mids_cache') and symbol in self._all_mids_cache:
            return float(self._all_mids_cache[symbol])
        
        return None

    def _get_bulk_market_data(self) -> Optional[Tuple[List[Any], List[Any]]]:
        """
        Get market data for all assets, cached for short duration.
        Used to prevent N+1 API calls when iterating over symbols.
        """
        cache_key = "bulk_market_data"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
            
        def _fetch():
            meta_and_ctxs = self.info.meta_and_asset_ctxs()
            if len(meta_and_ctxs) < 2:
                return None
            
            universe = meta_and_ctxs[0]['universe']
            asset_contexts = meta_and_ctxs[1]
            
            return universe, asset_contexts
            
        result = self._rate_limited_call(_fetch, weight=20)
        if result:
            self.cache.set(cache_key, result, ttl=5.0)  # Cache bulk data for 5 seconds
            
        return result
    
    def _get_price_from_rest(self, symbol: str) -> Optional[float]:
        """Get price via REST API (fallback)."""
        cache_key = f"price_{symbol}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            market_data = self.get_market_data(symbol)
            if market_data:
                price = market_data.get('current_price')
                if price:
                    self.cache.set(cache_key, price, ttl=self.cache_ttl_market_data)
                    return price
        except Exception as e:
            self.logger.error(f"REST price fetch failed for {symbol}: {e}")
        
        return None
    
    def get_market_data(self, symbol: str, timeframe: str = "1m") -> Optional[Dict[str, Any]]:
        """
        Get comprehensive market data for a symbol.
        
        Args:
            symbol: Trading symbol
            timeframe: Timeframe for data (not used currently, for interface compatibility)
        """
        cache_key = f"market_data_{symbol}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        result = None

        # 1. Check Native Universe via the shared bulk snapshot.
        # One metaAndAssetCtxs response covers ALL symbols, so we populate the
        # cache for every symbol in it - iterating 50 pairs per cycle then
        # costs one API call instead of one full-universe fetch per symbol.
        try:
            bulk = self._get_bulk_market_data()
            if bulk:
                universe, asset_contexts = bulk
                for i, asset in enumerate(universe):
                    name = asset.get('name')
                    if not name:
                        continue
                    ctx = asset_contexts[i] if i < len(asset_contexts) else {}
                    parsed = self._parse_market_ctx(name, asset, ctx)
                    self.cache.set(f"market_data_{name}", parsed, ttl=self.cache_ttl_market_data)
                    if name == symbol:
                        result = parsed
        except Exception as e:
            self.logger.warning(f"Bulk market data lookup failed for {symbol}: {e}")

        # 2. If not found and HIP-3 enabled, check other Dexes
        if result is None and self.hip3_enabled:
            try:
                dexs = self._rate_limited_call(self.info.perp_dexs, weight=20)
                # Skip first (Native)
                for dex in dexs[1:]:
                    dex_name = dex.get('name')
                    if not dex_name: continue

                    try:
                        def _fetch_dex_ctx():
                            return self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex_name})
                        res = self._rate_limited_call(_fetch_dex_ctx, weight=20)
                        if res and len(res) >= 2:
                            u_hip3 = res[0]['universe']
                            c_hip3 = res[1]

                            # Cache every symbol on this dex so sibling HIP-3
                            # lookups don't re-scan the same universe
                            for i, asset in enumerate(u_hip3):
                                name = asset.get('name')
                                if not name:
                                    continue
                                ctx = c_hip3[i] if i < len(c_hip3) else {}
                                parsed = self._parse_market_ctx(name, asset, ctx)
                                self.cache.set(f"market_data_{name}", parsed, ttl=self.cache_ttl_market_data)
                                if name == symbol:
                                    result = parsed
                            if result is not None:
                                break
                    except Exception:
                        continue
            except Exception as e:
                self.logger.warning(f"HIP-3 lookup failed in get_market_data: {e}")

        return result
    
    def _parse_market_ctx(self, symbol: str, asset: dict, ctx: dict) -> Dict[str, Any]:
        """Helper to parse raw asset context into standard dict."""
        from datetime import datetime
        return {
            'symbol': symbol,
            'current_price': float(ctx.get('markPx', 0)),
            'bid': float(ctx.get('impactPxs', [0, 0])[0]) if ctx.get('impactPxs') else 0,
            'ask': float(ctx.get('impactPxs', [0, 0])[1]) if ctx.get('impactPxs') and len(ctx.get('impactPxs')) > 1 else 0,
            'volume_24h': float(ctx.get('dayNtlVlm', 0)),
            'open_interest': float(ctx.get('openInterest', 0)),
            'funding_rate': float(ctx.get('funding', 0)),
            'max_leverage': asset.get('maxLeverage', 0),
            'sz_decimals': asset.get('szDecimals', 0),
            'timestamp': datetime.now(),
        }
    
    def get_asset_info(self) -> Optional[Dict[str, Any]]:
        """Get asset information for all perpetuals (Native + HIP-3 if enabled)."""
        cache_key = "asset_info"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        def _fetch():
            try:
                # 1. Fetch Native Universe
                meta_and_ctxs = self.info.meta_and_asset_ctxs()
                
                if len(meta_and_ctxs) < 2:
                    return None
                
                meta = meta_and_ctxs[0]
                asset_contexts = meta_and_ctxs[1]
                
                universe = []
                
                # Helper to process an asset batch
                def _process_assets(asset_list, ctx_list, dex_name="Native"):
                    for i, asset in enumerate(asset_list):
                        ctx = ctx_list[i] if i < len(ctx_list) else {}
                        
                        oi_tokens = float(ctx.get('openInterest', 0))
                        mark_price = float(ctx.get('markPx', 0))
                        
                        asset_data = {
                            'name': asset['name'],
                            'maxLeverage': asset.get('maxLeverage', 0),
                            'szDecimals': asset.get('szDecimals', 0),
                            'openInterest': oi_tokens * mark_price,
                            'volume24h': float(ctx.get('dayNtlVlm', 0)),
                            'markPrice': mark_price,
                            'funding': float(ctx.get('funding', 0)),
                            'bid': float(ctx.get('impactPxs', [0, 0])[0]) if ctx.get('impactPxs') else 0,
                            'ask': float(ctx.get('impactPxs', [0, 0])[1]) if ctx.get('impactPxs') and len(ctx.get('impactPxs')) > 1 else 0,
                            'dex': dex_name
                        }
                        
                        # Add IDs if present (crucial for execution)
                        if 'assetId' in asset:
                            asset_data['assetId'] = asset['assetId']
                            
                        universe.append(asset_data)

                        # [HIP-3 EXECUTION FIX] Patch SDK maps
                        # The underlying 'exchange' object uses these maps to translate "xyz" -> asset index 123
                        if self.exchange and hasattr(self.exchange, 'info'):
                            coin_name = asset['name']
                            
                            # Patch name_to_coin
                            if coin_name not in self.exchange.info.name_to_coin:
                                self.exchange.info.name_to_coin[coin_name] = asset
                                # self.logger.debug(f"[HIP-3] Patched SDK map: {coin_name}")
                            
                            # Patch coin_to_asset
                            if 'assetId' in asset and coin_name not in self.exchange.info.coin_to_asset:
                                self.exchange.info.coin_to_asset[coin_name] = asset['assetId']
                                self.logger.debug(f"[HIP-3] Patched execution ID: {coin_name} -> {asset['assetId']}")

                # Process Native
                _process_assets(meta['universe'], asset_contexts)

                # 2. Fetch HIP-3 Universes (if enabled)
                if self.hip3_enabled:
                    try:
                        dexs = self.info.perp_dexs()
                        # Skip first (Native) per documentation/convention
                        for dex in dexs[1:]:
                            if not isinstance(dex, dict): continue
                            dex_name = dex.get('name')
                            if not dex_name: continue
                            
                            try:
                                # Fetch DEX specific context
                                def _fetch_hip3_dex():
                                    return self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex_name})
                                res = self._rate_limited_call(_fetch_hip3_dex, weight=20)
                                if res and len(res) >= 2:
                                    _process_assets(res[0]['universe'], res[1], dex_name=dex_name)
                            except Exception as dex_err:
                                self.logger.warning(f"Failed to fetch HIP-3 DEX {dex_name}: {dex_err}")
                    except Exception as e:
                        self.logger.warning(f"Failed to discover HIP-3 universes: {e}")

                result = {'universe': universe, 'meta': meta} # Keeping original 'meta' structure for compatibility
                return result
            
            except Exception as e:
                self.logger.error(f"Error fetching asset info: {e}")
                return None
            
        result = self._rate_limited_call(_fetch, weight=20)
        if result:
             self.cache.set(cache_key, result, ttl=300.0)
             
        return result
    
    def _candles_snapshot_smart(self, api_symbol: str, timeframe: str,
                                start_ms: int, end_ms: int):
        """
        candleSnapshot via the SDK wrapper when its name map knows the
        symbol, else the raw info POST. Spot pair ids ('@107'), internal
        spot names and brand-new HIP-3 listings are EXPECTED to miss the
        SDK's name_to_coin — routing them straight to the raw call avoids
        a KeyError flowing through _rate_limited_call (ERROR log + spurious
        circuit-breaker failure on every fetch).
        """
        if api_symbol in getattr(self.info, 'name_to_coin', {}):
            def _fetch_sdk():
                return self.info.candles_snapshot(api_symbol, timeframe, start_ms, end_ms)
            return self._rate_limited_call(_fetch_sdk, weight=20)

        req = {"coin": api_symbol, "interval": timeframe,
               "startTime": start_ms, "endTime": end_ms}
        def _fetch_raw():
            return self.info.post("/info", {"type": "candleSnapshot", "req": req})
        return self._rate_limited_call(_fetch_raw, weight=20)

    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100, market_type: str = None) -> Optional[pd.DataFrame]:
        """
        Get OHLCV candlestick data with in-memory rolling cache.
        Seed once, then serve from cache (updated via ticks).
        
        Handles both perp symbols (e.g., "BTC") and spot tokens (e.g., "UBTC").
        Spot tokens are automatically converted to their API name (e.g., "@109").
        """

        # Determine the API symbol to use for fetching
        # Perp symbols work directly, spot tokens need conversion to @N format
        
        # 1. Normalize symbol for internal usage (Storage/Cache key)
        # e.g. "UBTC" -> "BTC_SPOT", "BTC" -> "BTC"
        internal_symbol = self.normalize_symbol(symbol)
        
        # 2. Resolve to API symbol for network requests
        # e.g. "BTC_SPOT" -> "@109", "BTC" -> "BTC"
        is_spot = (market_type == 'spot') if market_type else self._is_spot_symbol(internal_symbol)

        if is_spot:
             # It's a spot asset (e.g. BTC_SPOT), resolve to API name (e.g. @109)
             # First map back to API token name (e.g. BTC_SPOT -> UBTC)
             # Fallback: Strip _SPOT to handle dynamic assets (e.g. WOW_SPOT -> WOW)
             base_name_fallback = internal_symbol.replace('_SPOT', '')
             api_token_name = self.SPOT_INTERNAL_TO_API.get(internal_symbol, base_name_fallback)
             
             # Then get the internal ID (e.g. @109)
             api_symbol = self.get_spot_api_name(api_token_name)
             if not api_symbol:
                 self.logger.debug(f"Could not resolve spot API symbol for {internal_symbol} ({api_token_name})")
                 return None
        else:
             # Perp or standard asset
             api_symbol = internal_symbol

        # Use internal_symbol for Cache/DB operations
        symbol = internal_symbol

        # Try cache first (always use human-readable symbol for cache key)
        cached_bars = self.ohlcv_cache.get(symbol, timeframe)
        if cached_bars and len(cached_bars) >= min(limit, self.ohlcv_cache.maxlen[symbol][timeframe]):
            return self._bars_to_df(symbol, timeframe, cached_bars).tail(limit)
        
        # Otherwise fetch once, seed cache
        def _fetch():
            timeframe_ms = {
                '1m': 60 * 1000,
                '5m': 5 * 60 * 1000,
                '15m': 15 * 60 * 1000,
                '1h': 60 * 60 * 1000,
                '4h': 4 * 60 * 60 * 1000,
                '1d': 24 * 60 * 60 * 1000,
            }
            interval_ms = timeframe_ms.get(timeframe, 60 * 60 * 1000)
            end_time = int(time.time() * 1000)
            
            # Check for cached data in database
            df = None
            if self.market_db:
                try:
                    df = self.market_db.get_market_data(symbol, timeframe)
                    if not df.empty:
                        # Get latest timestamp from cached data
                        latest_ts = df.index.max()
                        gap_start_dt = latest_ts + pd.Timedelta(milliseconds=interval_ms)
                        # DB uses naive UTC (from unix timestamp), so we must use naive UTC for 'now'
                        now = pd.Timestamp.utcnow().replace(tzinfo=None)
                        
                        # --- 1. Forward Gap Logic (Existing) ---
                        if gap_start_dt < now:
                            # Fetch only the gap
                            gap_start_ms = int(gap_start_dt.timestamp() * 1000)
                            self.logger.debug(f"Gap-fill for {symbol} {timeframe}: fetching from {gap_start_dt}")
                            candles = self._candles_snapshot_smart(api_symbol, timeframe, gap_start_ms, end_time)
                            if candles:
                                # Filter for DB (Strictly Closed)
                                candles_db = [c for c in candles if c['t'] + interval_ms <= end_time]
                                
                                if candles_db:
                                    new_bars_db = [{
                                        'time': c['t'] // 1000,
                                        'open': float(c['o']),
                                        'high': float(c['h']),
                                        'low': float(c['l']),
                                        'close': float(c['c']),
                                        'volume': float(c['v']),
                                    } for c in candles_db]
                                    new_df_db = pd.DataFrame(new_bars_db)
                                    new_df_db['timestamp'] = pd.to_datetime(new_df_db['time'], unit='s')
                                    new_df_db.set_index('timestamp', inplace=True)
                                    self.market_db.insert_market_data(new_df_db, symbol, timeframe)
                                    self.logger.info(f"GAP FILL: Persisted {len(new_df_db)} candles for {symbol} {timeframe}")
                                    
                                    # Append to local df so we return full dataset
                                    df = pd.concat([df, new_df_db]).sort_index()
                                
                        # --- 2. Backward Gap Logic (New: Historical Backfill) ---
                        earliest_ts = df.index.min()
                        # Optimization 1: Over-fetch (Minimum Fetch Limit) to seed cache efficiently
                        MIN_FETCH_LIMIT = 500  # API limit is 5000 (1 wt per 60 items = ~8 weight for 500)
                        actual_limit = max(limit, MIN_FETCH_LIMIT)
                        
                        # Calculate required start time based on actual_limit
                        required_duration_ms = actual_limit * interval_ms
                        required_start_dt = (pd.Timestamp.utcnow().replace(tzinfo=None) - 
                                            pd.Timedelta(milliseconds=required_duration_ms))
                        
                        # Add a small buffer (e.g. 1 interval) to avoid fencepost errors
                        if earliest_ts > required_start_dt + pd.Timedelta(milliseconds=interval_ms):
                            backfill_start_ms = int(required_start_dt.timestamp() * 1000)
                            
                            # Loop Prevention: Check if we already tried this far back
                            min_checked_ms = self._backfill_state.get((symbol, timeframe))
                            
                            if min_checked_ms and backfill_start_ms < min_checked_ms:
                                # We already checked past this point and found nothing (genesis limit)
                                # Silently skip to avoid log spam
                                pass
                            else:
                                self.logger.info(f"Historical Backfill needed for {symbol} {timeframe}. "
                                               f"Have from {earliest_ts}, need from {required_start_dt}")
                                
                                backfill_end_ms = int(earliest_ts.timestamp() * 1000)
                            
                                # Sanity check
                                if backfill_end_ms > backfill_start_ms:
                                    try:
                                        hist_candles = self._candles_snapshot_smart(
                                            api_symbol, timeframe, backfill_start_ms, backfill_end_ms)
                                        
                                        # Filter strictly before earliest_ts to avoid dupes
                                        valid_candles = []
                                        if hist_candles:
                                            valid_candles = [c for c in hist_candles if c['t'] < backfill_end_ms]
                                        
                                        if valid_candles:
                                            hist_bars = [{
                                                'time': c['t'] // 1000,
                                                'open': float(c['o']),
                                                'high': float(c['h']),
                                                'low': float(c['l']),
                                                'close': float(c['c']),
                                                'volume': float(c['v']),
                                            } for c in valid_candles]
                                            hist_df = pd.DataFrame(hist_bars)
                                            hist_df['timestamp'] = pd.to_datetime(hist_df['time'], unit='s')
                                            hist_df.set_index('timestamp', inplace=True)
                                            
                                            self.market_db.insert_market_data(hist_df, symbol, timeframe)
                                            self.logger.info(f"BACKFILL: Persisted {len(hist_df)} historical candles for {symbol} {timeframe}")
                                            
                                            # Prepend to local df
                                            df = pd.concat([hist_df, df]).sort_index()
                                        else:
                                            # No candles found in this range (either empty fetch or all filtered).
                                            # This likely means we hit the asset genesis.
                                            # Update state to prevent infinite retries.
                                            self._backfill_state[(symbol, timeframe)] = backfill_end_ms
                                            self.logger.info(f"BACKFILL: No data found for {symbol} {timeframe} before {earliest_ts}. Marked genesis.")
                                                
                                    except Exception as e:
                                        self.logger.warning(f"Failed to backfill history for {symbol}: {e}")




                        
                        # Return from cache
                        bars = [{
                            'time': int(ts.timestamp()),
                            'open': row['open'],
                            'high': row['high'],
                            'low': row['low'],
                            'close': row['close'],
                            'volume': row['volume'],
                        } for ts, row in df.tail(limit).iterrows()]
                        
                        self.ohlcv_cache.seed(symbol, timeframe, bars, maxlen=max(limit, 300))
                        return df.tail(limit)
                except Exception as e:
                    self.logger.debug(f"Cache miss for {symbol} {timeframe}: {e}")
            
            # Full fetch (no cached data or cache error)
            MIN_FETCH_LIMIT = 500
            actual_limit = max(limit, MIN_FETCH_LIMIT)
            start_time = end_time - (actual_limit * interval_ms)
            
            # SDK wrapper when the symbol is known, raw call otherwise
            # (see _candles_snapshot_smart)
            candles = []
            try:
                candles = self._candles_snapshot_smart(api_symbol, timeframe, start_time, end_time)
            except Exception as e:
                error_details = str(e)
                if hasattr(e, 'response') and hasattr(e.response, 'text'):
                    error_details += f" | Response: {e.response.text}"
                self.logger.error(f"Error fetching candles for {api_symbol}: {error_details}")
                raise # Re-raise to caller
                
            if not candles:
                return None
            
            # Partition candles: Closed (DB) vs All (Cache)
            candles_db = [c for c in candles if c['t'] + interval_ms <= end_time]
            
            # 1. Prepare bars for DB (Closed Only)
            if candles_db:
                bars_db = [{
                    'time': c['t'] // 1000,
                    'open': float(c['o']),
                    'high': float(c['h']),
                    'low': float(c['l']),
                    'close': float(c['c']),
                    'volume': float(c['v']),
                } for c in candles_db]
            else:
                bars_db = []
                
            # 2. Prepare bars for Cache (All)
            bars_cache = [{
                'time': c['t'] // 1000,
                'open': float(c['o']),
                'high': float(c['h']),
                'low': float(c['l']),
                'close': float(c['c']),
                'volume': float(c['v']),
            } for c in candles]
            
            
            # Save to database for future restarts
            if self.market_db and bars_db:
                try:
                    df = pd.DataFrame(bars_db)
                    df['timestamp'] = pd.to_datetime(df['time'], unit='s')
                    df.set_index('timestamp', inplace=True)
                    self.market_db.insert_market_data(df, symbol, timeframe)
                    self.logger.info(f"STARTUP: Persisted {len(df)} candles for {symbol} {timeframe} to DB")
                except Exception as e:
                    self.logger.error(f"STARTUP: Failed to persist {symbol} {timeframe}: {e}")
            else:
                self.logger.warning(f"STARTUP: Skipping persistence for {symbol} {timeframe} - DB not connected")
            
            # Seed in-memory cache with ALL bars (including incomplete)
            self.ohlcv_cache.seed(symbol, timeframe, bars_cache, maxlen=max(actual_limit, 300))
            df = pd.DataFrame(bars_cache)
            df['timestamp'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('timestamp', inplace=True)
            return df.tail(limit)
        # Optimization 2: Request Coalescing (In-Flight Request Caching)
        cache_key = (symbol, timeframe)
        
        with self._flight_lock:
            if cache_key in self._flight_cache:
                # Another thread is already fetching this exact data. Wait for it.
                event = self._flight_cache[cache_key]
                is_fetching = True
            else:
                # We are the first. Create an event to let others know we are fetching.
                event = threading.Event()
                self._flight_cache[cache_key] = event
                is_fetching = False
                
        if is_fetching:
            # Wait for the first thread to finish fetching
            self.logger.debug(f"Coalescing request for {symbol} {timeframe} - waiting on flight cache")
            event.wait(timeout=30.0)
            
            # Now that it's done, try hitting the OhlcvCache again
            cached_bars = self.ohlcv_cache.get(symbol, timeframe)
            if cached_bars and len(cached_bars) >= min(limit, self.ohlcv_cache.maxlen[symbol][timeframe]):
                self.logger.debug(f"Coalesced request fulfilled from cache for {symbol} {timeframe}")
                return self._bars_to_df(symbol, timeframe, cached_bars).tail(limit)

            # If for some reason the cache is still empty, fallback locally (should be rare)
            self.logger.warning(f"Coalesced request for {symbol} {timeframe} finished but cache misses. Falling back.")
            return _fetch()

        # We are the fetching thread. Execute the fetch, then notify everyone else.
        # NOTE: _fetch is called directly (not via _rate_limited_call): it is an
        # orchestration of DB reads + network calls, and each network call inside
        # it is already individually rate-limited. Wrapping the whole thing would
        # double-consume tokens and multiply retries (inner ~102s backoff times
        # outer 4 retries = minutes of blocking).
        try:
            return _fetch()
        finally:
            with self._flight_lock:
                event.set()
                if cache_key in self._flight_cache:
                    del self._flight_cache[cache_key]
    def _bars_to_df(self, symbol: str, timeframe: str, bars: list) -> pd.DataFrame:
        """
        Build a DataFrame from cached OHLCV bars, reusing the previous build
        when the underlying bars are unchanged (same length, last bar time,
        close and volume). Callers must treat the returned frame as read-only.
        """
        last = bars[-1]
        state_key = (len(bars), last.get('time'), last.get('close'), last.get('volume'))

        cache_key = (symbol, timeframe)
        with self._df_cache_lock:
            cached = self._ohlcv_df_cache.get(cache_key)
            if cached is not None and cached[0] == state_key:
                return cached[1]

        df = pd.DataFrame(bars)
        df['timestamp'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('timestamp', inplace=True)

        with self._df_cache_lock:
            self._ohlcv_df_cache[cache_key] = (state_key, df)
        return df

    def update_ohlcv_from_tick(self, symbol: str, price: float, volume: float = 0.0, ts: Optional[float] = None):
        """Update rolling OHLCV cache from a live tick."""
        if ts is None:
            ts = time.time()
            
        # [BUG FIX] Detect if timestamp is in milliseconds (e.g. from WebSocket)
        # 3e10 is approx Year 2920, safely distinguishing seconds from milliseconds (currently ~1.76e9 vs ~1.76e12)
        if ts > 3e10:
            ts = ts / 1000.0
            
        self.ohlcv_cache.update_from_tick(symbol, price, volume, ts)
    
    def warmup_cache(self, symbols: List[str], timeframes: List[str], limit: int = 500):
        """
        Asynchronously warm up the OHLCV cache for multiple symbols and timeframes.
        This submits fetch tasks to the background persistence executor to avoid blocking the main thread,
        and uses request coalescing naturally via `get_ohlcv`.
        """
        if not symbols or not timeframes:
            return

        self.logger.info(f"Submitting cache warmup for {len(symbols)} symbols across {len(timeframes)} timeframes.")
        
        def _warm_task(sym, tf):
            try:
                # get_ohlcv will automatically handle limits, caching, and coalescing
                self.get_ohlcv(sym, tf, limit=limit)
            except Exception as e:
                self.logger.debug(f"Warmup failed for {sym} {tf}: {e}")

        # Submit tasks sequentially to the single worker pool so we don't burst the rate limiter
        for symbol in symbols:
            for tf in timeframes:
                self._persistence_executor.submit(_warm_task, symbol, tf)
    
    
    def _on_bar_complete(self, symbol: str, timeframe: str, bar: dict):
        """
        Called when a candle period closes. 
        Instead of persisting the in-memory bar directly, trigger a background verification fetch.
        This implements the "Verify-on-Write" pattern to ensure data integrity.
        """
        # DEBUG: Trace callback
        self.logger.info(f"CALLBACK: Bar complete for {symbol} {timeframe} @ {bar.get('time')}. DB={self.market_db is not None}")
        
        if not self.market_db:
            return
            
        # Submit to background executor to avoid blocking WebSocket thread
        try:
            self._persistence_executor.submit(
                self._persist_optimistic_candle, 
                symbol, 
                timeframe, 
                bar
            )
            self.logger.debug(f"CALLBACK: Subscribed persist task for {symbol} {timeframe}")
        except Exception as e:
            self.logger.error(f"CALLBACK: Failed to submit task: {e}")

    def _persist_optimistic_candle(self, symbol: str, timeframe: str, bar: dict):
        """
        Worker method to persist the cached candle directly to the DB.
        Optimistic write: Assumes WebSocket data is correct for the closed candle.
        """
        try:
            # Format for DB (Direct from OhlcvCache bar)
            # Market Data Table Schema: symbol, timeframe, timestamp, open, high, low, close, volume
            # No 'trades' column required.
            
            df = pd.DataFrame([bar])
            df['timestamp'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('timestamp', inplace=True)
            
            # Persist to DB
            self.market_db.insert_market_data(df, symbol, timeframe)
            self.logger.info(f"PERSIST: Wrote {symbol}/{timeframe} candle @ {bar['time']} (Optimistic)")
                
        except Exception as e:
            self.logger.error(f"Failed to persist optimistic candle for {symbol}/{timeframe}: {e}", exc_info=True)
    
    def _get_api_symbol(self, symbol: str) -> str:
        """Get symbol formatted for API requests."""
        # Currently a pass-through, but centralizes symbol formatting logic
        return symbol

    def _get_interval_ms(self, timeframe: str) -> int:
        """Get interval in milliseconds for a timeframe."""
        intervals = {
            '1m': 60 * 1000,
            '5m': 5 * 60 * 1000,
            '15m': 15 * 60 * 1000,
            '30m': 30 * 60 * 1000,
            '1h': 60 * 60 * 1000,
            '4h': 4 * 60 * 60 * 1000,
            '1d': 24 * 60 * 60 * 1000,
        }
        return intervals.get(timeframe, 60 * 60 * 1000)
    
    def _append_current_candle(self, symbol: str, timeframe: str, api_symbol: str):
        """Fetch in-progress candle to ensure no boundary gap. Uses rate limiter."""
        try:
            now_ms = int(time.time() * 1000)
            interval_ms = self._get_interval_ms(timeframe)
            current_bar_start = now_ms - (now_ms % interval_ms)
            
            # Route through rate limiter to prevent 429 bursts
            candles = self._candles_snapshot_smart(api_symbol, timeframe, current_bar_start, now_ms)
            if candles:
                bar = {
                    'time': candles[-1]['t'] // 1000,
                    'open': float(candles[-1]['o']),
                    'high': float(candles[-1]['h']),
                    'low': float(candles[-1]['l']),
                    'close': float(candles[-1]['c']),
                    'volume': float(candles[-1]['v']),
                }
                dq = self.ohlcv_cache.cache[symbol][timeframe]
                # Only append if not already present
                if not dq or dq[-1].get('time') != bar['time']:
                    dq.append(bar)
                    self.logger.debug(f"Appended current candle for {symbol}/{timeframe}")
        except Exception as e:
            self.logger.warning(f"Failed to append current candle for {symbol}/{timeframe}: {e}")
    
    def _initialize_live_data(self, symbol: str, api_symbol: str, required_timeframes: List[str] = None, max_retries: int = 2) -> bool:
        """
        Fetch in-progress candles for all timeframes and subscribe to WebSocket.
        If fails after retries, adds to pending queue for later retry.
        """
        # timeframes = ['5m', '15m', '1h', '4h']
        
        try:
            # Step 1: No API fetch needed - Background Fetcher handles all history/gap-filling
            # We just enable the WebSocket subscription here
            
            # Step 2: Finalize subscription (caches, active set)
            self._finalize_subscription(symbol, required_timeframes)
            
            # Step 3: Verify subscription is active
            if symbol in self._subscribed_symbols:
                self.logger.info(f"Initialized live data for {symbol}")
                self._pending_init_symbols.discard(symbol)
                return True
                
        except Exception as e:
            self.logger.warning(f"Live data init failed for {symbol}: {e}")
        
        # If we get here, initialization failed or was incomplete
        # Add to pending queue and continue (background worker will pick it up)
        self._pending_init_symbols.add(symbol)
        self.logger.warning(f"Deferred {symbol} initialization to retry queue")
        return False
    
    def _async_init_worker(self, symbol: str, api_symbol: str, required_timeframes: List[str] = None):
        """Background worker for initializing live data."""
        try:
            self._initializing_symbols.add(symbol)
            success = self._initialize_live_data(symbol, api_symbol, required_timeframes)
            if not success:
               self.logger.debug(f"Async init failed for {symbol}, kept in pending.")
        except Exception as e:
            self.logger.error(f"Async init worker error for {symbol}: {e}")
            self._pending_init_symbols.add(symbol)
        finally:
             self._initializing_symbols.discard(symbol)

    def retry_pending_subscriptions(self):
        """Called periodically to retry failed subscriptions (ASYNC)."""
        if not self._pending_init_symbols:
            return
        
        # Snapshot copy to iterate safely
        pending = list(self._pending_init_symbols)
        
        # Don't log spam if empty or just checking
        if not pending: return

        # Rate limit submission to avoid flooding executor if queue is huge
        submitted = 0
        for symbol in pending:
            if symbol in self._initializing_symbols:
                continue
                
            # Re-attempt with fresh data
            api_symbol = symbol  # May need conversion for spot
            asset_info = self._get_asset_info_for_symbol(symbol)
            if not asset_info:
                # For spot assets: BTC_SPOT -> UBTC -> @109. Static map with
                # strip-_SPOT fallback, same resolution as get_ohlcv — names
                # like TRUMP_SPOT are not in the static map and previously
                # leaked unresolved to SDK lookups (KeyError noise).
                if symbol.endswith('_SPOT') or symbol in self.SPOT_INTERNAL_TO_API:
                    api_token_name = self.SPOT_INTERNAL_TO_API.get(
                        symbol, symbol.replace('_SPOT', ''))
                    spot_api = self.get_spot_api_name(api_token_name)
                    if spot_api:
                        api_symbol = spot_api
                else:
                    spot_api = self.get_spot_api_name(symbol)
                    if spot_api:
                        api_symbol = spot_api
            
            # Submit to background executor
            self._persistence_executor.submit(self._async_init_worker, symbol, api_symbol)
            submitted += 1
            if submitted > 5: # Limit batch size to prevent storm
                break
    
    def get_all_prices(self) -> Dict[str, float]:
        """Get current prices for all symbols."""
        def _fetch():
            return self.info.all_mids()

        try:
            all_mids = self._rate_limited_call(_fetch)
            return {k: float(v) for k, v in all_mids.items()}
        except Exception as e:
            self.logger.error(f"Error getting all prices: {e}")
            return {}

    def get_order_book(self, symbol: str, depth: int = 20) -> Optional[Dict[str, Any]]:
        """Get order book for a symbol."""
        def _fetch():
            return self.info.l2_snapshot(symbol)

        try:
            book = self._rate_limited_call(_fetch)
            return {
                'symbol': symbol,
                'bids': book.get('levels', [[]])[0][:depth],
                'asks': book.get('levels', [[]])[1][:depth] if len(book.get('levels', [])) > 1 else [],
                'timestamp': datetime.now(),
            }
        except Exception as e:
            self.logger.error(f"Error getting order book for {symbol}: {e}")
            return None
    
    # =========================================================================
    # LEVERAGE & RISK
    # =========================================================================
    
    def get_asset_meta(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a specific asset (including max leverage).
        
        Args:
            symbol: Asset symbol (e.g. "BTC")
            
        Returns:
            Dict containing asset metadata or None if not found
        """
        def _fetch():
            return self.info.meta()

        try:
            meta = self._rate_limited_call(_fetch)
            if not meta or 'universe' not in meta:
                return None
            
            for asset_info in meta['universe']:
                if asset_info['name'] == symbol:
                    return asset_info
            
            return None
        except Exception as e:
            self.logger.error(f"Error getting asset meta for {symbol}: {e}")
            return None

    def update_leverage(self, symbol: str, leverage: int, is_cross: bool = True) -> bool:
        """
        Update leverage for a specific asset.
        
        Args:
            symbol: Asset symbol
            leverage: Target leverage (integer)
            is_cross: Whether to use cross margin (default: True)
            
        Returns:
            True if successful
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return False

        try:
            # The exchange rejects non-positive leverage with an opaque 422
            # ("Failed to deserialize the JSON body"). Clamp defensively —
            # sub-1x sizing leverage is a notional concept, not a valid
            # exchange setting (live failure 2026-07-03).
            leverage = max(1, int(leverage))

            # Check for isolated-only assets
            asset_info = self._get_asset_info_for_symbol(symbol)
            if asset_info and asset_info.get('onlyIsolated', False):
                if is_cross:
                    self.logger.warning(f"Asset {symbol} only supports Isolated Margin. Forcing is_cross=False.")
                    is_cross = False

            self.logger.info(f"Updating leverage for {symbol} to {leverage}x (Cross: {is_cross})")
            def _update():
                return self.exchange.update_leverage(leverage, symbol, is_cross)
            result = self._rate_limited_call(_update)
            
            # Some assets (HIP-3 equities) are isolated-only but do not
            # always carry the onlyIsolated flag in cached metadata — retry
            # as isolated instead of failing (live 2026-07-03, xyz:INTC).
            if (result.get('status') != 'ok' and is_cross
                    and 'cross margin is not allowed' in str(result.get('response', '')).lower()):
                self.logger.warning(f"{symbol} rejects cross margin; retrying leverage update as isolated")
                def _update_isolated():
                    return self.exchange.update_leverage(leverage, symbol, False)
                result = self._rate_limited_call(_update_isolated)

            if result.get('status') == 'ok':
                self.logger.info(f"Successfully updated leverage for {symbol}")
                # Invalidate positions cache as leverage changes affect margin calculation
                self.cache.invalidate("positions")
                return True
            else:
                self.logger.error(f"Failed to update leverage: {result}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error updating leverage for {symbol}: {e}")
            return False
    
    # =========================================================================
    # ACCOUNT & POSITIONS
    # =========================================================================
    
    def _get_account_abstraction_mode(self) -> str:
        """
        Account abstraction mode: 'unifiedAccount' | 'portfolioMargin' |
        'disabled' (legacy split spot/perp wallets).

        Hyperliquid's Dec-2025 upgrade merged spot and perp balances for
        unified/portfolio-margin accounts; for those, per-dex perp user
        states are NOT meaningful and all balances/holds live in the spot
        clearinghouse state. Cached for the process lifetime (mode changes
        require an explicit user action); falls back to 'disabled' so the
        legacy path remains the default on query failure.
        """
        mode = getattr(self, '_abstraction_mode', None)
        if mode is not None:
            return mode
        try:
            def _query():
                return self.info.query_user_abstraction_state(self.public_account_address)
            state = self._rate_limited_call(_query, weight=2)
            mode = state if state in ('unifiedAccount', 'portfolioMargin') else 'disabled'
            self.logger.info(f"Account abstraction mode: {state} -> using "
                             f"{'unified' if mode != 'disabled' else 'legacy'} balance accounting")
        except Exception as e:
            self.logger.warning(f"Could not query account abstraction state ({e}); "
                                f"assuming legacy split wallets")
            mode = 'disabled'
        self._abstraction_mode = mode
        return mode

    def _get_unified_balance(self) -> Optional[Dict[str, Any]]:
        """
        Balance for unified/portfolio-margin accounts: USDC collateral and
        free margin come from the SPOT clearinghouse state
        (tokenToAvailableAfterMaintenance, token 0 = USDC); unrealized PnL
        still comes from the perp positions.
        """
        def _fetch_spot_state():
            return self.info.spot_user_state(self.public_account_address)

        spot_state = self._rate_limited_call(_fetch_spot_state, weight=2)
        usdc_total = 0.0
        for bal in (spot_state or {}).get('balances', []):
            if bal.get('coin') == 'USDC':
                usdc_total = float(bal.get('total', 0))
                break

        available = usdc_total
        for token, amount in (spot_state or {}).get('tokenToAvailableAfterMaintenance', []):
            if token == 0:  # USDC
                available = float(amount)
                break

        unrealized_pnl = 0.0
        try:
            def _fetch_user_state():
                return self.info.user_state(self.public_account_address)

            user_state = self._rate_limited_call(_fetch_user_state, weight=2)
            for ap in (user_state or {}).get('assetPositions', []):
                unrealized_pnl += float(ap.get('position', {}).get('unrealizedPnl', 0))
        except Exception as e:
            self.logger.debug(f"Unified balance: could not read perp positions for PnL: {e}")

        total_equity = usdc_total + unrealized_pnl
        return {
            'wallet_address': self.public_account_address,
            'total_equity': total_equity,
            'free_margin': available,
            'used_margin': max(0.0, total_equity - available),
            'unrealized_pnl': unrealized_pnl,
        }

    def get_account_balance(self) -> Optional[Dict[str, Any]]:
        """Get account balance and margin information."""
        cache_key = "account_balance"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        if self._get_account_abstraction_mode() != 'disabled':
            def _fetch_unified():
                result = self._get_unified_balance()
                if result is not None:
                    self.cache.set(cache_key, result, ttl=self.cache_ttl_positions)
                return result
            try:
                return self._rate_limited_call(_fetch_unified)
            except Exception as e:
                # Same contract as the legacy path: None on total failure so
                # callers (PortfolioManager) hit their fallback, never an
                # unhandled exception mid-cycle.
                self.logger.error(f"Failed to fetch unified balance: {e}")
                return None

        def _fetch():
            # Iterate through all discovered DEXs to aggregate balance
            # HIP-3/Spot assets live in different contexts
            # perp_dexs is a list of dex name strings: "" for native, "Hypurr" etc for HIP-3
            dexs_to_query = self.perp_dexs if self.perp_dexs else [""]
            if "" not in dexs_to_query:
                dexs_to_query = [""] + dexs_to_query

            total_equity = 0.0
            total_free_margin = 0.0 # Will calculate as Total Equity - Total Margin Used
            total_used_margin = 0.0
            total_unrealized_pnl = 0.0
            
            # Track main withdrawable to detect shared collateral vs segregated
            main_withdrawable = 0.0
            
            fetches_succeeded = 0
            
            for i, dex_name in enumerate(dexs_to_query):
                try:
                    # Fetch state for this context
                    # Note: We don't cache individual DEX calls here efficiently yet,
                    # but rate limiter handles them.
                    # SDK expects dex as a string: "" for native, dex name for HIP-3
                    def _fetch_state(dex=dex_name):
                        return self.info.user_state(self.public_account_address, dex=dex)

                    user_state = self._rate_limited_call(_fetch_state, weight=2)

                    margin_summary = user_state.get('marginSummary', {})
                    account_value = float(margin_summary.get('accountValue', 0))
                    used_margin = float(margin_summary.get('totalMarginUsed', 0))
                    unrealized_pnl = float(margin_summary.get('totalUnrealizedPnl', 0))
                    withdrawable = float(margin_summary.get('withdrawable', 0))
                    
                    if dex_name == "":
                        # Native Context (Baseline)
                        total_equity += account_value
                        main_withdrawable = withdrawable
                    else:
                        # Secondary Context (HIP-3 / Spot)
                        
                        # Logic: Account Value = Withdrawable + Margin + PnL
                        # If 'withdrawable' matches Main (within epsilon), it's shared collateral.
                        # We shouldn't count it twice.
                        # If it's different (segregated), we count it.
                        
                        equity_contribution = account_value
                        
                        # Check for shared collateral (approx match)
                        if abs(withdrawable - main_withdrawable) < 1.0: # $1 tolerance
                            # Shared: Remove the cash component, keep Margin+PnL
                            equity_contribution -= withdrawable
                            
                        total_equity += equity_contribution
                    
                    total_used_margin += used_margin
                    total_unrealized_pnl += unrealized_pnl
                    fetches_succeeded += 1
                    
                except Exception as e:
                    self.logger.warning(f"Failed to fetch balance for dex '{dex_name}': {e}")

            if fetches_succeeded == 0:
                 self.logger.error("Failed to fetch balance for ANY dex context. Returning None to trigger fallback.")
                 return None

            result = {
                'wallet_address': self.public_account_address,
                'total_equity': total_equity,
                'free_margin': total_equity - total_used_margin,
                'used_margin': total_used_margin,
                'unrealized_pnl': total_unrealized_pnl,
            }
            
            self.cache.set(cache_key, result, ttl=self.cache_ttl_positions)
            return result
        
        return self._rate_limited_call(_fetch)
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions across all DEXs (native + HIP-3)."""
        cache_key = "positions"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        def _fetch():
            all_positions = []
            failed_dexs = []
            
            # Determine lists of DEXs to query
            # HIP-3 (Spot/Hyperliquidity) support is now enabled by default.
            # We query all discovered DEX names: "" for native, "Hypurr" etc for HIP-3.
            
            dexs_to_query = self.perp_dexs if self.perp_dexs else [""]
            
            # perp_dexs is a list of dex name strings.
            # The SDK expects string dex names for user_state calls.
            
            for dex_name in dexs_to_query:
                try:
                    # Pass the dex name explicitly. Native is "" (empty string).
                    def _fetch_state(dex=dex_name):
                        return self.info.user_state(self.public_account_address, dex=dex)

                    user_state = self._rate_limited_call(_fetch_state, weight=2)

                    for pos in user_state.get('assetPositions', []):
                        position_data = pos.get('position', {})
                        size = float(position_data.get('szi', 0))
                        
                        if size != 0:
                            coin = position_data.get('coin')
                            symbol = coin
                            
                            # Handle HIP-3 Symbol Prefixing (e.g. 'Hypurr:PLTR')
                            # If dex_name is present (not empty), it's a HIP-3 asset.
                            # We use the dex_name as the prefix to disambiguate.
                            # e.g. 'Hypurr' -> 'Hypurr:PLTR'
                            
                            # Check truthiness (empty string is falsy, which is correct for Native)
                            # Also check if prefix is already applied (some SDK versions might?)
                            if dex_name:
                                 if not coin.startswith(f"{dex_name}:"):
                                     symbol = f"{dex_name}:{coin}"
                            
                            all_positions.append({
                                'symbol': symbol,
                                'size': size,
                                'side': 'long' if size > 0 else 'short',
                                'entry_price': float(position_data.get('entryPx', 0)),
                                'mark_price': float(position_data.get('positionValue', 0)) / abs(size) if size != 0 else 0,
                                'unrealized_pnl': float(position_data.get('unrealizedPnl', 0)),
                                'leverage': position_data.get('leverage', {}),
                            })
                except Exception as e:
                    err_str = str(e)
                    # Re-raise retryable errors (429, 5xx, connection issues) for _rate_limited_call to handle
                    # This ensures transient failures get proper backoff/retry
                    if any(code in err_str for code in ["429", "500", "502", "503"]):
                        raise
                    if any(pattern in err_str.lower() for pattern in [
                        "connectionerror", "connection refused", "connection reset", "timeout"
                    ]):
                        raise
                    
                    # Log non-retryable errors but continue to get positions from other DEXs
                    # This is critical for ghost position detection - partial data is better than none
                    dex_label = dex_name if dex_name else "native"
                    failed_dexs.append(dex_label)
                    self.logger.warning(f"Failed to fetch positions for dex '{dex_label}' (non-retryable): {e}")
            
            # Log a summary warning if any DEXs failed - important for debugging ghost positions
            if failed_dexs:
                self.logger.warning(f"Position fetch incomplete - failed DEXs: {failed_dexs}. Ghost positions on these DEXs may be missed!")
            
            self.cache.set(cache_key, all_positions, ttl=self.cache_ttl_positions)
            return all_positions
        
        ndex = len(self.perp_dexs) if self.perp_dexs else 1
        return self._rate_limited_call(_fetch, weight=2 * ndex)
    
    def get_position(self, symbol: str) -> Dict[str, Any]:
        """Get current position for a specific symbol."""
        positions = self.get_positions()
        for pos in positions:
            if pos['symbol'] == symbol:
                return pos
        return {'symbol': symbol, 'size': 0.0, 'side': 'neutral', 'entry_price': 0.0, 'unrealized_pnl': 0.0}
    
    def get_user_fills(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get recent fills (trade history) for the user.
        
        Args:
            limit: Maximum number of fills to return
            
        Returns:
            List of fill dictionaries with keys: coin, side, px, sz, time, dir, etc.
        """
        cache_key = f"user_fills_{limit}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        def _fetch():
            try:
                fills = self.info.user_fills(self.public_account_address)
                # Sort by time descending and limit
                fills = sorted(fills, key=lambda x: x.get('time', 0), reverse=True)[:limit]
                self.cache.set(cache_key, fills, ttl=5.0)  # Short TTL for fills
                return fills
            except Exception as e:
                self.logger.error(f"Error fetching user fills: {e}")
                return []
        
        return self._rate_limited_call(_fetch, weight=20)
    
    # =========================================================================
    # FUND TRANSFERS & COLLATERAL MANAGEMENT
    # =========================================================================
    
    def get_spot_balance(self, token: str = "USDC") -> float:
        """Get balance of a specific token in spot account."""
        def _fetch():
            return self.info.spot_user_state(self.public_account_address)
        
        try:
            spot_state = self._rate_limited_call(_fetch, weight=2)
            for balance in spot_state.get('balances', []):
                if balance.get('coin') == token:
                    return float(balance.get('total', 0))
            return 0.0
        except Exception as e:
            self.logger.error(f"Error getting spot balance for {token}: {e}")
            return 0.0
    
    def get_perp_balance(self) -> Dict[str, float]:
        """Get perp account balance and margin info."""
        # Unified/portfolio-margin accounts: the perp marginSummary is not
        # meaningful — collateral lives in the spot clearinghouse state.
        if self._get_account_abstraction_mode() != 'disabled':
            try:
                unified = self._rate_limited_call(self._get_unified_balance)
                if unified:
                    return {
                        'account_value': unified['total_equity'],
                        'total_margin_used': unified['used_margin'],
                        'withdrawable': unified['free_margin'],
                        'total_ntl_pos': 0.0,
                        'unrealized_pnl': unified['unrealized_pnl'],
                    }
            except Exception as e:
                self.logger.error(f"Error getting unified perp balance: {e}")
                return {'account_value': 0, 'total_margin_used': 0, 'withdrawable': 0}

        def _fetch():
            return self.info.user_state(self.public_account_address)

        try:
            user_state = self._rate_limited_call(_fetch, weight=2)
            margin_summary = user_state.get('marginSummary', {})
            
            return {
                'account_value': float(margin_summary.get('accountValue', 0)),
                'total_margin_used': float(margin_summary.get('totalMarginUsed', 0)),
                # Fallback to account_value - margin_used if withdrawable (or similar key) is missing
                'withdrawable': float(margin_summary.get('withdrawable', float(margin_summary.get('accountValue', 0)) - float(margin_summary.get('totalMarginUsed', 0)))),
                'total_ntl_pos': float(margin_summary.get('totalNtlPos', 0)),
                'unrealized_pnl': float(margin_summary.get('totalUnrealizedPnl', 0)),
            }
        except Exception as e:
            self.logger.error(f"Error getting perp balance: {e}")
            return {'account_value': 0, 'total_margin_used': 0, 'withdrawable': 0}
    
    def transfer_usd_to_perp(self, amount: float) -> bool:
        """
        Transfer USDC from spot account to perp account.
        
        Required before opening perp positions if insufficient perp balance.
        
        Args:
            amount: USDC amount to transfer
            
        Returns:
            True if successful
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return False
        
        try:
            # Round to 6 decimals (USDC precision) to avoid API errors
            amount = round(amount, 6)
            self.logger.info(f"Transferring ${amount:.6f} USDC from spot to perp")
            def _transfer():
                return self.exchange.usd_class_transfer(amount, to_perp=True)
            result = self._rate_limited_call(_transfer)
            
            if result.get('status') == 'ok':
                self.logger.info(f"Successfully transferred ${amount:.6f} to perp account")
                # Invalidate cache
                self.cache.invalidate("account_balance")
                return True
            else:
                self.logger.error(f"Transfer failed: {result}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error transferring to perp: {e}")
            return False
    
    def transfer_usd_to_spot(self, amount: float) -> bool:
        """
        Transfer USDC from perp account to spot account.
        
        Required before buying spot tokens if insufficient spot balance.
        
        Args:
            amount: USDC amount to transfer
            
        Returns:
            True if successful
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return False
        
        try:
            # Round to 6 decimals (USDC precision) to avoid API errors
            amount = round(amount, 6)
            self.logger.info(f"Transferring ${amount:.6f} USDC from perp to spot")
            def _transfer():
                return self.exchange.usd_class_transfer(amount, to_perp=False)
            result = self._rate_limited_call(_transfer)
            
            if result.get('status') == 'ok':
                self.logger.info(f"Successfully transferred ${amount:.2f} to spot account")
                # Invalidate cache
                self.cache.invalidate("account_balance")
                return True
            else:
                self.logger.error(f"Transfer failed: {result}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error transferring to spot: {e}")
            return False
    
    def add_position_margin(self, symbol: str, amount: float) -> bool:
        """
        Add margin to an isolated position to reduce liquidation risk.
        
        Args:
            symbol: Position symbol
            amount: USDC amount to add (positive) or remove (negative)
            
        Returns:
            True if successful
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return False
        
        try:
            self.logger.info(f"Adding ${amount:.2f} margin to {symbol} position")
            def _update():
                return self.exchange.update_isolated_margin(amount, symbol)
            result = self._rate_limited_call(_update)
            
            if result.get('status') == 'ok':
                self.logger.info(f"Successfully updated margin for {symbol}")
                self.cache.invalidate("positions")
                return True
            else:
                self.logger.error(f"Margin update failed: {result}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error updating margin for {symbol}: {e}")
            return False
    
    def _get_cached_user_state(self) -> Optional[Dict[str, Any]]:
        """
        Get user state with caching to reduce redundant API calls.
        Uses retry logic before falling back to stale cache.
        
        Returns:
            User state dict or None if API call fails
        """
        now = time.time()
        
        # Return cached value if still valid
        if self._user_state_cache is not None:
            if now - self._user_state_cache_time < self._user_state_cache_ttl:
                return self._user_state_cache
        
        # Fetch fresh data with retry logic
        try:
            user_state = self._rate_limited_call(
                self.info.user_state,
                self.public_account_address,
                weight=2
            )
            self._user_state_cache = user_state
            self._user_state_cache_time = time.time()
            return user_state
        except Exception as e:
            self.logger.warning(f"Failed to fetch user_state after retries: {e}")
            # Return stale cache if available (defensive fallback)
            if self._user_state_cache is not None:
                self.logger.warning("Using stale user_state cache due to API failure")
                return self._user_state_cache
            return None
    
    def get_position_margin_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed margin information for a specific position.
        
        Returns liquidation price, margin ratio, etc.
        """
        try:
            user_state = self._get_cached_user_state()
            if not user_state:
                return None
            
            for pos in user_state.get('assetPositions', []):
                position_data = pos.get('position', {})
                if position_data.get('coin') == symbol:
                    size = float(position_data.get('szi', 0))
                    if size == 0:
                        return None
                    
                    leverage_info = position_data.get('leverage', {})
                    
                    return {
                        'symbol': symbol,
                        'size': size,
                        'entry_price': float(position_data.get('entryPx', 0)),
                        'liquidation_price': float(position_data.get('liquidationPx', 0)) if position_data.get('liquidationPx') else None,
                        'margin_used': float(position_data.get('marginUsed', 0)),
                        'unrealized_pnl': float(position_data.get('unrealizedPnl', 0)),
                        'leverage_type': leverage_info.get('type', 'cross'),
                        'leverage_value': int(leverage_info.get('value', 1)),
                        'position_value': float(position_data.get('positionValue', 0)),
                    }
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting margin info for {symbol}: {e}")
            return None
    
    def check_liquidation_risk(self, symbol: str, threshold_pct: float = 10.0) -> Dict[str, Any]:
        """
        Check if a position is at risk of liquidation.
        
        Args:
            symbol: Position symbol
            threshold_pct: Percentage distance from liquidation to trigger warning
            
        Returns:
            Dict with risk assessment
        """
        try:
            margin_info = self.get_position_margin_info(symbol)
            if not margin_info:
                return {'has_position': False}
            
            current_price = self.get_current_price(symbol)
            liq_price = margin_info.get('liquidation_price')
            
            if not current_price or not liq_price or liq_price == 0:
                return {
                    'has_position': True,
                    'can_calculate': False,
                    'margin_info': margin_info,
                }
            
            # Calculate distance to liquidation
            size = margin_info['size']
            if size > 0:  # Long position
                distance_pct = ((current_price - liq_price) / current_price) * 100
            else:  # Short position
                distance_pct = ((liq_price - current_price) / current_price) * 100
            
            at_risk = distance_pct <= threshold_pct
            
            return {
                'has_position': True,
                'can_calculate': True,
                'symbol': symbol,
                'current_price': current_price,
                'liquidation_price': liq_price,
                'distance_to_liquidation_pct': distance_pct,
                'at_risk': at_risk,
                'margin_info': margin_info,
            }
            
        except Exception as e:
            self.logger.error(f"Error checking liquidation risk for {symbol}: {e}")
            return {'has_position': False, 'error': str(e)}
    
    def ensure_perp_funds(self, required_amount: float) -> bool:
        """
        Ensure perp account has sufficient funds, transferring from spot if needed.
        
        Args:
            required_amount: Required USDC in perp account
            
        Returns:
            True if funds are available (or transferred successfully)
        """
        perp_balance = self.get_perp_balance()
        available = perp_balance.get('withdrawable', 0)
        
        if available >= required_amount:
            return True
        
        # Need to transfer from spot
        shortfall = required_amount - available
        spot_balance = self.get_spot_balance('USDC')
        
        if spot_balance < shortfall:
            self.logger.error(f"Insufficient funds: need ${required_amount:.2f} in perp, "
                            f"have ${available:.2f} perp + ${spot_balance:.2f} spot")
            return False
        
        return self.transfer_usd_to_perp(shortfall)
    
    def ensure_spot_funds(self, required_amount: float) -> bool:
        """
        Ensure spot account has sufficient USDC, transferring from perp if needed.
        
        Args:
            required_amount: Required USDC in spot account
            
        Returns:
            True if funds are available (or transferred successfully)
        """
        spot_balance = self.get_spot_balance('USDC')
        
        if spot_balance >= required_amount:
            return True
        
        # Need to transfer from perp
        shortfall = required_amount - spot_balance
        perp_balance = self.get_perp_balance()
        withdrawable = perp_balance.get('withdrawable', 0)
        
        if withdrawable < shortfall:
            self.logger.error(f"Insufficient funds: need ${required_amount:.2f} in spot, "
                            f"have ${spot_balance:.2f} spot + ${withdrawable:.2f} perp withdrawable")
            return False
        
        return self.transfer_usd_to_spot(shortfall)
    
    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================
    
    # Smart order execution parameters (not user-configurable)
    _ORDER_WALK_MAX_ATTEMPTS = 5  # Max price improvement attempts
    _ORDER_WALK_STEP_BPS = 20  # 0.2% price step per attempt (20 basis points)
    _ORDER_WALK_DELAY = 0.3  # Seconds between attempts
    _INITIAL_SLIPPAGE_BPS = 50  # 0.5% initial slippage tolerance (aggressive for IOC)
    _MAX_SLIPPAGE_BPS = 100  # 1% max slippage for aggressive fills
    # Slippage guard on a native stop-MARKET order's limit price. Wide on
    # purpose: a protective stop must fill once triggered even in a fast move
    # (the exact scenario it exists for), so we accept worse price for certainty.
    _STOP_LIMIT_SLIPPAGE_BPS = 500  # 5%
    
    def _resolve_market_info(
        self,
        symbol: str,
        market_type: str = "perp"
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve market information for any market type.
        
        Args:
            symbol: Trading symbol (e.g., "BTC" for perp, "BTC/USDC" for spot)
            market_type: "perp", "hip3", or "spot"
            
        Returns:
            Dict with 'symbol', 'price', 'sz_decimals', 'tick_size'
        """
        try:
            if market_type == "spot":
                # Parse spot pair (e.g., "BTC/USDC" or "BTC" defaults to /USDC)
                if "/" in symbol:
                    base_token, quote_token = symbol.split("/", 1)
                else:
                    base_token, quote_token = symbol, "USDC"
                
                # Use static mapping to get the actual spot token name
                # This prevents dangerous heuristic-based matching
                actual_base_token = self.get_spot_token_for_perp(base_token)
                if not actual_base_token:
                    self.logger.error(f"No spot mapping found for {base_token} - check PERP_TO_SPOT_MAPPING")
                    return None
                
                # Get spot metadata
                spot_meta = self.get_spot_meta()
                if not spot_meta:
                    return None
                
                token_list = spot_meta.get('tokens', [])
                
                # Find the pair info
                pair_name = None
                sz_decimals = 6  # Default for spot
                
                for pair in spot_meta.get('universe', []):
                    tokens = pair.get('tokens', [])
                    if len(tokens) >= 2:
                        if (tokens[0] < len(token_list) and tokens[1] < len(token_list)):
                            pair_base = token_list[tokens[0]].get('name', '')
                            pair_quote = token_list[tokens[1]].get('name', '')
                            if pair_base == actual_base_token and pair_quote == quote_token:
                                pair_name = pair.get('name')
                                sz_decimals = token_list[tokens[0]].get('szDecimals', 6)
                                break
                
                if not pair_name:
                    self.logger.error(f"Spot pair {actual_base_token}/{quote_token} not found on exchange")
                    return None
                
                # Get price
                price = self.get_spot_price(actual_base_token, quote_token)
                if not price:
                    return None
                
                # Log the mapping used
                if actual_base_token != base_token:
                    self.logger.debug(f"Using mapping: {base_token}/{quote_token} -> {actual_base_token}/{quote_token}")
                
                return {
                    'symbol': pair_name,
                    'display_symbol': f"{actual_base_token}/{quote_token}",
                    'original_symbol': f"{base_token}/{quote_token}",
                    'price': price,
                    'sz_decimals': sz_decimals,
                    'tick_size': 0.0001,  # Default for spot
                    'market_type': 'spot',
                }
            else:
                # Perp or HIP-3 (both use same symbol format)
                # HIP-3 symbols use prefix format: "xyz:TSLA", "Hypurr:PLTR", etc.
                # The SDK's name_to_coin mapping expects this full prefixed format
                # e.g., 'xyz:TSLA' -> 'xyz:TSLA' is in the mapping, but 'TSLA' is NOT
                clean_symbol = symbol  # Keep prefix for SDK - it's required

                # Asset-info lookup handles both prefixed ('xyz:GOLD') and
                # bare names internally; pass the symbol as-is.
                asset_info = self._get_asset_info_for_symbol(symbol)
                price = self.get_current_price(symbol) # This handles stripping internally now

                if not price:
                    return None

                if not asset_info:
                    self.logger.warning(
                        f"No asset metadata for {symbol}; defaulting sz_decimals=2 "
                        f"(small orders on high-priced assets may round to zero)")

                return {
                    'symbol': clean_symbol, # Important: Pass stripped symbol to SDK
                    'display_symbol': symbol,
                    'price': price,
                    'sz_decimals': asset_info.get('szDecimals', 2) if asset_info else 2,
                    'tick_size': self._get_tick_size(price),
                    'market_type': market_type,
                }
                
        except Exception as e:
            self.logger.error(f"Error resolving market info for {symbol}: {e}")
            return None
    
    def _get_fresh_price(
        self,
        symbol: str,
        market_type: str = "perp"
    ) -> Optional[float]:
        """Get fresh price for any market type."""
        if market_type == "spot":
            if "/" in symbol:
                base_token, quote_token = symbol.split("/", 1)
            else:
                base_token, quote_token = symbol, "USDC"
            return self.get_spot_price(base_token, quote_token)
        else:
            return self.get_current_price(symbol)
    
    def execute_order(
        self,
        symbol: str,
        side: str,
        size: float,
        reduce_only: bool = False,
        urgency: str = "normal",
        market_type: str = "perp",
        # Emergency overrides (used for forced unwind/kill-switch execution)
        max_slippage_bps: Optional[float] = None,
        initial_slippage_bps: Optional[float] = None,
        max_attempts_override: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Unified smart order execution for all market types.
        
        Works the same way for perps, HIP-3, and spot markets:
        - Starts with tight spread around mid-price
        - Walks the price if not immediately filled
        - Handles partial fills automatically
        - Returns aggregated fill information
        
        Args:
            symbol: Trading symbol
                   - For perp/hip3: "BTC", "ETH", etc.
                   - For spot: "BTC/USDC" or "BTC" (defaults to /USDC)
            side: 'buy' or 'sell'
            size: Order size in base asset
            reduce_only: Only reduce existing position (perp/hip3 only)
            urgency: 'low' (patient), 'normal', or 'high' (aggressive)
            market_type: 'perp', 'hip3', or 'spot'
            
        Returns:
            Order result with fill details
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return None
        
        try:
            # Resolve market information
            market_info = self._resolve_market_info(symbol, market_type)
            if not market_info:
                self.logger.error(f"Cannot resolve market info for {symbol} ({market_type})")
                return None
            
            trading_symbol = market_info['symbol']
            display_symbol = market_info['display_symbol']
            current_price = market_info['price']
            sz_decimals = market_info['sz_decimals']
            
            remaining_size = round(size, sz_decimals)
            
            # Validate minimum order value ($10) - but allow reduce_only to close dust positions
            order_value = remaining_size * current_price
            if order_value < 10.0 and not reduce_only:
                self.logger.error(f"Order value ${order_value:.2f} below minimum $10")
                return None
            
            is_buy = side.lower() == 'buy'
            
            # DUST POSITION HANDLING: If reduce_only and size rounds to ~0, handle specially
            # This handles positions too small for normal limit orders
            if reduce_only and (remaining_size < 0.0001 or order_value < 10.0):
                self.logger.info(f"Dust position detected ({size} {display_symbol} = ${size * current_price:.2f}). Attempting direct close...")
                try:
                    # For HIP-3 and other cases, get price from L2 order book and place aggressive order
                    # SDK's market_close doesn't work for HIP-3 (doesn't pass dex= to user_state)
                    l2 = self._rate_limited_call(self.info.l2_snapshot, trading_symbol, weight=2)
                    
                    if l2 and 'levels' in l2:
                        bids = l2['levels'][0]
                        asks = l2['levels'][1]
                        
                        # Determine price based on side (buy uses asks, sell uses bids)
                        if is_buy and asks:
                            base_price = float(asks[0]['px'])
                            exec_price = round(base_price * 1.05, 2)  # 5% above ask
                        elif not is_buy and bids:
                            base_price = float(bids[0]['px'])
                            exec_price = round(base_price * 0.95, 2)  # 5% below bid
                        else:
                            exec_price = round(current_price * (1.05 if is_buy else 0.95), 2)
                        
                        self.logger.info(f"Dust close: {side} {size} {display_symbol} @ {exec_price}")
                        
                        # Place aggressive IOC order with original (unrounded) size
                        dust_result = self._rate_limited_call(
                            self.exchange.order,
                            trading_symbol,
                            is_buy,
                            size,  # Use original size, not rounded
                            exec_price,
                            {"limit": {"tif": "Ioc"}},
                            reduce_only=True
                        )
                        
                        fill_result = self._parse_order_response(dust_result, trading_symbol, side, size, exec_price)
                        if fill_result and fill_result.get('filled_size', 0) > 0:
                            self.logger.info(f"✅ Closed dust position {display_symbol}: {fill_result['filled_size']} @ {fill_result['avg_fill_price']}")
                            return {
                                'order_id': f"dust_close_{trading_symbol}_{int(time.time() * 1000)}",
                                'symbol': trading_symbol,
                                'display_symbol': display_symbol,
                                'side': side,
                                'size': size,
                                'filled_size': fill_result['filled_size'],
                                'unfilled_size': 0,
                                'avg_fill_price': fill_result['avg_fill_price'],
                                'status': 'filled',
                                'market_type': market_type,
                                'timestamp': datetime.now(),
                            }
                        else:
                            self.logger.warning(f"Dust close order returned no fill for {display_symbol}")
                    else:
                        self.logger.warning(f"Could not get L2 orderbook for dust position {display_symbol}")
                except Exception as e:
                    self.logger.warning(f"Dust position close failed for {display_symbol}: {e}")
            
            # Configure slippage based on urgency (defaults)
            if urgency == "low":
                initial_slippage = self._INITIAL_SLIPPAGE_BPS / 2
                max_attempts = self._ORDER_WALK_MAX_ATTEMPTS + 3
            elif urgency == "high":
                initial_slippage = self._INITIAL_SLIPPAGE_BPS * 3
                max_attempts = 2
            else:
                initial_slippage = self._INITIAL_SLIPPAGE_BPS
                max_attempts = self._ORDER_WALK_MAX_ATTEMPTS

            # Apply emergency overrides (if provided)
            if initial_slippage_bps is not None:
                initial_slippage = float(initial_slippage_bps)
            if max_attempts_override is not None:
                max_attempts = int(max_attempts_override)
            max_slippage_cap = float(max_slippage_bps) if max_slippage_bps is not None else float(self._MAX_SLIPPAGE_BPS)
            
            # Track aggregated fills
            total_filled = 0.0
            weighted_price_sum = 0.0
            all_fills = []
            
            self.logger.info(
                f"Executing {market_type} order: {side} {remaining_size} {display_symbol} "
                f"@ ~{current_price:.6f} (urgency: {urgency}, max_slip_bps: {max_slippage_cap:.0f}, max_attempts: {max_attempts})"
            )
            
            for attempt in range(max_attempts):
                if remaining_size <= 0:
                    break
                
                # Refresh price on each attempt
                fresh_price = self._get_fresh_price(symbol, market_type)
                if fresh_price:
                    current_price = fresh_price
                
                # Calculate execution price with progressive slippage
                slippage_bps = min(
                    initial_slippage + (attempt * self._ORDER_WALK_STEP_BPS),
                    max_slippage_cap
                )
                slippage_mult = slippage_bps / 10000
                
                if is_buy:
                    exec_price = current_price * (1 + slippage_mult)
                else:
                    exec_price = current_price * (1 - slippage_mult)
                
                # CRITICAL FIX: Round to valid tick size with strict logic
                # Pass sz_decimals and market type to enforce decimal caps
                exec_price = self._round_to_tick(
                    exec_price, 
                    symbol=trading_symbol,
                    sz_decimals=sz_decimals,
                    is_perp=(market_type != "spot")
                )
                
                self.logger.debug(
                    f"Order attempt {attempt + 1}/{max_attempts}: {side} {remaining_size} "
                    f"{display_symbol} @ {exec_price:.6f} (slippage: {slippage_bps}bps)"
                )
                
                # Place IOC order (smart market execution)
                if market_type == "spot":
                    response = self._rate_limited_call(
                        self.exchange.order,
                        trading_symbol,
                        is_buy,
                        remaining_size,
                        exec_price,
                        {"limit": {"tif": "Ioc"}}
                    )
                else:
                    # Perp/HIP-3 order
                    response = self._rate_limited_call(
                        self.exchange.order,
                        trading_symbol,
                        is_buy,
                        remaining_size,
                        exec_price,
                        {"limit": {"tif": "Ioc"}},
                        reduce_only=reduce_only
                    )
                
                # Parse response
                fill_result = self._parse_order_response(
                    response, trading_symbol, side, remaining_size, exec_price
                )
                
                if fill_result:
                    filled = fill_result.get('filled_size', 0)
                    avg_px = fill_result.get('avg_fill_price', exec_price)
                    
                    if filled > 0:
                        total_filled += filled
                        weighted_price_sum += filled * avg_px
                        
                        # Extract fee - try standard keys
                        fee = fill_result.get('fee', 0.0)
                        
                        all_fills.append({
                            'attempt': attempt + 1,
                            'size': filled,
                            'price': avg_px,
                            'fee': fee,
                            'slippage_bps': slippage_bps,
                        })
                        remaining_size = round(remaining_size - filled, sz_decimals)
                        
                        self.logger.info(
                            f"✓ Fill: {filled:.6f} @ {avg_px:.6f} "
                            f"(total: {total_filled:.6f}/{size:.6f})"
                        )
                
                # If not fully filled and more attempts remain, wait before next try
                if remaining_size > 0 and attempt < max_attempts - 1:
                    time.sleep(self._ORDER_WALK_DELAY)
            
            # Build final result
            if total_filled > 0:
                avg_fill_price = weighted_price_sum / total_filled
                status = 'filled' if remaining_size <= 0 else 'partial'
                
                result = {
                    'order_id': f"smart_{trading_symbol}_{int(time.time() * 1000)}",
                    'symbol': trading_symbol,
                    'display_symbol': display_symbol,
                    'side': side,
                    'size': size,
                    'filled_size': total_filled,
                    'unfilled_size': remaining_size,
                    'avg_fill_price': avg_fill_price,
                    'total_fee': sum(f.get('fee', 0.0) for f in all_fills),
                    'status': status,
                    'fills': all_fills,
                    'market_type': market_type,
                    'timestamp': datetime.now(),
                }
                
                self.logger.info(
                    f"{'✓' if status == 'filled' else '⚠'} Order {status}: "
                    f"{side} {total_filled:.6f}/{size:.6f} {display_symbol} @ {avg_fill_price:.6f}"
                )
                
                # Invalidate caches
                self.cache.invalidate("positions")
                if market_type == "spot":
                    self.cache.invalidate("spot_balances")
                
                return result
            else:
                # FALLBACK: If limit orders failed and this is a position closure (reduce_only),
                # use market order to guarantee fill
                if reduce_only and remaining_size > 0:
                    self.logger.warning(
                        f"Limit orders exhausted for {display_symbol}. Falling back to MARKET order..."
                    )
                    try:
                        # Use SDK's market_open for position closure with 5% slippage
                        # CRITICAL: Pass current_price instead of None for HIP-3 assets
                        # The SDK's all_mids() doesn't include HIP-3 symbols, so px=None fails
                        market_response = self._rate_limited_call(
                            self.exchange.market_open,
                            trading_symbol,
                            is_buy,  # Opposite side to close position
                            remaining_size,
                            current_price,  # Must provide price for HIP-3 (all_mids doesn't have them)
                            0.05,  # 5% slippage tolerance
                        )
                        
                        market_fill = self._parse_order_response(
                            market_response, trading_symbol, side, remaining_size, current_price
                        )
                        
                        if market_fill and market_fill.get('filled_size', 0) > 0:
                            filled = market_fill['filled_size']
                            avg_px = market_fill.get('avg_fill_price', current_price)
                            total_filled += filled
                            weighted_price_sum += filled * avg_px
                            all_fills.append({
                                'attempt': 'market_fallback',
                                'size': filled,
                                'price': avg_px,
                                'slippage_bps': 500,  # 5% = 500 bps
                            })
                            remaining_size = round(remaining_size - filled, sz_decimals)
                            
                            self.logger.info(
                                f"✓ Market fill: {filled:.6f} @ {avg_px:.6f} "
                                f"(total: {total_filled:.6f}/{size:.6f})"
                            )
                    except Exception as market_err:
                        self.logger.error(f"Market order fallback failed: {market_err}")
                
                # Build result based on final state
                if total_filled > 0:
                    avg_fill_price = weighted_price_sum / total_filled
                    status = 'filled' if remaining_size <= 0 else 'partial'
                    
                    result = {
                        'order_id': f"smart_{trading_symbol}_{int(time.time() * 1000)}",
                        'symbol': trading_symbol,
                        'display_symbol': display_symbol,
                        'side': side,
                        'size': size,
                        'filled_size': total_filled,
                        'unfilled_size': remaining_size,
                        'avg_fill_price': avg_fill_price,
                        'status': status,
                        'fills': all_fills,
                        'market_type': market_type,
                        'timestamp': datetime.now(),
                    }
                    self.logger.info(
                        f"{'✓' if status == 'filled' else '⚠'} Order {status}: "
                        f"{side} {total_filled:.6f}/{size:.6f} {display_symbol} @ {avg_fill_price:.6f}"
                    )
                    return result
                else:
                    self.logger.warning(
                        f"Order not filled after {max_attempts} attempts: "
                        f"{side} {size} {display_symbol}"
                    )
                    return {
                        'order_id': None,
                        'symbol': trading_symbol,
                        'display_symbol': display_symbol,
                        'side': side,
                        'size': size,
                        'filled_size': 0,
                        'status': 'not_filled',
                        'market_type': market_type,
                        'timestamp': datetime.now(),
                    }
                
        except Exception as e:
            self.logger.error(f"Order execution failed for {symbol} ({market_type}): {e}")
            return None
    
    def execute_maker_order(
        self,
        symbol: str,
        side: str,
        size: float,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 5.0,
        market_type: str = 'perp'
    ) -> Optional[Dict[str, Any]]:
        """
        Post-only (ALO) entry at the touch.

        Places a resting limit order joining the best bid (buy) / best ask
        (sell) with tif=Alo so it can never take liquidity. Polls until
        filled or `timeout_seconds`, then cancels: an unfilled entry is
        MISSED (status 'missed'), never chased with a taker order — the
        live analogue of the backtest maker model (round 4).

        Entries only (reduce_only exits must stay taker: you cross the
        spread rather than fail to exit).
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return None

        try:
            market_info = self._resolve_market_info(symbol, market_type)
            if not market_info:
                self.logger.error(f"Cannot resolve market info for {symbol} ({market_type})")
                return None

            trading_symbol = market_info['symbol']
            display_symbol = market_info['display_symbol']
            current_price = market_info['price']
            sz_decimals = market_info['sz_decimals']

            rounded_size = round(size, sz_decimals)
            is_buy = side.lower() == 'buy'

            order_value = rounded_size * current_price
            if order_value < 10.0:
                self.logger.error(f"Maker order value ${order_value:.2f} below minimum $10")
                return None

            # Join the touch: best bid for buys, best ask for sells
            limit_price = current_price
            try:
                def _fetch_l2():
                    return self.info.l2_snapshot(trading_symbol)
                l2 = self._rate_limited_call(_fetch_l2, weight=2)
                levels = (l2 or {}).get('levels', [])
                if is_buy and levels and levels[0]:
                    limit_price = float(levels[0][0]['px'])
                elif (not is_buy) and len(levels) > 1 and levels[1]:
                    limit_price = float(levels[1][0]['px'])
            except Exception as e:
                self.logger.warning(f"Maker order: L2 unavailable for {display_symbol}, using mid ({e})")

            limit_price = self._round_to_tick(
                limit_price,
                symbol=trading_symbol,
                sz_decimals=sz_decimals,
                is_perp=(market_type != 'spot')
            )

            self.logger.info(
                f"Placing post-only entry: {side} {rounded_size} {display_symbol} "
                f"@ {limit_price:.6f} (Alo, timeout {timeout_seconds:.0f}s)"
            )

            response = self._rate_limited_call(
                self.exchange.order,
                trading_symbol,
                is_buy,
                rounded_size,
                limit_price,
                {"limit": {"tif": "Alo"}},
                reduce_only=False
            )

            order_result = self._parse_order_response(
                response, trading_symbol, side, rounded_size, limit_price
            )

            if not order_result:
                # ALO that would cross is rejected by the exchange — that is
                # a legitimate miss, not an error to retry with taker.
                self.logger.info(f"Maker entry rejected (would cross?) for {display_symbol} — missed")
                return {'status': 'missed', 'symbol': symbol, 'side': side,
                        'filled_size': 0.0, 'avg_fill_price': 0.0,
                        'reason': 'alo_rejected'}

            if order_result.get('status') == 'filled':
                return order_result

            order_id = order_result.get('order_id')
            if not order_id:
                return {'status': 'missed', 'symbol': symbol, 'side': side,
                        'filled_size': 0.0, 'avg_fill_price': 0.0,
                        'reason': 'no_order_id'}

            # Rest until filled or timeout
            deadline = time.time() + float(timeout_seconds)
            while time.time() < deadline:
                time.sleep(max(1.0, float(poll_interval_seconds)))
                status = self.get_order_status(order_id)
                if status and status.get('status') == 'filled':
                    self.logger.info(
                        f"✓ Maker entry filled: {side} {status.get('filled_size')} "
                        f"{display_symbol} @ {status.get('avg_fill_price')}"
                    )
                    return {
                        'status': 'filled',
                        'order_id': order_id,
                        'symbol': symbol,
                        'side': side,
                        'size': rounded_size,
                        'filled_size': float(status.get('filled_size') or rounded_size),
                        'avg_fill_price': float(status.get('avg_fill_price') or limit_price),
                        'fee': float(status.get('fee', 0.0) or 0.0),
                        'timestamp': datetime.now(),
                    }

            # Timeout: cancel the resting order; report any partial fill
            self.cancel_order(symbol, order_id)
            final = self.get_order_status(order_id) or {}
            filled_size = float(final.get('filled_size') or 0.0)
            if filled_size > 0:
                self.logger.info(
                    f"Maker entry PARTIAL at timeout: {filled_size}/{rounded_size} {display_symbol}"
                )
                return {
                    'status': 'partial',
                    'order_id': order_id,
                    'symbol': symbol,
                    'side': side,
                    'size': rounded_size,
                    'filled_size': filled_size,
                    'avg_fill_price': float(final.get('avg_fill_price') or limit_price),
                    'fee': float(final.get('fee', 0.0) or 0.0),
                    'timestamp': datetime.now(),
                }

            self.logger.info(f"Maker entry MISSED (timeout): {side} {rounded_size} {display_symbol}")
            return {'status': 'missed', 'order_id': order_id, 'symbol': symbol,
                    'side': side, 'filled_size': 0.0, 'avg_fill_price': 0.0,
                    'reason': 'timeout'}

        except Exception as e:
            self.logger.error(f"Error executing maker order for {symbol}: {e}")
            return None

    def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        price: Optional[float] = None,
        reduce_only: bool = False,
        order_type: str = "limit"
    ) -> Optional[Dict[str, Any]]:
        """
        Place a single order (low-level).
        
        For most use cases, prefer execute_order() which handles
        price management automatically.
        
        Args:
            symbol: Trading symbol
            side: 'buy' or 'sell'
            size: Order size
            price: Limit price (None uses smart pricing)
            reduce_only: Only reduce position
            order_type: 'limit' or 'market' (both use smart execution)
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return None
        
        try:
            # Get asset info for rounding
            asset_info = self._get_asset_info_for_symbol(symbol)
            sz_decimals = asset_info.get('szDecimals', 2) if asset_info else 2
            rounded_size = round(size, sz_decimals)
            
            # Get current price
            current_price = self.get_current_price(symbol)
            if not current_price:
                self.logger.error(f"Cannot place order: no price for {symbol}")
                return None
            
            # Validate minimum order value ($10) - but allow reduce_only to close dust positions
            order_value = rounded_size * current_price
            if order_value < 10.0 and not reduce_only:
                self.logger.error(f"Order value ${order_value:.2f} below minimum $10")
                return None
            
            is_buy = side.lower() == 'buy'
            
            # Determine execution price
            if price is None or order_type.lower() == 'market':
                # Smart pricing: start with minimal slippage
                slippage = self._INITIAL_SLIPPAGE_BPS / 10000
                if is_buy:
                    exec_price = current_price * (1 + slippage)
                else:
                    exec_price = current_price * (1 - slippage)
                exec_price = self._round_to_tick(
                    exec_price, 
                    symbol=symbol,
                    sz_decimals=sz_decimals,
                    # Safe default: assume strict perp rules unless explicitly spot (place_order is generic)
                    # Ideally place_order should take market_type, but for now we enforce stricter rules
                    is_perp=True 
                )
                tif = "Ioc"  # Immediate or cancel for market orders
            else:
                # Explicit limit price
                exec_price = self._round_to_tick(
                    price, 
                    symbol=symbol,
                    sz_decimals=sz_decimals,
                    is_perp=True
                )
                tif = "Gtc"  # Good til cancel for limit orders
            
            response = self._rate_limited_call(
                self.exchange.order,
                symbol,
                is_buy,
                rounded_size,
                exec_price,
                {"limit": {"tif": tif}},
                reduce_only=reduce_only
            )
            
            # Parse response and track order
            order_result = self._parse_order_response(
                response, symbol, side, rounded_size, exec_price
            )
            
            if order_result and order_result.get('order_id'):
                # Track the order
                self.order_tracker.track(TrackedOrder(
                    order_id=order_result['order_id'],
                    symbol=symbol,
                    side=side,
                    size=rounded_size,
                    price=exec_price,
                    status=order_result['status'],
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                    filled_size=order_result.get('filled_size', 0),
                    avg_fill_price=order_result.get('avg_fill_price', 0),
                ))
            
            # Invalidate position cache
            self.cache.invalidate("positions")
            
            return order_result
            
        except Exception as e:
            self.logger.error(f"Order placement failed: {e}")
            return None
    
    def place_stop_order(
        self,
        symbol: str,
        side: str,
        size: float,
        trigger_price: float,
        reduce_only: bool = True,
        is_market: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Place a native reduce-only stop (trigger) order on the exchange.

        The order rests on Hyperliquid and fires independently of the bot, so
        a stop is enforced even if the bot process hangs or crashes — defense
        in depth on top of the in-process realtime/monitor stops (added after
        the 2026-06-11 deadlock froze the in-process exit loop for ~43h).

        Args:
            symbol: Trading symbol.
            side: Side of the PROTECTIVE order — 'sell' closes a long, 'buy'
                  closes a short.
            size: Position size to protect (absolute).
            trigger_price: Price at which the stop fires.
            reduce_only: Keep True so the stop can only close, never open.
            is_market: True = stop-market (fills on trigger); False = stop-limit.

        Note: a 'scheduleCancel' dead man's switch (refreshed every 15s while
        the main loop lives) will cancel resting orders ~30s after the process
        fully dies. So this fully covers a hung/wedged-but-alive bot and gives
        ~30s of cover after a hard crash.
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return None

        try:
            asset_info = self._get_asset_info_for_symbol(symbol)
            sz_decimals = asset_info.get('szDecimals', 2) if asset_info else 2
            rounded_size = round(abs(size), sz_decimals)
            if rounded_size <= 0:
                self.logger.error(f"Stop order size rounds to 0 for {symbol} (size={size})")
                return None

            is_buy = side.lower() == 'buy'
            trig = self._round_to_tick(
                trigger_price, symbol=symbol, sz_decimals=sz_decimals, is_perp=True)

            # limit_px caps slippage once the stop triggers. Aggressive in the
            # fill direction so a stop-market actually fills: a buy stop (closing
            # a short) must be willing to pay up; a sell stop (closing a long)
            # must be willing to sell down.
            slippage = self._STOP_LIMIT_SLIPPAGE_BPS / 10000
            raw_limit = trig * (1 + slippage) if is_buy else trig * (1 - slippage)
            limit_px = self._round_to_tick(
                raw_limit, symbol=symbol, sz_decimals=sz_decimals, is_perp=True)

            order_type = {"trigger": {
                "triggerPx": trig, "isMarket": is_market, "tpsl": "sl"}}

            response = self._rate_limited_call(
                self.exchange.order,
                symbol,
                is_buy,
                rounded_size,
                limit_px,
                order_type,
                reduce_only=reduce_only,
            )

            order_result = self._parse_order_response(
                response, symbol, side, rounded_size, trig)

            if order_result and order_result.get('order_id'):
                self.order_tracker.track(TrackedOrder(
                    order_id=order_result['order_id'],
                    symbol=symbol,
                    side=side,
                    size=rounded_size,
                    price=trig,
                    status=order_result.get('status', 'open'),
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                    filled_size=order_result.get('filled_size', 0),
                    avg_fill_price=order_result.get('avg_fill_price', 0),
                ))
                self.logger.info(
                    f"🛡️ Native stop placed for {symbol}: {side} {rounded_size} "
                    f"trigger={trig} (oid={order_result['order_id']})")

            self.cache.invalidate("positions")
            return order_result

        except Exception as e:
            self.logger.error(f"Stop order placement failed for {symbol}: {e}")
            return None

    def _parse_order_response(
        self,
        response: Any,
        symbol: str,
        side: str,
        size: float,
        price: Optional[float]
    ) -> Optional[Dict[str, Any]]:
        """Parse SDK order response."""
        if not response:
            return None
        
        # Log response type and content for troubleshooting HIP-3 orders
        self.logger.info(f"Order response for {symbol}: type={type(response).__name__}, content={str(response)[:300]}")
        
        # Handle string responses (usually error messages from SDK)
        if isinstance(response, str):
            self.logger.error(f"Order rejected for {symbol} (string response): {response}")
            return None
        
        # Handle non-dict responses
        if not isinstance(response, dict):
            self.logger.error(f"Order rejected for {symbol} - unexpected type {type(response).__name__}: {response}")
            return None
        
        try:
            # Safely navigate nested response structure
            # The SDK can return various structures, including error strings at any level
            resp_data = response.get('response', {})
            if isinstance(resp_data, str):
                self.logger.error(f"Order rejected for {symbol}: {resp_data}")
                return None
            
            data = resp_data.get('data', {}) if isinstance(resp_data, dict) else {}
            if isinstance(data, str):
                self.logger.error(f"Order rejected for {symbol}: {data}")
                return None
                
            status_data = data.get('statuses', []) if isinstance(data, dict) else []
            
            if not status_data:
                return None
            
            status = status_data[0]
            
            if 'error' in status:
                self.logger.error(f"Order error for {symbol}: {status['error']}")
                return None
            
            order_id = None
            order_status = 'pending'
            filled_size = 0.0
            avg_fill_price = 0.0
            fee = 0.0
            
            if 'filled' in status:
                order_id = status['filled'].get('oid')
                order_status = 'filled'
                filled_size = float(status['filled'].get('totalSz', size))
                avg_fill_price = float(status['filled'].get('avgPx', price or 0))
                
                # DEBUG: Log raw fill content
                self.logger.info(f"DEBUG: Raw fill data: {status['filled']}")
                
                # Try to extract fee
                fee = float(status['filled'].get('fee', 0.0))
                if 'totalFee' in status['filled']:
                    fee = float(status['filled']['totalFee'])
            elif 'resting' in status:
                order_id = status['resting'].get('oid')
                order_status = 'open'
            
            return {
                'order_id': order_id,
                'symbol': symbol,
                'side': side,
                'size': size,
                'price': price,
                'status': order_status,
                'filled_size': filled_size,
                'avg_fill_price': avg_fill_price,
                'fee': fee,
                'timestamp': datetime.now(),
                'raw_response': response,
            }
            
        except Exception as e:
            self.logger.error(f"Error parsing order response: {e}")
            return None
    
    def get_execution_fee(self, order_id: int) -> float:
        """
        Get the actual fee paid for a specific order ID by checking user fills.
        
        Args:
            order_id: The order ID (oid) to find fees for
            
        Returns:
            float: Total fee paid for this order, or 0.0 if not found
        """
        try:
            # Fetch recent user fills
            # user_fills expects address. We use the configured wallet address.
            def _fetch_fills():
                return self.info.user_fills(self.wallet_address)

            fills = self._rate_limited_call(_fetch_fills, weight=2)
            
            total_fee = 0.0
            found = False
            
            for fill in fills:
                # Fill object structure from API doc search:
                # {'oid': 123, 'fee': '0.05', 'feeToken': 'USDC', ...}
                if fill.get('oid') == order_id:
                    fee_str = fill.get('fee', '0.0')
                    try:
                        total_fee += float(fee_str)
                        found = True
                    except ValueError:
                        pass
            
            if found:
                self.logger.debug(f"Retrieved fee from API for order {order_id}: {total_fee}")
                return total_fee
            
            self.logger.warning(f"Could not find fee info for order {order_id} in recent fills")
            return 0.0
            
        except Exception as e:
            self.logger.error(f"Error fetching execution fee: {e}")
            return 0.0

    def get_order_status(self, order_id: int) -> Optional[Dict[str, Any]]:
        """
        Get current status of an order.
        
        Uses order tracker and verifies with API.
        """
        # Check tracker first
        tracked = self.order_tracker.get(order_id)
        if tracked and tracked.is_terminal():
            return {
                'order_id': tracked.order_id,
                'symbol': tracked.symbol,
                'status': tracked.status,
                'filled_size': tracked.filled_size,
                'avg_fill_price': tracked.avg_fill_price,
            }
        
        # Query API for open orders
        try:
            open_orders = self.get_open_orders()
            
            for order in open_orders:
                if order.get('order_id') == order_id:
                    # Update tracker
                    self.order_tracker.update(order_id, status='open')
                    return order
            
            # Order not in open orders - check if it was filled
            if tracked:
                # Assume filled if not open
                self.order_tracker.update(order_id, status='filled', filled_size=tracked.size)
                return {
                    'order_id': order_id,
                    'symbol': tracked.symbol,
                    'status': 'filled',
                    'filled_size': tracked.size,
                    'avg_fill_price': tracked.price or 0,
                }
            
            return {'order_id': order_id, 'status': 'not_found'}
            
        except Exception as e:
            self.logger.error(f"Error getting order status: {e}")
            return None
    
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get all open orders."""
        def _fetch():
            orders_data = self.info.open_orders(self.public_account_address)
            
            orders = []
            for order in orders_data:
                orders.append({
                    'order_id': order.get('oid'),
                    'symbol': order.get('coin'),
                    'side': 'buy' if order.get('side') == 'B' else 'sell',
                    'size': float(order.get('sz', 0)),
                    'price': float(order.get('limitPx', 0)),
                    'filled_size': float(order.get('sz', 0)) - float(order.get('origSz', order.get('sz', 0))),
                    'status': 'open',
                    'timestamp': order.get('timestamp'),
                })
            
            return orders
        
        return self._rate_limited_call(_fetch)
    
    def cancel_order(self, symbol: str, order_id: int) -> bool:
        """Cancel an open order."""
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return False
        
        try:
            response = self._rate_limited_call(
                self.exchange.cancel,
                symbol,
                order_id
            )
            
            if response:
                self.order_tracker.update(order_id, status='cancelled')
                self.logger.info(f"Order {order_id} cancelled")
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Cancel order failed: {e}")
            return False
    
    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel all open orders, optionally for a specific symbol."""
        cancelled = 0
        open_orders = self.get_open_orders()
        
        for order in open_orders:
            if symbol is None or order.get('symbol') == symbol:
                if self.cancel_order(order['symbol'], order['order_id']):
                    cancelled += 1
        
        return cancelled
    
    def set_dead_mans_switch(self, timeout_seconds: int = 30) -> bool:
        """
        Set a dead man's switch that auto-cancels all orders after timeout.
        
        This is a safety mechanism - if the bot crashes, all pending orders
        will be cancelled by the exchange after the timeout period.
        
        Args:
            timeout_seconds: Seconds until auto-cancel (0 to disable)
            
        Returns:
            True if successful
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return False
        
        try:
            # Calculate timeout timestamp (milliseconds UTC)
            # SDK's schedule_cancel expects time in UTC ms, or None to disable
            timeout_ms = int((time.time() + timeout_seconds) * 1000) if timeout_seconds > 0 else None
            
            # Use the SDK's built-in schedule_cancel method which handles proper signing
            response = self._rate_limited_call(
                self.exchange.schedule_cancel,
                timeout_ms
            )
            
            if timeout_seconds > 0:
                self.logger.info(f"Dead man's switch set: auto-cancel in {timeout_seconds}s")
            else:
                self.logger.info("Dead man's switch disabled")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to set dead man's switch: {e}")
            return False
    
    def refresh_dead_mans_switch(self, timeout_seconds: int = 30) -> bool:
        """
        Refresh the dead man's switch heartbeat.
        
        Should be called periodically (e.g., every 15s for a 30s timeout).
        """
        return self.set_dead_mans_switch(timeout_seconds)
    
    def disable_dead_mans_switch(self) -> bool:
        """Disable the dead man's switch (for graceful shutdown)."""
        return self.set_dead_mans_switch(0)
    
    # =========================================================================
    # SPOT TRADING
    # =========================================================================

    
    def get_spot_meta(self) -> Optional[Dict[str, Any]]:
        """Get spot market metadata."""
        try:
            cache_key = "spot_meta"
            cached = self.cache.get(cache_key)
            if cached:
                return cached
                
            def _fetch():
                return self.info.spot_meta()
            data = self._rate_limited_call(_fetch)
            
            # Cache for 5 minutes
            self.cache.set(cache_key, data, ttl=300)
            return data
        except Exception as e:
            self.logger.error(f"Error fetching spot meta: {e}")
            return None
    
    def get_spot_meta_and_asset_ctxs(self) -> Optional[Tuple]:
        """Get spot metadata and asset contexts."""
        try:
            def _fetch():
                return self.info.spot_meta_and_asset_ctxs()
            result = self._rate_limited_call(_fetch)
            return (result[0], result[1]) if len(result) >= 2 else None
        except Exception as e:
            self.logger.error(f"Error fetching spot meta and ctxs: {e}")
            return None
    
    def get_spot_api_name(self, base_token: str, quote_token: str = "USDC") -> Optional[str]:
        """
        Convert a human-readable spot token name to its API name for OHLCV fetching.
        
        The candleSnapshot endpoint requires the internal API name (e.g., "@109")
        not the human-readable token name (e.g., "UBTC").
        
        Args:
            base_token: Human-readable token name (e.g., "UBTC", "HYPE")
            quote_token: Quote token (default "USDC")
            
        Returns:
            API name (e.g., "@109") or None if not found
        """
        if not hasattr(self, '_spot_api_name_cache'):
            self._spot_api_name_cache = {}
            
        cache_key = f"{base_token}/{quote_token}"
        if cache_key in self._spot_api_name_cache:
            return self._spot_api_name_cache[cache_key]

        try:
            spot_meta = self.get_spot_meta()
            if not spot_meta:
                return None
            
            token_list = spot_meta.get('tokens', [])
            
            for pair in spot_meta.get('universe', []):
                tokens = pair.get('tokens', [])
                if len(tokens) >= 2:
                    base_idx, quote_idx = tokens[0], tokens[1]
                    if base_idx < len(token_list) and quote_idx < len(token_list):
                        pair_base = token_list[base_idx].get('name', '')
                        pair_quote = token_list[quote_idx].get('name', '')
                        if pair_base == base_token and pair_quote == quote_token:
                            api_name = pair.get('name')  # Returns "@109" format
                            self._spot_api_name_cache[cache_key] = api_name
                            return api_name
            
            return None
        except Exception as e:
            self.logger.error(f"Error getting spot API name for {base_token}: {e}")
            return None
    
    def get_spot_price(self, base_token: str, quote_token: str = "USDC") -> Optional[float]:
        """
        Get spot price for a token using the tokenDetails endpoint.
        
        Note: The spotMetaAndAssetCtxs endpoint returns lot-scaled prices which
        are incorrect. The tokenDetails endpoint returns the actual market price.
        """
        try:
            # Get token metadata to find the tokenId
            spot_meta = self.get_spot_meta()
            if not spot_meta:
                return None
            
            # Find the base token's tokenId
            token_id = None
            for token in spot_meta.get('tokens', []):
                if token.get('name') == base_token:
                    token_id = token.get('tokenId')
                    break
            
            if not token_id:
                self.logger.debug(f"Token {base_token} not found in spot metadata")
                return None
            
            # Use tokenDetails endpoint to get the correct price
            # This returns the actual market price, not lot-scaled
            import requests
            response = requests.post(
                'https://api.hyperliquid.xyz/info',
                json={'type': 'tokenDetails', 'tokenId': token_id},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                mid_px = data.get('midPx')
                if mid_px:
                    return float(mid_px)
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting spot price for {base_token}: {e}")
            return None
    
    def get_spot_balances(self) -> Dict[str, float]:
        """Get spot token balances."""
        try:
            spot_state = self.info.spot_user_state(self.public_account_address)
            
            balances = {}
            for balance in spot_state.get('balances', []):
                token = balance.get('coin', '')
                total = float(balance.get('total', 0))
                if total > 0:
                    balances[token] = total
            
            return balances
            
        except Exception as e:
            self.logger.error(f"Error getting spot balances: {e}")
            return {}
    
    def execute_spot_order(
        self,
        base_token: str,
        side: str,
        size: float,
        quote_token: str = "USDC",
        urgency: str = "normal"
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a spot order using unified smart execution.
        
        Args:
            base_token: Base token (e.g., "BTC", "ETH")
            side: 'buy' or 'sell'
            size: Order size in base token
            quote_token: Quote token (default: "USDC")
            urgency: 'low', 'normal', or 'high'
            
        Returns:
            Order result with fill details
        """
        symbol = f"{base_token}/{quote_token}"
        return self.execute_order(
            symbol=symbol,
            side=side,
            size=size,
            urgency=urgency,
            market_type="spot"
        )
    
    def execute_hip3_order(
        self,
        symbol: str,
        side: str,
        size: float,
        reduce_only: bool = False,
        urgency: str = "normal"
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a HIP-3 perp order using unified smart execution.
        
        Args:
            symbol: HIP-3 perp symbol
            side: 'buy' or 'sell'
            size: Order size
            reduce_only: Only reduce existing position
            urgency: 'low', 'normal', or 'high'
            
        Returns:
            Order result with fill details
        """
        return self.execute_order(
            symbol=symbol,
            side=side,
            size=size,
            reduce_only=reduce_only,
            urgency=urgency,
            market_type="hip3"
        )
    
    # =========================================================================
    # FUNDING RATES
    # =========================================================================
    
    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Get current funding rate for a perpetual."""
        market_data = self.get_market_data(symbol)
        return market_data.get('funding_rate') if market_data else None
    
    def get_all_funding_rates(self) -> Dict[str, float]:
        """Get funding rates for all perpetuals."""
        try:
            asset_info = self.get_asset_info()
            if not asset_info:
                return {}
            
            return {
                asset['name']: asset.get('funding', 0)
                for asset in asset_info.get('universe', [])
            }
        except Exception as e:
            self.logger.error(f"Error getting funding rates: {e}")
            return {}
            
    def get_funding_history(self, symbol: str, start_time_ms: int, end_time_ms: int) -> List[Dict[str, Any]]:
        """
        Get funding history for a symbol.
        
        Args:
            symbol: Trading symbol
            start_time_ms: Start time in milliseconds
            end_time_ms: End time in milliseconds
            
        Returns:
            List of funding rate records
        """
        def _fetch():
            return self.info.funding_history(symbol, start_time_ms, end_time_ms)

        try:
            return self._rate_limited_call(_fetch, weight=2)
        except Exception as e:
            self.logger.error(f"Error getting funding history for {symbol}: {e}")
            return []
    
    # =========================================================================
    # HIP-3 PERPS
    # =========================================================================
    
    def get_perp_dexs(self) -> List[Dict[str, Any]]:
        """Get available perp dexes."""
        try:
            return self.info.perp_dexs()
        except Exception as e:
            self.logger.error(f"Error fetching perp dexs: {e}")
            return []
    
    def get_all_perp_assets(self, include_hip3: bool = True) -> List[Dict[str, Any]]:
        """
        Get all perpetual assets across native and HIP-3.
        
        Fetches native assets via default endpoint, and iteratively fetches
        HIP-3 assets from builder-deployed Dexes if enabled.
        """
        all_assets = []
        
        try:
            # 1. Fetch Native Assets (Default Context)
            meta_and_ctxs = self.info.meta_and_asset_ctxs()
            
            if len(meta_and_ctxs) >= 2:
                universe = meta_and_ctxs[0].get('universe', [])
                contexts = meta_and_ctxs[1]
                
                for i, asset in enumerate(universe):
                    ctx = contexts[i] if i < len(contexts) else {}
                    # Native assets process as before
                    all_assets.append({
                        'name': asset.get('name', ''),
                        'dex': '', # Native
                        'is_hip3': False,
                        'maxLeverage': asset.get('maxLeverage', 0),
                        'szDecimals': asset.get('szDecimals', 0),
                        'openInterest': float(ctx.get('openInterest', 0)) * float(ctx.get('markPx', 0)) if ctx.get('markPx') else 0,
                        'volume24h': float(ctx.get('dayNtlVlm', 0)),
                        'markPrice': float(ctx.get('markPx', 0)) if ctx.get('markPx') else 0,
                        'funding': float(ctx.get('funding', 0)),
                    })

            # 2. Fetch HIP-3 Assets if requested and enabled
            if include_hip3 and self.hip3_enabled:
                try:
                    # Discover all Dexes
                    dex_list = self.info.perp_dexs()
                    
                    for dex_obj in dex_list:
                        # Ensure it's a dict and has a name
                        if not isinstance(dex_obj, dict):
                            continue
                            
                        dex_name = dex_obj.get('name', '')
                        # Skip Native (empty name) or if name is missing
                        if not dex_name:
                            continue
                            
                        # Fetch context for this specific Dex
                        try:
                            # Use Info's internal post method to reuse config/session
                            # Payload must match what verified script used: {"dex": "name"}
                            def _fetch_dex_meta():
                                return self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex_name})
                            dex_meta_ctx = self._rate_limited_call(_fetch_dex_meta, weight=20)
                            
                            if len(dex_meta_ctx) >= 2:
                                d_universe = dex_meta_ctx[0].get('universe', [])
                                d_contexts = dex_meta_ctx[1]
                                
                                for i, asset in enumerate(d_universe):
                                    ctx = d_contexts[i] if i < len(d_contexts) else {}
                                    
                                    all_assets.append({
                                        'name': asset.get('name', ''),
                                        'dex': dex_name, # Specific Dex Name
                                        'is_hip3': True,
                                        'maxLeverage': asset.get('maxLeverage', 0),
                                        'szDecimals': asset.get('szDecimals', 0),
                                        'openInterest': float(ctx.get('openInterest', 0)) * float(ctx.get('markPx', 0)) if ctx.get('markPx') else 0,
                                        'volume24h': float(ctx.get('dayNtlVlm', 0)),
                                        'markPrice': float(ctx.get('markPx', 0)) if ctx.get('markPx') else 0,
                                        'funding': float(ctx.get('funding', 0)),
                                    })

                                    # [HIP-3 FIX] Update SDK Exchange maps (via Info)
                                    # The SDK Exchange client relies on its internal Info object for mappings.
                                    if self.exchange and hasattr(self.exchange, 'info'):
                                        coin_name = asset.get('name', '')
                                        if coin_name:
                                            # Update name_to_coin
                                            if coin_name not in self.exchange.info.name_to_coin:
                                                self.exchange.info.name_to_coin[coin_name] = asset
                                                self.logger.debug(f"[HIP-3] Added {coin_name} to Exchange.info.name_to_coin")
                                            
                                            # Update coin_to_asset if ID is present
                                            if 'assetId' in asset and coin_name not in self.exchange.info.coin_to_asset:
                                                self.exchange.info.coin_to_asset[coin_name] = asset['assetId']
                                                self.logger.debug(f"[HIP-3] Added {coin_name} -> ID {asset['assetId']} to Exchange.info.coin_to_asset")
                                    
                        except Exception as e:
                            # Log warning but continue to next Dex
                            self.logger.warning(f"Failed to fetch HIP-3 Dex '{dex_name}': {e}")
                            
                except Exception as e:
                    self.logger.error(f"Error discovering/fetching HIP-3 Dexes: {e}")
                
        except Exception as e:
            self.logger.error(f"Error fetching all perp assets: {e}")
        
        return all_assets
    
    def get_all_positions(self, include_hip3: bool = True) -> List[Dict[str, Any]]:
        """
        Get all positions across native and HIP-3.
        
        Note: When Info is initialized with perp_dexs, HIP-3 positions are automatically
        included in user_state(). We return all positions from get_positions().
        """
        # get_positions() already returns all positions (native + HIP-3 when perp_dexs is set)
        positions = self.get_positions()
        
        all_positions = []
        for pos in positions:
            # Add metadata fields if not present
            if 'dex' not in pos:
                pos['dex'] = ''
            if 'is_hip3' not in pos:
                pos['is_hip3'] = pos.get('dex', '') != ''
            
            # Filter HIP-3 if not requested
            if pos['is_hip3'] and not include_hip3:
                continue
            
            all_positions.append(pos)
        
        return all_positions
    
    # =========================================================================
    # FUNDING RATE ARBITRAGE HELPERS
    # =========================================================================
    
    # ==========================================================================
    # PERP-TO-SPOT TOKEN MAPPING (MANUALLY MAINTAINED)
    # ==========================================================================
    # This mapping connects perpetual symbols to their corresponding spot tokens.
    # Hyperliquid has NO canonical API for this mapping, so it must be maintained
    # manually to ensure safe delta-neutral trading.
    #
    # IMPORTANT:
    # - NO HEURISTICS are used - only tokens in this mapping can be traded
    # - Periodically check for new spot markets and update this table
    # - Run get_funding_arb_eligible_symbols() to verify which mappings are active
    # - Last verified: January 2026
    #
    # Naming conventions observed:
    # - U-prefix: Major tokens use wrapped versions (BTC->UBTC, ETH->UETH, etc.)
    # - Direct: Some tokens have same name for perp and spot (PURR, HYPE, etc.)
    #
    # To find new mappings, query the spot API and look for tokens that match
    # existing perp symbols (with or without U-prefix).
    # ==========================================================================
    PERP_TO_SPOT_MAPPING = {
        # Major tokens with U-prefix wrapped versions
        'BTC': 'BTC_SPOT',      # Was 'UBTC'
        'ETH': 'ETH_SPOT',      # Was 'UETH'
        'SOL': 'SOL_SPOT',      # Was 'USOL' - wait, USOL? Yes.
        'BONK': 'BONK_SPOT',    # Was 'UBONK'
        # 'DOGE': 'UDOGE',      # Not available on exchange as of Jan 2026
        'MOG': 'MOG_SPOT',      # Was 'UMOG'
        'WLD': 'WLD_SPOT',      # Was 'UWLD'
        'ENA': 'ENA_SPOT',      # Was 'UENA'
        'XPL': 'XPL_SPOT',      # Was 'UXPL'
        'MON': 'MON_SPOT',      # Was 'UMON'
        'PUMP': 'PUMP_SPOT',    # Was 'UPUMP'
        'FARTCOIN': 'FARTCOIN_SPOT', # Was 'UFART'
        'MEGA': 'MEGA_SPOT',    # Was 'UMEGA'
        # Direct matches
        'PURR': 'PURR_SPOT',    # Was 'PURR'
        'HYPE': 'HYPE_SPOT',    # Was 'HYPE'
        'TRUMP': 'TRUMP_SPOT',  # Was 'TRUMP'
        'STABLE': 'STABLE_SPOT',# Was 'STABLE'
        'BERA': 'BERA_SPOT',    # Was 'BERA'
    }

    # Reverse mapping + Legacy Support (Internal -> API Token Name)
    # This is critical for translating BTC_SPOT -> UBTC for the API
    SPOT_INTERNAL_TO_API = {
        'BTC_SPOT': 'UBTC',
        'ETH_SPOT': 'UETH',
        'SOL_SPOT': 'USOL',
        'BONK_SPOT': 'UBONK',
        'MOG_SPOT': 'UMOG',
        'WLD_SPOT': 'UWLD',
        'ENA_SPOT': 'UENA',
        'XPL_SPOT': 'UXPL',
        'MON_SPOT': 'UMON',
        'PUMP_SPOT': 'UPUMP',
        'FARTCOIN_SPOT': 'UFART',
        'MEGA_SPOT': 'UMEGA',
        'PURR_SPOT': 'PURR',
        'HYPE_SPOT': 'HYPE',
        'TRUMP_SPOT': 'TRUMP',
        'STABLE_SPOT': 'STABLE',
        'BERA_SPOT': 'BERA',
    }

    def normalize_symbol(self, symbol: str) -> str:
        """
        Normalize symbol to internal storage convention.
        
        Rules:
        1. If it's a known mapping key (i.e. API name like 'UBTC'), convert to internal 'BTC_SPOT'.
        2. If it already ends with '_SPOT', keep it.
        3. If it's a perp symbol, keep it.
        """
        if not symbol: return symbol
        
        # Check reverse mapping (API Value -> Internal Key)
        # We need to construct this lookup. Since it's 1:1, we can search values.
        # Ensure we prioritize: UBTC -> BTC_SPOT
        
        # Optimization: Manual lookup for common cases or inverted dict
        # Create inverted dict only once if possible, but for now linear scan is fine or manual check
        
        # Check if symbol is a known API token name that maps to a SPOT internal name
        # (e.g. input "UBTC" -> should become "BTC_SPOT")
        # Phase 13: Prioritize known Perps to avoid collision (e.g. HYPE perp vs HYPE_SPOT)
        # Check active universe from cache if available
        # Cache stores (universe, asset_contexts) under "bulk_market_data"
        cached_bulk = self.cache.get("bulk_market_data")
        if cached_bulk and len(cached_bulk) > 0:
             universe = cached_bulk[0]
             # Fast check: If symbol is in the Perp universe, return as is.
             for asset in universe:
                 if asset['name'] == symbol:
                     return symbol

        # Check if symbol is a known API token name that maps to a SPOT internal name
        # (e.g. input "UBTC" -> should become "BTC_SPOT")
        for internal, api_name in self.SPOT_INTERNAL_TO_API.items():
            if symbol == api_name:
                return internal
                
        # If it's already a valid internal spot name, return as is
        if symbol in self.SPOT_INTERNAL_TO_API:
            return symbol
            
        return symbol

    def _is_spot_symbol(self, symbol: str) -> bool:
        """Check if symbol is an internal spot symbol."""
        return symbol.endswith('_SPOT') or symbol in self.SPOT_INTERNAL_TO_API
    
    def get_spot_token_for_perp(self, perp_symbol: str) -> Optional[str]:
        """
        Get the spot token name that corresponds to a perp symbol.
        
        ONLY uses the static PERP_TO_SPOT_MAPPING table - no heuristics.
        This prevents dangerous false matches between unrelated assets.
        
        The mapping table should be manually reviewed and updated periodically
        when new spot markets are added to Hyperliquid.
        
        Args:
            perp_symbol: The perpetual symbol (e.g., 'BTC', 'ETH', 'PURR')
            
        Returns:
            The corresponding spot base token name, or None if not in mapping
        """
        # Only use static mapping - no heuristics to avoid false matches
        if perp_symbol not in self.PERP_TO_SPOT_MAPPING:
            return None
        
        mapped_spot = self.PERP_TO_SPOT_MAPPING[perp_symbol]
        
        # Verify the spot token actually exists on the exchange
        try:
            spot_meta = self.get_spot_meta()
            if not spot_meta:
                return None
            
            token_list = spot_meta.get('tokens', [])
            
            # Check if the mapped spot token exists in a USDC pair
            # mapped_spot is internal (e.g. BTC_SPOT). API has API names (e.g. UBTC).
            # Resolve to API name for verification.
            api_mapped_spot = self.SPOT_INTERNAL_TO_API.get(mapped_spot, mapped_spot)
            
            for pair in spot_meta.get('universe', []):
                tokens = pair.get('tokens', [])
                if len(tokens) >= 2:
                    base_idx, quote_idx = tokens[0], tokens[1]
                    if base_idx < len(token_list) and quote_idx < len(token_list):
                        base_name = token_list[base_idx].get('name', '')
                        quote_name = token_list[quote_idx].get('name', '')
                        if base_name == api_mapped_spot and quote_name == 'USDC':
                            return mapped_spot
            
            self.logger.warning(f"Mapped spot token {mapped_spot} ({api_mapped_spot}) for {perp_symbol} not found on exchange - please update PERP_TO_SPOT_MAPPING")
            return None
            
        except Exception as e:
            self.logger.error(f"Error verifying spot token for {perp_symbol}: {e}")
            return None
    
    def get_funding_arb_eligible_symbols(self) -> List[str]:
        """
        Get perp symbols that have corresponding spot markets.
        
        These are the only symbols that can be used for delta-neutral
        funding rate arbitrage (perp + spot hedge).
        
        Handles both direct matches (PURR) and U-prefixed matches (BTC->UBTC).
        
        Returns:
            List of perp symbols that have spot market equivalents
        """
        try:
            # Get all perp symbols (exclude spot market symbols like @1, @109)
            perp_symbols = set()
            all_perps = self.get_all_perp_assets(include_hip3=self.hip3_enabled)
            for asset in all_perps:
                name = asset.get('name', '')
                # Skip spot market references (internal @N names)
                if name and not name.startswith('@'):
                    perp_symbols.add(name)
            
            # Find perps that have corresponding spot markets
            eligible = []
            spot_mapping = {}
            
            for perp_symbol in perp_symbols:
                spot_token = self.get_spot_token_for_perp(perp_symbol)
                if spot_token:
                    eligible.append(perp_symbol)
                    spot_mapping[perp_symbol] = spot_token
            
            # Log the mapping for visibility
            self.logger.info(f"Found {len(eligible)} symbols eligible for funding rate arbitrage:")
            for perp in sorted(eligible):
                spot = spot_mapping.get(perp)
                if perp == spot:
                    self.logger.info(f"  {perp} (direct match)")
                else:
                    self.logger.info(f"  {perp} -> {spot}/USDC (U-prefixed)")
            
            return sorted(eligible)
            
        except Exception as e:
            self.logger.error(f"Error getting funding arb eligible symbols: {e}")
            return []
    
    def has_spot_market(self, symbol: str) -> bool:
        """Check if a perp symbol has a spot market available."""
        return self.get_spot_token_for_perp(symbol) is not None
    
    # =========================================================================
    # CALLBACKS & SUBSCRIPTIONS
    # =========================================================================
    
    def subscribe_symbol(self, symbol: str, required_timeframes: List[str] = None):
        """Subscribe to real-time data for a symbol (Non-Blocking)."""
        if symbol in self._subscribed_symbols or symbol in self._initializing_symbols:
             return

        self.logger.info(f"Scheduling async subscription for {symbol}")
        self._pending_init_symbols.add(symbol)
        
        # Determine API symbol
        api_symbol = symbol
        asset_info = self._get_asset_info_for_symbol(symbol)
        if not asset_info:
            # For spot assets: BTC_SPOT -> UBTC -> @109
            if symbol in self.SPOT_INTERNAL_TO_API:
                api_token_name = self.SPOT_INTERNAL_TO_API[symbol]  # BTC_SPOT -> UBTC
                spot_api = self.get_spot_api_name(api_token_name)   # UBTC -> @109
                if spot_api:
                    api_symbol = spot_api
                else:
                    self.logger.warning(f"No @index found for spot {symbol} ({api_token_name})")
                    # ABORT: Do not attempt to subscribe with unresolved symbol
                    self._pending_init_symbols.discard(symbol)
                    return
            else:
                # Might be a raw spot symbol like WOW, try direct lookup
                spot_api = self.get_spot_api_name(symbol)
                if spot_api:
                    api_symbol = spot_api
                elif self.has_spot_market(symbol):
                    # It's a spot market but we couldn't resolve API name - abort
                    self.logger.error(f"Failed to resolve API name for spot symbol {symbol} - skipping subscription")
                    self._pending_init_symbols.discard(symbol)
                    return

        self._persistence_executor.submit(self._async_init_worker, symbol, api_symbol, required_timeframes)

    def _finalize_subscription(self, symbol: str, required_timeframes: List[str] = None):
        """Internal: Finalize subscription after data is ready."""
        self._subscribed_symbols.add(symbol)
        
        # Initialize tracking for required timeframes
        # User requested NO fallback defaults. Strictly opt-in.
        target_timeframes = required_timeframes if required_timeframes else []
        
        for tf in target_timeframes:
            self.ohlcv_cache.ensure_timeframe(symbol, tf, maxlen=1000)
            # Note: Explicit backfill removed here as it is handled by _initialize_live_data -> _append_current_candle
            # AND the persistence verification logic.
            
        # SDK handles subscriptions via allMids automatically

    def unsubscribe_symbol(self, symbol: str):
        """Unsubscribe from real-time data for a symbol."""
        self._subscribed_symbols.discard(symbol)
    
    def add_price_callback(self, callback: Callable):
        """Add callback for price updates."""
        self._callbacks['price'].append(callback)
    
    def add_position_callback(self, callback: Callable):
        """Add callback for position updates."""
        self._callbacks['position'].append(callback)
    
    def add_order_callback(self, callback: Callable):
        """Add callback for order updates."""
        self._callbacks['order'].append(callback)
    
    # =========================================================================
    # UTILITY METHODS
    # =========================================================================
    
    def _get_asset_info_for_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get asset info for a specific symbol."""
        asset_info = self.get_asset_info()
        if not asset_info:
            return None
        universe = asset_info.get('universe', [])

        # Exact match first: HIP-3 entries are stored dex-prefixed
        # ('xyz:GOLD'), and stripping the prefix before lookup made them
        # unfindable — callers then fell back to sz_decimals=2, which
        # rounded small high-priced orders (e.g. 0.0031 GOLD) down to a
        # $0 order.
        for asset in universe:
            if asset['name'] == symbol:
                return asset

        # Fallback: strip "<prefix>:" (numeric coin ids like "147:GOLD"
        # and any dex prefix) and match the bare name.
        clean_symbol = symbol.split(":", 1)[1] if ":" in symbol else symbol
        for asset in universe:
            if asset['name'] == clean_symbol:
                return asset
        return None
    
    def _get_tick_size(self, price: float) -> float:
        """
        Calculate tick size for Hyperliquid.
        
        Hyperliquid uses 5 significant figures for prices.
        Examples:
            - $2981.15 -> tick size 0.1 (price rounds to 2981.1)
            - $87853.00 -> tick size 1.0 (price rounds to 87853)
            - $0.1234 -> tick size 0.0001 (price rounds to 0.1234)
        """
        if price <= 0:
            return 0.01
        
        import math
        # Find order of magnitude
        magnitude = math.floor(math.log10(abs(price)))
        # 5 significant figures means tick is 10^(magnitude - 4)
        tick_size = 10 ** (magnitude - 4)
        return tick_size
    
    def _round_to_tick(self, price: float, symbol: Optional[str] = None, sz_decimals: Optional[int] = None, is_perp: bool = True) -> float:
        """
        Round price to valid tick size based on Hyperliquid rules:
        1. Max 5 significant figures.
        2. Max decimals = (6 for perps, 8 for spot) - sz_decimals.
           (Official formula: Price decimals <= MAX_DECIMALS - szDecimals)
        
        Args:
            price: The price to round
            symbol: Optional symbol (unused now, but kept for compatibility)
            sz_decimals: Size decimals for the asset (required for rule #2)
            is_perp: True for perps/hip3, False for spot
            
        Returns:
            Price rounded to valid tick size
        """
        if price <= 0:
            return price
        
        # Rule 1: 5 Significant Figures
        import math
        magnitude = math.floor(math.log10(abs(price)))
        # 5 significant figures means tick is 10^(magnitude - 4)
        tick_size_sig = 10 ** (magnitude - 4)
        rounded_sig = round(price / tick_size_sig) * tick_size_sig
        
        # Rule 2: Max Decimals
        # Perps: 6, Spot: 8 (minus szDecimals)
        # Default sz_decimals to 0 if not provided (safety)
        eff_sz_decimals = sz_decimals if sz_decimals is not None else 0
        max_decimals_base = 6 if is_perp else 8
        max_allowed_decimals = max_decimals_base - eff_sz_decimals
        
        # Determine strict decimal rounding
        # We need to round the result of Rule 1 to meet Rule 2
        final_price = round(rounded_sig, max_allowed_decimals)
        
        return final_price
    
    def is_data_available(self, symbol: str) -> bool:
        """Check if data is available for a symbol."""
        return self.get_current_price(symbol) is not None
    
    def wait_for_data(self, symbol: str, timeout: int = 60) -> bool:
        """Wait for data to become available."""
        start = time.time()
        while time.time() - start < timeout:
            if self.is_data_available(symbol):
                return True
            time.sleep(1)
        return False
    
    def get_data_summary(self) -> Dict[str, Any]:
        """Get comprehensive summary of API state including health."""
        return {
            'health': self.health_monitor.get_health_summary(),
            'circuit_breaker_state': self.circuit_breaker.state.value,
            'rate_limiter_tokens': self.rate_limiter.tokens,
            'cache_entries': len(self.cache._cache),
            'tracked_orders': len(self.order_tracker.orders),
            'subscribed_symbols': list(self._subscribed_symbols),
            'hip3_enabled': self.hip3_enabled,
            'timestamp': datetime.now().isoformat(),
        }
    
    def is_healthy(self) -> bool:
        """Quick check if API is healthy."""
        return self.health_monitor.is_healthy()
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get detailed health status."""
        return self.health_monitor.get_health_summary()
    
    def add_health_callback(self, callback: Callable[[HealthStatus, HealthStatus], None]):
        """Add callback for health status changes."""
        self.health_monitor.add_status_callback(callback)
    
    def get_price_history(self, symbol: str) -> List[Dict[str, Any]]:
        """Get recent price history from WebSocket data."""
        with self._data_lock:
            if symbol in self._price_data:
                return list(self._price_data[symbol])
        return []

