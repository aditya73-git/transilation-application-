"""Main entry point for the application"""
import sys
import logging
from pathlib import Path
from PyQt5.QtWidgets import QApplication

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.logger import setup_logger, create_log_file
from src.config import get_config
from src.ui.main_window import MainWindow


def main():
    """Main application entry point"""
    # Setup logging
    log_file = create_log_file("logs")
    config = get_config()
    debug_mode = config.is_debug_mode()
    setup_logger("", debug_mode=debug_mode, log_file=log_file)

    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Starting Offline Translator Application")
    logger.info("=" * 60)

    # Check Python version
    if sys.version_info < (3, 9):
        logger.error("Python 3.9+ required")
        sys.exit(1)

    # Create application
    app = QApplication(sys.argv)

    try:
        # Create main window
        window = MainWindow()
        window.show()

        logger.info("Application window opened")

        # Run event loop
        sys.exit(app.exec_())

    except Exception as e:
        logger.error(f"Application error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
