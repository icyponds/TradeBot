"""Tests for robust dashboard port handling.

The dashboard used to bind a fixed port (5050) with app.run(); a busy port
(leftover instance, another process, or a prior test's server) crashed the
server thread and, in the suite, hung the whole run. run_dashboard now finds a
free port (or cleanly skips the dashboard) and guards the server thread.
"""

import socket
from unittest.mock import MagicMock, patch

import pytest

from src.dashboard import app as dashboard_app
from src.dashboard.app import _find_free_port, run_dashboard


@pytest.fixture
def occupied_port():
    """Bind and listen on an ephemeral port, yield it, then release."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


class TestFindFreePort:
    def test_returns_preferred_when_free(self):
        # Grab a free port, release it, then ask for it — should be returned.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 0))
        free = s.getsockname()[1]
        s.close()
        assert _find_free_port(free, host='127.0.0.1') == free

    def test_skips_occupied_port(self, occupied_port):
        chosen = _find_free_port(occupied_port, host='127.0.0.1')
        assert chosen is not None
        assert chosen != occupied_port
        assert chosen > occupied_port

    def test_returns_none_when_exhausted(self, occupied_port):
        # Only one candidate (the occupied port) -> nothing free.
        assert _find_free_port(occupied_port, host='127.0.0.1', max_tries=1) is None


class TestRunDashboardPortHandling:
    def test_uses_free_port_and_starts_thread(self):
        # Patch Thread so no real server starts; assert it launches on the
        # port the finder picked.
        with patch.object(dashboard_app, '_find_free_port', return_value=5099), \
             patch.object(dashboard_app, 'create_dashboard_app', return_value=MagicMock()), \
             patch.object(dashboard_app, 'Thread') as MockThread:
            result = run_dashboard(strategy_manager=MagicMock(), port=5050)
        MockThread.assert_called_once()
        # Daemon so it never blocks process exit.
        assert MockThread.call_args.kwargs.get('daemon') is True
        MockThread.return_value.start.assert_called_once()
        assert result is MockThread.return_value

    def test_returns_none_when_no_free_port(self):
        # No real server, no hang: when nothing is free, skip the dashboard.
        with patch.object(dashboard_app, '_find_free_port', return_value=None), \
             patch.object(dashboard_app, 'create_dashboard_app', return_value=MagicMock()), \
             patch.object(dashboard_app, 'Thread') as MockThread:
            result = run_dashboard(strategy_manager=MagicMock(), port=5050)
        assert result is None
        MockThread.assert_not_called()
