'''Bounded project-owned operational logging.'''

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.core.config import RuntimePaths


def configure_file_logging(paths: RuntimePaths) -> RotatingFileHandler:
    paths.log_root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        paths.log_root / 'publicdb2.log', maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.getLogger('publicdb2').addHandler(handler)
    logging.getLogger('publicdb2').setLevel(logging.INFO)
    return handler


def close_file_logging(handler: RotatingFileHandler) -> None:
    logging.getLogger('publicdb2').removeHandler(handler)
    handler.close()
