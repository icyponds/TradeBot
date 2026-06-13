"""Helpers backing the explicit close-all-positions command.

Closing the book is OPT-IN. The bot KEEPS open positions on shutdown by
default (settings: system.close_on_shutdown = False) so a routine restart for
a code change does not churn the book (fees + slippage + lost entries). This
module backs scripts/close_all_positions.py, the manual command used to
flatten the book on purpose.
"""

from typing import Any, Dict, Optional


# place_order() status values that mean the order was accepted by the exchange.
# It returns None on failure, otherwise a dict whose top-level 'status' is one
# of these. It NEVER returns 'ok' at the top level (that key lives nested under
# 'raw_response'); checking for 'ok' wrongly reports every fill as a failure.
_ACCEPTED_STATUSES = ('filled', 'open', 'pending')


def plan_close_order(position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the reduce-only market order that flattens ``position``.

    A long (size > 0) is closed with a 'sell'; a short (size < 0) with a 'buy'.
    Returns ``None`` when there is nothing to close (zero or missing size), so
    callers can skip dust/closed rows without special-casing.
    """
    try:
        size = float(position.get('size', 0) or 0)
    except (TypeError, ValueError):
        return None
    if size == 0:
        return None
    return {
        'symbol': position.get('symbol'),
        'side': 'sell' if size > 0 else 'buy',
        'size': abs(size),
        'reduce_only': True,
        'order_type': 'market',
    }


def order_succeeded(result: Any) -> bool:
    """True if a ``place_order()`` result represents an accepted/filled order.

    ``place_order`` returns ``None`` on any failure, otherwise a dict whose
    top-level ``status`` is one of 'filled', 'open', or 'pending'. The legacy
    check ``result.get('status') == 'ok'`` is always False against that
    contract and so reported successful closes as failures.
    """
    if not isinstance(result, dict):
        return False
    return result.get('status') in _ACCEPTED_STATUSES
