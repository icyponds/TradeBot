"""Tests for the exit-monitor liveness/recovery hardening.

Incident (live failure 2026-06-11 13:12): a stop-loss trigger fired correctly
for CRV on the WebSocket callback thread and called close_position INLINE while
the API held its _data_lock. That deadlocked against the monitor thread
(holding _position_monitor_lock, needing _data_lock back). Both threads froze
silently; the exit loop never ran again for ~43h, so NO stop-loss was evaluated
for any position. XPL then ran 41% through its 5% stop.

Three guards, all covered here:
  #3 off-thread closes  — _check_triggers_realtime must ENQUEUE, never close inline
  #1 heartbeat watchdog — a hung-but-alive monitor is detected and respawned
  #2 bounded acquire    — _exit_monitor_active() is heartbeat-aware, not is_alive()-only
"""

import time
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.strategies.strategy_manager import StrategyManager


def make_manager(interval=10):
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.logger = logging.getLogger('test_watchdog')
    mgr.config = {'trading': {'position_monitoring_interval': interval},
                  'risk_management': {}}
    mgr.is_running = True
    # Monitor/heartbeat state (mirrors __init__)
    mgr._exit_monitor_thread = None
    mgr._position_monitor_lock = threading.RLock()
    mgr._monitor_heartbeat_ts = 0.0
    mgr._monitor_generation = 0
    mgr._monitor_watchdog_thread = None
    mgr._monitor_lock_timeout = 0.5
    mgr._monitor_staleness_floor = 1.0
    mgr._monitor_watchdog_interval = 0.2
    # Trigger executor state
    import queue
    mgr._trigger_close_queue = queue.Queue()
    mgr._trigger_close_inflight = set()
    mgr._trigger_close_inflight_lock = threading.Lock()
    mgr._trigger_executor_thread = None
    # Position + engine stubs. positions/multi_leg_positions are read-only
    # properties delegating to execution_engine, so seed them there.
    mgr.execution_engine = SimpleNamespace(
        close_position=MagicMock(), positions={}, multi_leg_positions={})
    return mgr


# --- #3: realtime triggers must not close inline (the deadlock regression) ---

class TestOffThreadTriggers:
    def _short_pos(self, stop):
        return SimpleNamespace(side='short', stop_loss=stop, take_profit=None)

    def test_breach_enqueues_and_does_not_close_inline(self):
        mgr = make_manager()
        mgr.execution_engine.positions = {'XPL': self._short_pos(0.0653)}
        mgr._check_triggers_realtime('XPL', 0.088)   # well above the short stop
        # Must NOT have closed on the WS/caller thread (the deadlock cause).
        mgr.execution_engine.close_position.assert_not_called()
        # Must have queued exactly one close request.
        assert mgr._trigger_close_queue.qsize() == 1
        assert mgr._trigger_close_queue.get_nowait() == ('XPL', 'stop_loss_realtime')

    def test_no_breach_does_not_enqueue(self):
        mgr = make_manager()
        mgr.execution_engine.positions = {'XPL': self._short_pos(0.0653)}
        mgr._check_triggers_realtime('XPL', 0.060)   # below the short stop
        assert mgr._trigger_close_queue.qsize() == 0

    def test_repeated_breaches_dedup_to_one_pending(self):
        mgr = make_manager()
        mgr.execution_engine.positions = {'XPL': self._short_pos(0.0653)}
        for _ in range(5):
            mgr._check_triggers_realtime('XPL', 0.09)
        assert mgr._trigger_close_queue.qsize() == 1   # de-duped while in flight

    def test_executor_drains_queue_and_closes(self):
        mgr = make_manager()
        t = threading.Thread(target=mgr._run_trigger_executor_loop, daemon=True)
        t.start()
        mgr._enqueue_trigger_close('XPL', 'stop_loss_realtime')
        # Wait for the executor to process it.
        deadline = time.time() + 3
        while time.time() < deadline and not mgr.execution_engine.close_position.called:
            time.sleep(0.02)
        mgr.is_running = False
        t.join(timeout=2)
        mgr.execution_engine.close_position.assert_called_once_with(
            symbol='XPL', reason='stop_loss_realtime')
        # In-flight cleared so a later breach can re-queue.
        assert 'XPL' not in mgr._trigger_close_inflight

    def test_inflight_cleared_allows_requeue_after_drain(self):
        mgr = make_manager()
        t = threading.Thread(target=mgr._run_trigger_executor_loop, daemon=True)
        t.start()

        def wait_for(predicate, timeout=3):
            deadline = time.time() + timeout
            while time.time() < deadline and not predicate():
                time.sleep(0.02)
            return predicate()

        mgr._enqueue_trigger_close('XPL', 'stop_loss_realtime')
        # First close drains and clears in-flight before we re-queue.
        assert wait_for(lambda: mgr.execution_engine.close_position.call_count == 1)
        assert wait_for(lambda: 'XPL' not in mgr._trigger_close_inflight)

        mgr._enqueue_trigger_close('XPL', 'stop_loss_realtime')
        assert wait_for(lambda: mgr.execution_engine.close_position.call_count == 2)

        mgr.is_running = False
        t.join(timeout=2)


