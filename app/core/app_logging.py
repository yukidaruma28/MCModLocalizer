"""File-based logging helper.

Sets up a `RotatingFileHandler` writing to `mcmodlocalizer.log` next to the
chosen output folder (or a fallback under the user's home if no output folder
is configured yet). The same handler is reused across reconfigurations so
calling `configure_file_logging()` repeatedly with a new path simply migrates
the file destination without losing the in-process queue.

The GUI log panel (`_append_log`) keeps working as before; this module just
mirrors those entries — plus stdout/stderr captured via `redirect_print` —
into a persistent file so a user can inspect what happened after closing the
app.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

LOG_FILENAME = "mcmodlocalizer.log"
LOGGER_NAME = "mcmodlocalizer"
_MAX_BYTES = 1 * 1024 * 1024  # 1 MB
_BACKUP_COUNT = 5

_current_handler: Optional[logging.handlers.RotatingFileHandler] = None
_print_redirected = False


def _resolve_log_dir(preferred: Optional[Path]) -> Path:
    if preferred:
        try:
            preferred.mkdir(parents=True, exist_ok=True)
            return preferred
        except Exception:
            pass
    fallback = Path.home() / ".mcmodlocalizer"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def configure_file_logging(output_dir: Optional[Path] = None) -> Path:
    """Attach (or re-attach) a rotating file handler to the app logger.

    Returns the resolved log file path so callers can show it to the user.
    Safe to call multiple times — the previous handler is replaced.
    """
    global _current_handler

    log_dir = _resolve_log_dir(output_dir)
    log_path = log_dir / LOG_FILENAME

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if _current_handler is not None:
        try:
            logger.removeHandler(_current_handler)
            _current_handler.close()
        except Exception:
            pass
        _current_handler = None

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    _current_handler = handler
    return log_path


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_gui_line(line: str) -> None:
    """Mirror a GUI log line into the file logger.

    Levels are inferred from `[ERROR]` / `[WARN]` / `[INFO]` markers in the
    line so severities show up correctly in the log file.
    """
    logger = get_logger()
    upper = line
    if "[ERROR]" in upper:
        logger.error(line)
    elif "[WARN]" in upper:
        logger.warning(line)
    elif "[DEBUG]" in upper:
        logger.debug(line)
    else:
        logger.info(line)


class _TeeStream:
    """Wraps a real stream and tees writes to the file logger."""

    def __init__(self, original, level: int):
        self._original = original
        self._level = level
        self._buffer = ""

    def write(self, data: str) -> int:
        try:
            written = self._original.write(data) if self._original else len(data)
        except Exception:
            written = len(data)
        if not data:
            return written or 0
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                try:
                    get_logger().log(self._level, line)
                except Exception:
                    pass
        return written or len(data)

    def flush(self) -> None:
        if self._buffer:
            try:
                get_logger().log(self._level, self._buffer.rstrip())
            except Exception:
                pass
            self._buffer = ""
        if self._original:
            try:
                self._original.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._original, name)


def redirect_print() -> None:
    """Tee stdout/stderr through the file logger.

    Existing `print(...)` calls (e.g. the `--- [DEBUG] SEND User ---` lines
    emitted by providers) end up in the same log file without changing call
    sites. The original stream is preserved so terminal output still works.
    """
    global _print_redirected
    if _print_redirected:
        return
    sys.stdout = _TeeStream(sys.stdout, logging.DEBUG)
    sys.stderr = _TeeStream(sys.stderr, logging.ERROR)
    _print_redirected = True


def log_startup_context(**fields: object) -> None:
    """Write a one-line startup banner to make session boundaries obvious."""
    logger = get_logger()
    logger.info("=" * 60)
    parts = [f"{k}={v}" for k, v in fields.items() if v is not None]
    logger.info("startup " + " ".join(parts) if parts else "startup")
    if os.name:
        logger.debug(f"os={os.name} cwd={os.getcwd()}")


__all__ = [
    "LOG_FILENAME",
    "configure_file_logging",
    "get_logger",
    "log_gui_line",
    "redirect_print",
    "log_startup_context",
]
