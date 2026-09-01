import logging
import sys
from logging.handlers import RotatingFileHandler

from config import Config

_LOGGING_INITIALIZED = False


def setup_logging(default_level: int = logging.INFO):
    """
    Configure structured logging for the Vantrue Automation system.
    Outputs to both stdout (for journald / systemd) and persistent rotating log files.
    """
    global _LOGGING_INITIALIZED
    if _LOGGING_INITIALIZED:
        return

    root_logger = logging.getLogger()
    root_logger.setLevel(default_level)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Console Handler (for systemd / journalctl)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(default_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 2. Persistent Rotating File Handler
    try:
        Config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Config.LOG_FILE,
            maxBytes=Config.LOG_MAX_BYTES,
            backupCount=Config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(default_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except Exception as exc:
        sys.stderr.write(f"[Logging] Failed to initialize file logger: {exc}\n")

    _LOGGING_INITIALIZED = True
