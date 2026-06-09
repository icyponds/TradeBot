"""
Tests for the API-layer hardening:
- weight-aware token bucket rate limiter
- per-attempt token acquisition in _rate_limited_call
- typed retryable-error classification
- default HTTP timeout injection into the SDK session
- bulk metaAndAssetCtxs population in get_market_data (N+1 fix)
- OHLCV DataFrame build cache
"""

import pytest
from unittest.mock import MagicMock

# Imported at module level: some tests in the suite stub out sys.modules['hyperliquid'],
# which would break this import if done lazily inside a test body.
from hyperliquid.utils.error import ClientError, ServerError

from src.api.hyperliquid_api import RateLimiter


@pytest.fixture
def api_client(shared_api_client):
    """Use shared module-scoped client, reset mocks before each test."""
    shared_api_client.exchange.reset_mock()
    shared_api_client.info.reset_mock()
    shared_api_client.cache.clear()
    return shared_api_client


class TestWeightedRateLimiter:

    def test_acquire_consumes_weight_units(self):
        # Slow refill (1/s) so the bucket does not recover within the test
        rl = RateLimiter(calls_per_second=1.0, burst_size=40)

        assert rl.acquire(timeout=0.1, weight=20) is True
        assert rl.acquire(timeout=0.1, weight=20) is True
        # Bucket empty: 20 more units would need ~20s of refill
        assert rl.acquire(timeout=0.1, weight=20) is False

    def test_cheap_calls_fit_more_often(self):
        rl = RateLimiter(calls_per_second=1.0, burst_size=40)

        for _ in range(20):
            assert rl.acquire(timeout=0.1, weight=2) is True
        assert rl.acquire(timeout=0.1, weight=2) is False

    def test_weight_clamped_to_burst_capacity(self):
        """A weight above the bucket capacity must not deadlock."""
        rl = RateLimiter(calls_per_second=1000.0, burst_size=10)

        assert rl.acquire(timeout=1.0, weight=50) is True


class TestRateLimitedCall:

    def test_token_acquired_on_every_attempt(self, api_client):
        """
        Regression test: retries used to bypass the token bucket entirely -
        retry traffic must also be throttled.
        """
        original_limiter = api_client.rate_limiter
        try:
            api_client.rate_limiter = MagicMock()
            api_client.rate_limiter.acquire.return_value = True

            attempts = {'n': 0}

            def flaky():
                attempts['n'] += 1
                if attempts['n'] < 3:
                    raise Exception("429 Too Many Requests")
                return "ok"

            result = api_client._rate_limited_call(flaky, weight=20)

            assert result == "ok"
            assert attempts['n'] == 3
            assert api_client.rate_limiter.acquire.call_count == 3
            api_client.rate_limiter.acquire.assert_called_with(timeout=30.0, weight=20)
        finally:
            api_client.rate_limiter = original_limiter

    def test_weight_kwarg_not_forwarded_to_wrapped_func(self, api_client):
        def func(**kwargs):
            return kwargs

        assert api_client._rate_limited_call(func, weight=5) == {}


class TestRetryableErrorClassification:

    def test_typed_sdk_errors(self, api_client):
        # 429 is the only retryable client error
        assert api_client._is_retryable_error(
            ClientError(429, None, "rate limited", None)) is True
        assert api_client._is_retryable_error(
            ClientError(422, None, "bad order", None)) is False

        # Server errors are retryable except the (500, 'null') symbol-not-found case
        assert api_client._is_retryable_error(ServerError(503, "unavailable")) is True
        assert api_client._is_retryable_error(ServerError(500, "null")) is False

    def test_requests_network_errors_retryable(self, api_client):
        import requests

        assert api_client._is_retryable_error(
            requests.exceptions.ConnectTimeout("x")) is True
        assert api_client._is_retryable_error(
            requests.exceptions.ConnectionError("x")) is True

    def test_string_fallback(self, api_client):
        assert api_client._is_retryable_error(
            Exception("HTTP 429 too many requests")) is True
        assert api_client._is_retryable_error(
            Exception("Read timed out")) is True
        # HTTP-header-like text must NOT be classified as a connection error
        assert api_client._is_retryable_error(
            KeyError("Connection")) is False
        assert api_client._is_retryable_error(
            Exception("invalid signature")) is False


