"""Chart timeframes. Bars are labelled by their OPEN time in UTC."""
from __future__ import annotations

from enum import StrEnum


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"

    @property
    def seconds(self) -> int:
        return _SECONDS[self]

    @property
    def minutes(self) -> int:
        return _SECONDS[self] // 60

    def floor(self, epoch_seconds: int) -> int:
        """The open time of the bar containing ``epoch_seconds``.

        Every supported timeframe divides one hour, so UTC alignment equals
        alignment in any whole-hour-offset timezone.
        """
        return epoch_seconds - (epoch_seconds % self.seconds)

    def is_aligned(self, epoch_seconds: int) -> bool:
        return epoch_seconds % self.seconds == 0

    @classmethod
    def parse(cls, text: str) -> Timeframe:
        """Strict parse: M1/M5/M15/H1 or the aliases 1m/5m/15m/1h. Anything else raises."""
        key = str(text).strip()
        if key in cls.__members__:
            return cls[key]
        if key.lower() in _ALIASES:
            return _ALIASES[key.lower()]
        raise ValueError(f"unknown timeframe {text!r}; expected one of M1, M5, M15, H1")


_SECONDS = {Timeframe.M1: 60, Timeframe.M5: 300, Timeframe.M15: 900, Timeframe.H1: 3600}
_ALIASES = {"1m": Timeframe.M1, "5m": Timeframe.M5, "15m": Timeframe.M15, "1h": Timeframe.H1,
            "60m": Timeframe.H1}
ALL_TIMEFRAMES: tuple[Timeframe, ...] = (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1)
