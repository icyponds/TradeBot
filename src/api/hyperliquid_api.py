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

# Lazy imports for SDK modules - these are expensive and should only be
# imported when actually needed. This speeds up test setup significantly.
# The actual imports happen in _init_sdk_clients() and _get_account_class()
if TYPE_CHECKING:
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    from eth_account import Account

from .interface import MarketInterface

# =============================================================================
# RATE LIMITER
# =============================================================================

class RateLimiter:
    """Token bucket rate limiter for API calls."""
    
    def __init__(self, calls_per_second: float = 10, burst_size: int = 20):
        """
        Initialize rate limiter.
        
        Args:
            calls_per_second: Sustained rate limit
            burst_size: Maximum burst capacity
        """
        self.calls_per_second = calls_per_second
        self.burst_size = burst_size
        self.tokens = burst_size
        self.last_update = time.time()
        self._lock = threading.Lock()
    
    def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire a token, blocking if necessary.
        
        Args:
            timeout: Maximum time to wait for a token
            
        Returns:
            True if token acquired, False if timeout
        """
        deadline = time.time() + timeout
        
        while time.time() < deadline:
            with self._lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
            
            # Wait a bit before retrying
            time.sleep(0.05)
        
        return False
    
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
                try:
                    self.on_bar_complete_callback(symbol, timeframe, completed_bar)
                except Exception:
                    pass  # Don't let callback errors break tick processing
            
            # Create new bar
            bar = {"time": key, "open": price, "high": price, "low": price, "close": price, "volume": 0.0}
            dq.append(bar)
        bar["close"] = price
        bar["high"] = max(bar["high"], price)
        bar["low"] = min(bar["low"], price)
        bar["volume"] = bar.get("volume", 0.0) + (volume or 0.0)


# =============================================================================
# RETRY DECORATOR
# =============================================================================

def with_retry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple = (Exception,)
):
    """
    Decorator for automatic retry with exponential backoff.
    
    Args:
        max_attempts: Maximum number of attempts
        base_delay: Initial delay between retries
        max_delay: Maximum delay between retries
        exponential_base: Base for exponential backoff
        retryable_exceptions: Exceptions that trigger retry
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (exponential_base ** attempt), max_delay)
                        logging.getLogger(__name__).warning(
                            f"Retry {attempt + 1}/{max_attempts} for {func.__name__}: {e}. "
                            f"Waiting {delay:.2f}s"
                        )
                        time.sleep(delay)
            
            raise last_exception
        return wrapper
    return decorator


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
        self.public_account_address = config['api'].get(
            'public_account_address', 
            config['api']['wallet_address']
        )
        
        # HIP-3 configuration
        hip3_config = config.get('hip3', {})
        self.hip3_enabled = hip3_config.get('enabled', False)
        self.perp_dexs = hip3_config.get('perp_dexs', None)
        
        # Rate limiter configuration
        rate_config = config.get('api', {}).get('rate_limit', {})
        self.rate_limiter = RateLimiter(
            calls_per_second=rate_config.get('calls_per_second', 10),
            burst_size=rate_config.get('burst_size', 20)
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
        
        # Async persistence worker
        self._persistence_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db_persist")
        
        # Wire up OhlcvCache callback for DB persistence on boundary crossing
        self.ohlcv_cache.on_bar_complete_callback = self._on_bar_complete
        
        # Real-time data storage
        self._price_data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._symbol_last_tick: Dict[str, float] = {}  # Per-symbol last tick timestamp
        self._subscribed_symbols: set = set()
        self._callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._data_lock = threading.Lock()
        
        # Initialize SDK clients
        self._init_sdk_clients()
        
        self.logger.info(f"Initialized HyperliquidAPI (HIP-3: {self.hip3_enabled})")
    
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

            # Auto-discover HIP-3 dexes if enabled but not specified
            if self.hip3_enabled and self.perp_dexs is None:
                self.perp_dexs = self._discover_perp_dexs()
            
            # Initialize Info client WITHOUT WebSocket first (fast, non-blocking)
            # WebSocket will be enabled when start() is called
            self.info = Info(
                self.base_url,
                skip_ws=True,  # Start without WebSocket for fast init
                perp_dexs=self.perp_dexs if self.hip3_enabled else None
            )
            self._ws_enabled = False
            
            # Initialize Exchange client if credentials provided
            self.exchange = None
            if self.private_key and self.wallet_address:
                try:
                    wallet = Account.from_key(self.private_key)
                    self.exchange = Exchange(
                        wallet=wallet,
                        base_url=self.base_url,
                        perp_dexs=self.perp_dexs if self.hip3_enabled else None
                    )
                    self.logger.info("Exchange client initialized")
                except Exception as e:
                    self.logger.warning(f"Exchange client init failed: {e}")
            
        except Exception as e:
            self.logger.error(f"SDK initialization failed: {e}")
            raise
    
    def _enable_websocket(self, timeout: float = 10.0):
        """Enable WebSocket connection (called from start())."""
        if self._ws_enabled:
            return
        
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
            self._ws_enabled = True
            self._setup_websocket_subscriptions()
            self.logger.info("WebSocket enabled successfully")
    
    def _discover_perp_dexs(self) -> List[str]:
        """Discover available perp dexes."""
        try:
            from hyperliquid.info import Info
            temp_info = Info(self.base_url, skip_ws=True)
            dexs = temp_info.perp_dexs()
            
            dex_names = [""]  # Always include native
            if dexs and len(dexs) > 1:
                for dex in dexs[1:]:
                    if isinstance(dex, dict) and 'name' in dex:
                        dex_names.append(dex['name'])
            
            self.logger.info(f"Discovered perp dexes: {dex_names}")
            return dex_names
        except Exception as e:
            self.logger.warning(f"Perp dex discovery failed: {e}")
            return [""]
    
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
            
            mids = data.get('mids', {})
            timestamp = time.time()
            
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
    
    def _rate_limited_call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute a rate-limited API call with circuit breaker and latency tracking."""
        # Check circuit breaker
        if not self.circuit_breaker.can_execute():
            raise RuntimeError("Circuit breaker is open - API temporarily unavailable")
        
        # Acquire rate limit token
        if not self.rate_limiter.acquire(timeout=30.0):
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
            self.circuit_breaker.record_failure()
            raise
    
    # =========================================================================
    # CONNECTION & LIFECYCLE
    # =========================================================================
    
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
        
        self.logger.info("HyperliquidAPI started successfully")
        return True
    
    def stop(self):
        """Stop the API, health monitor, and cleanup."""
        self.logger.info("Stopping HyperliquidAPI...")
        
        # Stop health monitor
        self.health_monitor.stop()
        
        # Cleanup
        self.cache.clear()
        self.order_tracker.cleanup_old(max_age_hours=0)
        
        # Stop persistence executor
        self._persistence_executor.shutdown(wait=False)
        
        self.logger.info("HyperliquidAPI stopped")
    
    def test_connection(self) -> bool:
        """Test API connection."""
        try:
            meta = self.info.meta()
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
        with self._data_lock:
            if symbol in self._price_data and self._price_data[symbol]:
                latest = self._price_data[symbol][-1]
                # Only use if fresh (< 5 seconds old)
                if time.time() - latest['timestamp'] < 5.0:
                    return latest['price']
        
        # Fall back to SDK's allMids (also uses WebSocket internally)
        try:
            all_mids = self.info.all_mids()
            if symbol in all_mids:
                return float(all_mids[symbol])
        except Exception as e:
            self.logger.debug(f"all_mids failed: {e}")
        
        # Last resort: REST call
        return self._get_price_from_rest(symbol)

    @with_retry(max_attempts=3, base_delay=0.5)
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
            
        result = self._rate_limited_call(_fetch)
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
    
    @with_retry(max_attempts=3, base_delay=0.5)
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
        
        def _fetch():
            # 1. Check Native Universe
            meta_and_ctxs = self.info.meta_and_asset_ctxs()
            
            if len(meta_and_ctxs) >= 2:
                universe = meta_and_ctxs[0]['universe']
                asset_contexts = meta_and_ctxs[1]
                
                for i, asset in enumerate(universe):
                    if asset.get('name') == symbol:
                        ctx = asset_contexts[i] if i < len(asset_contexts) else {}
                        return self._parse_market_ctx(symbol, asset, ctx)

            # 2. If not found and HIP-3 enabled, check other Dexes
            if self.hip3_enabled:
                try:
                    dexs = self.info.perp_dexs()
                    # Skip first (Native)
                    for dex in dexs[1:]:
                        dex_name = dex.get('name')
                        if not dex_name: continue
                        
                        # Optimization: If symbol structure implies dex (e.g. hyna:BTC), maybe strictly check that dex?
                        # But for now, simple iteration.
                        
                        try:
                            # Note: This is expensive if we have many dexes.
                            # Ideally we cache which symbol is on which dex.
                            res = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex_name})
                            if res and len(res) >= 2:
                                u_hip3 = res[0]['universe']
                                c_hip3 = res[1]
                                
                                for i, asset in enumerate(u_hip3):
                                    if asset.get('name') == symbol:
                                        ctx = c_hip3[i] if i < len(c_hip3) else {}
                                        return self._parse_market_ctx(symbol, asset, ctx)
                        except Exception:
                            continue
                except Exception as e:
                    self.logger.warning(f"HIP-3 lookup failed in get_market_data: {e}")

            return None
            
        result = self._rate_limited_call(_fetch)
        if result:
            self.cache.set(cache_key, result, ttl=self.cache_ttl_market_data)
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
    
    @with_retry(max_attempts=3, base_delay=0.5)
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
                                res = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex_name})
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
            
        result = self._rate_limited_call(_fetch)
        if result:
             self.cache.set(cache_key, result, ttl=300.0)
             
        return result
    
    @with_retry(max_attempts=3, base_delay=0.5)
    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Get OHLCV candlestick data with in-memory rolling cache.
        Seed once, then serve from cache (updated via ticks).
        
        Handles both perp symbols (e.g., "BTC") and spot tokens (e.g., "UBTC").
        Spot tokens are automatically converted to their API name (e.g., "@109").
        """
        # Try cache first (always use human-readable symbol for cache key)
        cached_bars = self.ohlcv_cache.get(symbol, timeframe)
        if cached_bars and len(cached_bars) >= min(limit, self.ohlcv_cache.maxlen[symbol][timeframe]):
            df = pd.DataFrame(cached_bars)
            df['timestamp'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('timestamp', inplace=True)
            return df.tail(limit)
        
        # Determine the API symbol to use for fetching
        # Perp symbols work directly, spot tokens need conversion to @N format
        api_symbol = symbol
        is_spot = False
        
        # Check if this is a valid perp symbol first
        asset_info = self._get_asset_info_for_symbol(symbol)
        is_valid_perp = asset_info is not None
        
        # If not a perp, check if it's a spot token
        if not is_valid_perp:
            spot_api_name = self.get_spot_api_name(symbol)
            if spot_api_name:
                api_symbol = spot_api_name
                is_spot = True
                self.logger.debug(f"Spot token {symbol} -> API name {api_symbol}")
            else:
                # Neither a valid perp nor a resolvable spot token
                self.logger.debug(f"Symbol {symbol} is neither a valid perp nor a resolvable spot token")
                return None
        
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
                        now = pd.Timestamp.now()
                        
                        if gap_start_dt < now:
                            # Fetch only the gap
                            gap_start_ms = int(gap_start_dt.timestamp() * 1000)
                            self.logger.debug(f"Gap-fill for {symbol} {timeframe}: fetching from {gap_start_dt}")
                            candles = self.info.candles_snapshot(api_symbol, timeframe, gap_start_ms, end_time)
                            if candles:
                                new_bars = [{
                                    'time': c['t'] // 1000,
                                    'open': float(c['o']),
                                    'high': float(c['h']),
                                    'low': float(c['l']),
                                    'close': float(c['c']),
                                    'volume': float(c['v']),
                                } for c in candles]
                                new_df = pd.DataFrame(new_bars)
                                new_df['timestamp'] = pd.to_datetime(new_df['time'], unit='s')
                                new_df.set_index('timestamp', inplace=True)
                                self.market_db.insert_market_data(new_df, symbol, timeframe)
                                # Merge with existing
                                df = pd.concat([df, new_df]).drop_duplicates()
                        
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
            start_time = end_time - (limit * interval_ms)
            
            # Try using SDK wrapper first, fallback to direct call if symbol unknown (common for HIP-3)
            candles = []
            # Try using SDK wrapper first, fallback to direct call if symbol unknown (common for HIP-3)
            candles = []
            try:
                # This uses info.name_to_coin map which might miss new HIP-3 assets
                candles = self.info.candles_snapshot(api_symbol, timeframe, start_time, end_time)
            except KeyError:
                # Fallback: Symbol not in SDK map, try raw API call
                # HIP-3 assets are often queryable by their name directly
                try:
                    req = {
                        "coin": api_symbol, 
                        "interval": timeframe, 
                        "startTime": start_time, 
                        "endTime": end_time
                    }
                    candles = self.info.post("/info", {"type": "candleSnapshot", "req": req})
                except Exception as e_raw:
                     self.logger.warning(f"Failed raw candle fetch for {api_symbol}: {e_raw}")
                     return None
            except Exception as e:
                self.logger.error(f"Error fetching candles for {api_symbol}: {e}")
                return None
                
            if not candles:
                return None
            
            bars = [{
                'time': c['t'] // 1000,
                'open': float(c['o']),
                'high': float(c['h']),
                'low': float(c['l']),
                'close': float(c['c']),
                'volume': float(c['v']),
            } for c in candles]
            
            # Save to database for future restarts
            if self.market_db:
                try:
                    df = pd.DataFrame(bars)
                    df['timestamp'] = pd.to_datetime(df['time'], unit='s')
                    df.set_index('timestamp', inplace=True)
                    self.market_db.insert_market_data(df, symbol, timeframe)
                except Exception as e:
                    self.logger.debug(f"Failed to cache {symbol} {timeframe}: {e}")
            
            # Seed in-memory cache
            self.ohlcv_cache.seed(symbol, timeframe, bars, maxlen=max(limit, 300))
            df = pd.DataFrame(bars)
            df['timestamp'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('timestamp', inplace=True)
            return df.tail(limit)
        
        return self._rate_limited_call(_fetch)

    def update_ohlcv_from_tick(self, symbol: str, price: float, volume: float = 0.0, ts: Optional[float] = None):
        """Update rolling OHLCV cache from a live tick."""
        if ts is None:
            ts = time.time()
        self.ohlcv_cache.update_from_tick(symbol, price, volume, ts)
    
    
    def _on_bar_complete(self, symbol: str, timeframe: str, bar: dict):
        """
        Called when a candle period closes. 
        Instead of persisting the in-memory bar directly, trigger a background verification fetch.
        This implements the "Verify-on-Write" pattern to ensure data integrity.
        """
        if not self.market_db:
            return
            
        # Submit to background executor to avoid blocking WebSocket thread
        self._persistence_executor.submit(
            self._fetch_and_persist_candle, 
            symbol, 
            timeframe, 
            bar['time']
        )

    def _fetch_and_persist_candle(self, symbol: str, timeframe: str, timestamp: int):
        """
        Worker method to fetch the official finalized candle from API and persist it.
        """
        try:
            # Wait a brief buffer to ensure exchange engine has finalized the candle
            time.sleep(2.0)
            
            # Fetch specific candle from API (Source of Truth)
            # We want the candle STARTING at timestamp
            start_ms = timestamp * 1000
            end_ms = start_ms + self._get_interval_ms(timeframe) - 1
            
            # Use candles_snapshot to fetch strictly finalized data
            candles = self.info.candles_snapshot(
                self._get_api_symbol(symbol), 
                timeframe, 
                start_ms, 
                end_ms + 1000 # Buffer to ensure we cover the range
            )
            
            # Find the exact candle matching our timestamp
            target_candle = None
            if candles:
                for c in candles:
                    if c['t'] == start_ms:
                        target_candle = c
                        break
            
            if target_candle:
                # Format for DB
                bar = {
                    'time': target_candle['t'] // 1000,
                    'open': float(target_candle['o']),
                    'high': float(target_candle['h']),
                    'low': float(target_candle['l']),
                    'close': float(target_candle['c']),
                    'volume': float(target_candle['v']),
                }
                
                # Persist to DB
                df = pd.DataFrame([bar])
                df['timestamp'] = pd.to_datetime(df['time'], unit='s')
                df.set_index('timestamp', inplace=True)
                
                self.market_db.insert_market_data(df, symbol, timeframe)
                self.logger.debug(f"Persisted VERIFIED {symbol}/{timeframe} candle @ {bar['time']}")
                
                # OPTIONAL: Self-heal in-memory cache if needed (omitted for safety to avoid race conditions 
                # with active WebSocket updates, as the next in-memory candle is already building)
                
            else:
                self.logger.warning(
                    f"Could not verify candle for {symbol}/{timeframe} @ {timestamp}. "
                    "Skipping persistence to avoid corruption."
                )
                
        except Exception as e:
            self.logger.error(f"Failed to persist verified candle for {symbol}/{timeframe}: {e}")
    
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
        """Fetch in-progress candle to ensure no boundary gap."""
        try:
            now_ms = int(time.time() * 1000)
            interval_ms = self._get_interval_ms(timeframe)
            current_bar_start = now_ms - (now_ms % interval_ms)
            
            candles = self.info.candles_snapshot(api_symbol, timeframe, current_bar_start, now_ms)
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
    
    def _initialize_live_data(self, symbol: str, api_symbol: str, max_retries: int = 2) -> bool:
        """
        Fetch in-progress candles for all timeframes and subscribe to WebSocket.
        If fails after retries, adds to pending queue for later retry.
        """
        timeframes = ['5m', '15m', '1h', '4h']
        
        for attempt in range(max_retries):
            try:
                # Step 1: Fetch current in-progress candles for all timeframes
                for tf in timeframes:
                    self._append_current_candle(symbol, tf, api_symbol)
                
                # Step 2: Subscribe to WebSocket immediately after
                self.subscribe_symbol(symbol)
                
                # Step 3: Verify subscription is active
                if symbol in self._subscribed_symbols:
                    self.logger.info(f"Initialized live data for {symbol}")
                    self._pending_init_symbols.discard(symbol)
                    return True
            except Exception as e:
                self.logger.warning(f"Live data init failed for {symbol}: {e} (attempt {attempt+1}/{max_retries})")
                time.sleep(0.3)
        
        # After max_retries, add to pending queue and continue
        self._pending_init_symbols.add(symbol)
        self.logger.warning(f"Deferred {symbol} initialization to retry queue")
        return False
    
    def retry_pending_subscriptions(self):
        """Called periodically to retry failed subscriptions."""
        if not self._pending_init_symbols:
            return
        
        pending = list(self._pending_init_symbols)
        self.logger.info(f"Retrying {len(pending)} pending subscriptions: {pending}")
        
        for symbol in pending:
            # Re-attempt with fresh data
            api_symbol = symbol  # May need conversion for spot
            asset_info = self._get_asset_info_for_symbol(symbol)
            if not asset_info:
                spot_api_name = self.get_spot_api_name(symbol)
                if spot_api_name:
                    api_symbol = spot_api_name
            
            self._initialize_live_data(symbol, api_symbol, max_retries=1)
            
            # Rate limit: small delay between symbols
            time.sleep(0.2)
    
    def get_all_prices(self) -> Dict[str, float]:
        """Get current prices for all symbols."""
        try:
            all_mids = self.info.all_mids()
            return {k: float(v) for k, v in all_mids.items()}
        except Exception as e:
            self.logger.error(f"Error getting all prices: {e}")
            return {}
    
    def get_order_book(self, symbol: str, depth: int = 20) -> Optional[Dict[str, Any]]:
        """Get order book for a symbol."""
        try:
            book = self.info.l2_snapshot(symbol)
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
        try:
            meta = self.info.meta()
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
            # Check for isolated-only assets
            asset_info = self._get_asset_info_for_symbol(symbol)
            if asset_info and asset_info.get('onlyIsolated', False):
                if is_cross:
                    self.logger.warning(f"Asset {symbol} only supports Isolated Margin. Forcing is_cross=False.")
                    is_cross = False
            
            self.logger.info(f"Updating leverage for {symbol} to {leverage}x (Cross: {is_cross})")
            result = self.exchange.update_leverage(leverage, symbol, is_cross)
            
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
    
    @with_retry(max_attempts=3, base_delay=0.5)
    def get_account_balance(self) -> Optional[Dict[str, Any]]:
        """Get account balance and margin information."""
        cache_key = "account_balance"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        def _fetch():
            user_state = self.info.user_state(self.public_account_address)
            
            margin_summary = user_state.get('marginSummary', {})
            
            account_value = float(margin_summary.get('accountValue', 0))
            total_margin_used = float(margin_summary.get('totalMarginUsed', 0))
            
            result = {
                'wallet_address': self.public_account_address,
                'total_equity': account_value,
                'free_margin': account_value - total_margin_used,
                'used_margin': total_margin_used,
                'unrealized_pnl': float(margin_summary.get('totalUnrealizedPnl', 0)),
            }
            
            self.cache.set(cache_key, result, ttl=self.cache_ttl_positions)
            return result
        
        return self._rate_limited_call(_fetch)
    
    @with_retry(max_attempts=3, base_delay=0.5)
    def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions."""
        cache_key = "positions"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        def _fetch():
            user_state = self.info.user_state(self.public_account_address)
            
            positions = []
            for pos in user_state.get('assetPositions', []):
                position_data = pos.get('position', {})
                size = float(position_data.get('szi', 0))
                
                if size != 0:
                    positions.append({
                        'symbol': position_data.get('coin'),
                        'size': size,
                        'side': 'long' if size > 0 else 'short',
                        'entry_price': float(position_data.get('entryPx', 0)),
                        'mark_price': float(position_data.get('positionValue', 0)) / abs(size) if size != 0 else 0,
                        'unrealized_pnl': float(position_data.get('unrealizedPnl', 0)),
                        'leverage': position_data.get('leverage', {}),
                    })
            
            self.cache.set(cache_key, positions, ttl=self.cache_ttl_positions)
            return positions
        
        return self._rate_limited_call(_fetch)
    
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
        
        return self._rate_limited_call(_fetch)
    
    # =========================================================================
    # FUND TRANSFERS & COLLATERAL MANAGEMENT
    # =========================================================================
    
    def get_spot_balance(self, token: str = "USDC") -> float:
        """Get balance of a specific token in spot account."""
        try:
            spot_state = self.info.spot_user_state(self.public_account_address)
            for balance in spot_state.get('balances', []):
                if balance.get('coin') == token:
                    return float(balance.get('total', 0))
            return 0.0
        except Exception as e:
            self.logger.error(f"Error getting spot balance for {token}: {e}")
            return 0.0
    
    def get_perp_balance(self) -> Dict[str, float]:
        """Get perp account balance and margin info."""
        try:
            user_state = self.info.user_state(self.public_account_address)
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
            result = self.exchange.usd_class_transfer(amount, to_perp=True)
            
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
            result = self.exchange.usd_class_transfer(amount, to_perp=False)
            
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
            result = self.exchange.update_isolated_margin(amount, symbol)
            
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
    
    def get_position_margin_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed margin information for a specific position.
        
        Returns liquidation price, margin ratio, etc.
        """
        try:
            user_state = self.info.user_state(self.public_account_address)
            
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
    _INITIAL_SLIPPAGE_BPS = 10  # 0.1% initial slippage tolerance
    _MAX_SLIPPAGE_BPS = 100  # 1% max slippage for aggressive fills
    
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
                asset_info = self._get_asset_info_for_symbol(symbol)
                price = self.get_current_price(symbol)
                
                if not price:
                    return None
                
                return {
                    'symbol': symbol,
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
            
            # Validate minimum order value ($10)
            order_value = remaining_size * current_price
            if order_value < 10.0:
                self.logger.error(f"Order value ${order_value:.2f} below minimum $10")
                return None
            
            is_buy = side.lower() == 'buy'
            
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
                    # Spot order
                    response = self._rate_limited_call(
                        self.exchange.order,
                        trading_symbol,
                        is_buy,
                        remaining_size,
                        exec_price,
                        {"limit": {"tif": "Ioc"}}
                    )
                    # Spot order
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
                        market_response = self._rate_limited_call(
                            self.exchange.market_open,
                            trading_symbol,
                            is_buy,  # Opposite side to close position
                            remaining_size,
                            None,  # px=None for market order
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
            
            # Validate minimum order value ($10)
            order_value = rounded_size * current_price
            if order_value < 10.0:
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
    
    def _parse_order_response(
        self,
        response: Dict[str, Any],
        symbol: str,
        side: str,
        size: float,
        price: Optional[float]
    ) -> Optional[Dict[str, Any]]:
        """Parse SDK order response."""
        if not response:
            return None
        
        try:
            status_data = response.get('response', {}).get('data', {}).get('statuses', [])
            
            if not status_data:
                return None
            
            status = status_data[0]
            
            if 'error' in status:
                self.logger.error(f"Order error: {status['error']}")
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
            fills = self.info.user_fills(self.wallet_address)
            
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
    
    @with_retry(max_attempts=3, base_delay=0.5)
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
    
    # =========================================================================
    # SPOT TRADING
    # =========================================================================
    
    @with_retry(max_attempts=3, base_delay=0.5)
    def get_spot_meta(self) -> Optional[Dict[str, Any]]:
        """Get spot market metadata."""
        try:
            cache_key = "spot_meta"
            cached = self.cache.get(cache_key)
            if cached:
                return cached
                
            data = self.info.spot_meta()
            
            # Cache for 5 minutes
            self.cache.set(cache_key, data, ttl=300)
            return data
        except Exception as e:
            self.logger.error(f"Error fetching spot meta: {e}")
            return None
    
    def get_spot_meta_and_asset_ctxs(self) -> Optional[Tuple]:
        """Get spot metadata and asset contexts."""
        try:
            result = self.info.spot_meta_and_asset_ctxs()
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
                            return pair.get('name')  # Returns "@109" format
            
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
        try:
            return self.info.funding_history(symbol, start_time_ms, end_time_ms)
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
                            dex_meta_ctx = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex_name})
                            
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
        'BTC': 'UBTC',
        'ETH': 'UETH',
        'SOL': 'USOL',
        'BONK': 'UBONK',
        # 'DOGE': 'UDOGE',  # Not available on exchange as of Jan 2026
        'MOG': 'UMOG',
        'WLD': 'UWLD',
        'ENA': 'UENA',
        'XPL': 'UXPL',
        'MON': 'UMON',  # Monad - verified via tokenDetails endpoint
        'PUMP': 'UPUMP',
        'FARTCOIN': 'UFART',
        'MEGA': 'UMEGA',
        # Direct matches (token name is the same for perp and spot)
        'PURR': 'PURR',
        'HYPE': 'HYPE',
        'TRUMP': 'TRUMP',
        'STABLE': 'STABLE',
        'BERA': 'BERA',
    }
    
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
            for pair in spot_meta.get('universe', []):
                tokens = pair.get('tokens', [])
                if len(tokens) >= 2:
                    base_idx, quote_idx = tokens[0], tokens[1]
                    if base_idx < len(token_list) and quote_idx < len(token_list):
                        base_name = token_list[base_idx].get('name', '')
                        quote_name = token_list[quote_idx].get('name', '')
                        if base_name == mapped_spot and quote_name == 'USDC':
                            return mapped_spot
            
            self.logger.warning(f"Mapped spot token {mapped_spot} for {perp_symbol} not found on exchange - please update PERP_TO_SPOT_MAPPING")
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
    
    def subscribe_symbol(self, symbol: str):
        """Subscribe to real-time data for a symbol."""
        self._subscribed_symbols.add(symbol)
        
        # Initialize tracking for standard timeframes
        # This ensures background assets (not in active strategies) still build candle history
        # and persist it to DB via verify-on-write pattern
        standard_timeframes = ['5m', '15m', '1h', '4h', '1d']
        for tf in standard_timeframes:
            self.ohlcv_cache.ensure_timeframe(symbol, tf, maxlen=1000)
            
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
        if asset_info:
            for asset in asset_info.get('universe', []):
                if asset['name'] == symbol:
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

