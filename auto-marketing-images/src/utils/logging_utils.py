from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "pipeline.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(logfile, encoding="utf-8"),
        ],
    )
