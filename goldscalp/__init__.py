"""GoldScalp AI - educational XAUUSD analysis and lot-based PAPER trading.

Safety contract (enforced by tests/test_safety.py):
- No real-money trading, no broker order execution, no order_send, no broker
  login credentials, no automatic live trading.
- Every simulated position is labelled as simulated.
"""

__version__ = "0.1.0"
