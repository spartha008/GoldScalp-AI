"""Analysis engine. Pure functions over canonical candle frames.

Never imports goldscalp.data.providers (tests/test_boundaries.py). Results are
HISTORICAL observations, versioned as name@version, and use CLOSED bars only.
"""
