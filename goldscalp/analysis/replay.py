"""Historical replay (PHASE2_ARCHITECTURE.md section 7).

A replay session holds a dataset loaded once (base candles + native higher-timeframe history that ENDED
before the base candles start) and a cursor. The cursor is the replay timestamp: the close time of the
last revealed step candle. At every cursor position the analysis sees ONLY:
- base candles that closed at or before the cursor, and higher timeframes REBUILT from them
  (a partially elapsed higher-timeframe candle is marked forming and excluded from confirmed analysis);
- native higher-timeframe candles that ended before the first base candle.
Nothing after the cursor is passed to any engine. The browser drives the clock (play/pause/speed);
the server only moves the cursor when asked.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from goldscalp.analysis.intelligence import FRAME_LIMITS
from goldscalp.core.clock import ensure_aware, iso
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.data.resample import resample

MAX_SESSIONS = 8
MAX_STEP = 500


class ReplayError(ValueError):
    pass


@dataclass
class ReplayDataset:
    series_key: str
    source_id: str | None
    base_tf: Timeframe
    base: pd.DataFrame                                   # closed base candles, oldest first
    history: dict[str, pd.DataFrame] = field(default_factory=dict)   # native bars ended before base[0]
    note: str = ""                                       # how the base timeframe was chosen


def frames_at(ds: ReplayDataset, cutoff: datetime) -> dict[str, pd.DataFrame]:
    """Every timeframe as it was knowable at ``cutoff``. The ONLY place replay data reaches analysis."""
    cut = pd.Timestamp(ensure_aware(cutoff))
    base = ds.base
    visible = base[(base.index + pd.Timedelta(seconds=ds.base_tf.seconds)) <= cut]
    first = base.index[0] if len(base) else cut
    out: dict[str, pd.DataFrame] = {}
    for tf in ALL_TIMEFRAMES:
        if tf.seconds < ds.base_tf.seconds:
            continue
        if tf is ds.base_tf:
            derived = visible.copy()
        else:
            derived = resample(visible, ds.base_tf, tf, as_of=cut.to_pydatetime())
            derived = derived[derived.index >= first]         # drop a bar that straddles the dataset start
        hist = ds.history.get(tf.value)
        if hist is not None and len(hist):
            hist = hist[(hist.index + pd.Timedelta(seconds=tf.seconds)) <= min(first, cut)]
            frame = pd.concat([hist, derived]) if len(derived) else hist.copy()
        else:
            frame = derived
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        out[tf.value] = frame.tail(FRAME_LIMITS[tf.value])
    return out


@dataclass
class ReplaySession:
    id: str
    dataset: ReplayDataset
    step_tf: Timeframe
    start: datetime
    end: datetime
    steps: list[pd.Timestamp]                            # close times of the step candles in [start, end]
    position: int = 0                                    # step candles revealed
    display_swing_strength: int | None = None            # chart overlays only (the signal uses the config)
    created_at: datetime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def cursor(self) -> datetime:
        return self.start if self.position == 0 else self.steps[self.position - 1].to_pydatetime()

    @property
    def finished(self) -> bool:
        return self.position >= len(self.steps)

    def step(self, n: int = 1) -> datetime:
        if n < 1 or n > MAX_STEP:
            raise ReplayError(f"step must be between 1 and {MAX_STEP} candles")
        self.position = min(len(self.steps), self.position + n)
        return self.cursor

    def seek(self, position: int) -> datetime:
        if position < 0 or position > len(self.steps):
            raise ReplayError(f"position must be between 0 and {len(self.steps)}")
        self.position = position
        return self.cursor

    def frames(self) -> dict[str, pd.DataFrame]:
        return frames_at(self.dataset, self.cursor)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "series": self.dataset.series_key, "source": self.dataset.source_id,
                "base_tf": self.dataset.base_tf.value, "step_tf": self.step_tf.value,
                "start": iso(self.start), "end": iso(self.end), "cursor": iso(ensure_aware(self.cursor)),
                "position": self.position, "total_steps": len(self.steps), "finished": self.finished,
                "base_candles": int(len(self.dataset.base)),
                "history": {k: int(len(v)) for k, v in self.dataset.history.items()},
                "first_base_candle": iso(self.dataset.base.index[0].to_pydatetime()) if len(self.dataset.base) else None,
                "created_at": iso(self.created_at) if self.created_at else None}


def build_session(ds: ReplayDataset, step_tf: Timeframe, start: datetime, end: datetime,
                  created_at: datetime) -> ReplaySession:
    start, end = ensure_aware(start), ensure_aware(end)
    if end <= start:
        raise ReplayError("the replay end must be after its start")
    if step_tf.seconds < ds.base_tf.seconds:
        raise ReplayError(f"this dataset's finest timeframe is {ds.base_tf.value}; cannot step in {step_tf.value}")
    if ds.base.empty:
        raise ReplayError("the dataset has no closed candles in that range")
    window = ds.base[(ds.base.index >= pd.Timestamp(start)) & (ds.base.index < pd.Timestamp(end))]
    if window.empty:
        raise ReplayError("the dataset has no candles between the chosen start and end")
    bars = resample(window, ds.base_tf, step_tf) if step_tf is not ds.base_tf else window
    steps = [ts + pd.Timedelta(seconds=step_tf.seconds) for ts in bars.index
             if ts + pd.Timedelta(seconds=step_tf.seconds) <= pd.Timestamp(end)]
    if not steps:
        raise ReplayError("no complete step candles between the chosen start and end")
    return ReplaySession(id="rp_" + secrets.token_hex(8), dataset=ds, step_tf=step_tf, start=start, end=end,
                         steps=steps, created_at=created_at)


class ReplayStore:
    """In-memory replay sessions (bounded; the oldest is dropped)."""

    def __init__(self, limit: int = MAX_SESSIONS) -> None:
        self.limit = limit
        self._items: dict[str, ReplaySession] = {}
        self._lock = threading.Lock()

    def add(self, session: ReplaySession) -> None:
        with self._lock:
            while len(self._items) >= self.limit:
                self._items.pop(next(iter(self._items)))
            self._items[session.id] = session

    def get(self, replay_id: str) -> ReplaySession | None:
        with self._lock:
            return self._items.get(replay_id)

    def remove(self, replay_id: str) -> bool:
        with self._lock:
            return self._items.pop(replay_id, None) is not None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.view() for s in self._items.values()]
