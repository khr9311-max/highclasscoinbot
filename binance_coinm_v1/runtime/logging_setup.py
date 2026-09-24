"""로깅: 내부 시각 UTC, 모든 출력은 비밀값 마스킹 포매터를 거친다."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path

from ..storage.redact import GLOBAL_REDACTOR, RedactingFormatter


def setup_logging(state_dir: str, level: int = logging.INFO, console: bool = True) -> str:
    log_dir = Path(state_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "coinm_v1.log"
    fmt = RedactingFormatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s",
                             datefmt="%Y-%m-%dT%H:%M:%S", redactor=GLOBAL_REDACTOR)
    fmt.converter = time.gmtime                      # 내부 시각은 UTC
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    for noisy in ("websockets", "aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return str(path)
