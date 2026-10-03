"""Retention-aware fetching and backfill in scripts/refresh_market_data.py.

Hyperliquid serves only the most recent ~5000 candles per series. Research
round 8 (2026-10-03): the incremental refresh resumed 15m data from the last
stored bar (2026-07-03), which by then sat beyond retention — the API returned
[] and the loop stopped, so NO new 15m data was ever fetched again.
"""
import importlib.util
import os

import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location(
    "refresh_market_data",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "refresh_market_data.py"),
)
rmd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rmd)

H = 3600 * 1000
NOW_MS = 1_790_000_000_000 - (1_790_000_000_000 % (4 * H))  # 4h-aligned "now"


class FakeDB:
    def __init__(self):
        self.candles = {}
        self.funding = {}

    def get_market_data(self, symbol, timeframe):
        return self.candles.get((symbol, timeframe), pd.DataFrame())

    def insert_market_data(self, df, symbol, timeframe):
        cur = self.candles.get((symbol, timeframe))
        merged = df if cur is None or cur.empty else pd.concat([cur, df])
        self.candles[(symbol, timeframe)] = merged[~merged.index.duplicated()].sort_index()

    def get_funding_rates(self, symbol):
        return self.funding.get(symbol, pd.DataFrame())

    def insert_funding_rates(self, df, symbol):
        cur = self.funding.get(symbol)
        merged = df if cur is None or cur.empty else pd.concat([cur, df])
        self.funding[symbol] = merged[~merged.index.duplicated()].sort_index()


def _bars(start_ms, end_ms, interval_ms):
    out = []
    t = start_ms
    while t <= end_ms:
        out.append({'t': t, 'o': '1', 'h': '1', 'l': '1', 'c': '1', 'v': '10'})
        t += interval_ms
    return out


