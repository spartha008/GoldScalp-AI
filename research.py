"""Start the GoldScalp research service (Phase 5 monthly backtests) from any working directory:  python research.py

A separate process from the main app (python run.py): it reads the application database READ-ONLY, writes only to
var/monthly/monthly.sqlite3, never connects to MT5 and runs at below-normal priority."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from goldscalp.monthly.standalone import main  # noqa: E402

if __name__ == "__main__":
    main()
