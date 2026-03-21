"""Internet connectivity detection service"""
import socket
import threading
from typing import Callable
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ConnectivityService:
    """Monitor and check internet connectivity"""

    def __init__(self):
        """Initialize connectivity service"""
        self.is_online = False
        self.check_interval = 10  # seconds
        self._monitoring = False
        self._monitor_thread = None
        self._callbacks = []

    def check_connection(self, host="8.8.8.8", port=53, timeout=3) -> bool:
        """
        Check internet connectivity

        Args:
            host: Host to ping (default Google DNS)
            port: Port number
            timeout: Timeout in seconds

        Returns:
            True if online, False otherwise
        """
        try:
            socket.setdefaulttimeout(timeout)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            return True
        except socket.error:
            return False

    def is_connected(self) -> bool:
        """Get current connectivity status"""
        return self.is_online

    def update_status(self) -> bool:
        """Update connectivity status"""
        previous = self.is_online
        self.is_online = self.check_connection()

        if self.is_online != previous:
            status = "ONLINE" if self.is_online else "OFFLINE"
            logger.info(f"Connectivity changed to: {status}")
            self._notify_callbacks()

        return self.is_online

    def add_callback(self, callback: Callable):
        """
        Add callback function to be called on connectivity change

        Args:
            callback: Function to call with (is_online: bool) parameter
        """
        self._callbacks.append(callback)

    def _notify_callbacks(self):
        """Notify all registered callbacks"""
        for callback in self._callbacks:
            try:
                callback(self.is_online)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def start_monitoring(self, interval: int = 10):
        """
        Start background monitoring of connectivity

        Args:
            interval: Check interval in seconds
        """
        if self._monitoring:
            return

        self.check_interval = interval
        self._monitoring = True

        def monitor():
            while self._monitoring:
                self.update_status()
                import time
                time.sleep(interval)

        self._monitor_thread = threading.Thread(target=monitor, daemon=True)
        self._monitor_thread.start()
        logger.info(f"Connectivity monitoring started (interval: {interval}s)")

    def stop_monitoring(self):
        """Stop background monitoring"""
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("Connectivity monitoring stopped")


# Global instance
_connectivity_instance = None


def get_connectivity_service() -> ConnectivityService:
    """Get global connectivity service instance"""
    global _connectivity_instance
    if _connectivity_instance is None:
        _connectivity_instance = ConnectivityService()
    return _connectivity_instance
