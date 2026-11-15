"""Start GoldScalp AI from any working directory:  python run.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from goldscalp.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
