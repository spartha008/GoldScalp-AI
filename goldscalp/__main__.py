"""Start GoldScalp AI:  python -m goldscalp"""
from __future__ import annotations

import logging

import uvicorn

from goldscalp.api.app import create_app
from goldscalp.core.config import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    app = create_app(settings)
    print(f"GoldScalp AI - educational paper trading only. Open http://{settings.host}:{settings.port}/")
    print(f"Live data provider: {settings.resolved_provider()}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
