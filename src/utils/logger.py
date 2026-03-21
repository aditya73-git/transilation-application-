"""Logging configuration for the application"""
import logging
import sys
from pathlib import Path
from datetime import datetime


def setup_logger(name, debug_mode=False, log_file=None):
    """
    Setup logger with console and file handlers

    Args:
        name: Logger name
        debug_mode: Enable debug logging
        log_file: Optional log file path

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Set log level
    level = logging.DEBUG if debug_mode else logging.INFO
    logger.setLevel(level)

    # Format
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler
    if log_file:
        log_path = Path(log_file).parent
        log_path.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name):
    """Get logger instance"""
    return logging.getLogger(name)


def create_log_file(logs_dir="logs"):
    """Create log file with timestamp"""
    Path(logs_dir).mkdir(exist_ok=True)
    log_file = Path(logs_dir) / f"translator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    return str(log_file)
