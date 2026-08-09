from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Union


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("video_tool")
    if logger.handlers:
        logger.setLevel(level)
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


logger = setup_logging()


def ensure_dir(path: Union[str, Path]) -> Path:
    path_obj = Path(path)
    path_obj.mkdir(parents=True, exist_ok=True)
    return path_obj