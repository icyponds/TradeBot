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
from typing import Dict, List, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict, deque
from functools import wraps
from enum import Enum
import pandas as pd

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from eth_account import Account


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
        ws_stale_threshold: float = 60.0,
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

class HyperliquidAPI:
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
    SPOT_START = 10000
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
        
        # Real-time data storage
        self._price_data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._subscribed_symbols: set = set()
        self._callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._data_lock = threading.Lock()
        
        # Initialize SDK clients
        self._init_sdk_clients()
        
        self.logger.info(f"Initialized HyperliquidAPI (HIP-3: {self.hip3_enabled})")
    
    def _init_sdk_clients(self):
        """Initialize SDK Info and Exchange clients."""
        try:
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
                                
                    except (ValueError, TypeError):
                        pass
                        
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
            meta_and_ctxs = self.info.meta_and_asset_ctxs()
            
            if len(meta_and_ctxs) < 2:
                return None
            
            universe = meta_and_ctxs[0]['universe']
            asset_contexts = meta_and_ctxs[1]
            
            for i, asset in enumerate(universe):
                if asset.get('name') == symbol:
                    ctx = asset_contexts[i] if i < len(asset_contexts) else {}
                    
                    market_data = {
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
                    
                    self.cache.set(cache_key, market_data, ttl=self.cache_ttl_market_data)
                    return market_data
            
            return None
        
        return self._rate_limited_call(_fetch)
    
    @with_retry(max_attempts=3, base_delay=0.5)
    def get_asset_info(self) -> Optional[Dict[str, Any]]:
        """Get asset information for all perpetuals."""
        cache_key = "asset_info"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        def _fetch():
            meta_and_ctxs = self.info.meta_and_asset_ctxs()
            
            if len(meta_and_ctxs) < 2:
                return None
            
            meta = meta_and_ctxs[0]
            asset_contexts = meta_and_ctxs[1]
            
            universe = []
            for i, asset in enumerate(meta['universe']):
                ctx = asset_contexts[i] if i < len(asset_contexts) else {}
                
                oi_tokens = float(ctx.get('openInterest', 0))
                mark_price = float(ctx.get('markPx', 0))
                
                universe.append({
                    'name': asset['name'],
                    'maxLeverage': asset.get('maxLeverage', 0),
                    'szDecimals': asset.get('szDecimals', 0),
                    'openInterest': oi_tokens * mark_price,
                    'volume24h': float(ctx.get('dayNtlVlm', 0)),
                    'markPrice': mark_price,
                    'funding': float(ctx.get('funding', 0)),
                    'bid': float(ctx.get('impactPxs', [0, 0])[0]) if ctx.get('impactPxs') else 0,
                    'ask': float(ctx.get('impactPxs', [0, 0])[1]) if ctx.get('impactPxs') and len(ctx.get('impactPxs')) > 1 else 0,
                })
            
            result = {'universe': universe, 'meta': meta}
            self.cache.set(cache_key, result, ttl=self.cache_ttl_asset_info)
            return result
        
        return self._rate_limited_call(_fetch)
    
    @with_retry(max_attempts=3, base_delay=0.5)
    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """Get OHLCV candlestick data."""
        cache_key = f"ohlcv_{symbol}_{timeframe}_{limit}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        def _fetch():
            # Convert timeframe to milliseconds
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
            start_time = end_time - (limit * interval_ms)
            
            candles = self.info.candles_snapshot(symbol, timeframe, start_time, end_time)
            
            if not candles:
                return None
            
            data = []
            for candle in candles:
                data.append({
                    'timestamp': pd.to_datetime(candle['t'], unit='ms'),
                    'open': float(candle['o']),
                    'high': float(candle['h']),
                    'low': float(candle['l']),
                    'close': float(candle['c']),
                    'volume': float(candle['v']),
                })
            
            df = pd.DataFrame(data)
            df.set_index('timestamp', inplace=True)
            
            self.cache.set(cache_key, df, ttl=60.0)  # Cache for 1 minute
            return df
        
        return self._rate_limited_call(_fetch)
    
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
    
    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================
    
    # Smart order execution parameters (not user-configurable)
    _ORDER_WALK_MAX_ATTEMPTS = 5  # Max price improvement attempts
    _ORDER_WALK_STEP_BPS = 20  # 0.2% price step per attempt (20 basis points)
    _ORDER_WALK_DELAY = 0.3  # Seconds between attempts
    _INITIAL_SLIPPAGE_BPS = 10  # 0.1% initial slippage tolerance
    _MAX_SLIPPAGE_BPS = 100  # 1% max slippage for aggressive fills
    
    def execute_order(
        self,
        symbol: str,
        side: str,
        size: float,
        reduce_only: bool = False,
        urgency: str = "normal"
    ) -> Optional[Dict[str, Any]]:
        """
        Smart order execution with automatic price management.
        
        Automatically determines optimal execution strategy:
        - Starts with tight spread around mid-price
        - Walks the price if not immediately filled
        - Handles partial fills automatically
        - Returns aggregated fill information
        
        Args:
            symbol: Trading symbol
            side: 'buy' or 'sell'
            size: Order size
            reduce_only: Only reduce existing position
            urgency: 'low' (patient), 'normal', or 'high' (aggressive)
            
        Returns:
            Order result with fill details
        """
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return None
        
        try:
            # Get asset info and round size
            asset_info = self._get_asset_info_for_symbol(symbol)
            sz_decimals = asset_info.get('szDecimals', 2) if asset_info else 2
            remaining_size = round(size, sz_decimals)
            
            # Get current market price
            current_price = self.get_current_price(symbol)
            if not current_price:
                self.logger.error(f"Cannot execute order: no price for {symbol}")
                return None
            
            # Validate minimum order value ($10)
            order_value = remaining_size * current_price
            if order_value < 10.0:
                self.logger.error(f"Order value ${order_value:.2f} below minimum $10")
                return None
            
            is_buy = side.lower() == 'buy'
            
            # Configure slippage based on urgency
            if urgency == "low":
                initial_slippage = self._INITIAL_SLIPPAGE_BPS / 2
                max_attempts = self._ORDER_WALK_MAX_ATTEMPTS + 3
            elif urgency == "high":
                initial_slippage = self._INITIAL_SLIPPAGE_BPS * 3
                max_attempts = 2
            else:
                initial_slippage = self._INITIAL_SLIPPAGE_BPS
                max_attempts = self._ORDER_WALK_MAX_ATTEMPTS
            
            # Track aggregated fills
            total_filled = 0.0
            weighted_price_sum = 0.0
            all_fills = []
            
            for attempt in range(max_attempts):
                if remaining_size <= 0:
                    break
                
                # Refresh price on each attempt
                fresh_price = self.get_current_price(symbol)
                if fresh_price:
                    current_price = fresh_price
                
                # Calculate execution price with progressive slippage
                slippage_bps = min(
                    initial_slippage + (attempt * self._ORDER_WALK_STEP_BPS),
                    self._MAX_SLIPPAGE_BPS
                )
                slippage_mult = slippage_bps / 10000
                
                if is_buy:
                    exec_price = current_price * (1 + slippage_mult)
                else:
                    exec_price = current_price * (1 - slippage_mult)
                
                exec_price = self._round_to_tick(exec_price, symbol)
                
                self.logger.debug(
                    f"Order attempt {attempt + 1}/{max_attempts}: {side} {remaining_size} {symbol} "
                    f"@ {exec_price:.6f} (slippage: {slippage_bps}bps)"
                )
                
                # Place IOC order
                response = self._rate_limited_call(
                    self.exchange.order,
                    symbol,
                    is_buy,
                    remaining_size,
                    exec_price,
                    {"limit": {"tif": "Ioc"}},
                    reduce_only=reduce_only
                )
                
                # Parse response
                fill_result = self._parse_order_response(
                    response, symbol, side, remaining_size, exec_price
                )
                
                if fill_result:
                    filled = fill_result.get('filled_size', 0)
                    avg_px = fill_result.get('avg_fill_price', exec_price)
                    
                    if filled > 0:
                        total_filled += filled
                        weighted_price_sum += filled * avg_px
                        all_fills.append({
                            'attempt': attempt + 1,
                            'size': filled,
                            'price': avg_px,
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
                    'order_id': f"smart_{symbol}_{int(time.time() * 1000)}",
                    'symbol': symbol,
                    'side': side,
                    'size': size,
                    'filled_size': total_filled,
                    'unfilled_size': remaining_size,
                    'avg_fill_price': avg_fill_price,
                    'status': status,
                    'fills': all_fills,
                    'timestamp': datetime.now(),
                }
                
                self.logger.info(
                    f"{'✓' if status == 'filled' else '⚠'} Order {status}: "
                    f"{side} {total_filled:.6f}/{size:.6f} {symbol} @ {avg_fill_price:.6f}"
                )
                
                # Invalidate caches
                self.cache.invalidate("positions")
                
                return result
            else:
                self.logger.warning(
                    f"Order not filled after {max_attempts} attempts: {side} {size} {symbol}"
                )
                return {
                    'order_id': None,
                    'symbol': symbol,
                    'side': side,
                    'size': size,
                    'filled_size': 0,
                    'status': 'not_filled',
                    'timestamp': datetime.now(),
                }
                
        except Exception as e:
            self.logger.error(f"Smart order execution failed: {e}")
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
                exec_price = self._round_to_tick(exec_price, symbol)
                tif = "Ioc"  # Immediate or cancel for market orders
            else:
                # Explicit limit price
                exec_price = self._round_to_tick(price, symbol)
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
            
            if 'filled' in status:
                order_id = status['filled'].get('oid')
                order_status = 'filled'
                filled_size = float(status['filled'].get('totalSz', size))
                avg_fill_price = float(status['filled'].get('avgPx', price or 0))
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
                'timestamp': datetime.now(),
                'raw_response': response,
            }
            
        except Exception as e:
            self.logger.error(f"Error parsing order response: {e}")
            return None
    
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
    
    def get_spot_meta(self) -> Optional[Dict[str, Any]]:
        """Get spot market metadata."""
        try:
            return self.info.spot_meta()
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
    
    def get_spot_price(self, base_token: str, quote_token: str = "USDC") -> Optional[float]:
        """Get spot price for a token pair."""
        try:
            result = self.get_spot_meta_and_asset_ctxs()
            if not result:
                return None
            
            spot_meta, contexts = result
            
            for i, pair in enumerate(spot_meta.get('universe', [])):
                tokens = pair.get('tokens', [])
                if len(tokens) >= 2:
                    token_list = spot_meta.get('tokens', [])
                    base_idx, quote_idx = tokens[0], tokens[1]
                    
                    if base_idx < len(token_list) and quote_idx < len(token_list):
                        if (token_list[base_idx].get('name') == base_token and
                            token_list[quote_idx].get('name') == quote_token):
                            if i < len(contexts):
                                return float(contexts[i].get('midPx', 0))
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting spot price: {e}")
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
    
    def place_spot_order(
        self,
        base_token: str,
        quote_token: str,
        side: str,
        size: float,
        price: Optional[float] = None,
        slippage: float = 0.05
    ) -> Optional[Dict[str, Any]]:
        """Place a spot order."""
        if not self.exchange:
            self.logger.error("Exchange client not initialized")
            return None
        
        try:
            spot_meta = self.get_spot_meta()
            if not spot_meta:
                return None
            
            # Find pair name
            pair_name = None
            for pair in spot_meta.get('universe', []):
                tokens = pair.get('tokens', [])
                if len(tokens) >= 2:
                    token_list = spot_meta.get('tokens', [])
                    if (tokens[0] < len(token_list) and tokens[1] < len(token_list)):
                        if (token_list[tokens[0]].get('name') == base_token and
                            token_list[tokens[1]].get('name') == quote_token):
                            pair_name = pair.get('name')
                            break
            
            if not pair_name:
                self.logger.error(f"Spot pair {base_token}/{quote_token} not found")
                return None
            
            is_buy = side.lower() == 'buy'
            
            if price is None:
                response = self._rate_limited_call(
                    self.exchange.market_open,
                    pair_name,
                    is_buy,
                    size,
                    slippage=slippage
                )
            else:
                response = self._rate_limited_call(
                    self.exchange.order,
                    pair_name,
                    is_buy,
                    size,
                    price,
                    {"limit": {"tif": "Gtc"}}
                )
            
            return self._parse_order_response(response, pair_name, side, size, price)
            
        except Exception as e:
            self.logger.error(f"Spot order failed: {e}")
            return None
    
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
        
        Note: When Info is initialized with perp_dexs, HIP-3 assets are automatically
        included in meta_and_asset_ctxs(). We identify them by checking the asset index
        against the native asset count.
        """
        all_assets = []
        
        try:
            # Get all assets (native + HIP-3 if perp_dexs was passed to Info)
            meta_and_ctxs = self.info.meta_and_asset_ctxs()
            
            if len(meta_and_ctxs) < 2:
                return all_assets
            
            universe = meta_and_ctxs[0].get('universe', [])
            contexts = meta_and_ctxs[1]
            
            # Get native perp count from meta (if available)
            # Assets beyond this index are HIP-3
            native_count = len(universe)  # Default assume all native
            
            # Try to determine native vs HIP-3 boundary
            # Native perps have '' as dex, HIP-3 have a dex name
            for i, asset in enumerate(universe):
                ctx = contexts[i] if i < len(contexts) else {}
                
                # Check if this is a HIP-3 asset
                # HIP-3 assets may have different structure or dex field
                is_hip3 = asset.get('dex', '') != '' if 'dex' in asset else False
                
                # Skip HIP-3 if not requested
                if is_hip3 and not include_hip3:
                    continue
                
                # Skip HIP-3 if not enabled
                if is_hip3 and not self.hip3_enabled:
                    continue
                
                all_assets.append({
                    'name': asset.get('name', ''),
                    'dex': asset.get('dex', ''),
                    'is_hip3': is_hip3,
                    'maxLeverage': asset.get('maxLeverage', 0),
                    'szDecimals': asset.get('szDecimals', 0),
                    'openInterest': float(ctx.get('openInterest', 0)) * float(ctx.get('markPx', 0)) if ctx.get('markPx') else 0,
                    'volume24h': float(ctx.get('dayNtlVlm', 0)),
                    'markPrice': float(ctx.get('markPx', 0)) if ctx.get('markPx') else 0,
                    'funding': float(ctx.get('funding', 0)),
                })
                
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
    # CALLBACKS & SUBSCRIPTIONS
    # =========================================================================
    
    def subscribe_symbol(self, symbol: str):
        """Subscribe to real-time data for a symbol."""
        self._subscribed_symbols.add(symbol)
        # SDK handles subscriptions via allMids automatically
    
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
    
    def _round_to_tick(self, price: float, symbol: Optional[str] = None) -> float:
        """
        Round price to valid tick size for Hyperliquid.
        
        Args:
            price: The price to round
            symbol: Optional symbol for future per-asset tick sizes
            
        Returns:
            Price rounded to valid tick size
        """
        if price <= 0:
            return price
        
        tick_size = self._get_tick_size(price)
        rounded = round(price / tick_size) * tick_size
        
        # Ensure we don't have floating point artifacts
        # Determine decimal places from tick size
        import math
        if tick_size >= 1:
            decimals = 0
        else:
            decimals = -int(math.floor(math.log10(tick_size)))
        
        return round(rounded, decimals)
    
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

