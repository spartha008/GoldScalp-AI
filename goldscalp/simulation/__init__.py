"""Lot-based paper-trading simulator.

Independent of any broker: it imports no provider and no broker SDK. Fills are
computed from a market quote plus the configured cost model. Every position is
SIMULATED. There is no order_send and no live execution path.
"""