class TestHttpTimeoutInjection:

    class FakeSession:
        def __init__(self):
            self.kwargs = None

        def request(self, *args, **kwargs):
            self.kwargs = kwargs
            return "resp"

    def _client_with_session(self):
        client = MagicMock(spec=[])
        client.session = self.FakeSession()
        return client

    def test_default_timeout_injected(self, api_client):
        client = self._client_with_session()
        api_client._apply_http_timeout(client)

        client.session.request("POST", "http://x")

        # (connect, read) - read comes from config api.timeout (default 30)
        assert client.session.kwargs['timeout'] == (5, 30.0)

    def test_explicit_timeout_preserved(self, api_client):
        client = self._client_with_session()
        api_client._apply_http_timeout(client)

        client.session.request("POST", "http://x", timeout=99)

        assert client.session.kwargs['timeout'] == 99

    def test_no_double_wrap(self, api_client):
        client = self._client_with_session()
        api_client._apply_http_timeout(client)
        wrapped_once = client.session.request

        api_client._apply_http_timeout(client)

        assert client.session.request is wrapped_once

    def test_missing_session_is_harmless(self, api_client):
        api_client._apply_http_timeout(MagicMock(spec=[]))  # no .session attribute


class TestBulkMarketData:

    def _setup_universe(self, api_client):
        universe = [
            {'name': 'BTC', 'maxLeverage': 50, 'szDecimals': 3},
            {'name': 'ETH', 'maxLeverage': 50, 'szDecimals': 2},
        ]
        ctxs = [
            {'markPx': '50000', 'dayNtlVlm': '1000', 'openInterest': '10', 'funding': '0'},
            {'markPx': '3000', 'dayNtlVlm': '2000', 'openInterest': '20', 'funding': '0'},
        ]
        api_client.info.meta_and_asset_ctxs.return_value = [{'universe': universe}, ctxs]

    def test_one_bulk_fetch_serves_all_symbols(self, api_client):
        """
        Regression test for the N+1: looking up N symbols must cost ONE
        metaAndAssetCtxs call, not one full-universe fetch per symbol.
        """
        self._setup_universe(api_client)

        md_btc = api_client.get_market_data('BTC')
        md_eth = api_client.get_market_data('ETH')

        assert md_btc['current_price'] == 50000.0
        assert md_eth['current_price'] == 3000.0
        assert api_client.info.meta_and_asset_ctxs.call_count == 1

    def test_unknown_symbol_returns_none(self, api_client):
        self._setup_universe(api_client)
        api_client.hip3_enabled = False
        try:
            assert api_client.get_market_data('DOGE') is None
        finally:
            api_client.hip3_enabled = True


class TestOhlcvDataFrameCache:

    def _bars(self, n=30):
        return [{'time': 1700000000 + i * 60, 'open': 1.0, 'high': 2.0,
                 'low': 0.5, 'close': 1.5, 'volume': 10.0} for i in range(n)]

    def test_dataframe_reused_when_bars_unchanged(self, api_client):
        bars = self._bars()

        df1 = api_client._bars_to_df('TESTX', '1m', bars)
        df2 = api_client._bars_to_df('TESTX', '1m', bars)

        assert df1 is df2

    def test_dataframe_rebuilt_on_new_data(self, api_client):
        bars = self._bars()
        df1 = api_client._bars_to_df('TESTY', '1m', bars)

        # New tick mutates the last bar's close
        bars2 = [dict(b) for b in bars]
        bars2[-1]['close'] = 9.9
        df2 = api_client._bars_to_df('TESTY', '1m', bars2)

        assert df2 is not df1
        assert df2['close'].iloc[-1] == 9.9

        # New bar appended
        bars3 = bars2 + [{'time': 1700000000 + 30 * 60, 'open': 9.9, 'high': 9.9,
                          'low': 9.9, 'close': 9.9, 'volume': 1.0}]
        df3 = api_client._bars_to_df('TESTY', '1m', bars3)

        assert df3 is not df2
        assert len(df3) == len(df2) + 1
