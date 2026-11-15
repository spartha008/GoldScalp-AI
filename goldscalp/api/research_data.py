"""Research-only candle access for the main application: Strategy Lab (backtests, comparisons, walk-forward),
Performance and improvement studies.

These are the same loaders raw monitoring uses, plus the forward-data guard (goldscalp.core.forward):
  - the requested window is checked before anything is read;
  - every frame that is read is checked afterwards, including the history look-back and the outcome extension
    after the window.
Raw monitoring (dashboard, charts, replay, live monitor) keeps calling ``services.load_dataset`` directly and is
unaffected. Research routers must use these functions only; a test enforces that.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from goldscalp.api.services import load_dataset, load_outcome_candles
from goldscalp.core import forward
from goldscalp.core.timeframes import Timeframe
from goldscalp.storage.db import Database


def research_dataset(db: Database, row: Any, step_tf: Timeframe, start: datetime, end: datetime, lookback_days: int,
                     *, what: str) -> Any:
    forward.check_range(start, end, what)
    ds = load_dataset(db, row, step_tf, start, end, lookback_days)
    forward.check_dataset(ds, what)
    return ds


def research_outcome_candles(db: Database, row: Any, base_tf: Timeframe, start: datetime, until: datetime, *,
                             what: str) -> pd.DataFrame:
    candles = load_outcome_candles(db, row, base_tf, start, until)
    forward.check_frame(candles, f"{what}: the candles after the window that manage or score the last positions")
    return candles