# --- #1/#2: heartbeat-aware liveness ---

class TestExitMonitorLiveness:
    def test_fresh_heartbeat_is_active(self):
        mgr = make_manager()
        mgr._exit_monitor_thread = SimpleNamespace(is_alive=lambda: True)
        mgr._monitor_heartbeat_ts = time.time()
        assert mgr._exit_monitor_active() is True

    def test_stale_heartbeat_is_not_active(self):
        # The 2026-06-11 bug: thread alive but wedged -> old is_alive() check
        # returned True and the fallback never ran. Now it must read inactive.
        mgr = make_manager()
        mgr._exit_monitor_thread = SimpleNamespace(is_alive=lambda: True)
        mgr._monitor_heartbeat_ts = time.time() - 999
        assert mgr._exit_monitor_active() is False

    def test_dead_thread_is_not_active(self):
        mgr = make_manager()
        mgr._exit_monitor_thread = SimpleNamespace(is_alive=lambda: False)
        mgr._monitor_heartbeat_ts = time.time()
        assert mgr._exit_monitor_active() is False

    def test_no_thread_is_not_active(self):
        mgr = make_manager()
        mgr._exit_monitor_thread = None
        assert mgr._exit_monitor_active() is False

    def test_staleness_threshold_respects_floor(self):
        mgr = make_manager(interval=1)
        mgr._monitor_staleness_floor = 30.0
        assert mgr._monitor_staleness_threshold() == 30.0


# --- #1: watchdog respawns a wedged monitor ---

class TestWatchdogRespawn:
    def test_watchdog_respawns_hung_monitor(self):
        mgr = make_manager(interval=1)

        # A "hung" exit monitor: alive, never updates the heartbeat.
        def _wedged():
            while mgr.is_running:
                time.sleep(0.05)
        hung = threading.Thread(target=_wedged, daemon=True)
        hung.start()
        mgr._exit_monitor_thread = hung
        mgr._monitor_heartbeat_ts = time.time() - 100   # already stale
        gen_before = mgr._monitor_generation

        wd = threading.Thread(target=mgr._run_monitor_watchdog, daemon=True)
        wd.start()
        # Watchdog interval 0.2s, threshold floor 1.0s -> should fire quickly.
        deadline = time.time() + 3
        while time.time() < deadline and mgr._monitor_generation == gen_before:
            time.sleep(0.05)

        assert mgr._monitor_generation == gen_before + 1   # superseded
        # A fresh exit-monitor thread was spawned and is running.
        assert mgr._exit_monitor_thread is not None
        assert mgr._exit_monitor_thread.name == f"exit-monitor-{gen_before + 1}"

        mgr.is_running = False
        wd.join(timeout=2)

    def test_watchdog_leaves_healthy_monitor_alone(self):
        mgr = make_manager(interval=1)
        mgr._monitor_staleness_floor = 1.0

        # A healthy monitor: alive and refreshes the heartbeat continuously.
        def _healthy():
            while mgr.is_running:
                mgr._monitor_heartbeat_ts = time.time()
                time.sleep(0.05)
        good = threading.Thread(target=_healthy, daemon=True)
        good.start()
        mgr._exit_monitor_thread = good
        gen_before = mgr._monitor_generation

        wd = threading.Thread(target=mgr._run_monitor_watchdog, daemon=True)
        wd.start()
        time.sleep(1.0)   # several watchdog checks
        mgr.is_running = False
        wd.join(timeout=2)
        good.join(timeout=2)

        assert mgr._monitor_generation == gen_before   # never respawned
