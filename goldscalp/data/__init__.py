"""Market data: providers (the only code that talks to a source), normalisation,
validation, gap detection, resampling and ingestion into SQLite.

analysis/ and simulation/ never import data.providers (tests/test_boundaries.py).
"""
