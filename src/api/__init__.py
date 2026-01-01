"""
Hyperliquid API package.

Provides a unified API client using the official SDK with:
- Real-time WebSocket data (built into SDK)
- REST operations for trading
- Rate limiting
- Automatic retry with backoff
- Request caching with TTL
- Circuit breaker for resilience
- Proper order tracking
- Connection health monitoring
"""

from .hyperliquid_api import (
    HyperliquidAPI,
    RateLimiter,
    CircuitBreaker,
    CircuitState,
    TTLCache,
    OrderTracker,
    TrackedOrder,
    ConnectionHealthMonitor,
    HealthStatus,
    HealthCheckResult,
)

__all__ = [
    'HyperliquidAPI',
    'RateLimiter',
    'CircuitBreaker',
    'CircuitState',
    'TTLCache',
    'OrderTracker',
    'TrackedOrder',
    'ConnectionHealthMonitor',
    'HealthStatus',
    'HealthCheckResult',
]