def _stored(start_ms, n, interval_ms):
    idx = pd.to_datetime([start_ms + i * interval_ms for i in range(n)], unit='ms')
    return pd.DataFrame({'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1.0}, index=idx)


@pytest.fixture
def fake_api(monkeypatch):
    """candleSnapshot honours retention (empty before the horizon) and the
    5000-bar cap; records every request."""
    calls = []

    def _post(payload, retries=4):
        calls.append(payload)
        if payload['type'] == 'candleSnapshot':
            req = payload['req']
            interval_ms = rmd.TIMEFRAMES[req['interval']] * 1000
            horizon = NOW_MS - rmd.HISTORY_DEPTH_BARS * interval_ms
            if req['startTime'] < horizon:
                return []  # real API: whole request beyond retention -> []
            end = min(req['endTime'], NOW_MS - interval_ms)
            return _bars(req['startTime'], end, interval_ms)[:5000]
        if payload['type'] == 'fundingHistory':
            start = payload['startTime']
            end = payload.get('endTime', NOW_MS)
            times = list(range(start - start % H + (H if start % H else 0), end + 1, H))[:500]
            return [{'time': t, 'fundingRate': '0.0000125'} for t in times]
        raise AssertionError(payload)

    monkeypatch.setattr(rmd, '_post', _post)
    monkeypatch.setattr(rmd.time, 'time', lambda: NOW_MS / 1000)
    return calls


def test_forward_refresh_jumps_over_gap_beyond_retention(fake_api):
    """Pre-fix: first request started at the stale last bar, got [], stopped (0 rows)."""
    db = FakeDB()
    i15 = 15 * 60 * 1000
    stale_start = NOW_MS - 6000 * i15  # last stored bar is beyond 15m retention
    db.candles[('BTC', '15m')] = _stored(stale_start, 1, i15)

    n = rmd.refresh_candles(db, 'BTC', '15m', genesis_ms=0)

    assert n > 4000
    first_req = fake_api[0]['req']
    assert first_req['startTime'] == rmd.retention_floor_ms(900, NOW_MS)
    stored = db.candles[('BTC', '15m')]
    assert stored.index.max() == pd.to_datetime(NOW_MS - i15, unit='ms')  # caught up to last closed bar


def test_forward_refresh_within_retention_starts_after_last_bar(fake_api):
    db = FakeDB()
    i4 = 4 * H
    last = NOW_MS - 10 * i4
    db.candles[('ETH', '4h')] = _stored(last, 1, i4)

    n = rmd.refresh_candles(db, 'ETH', '4h', genesis_ms=0)

    assert fake_api[0]['req']['startTime'] == last + i4
    assert n == 9  # bars last+1 .. now-1 (the forming bar is excluded)


def test_backfill_fills_only_before_earliest_stored_bar(fake_api):
    db = FakeDB()
    i4 = 4 * H
    earliest = NOW_MS - 2000 * i4
    db.candles[('SOL', '4h')] = _stored(earliest, 5, i4)
    genesis = NOW_MS - 3000 * i4  # inside 4h retention

    n = rmd.backfill_candles(db, 'SOL', '4h', genesis_ms=genesis)

    assert n == 1000
    stored = db.candles[('SOL', '4h')]
    assert stored.index.min() == pd.to_datetime(genesis, unit='ms')
    assert len(stored) == 1005
    assert all(r['req']['endTime'] < earliest for r in fake_api)


def test_backfill_clamps_genesis_to_retention(fake_api):
    db = FakeDB()
    i4 = 4 * H
    earliest = NOW_MS - 1000 * i4
    db.candles[('SOL', '4h')] = _stored(earliest, 1, i4)

    n = rmd.backfill_candles(db, 'SOL', '4h', genesis_ms=NOW_MS - 9000 * i4)

    assert n > 0
    assert fake_api[0]['req']['startTime'] == rmd.retention_floor_ms(14400, NOW_MS)


def test_backfill_range_entirely_beyond_retention_is_skipped(fake_api):
    db = FakeDB()
    i1 = H
    earliest = NOW_MS - 8000 * i1  # stored 1h history already older than retention
    db.candles[('BTC', '1h')] = _stored(earliest, 3, i1)

    n = rmd.backfill_candles(db, 'BTC', '1h', genesis_ms=earliest - 500 * i1)

    assert n == 0
    assert fake_api == []


def test_backfill_noop_when_history_reaches_genesis(fake_api):
    db = FakeDB()
    i4 = 4 * H
    db.candles[('BTC', '4h')] = _stored(NOW_MS - 100 * i4, 3, i4)
    # genesis is AFTER the earliest stored bar: nothing to backfill
    assert rmd.backfill_candles(db, 'BTC', '4h', genesis_ms=NOW_MS - 50 * i4) == 0
    assert fake_api == []


def test_backfill_empty_series_fetches_from_genesis(fake_api):
    db = FakeDB()
    i4 = 4 * H
    n = rmd.backfill_candles(db, 'NEW', '4h', genesis_ms=NOW_MS - 50 * i4)
    assert n == 50  # through the bar that closed exactly at now
    assert fake_api[0]['req']['startTime'] == NOW_MS - 50 * i4


def test_backfill_funding_paginates_up_to_earliest_record(fake_api):
    db = FakeDB()
    earliest = NOW_MS - 100 * H
    db.funding['BTC'] = pd.DataFrame({'funding': [0.0001]}, index=pd.to_datetime([earliest], unit='ms'))
    genesis = earliest - 1200 * H  # needs 3 pages of 500

    n = rmd.backfill_funding(db, 'BTC', genesis)

    assert n == 1200
    stored = db.funding['BTC']
    assert stored.index.min() == pd.to_datetime(genesis, unit='ms')
    assert len(stored) == 1201
    assert sum(1 for c in fake_api if c['type'] == 'fundingHistory') == 3


def test_native_perp_names_delisted_toggle(monkeypatch):
    meta = {'universe': [{'name': 'BTC'}, {'name': 'MATIC', 'isDelisted': True}]}
    monkeypatch.setattr(rmd, '_post', lambda payload, retries=4: meta)
    assert rmd.get_native_perp_names() == {'BTC'}
    assert rmd.get_native_perp_names(include_delisted=True) == {'BTC', 'MATIC'}
