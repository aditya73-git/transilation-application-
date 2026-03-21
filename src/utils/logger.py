"""Logging configuration for the application."""
import logging
import sys
from datetime import datetime
from pathlib import Path


NOISY_LOGGERS = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "filelock": logging.WARNING,
    "urllib3": logging.WARNING,
    "huggingface_hub": logging.WARNING,
    "transformers": logging.WARNING,
}


def _resolve_level(debug_mode=False, log_level=None):
    """Resolve the effective log level."""
    if log_level:
        return getattr(logging, str(log_level).upper(), logging.INFO)
    return logging.DEBUG if debug_mode else logging.INFO


def setup_logger(name, debug_mode=False, log_file=None, log_level=None):
    """
    Setup logger with console and file handlers.

    Args:
        name: Logger name
        debug_mode: Enable debug logging when no explicit log level is set
        log_file: Optional log file path
        log_level: Optional explicit log level (INFO, DEBUG, WARNING, ERROR)

    Returns:
        Configured logger instance
    """
    level = _resolve_level(debug_mode=debug_mode, log_level=log_level)
    logger = logging.getLogger(name)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_file:
        log_path = Path(log_file).parent
        log_path.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    for logger_name, logger_level in NOISY_LOGGERS.items():
        noisy_logger = logging.getLogger(logger_name)
        noisy_logger.setLevel(logger_level)
        noisy_logger.propagate = True

    logger.setLevel(level)
    return logger


def get_logger(name):
    """Get logger instance."""
    return logging.getLogger(name)


def create_log_file(logs_dir="logs"):
    """Create log file with timestamp."""
    Path(logs_dir).mkdir(exist_ok=True)
    log_file = Path(logs_dir) / f"translator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    return str(log_file)
