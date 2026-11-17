"""日志配置 — 控制台 + 文件，密码/Token 不落日志。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


class SensitiveFilter(logging.Filter):
    """防止密码/Token 出现在日志中（仅替换敏感记录，其余保持原样）。"""

    SENSITIVE = ("password", "token", "secret", "authorization")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage()).lower()
            if any(s in msg for s in self.SENSITIVE):
                record.msg = "[已过滤含敏感信息的日志]"
                record.args = None
        except Exception:
            pass
        return True


def setup_logging(level: str = "INFO", log_file: str | Path | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(SensitiveFilter())
    root.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(SensitiveFilter())
        root.addHandler(fh)
    _CONFIGURED = True


def get_logger(name: str = "recon") -> logging.Logger:
    return logging.getLogger(name)
