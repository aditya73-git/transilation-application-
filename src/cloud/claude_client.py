"""Claude API client for translation refinement"""
import requests
import json
import threading
from typing import Optional, Callable
from src.utils.logger import get_logger
from src.config import get_config

logger = get_logger(__name__)


class ClaudeClient:
    """Client for Claude API-based translation refinement"""

    CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self):
        """Initialize Claude client"""
        config = get_config()
        self.cloud_config = config.get_cloud_config()
        self.api_key = self.cloud_config.get("claude_api_key", "")
        self.model = self.cloud_config.get("refinement_model", "claude-3-5-sonnet-20241022")
        self.enabled = self.cloud_config.get("enabled", False) and bool(self.api_key)

        if self.enabled:
            logger.info(f"Claude client initialized (model: {self.model})")
        else:
            logger.info("Claude client disabled (no API key)")

    def refine_translation(self, original_text: str, translated_text: str,
                          source_lang: str, target_lang: str) -> Optional[str]:
        """
        Refine translation using Claude

        Args:
            original_text: Original source text
            translated_text: Initial translation
            source_lang: Source language
            target_lang: Target language

        Returns:
            Refined translation or None if failed
        """
        if not self.enabled:
            return None

        try:
            prompt = f"""You are a professional translator. Refine the following translation to make it more natural and accurate.

Original text ({source_lang}): {original_text}
Current translation ({target_lang}): {translated_text}

Provide only the refined translation without any explanation."""

            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }

            payload = {
                "model": self.model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }

            response = requests.post(
                self.CLAUDE_API_URL,
                headers=headers,
                json=payload,
                timeout=10,
            )

            if response.status_code == 200:
                result = response.json()
                refined = result.get("content", [{}])[0].get("text", "").strip()
                logger.info(f"Translation refined: {refined[:50]}...")
                return refined
            else:
                logger.error(f"Claude API error: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"Refinement error: {e}")
            return None

    def refine_translation_async(
        self,
        original_text: str,
        translated_text: str,
        source_lang: str,
        target_lang: str,
        callback: Optional[Callable] = None,
    ):
        """
        Refine translation asynchronously in background

        Args:
            original_text: Original source text
            translated_text: Initial translation
            source_lang: Source language
            target_lang: Target language
            callback: Function to call with refined text when done
        """
        def _refine_thread():
            refined = self.refine_translation(original_text, translated_text, source_lang, target_lang)
            if callback and refined:
                try:
                    callback(refined)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

        thread = threading.Thread(target=_refine_thread, daemon=True)
        thread.start()

    def is_enabled(self) -> bool:
        """Check if Claude client is enabled"""
        return self.enabled

    def test_connection(self) -> bool:
        """Test connection to Claude API"""
        if not self.enabled:
            return False

        try:
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }

            payload = {
                "model": self.model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
            }

            response = requests.post(
                self.CLAUDE_API_URL,
                headers=headers,
                json=payload,
                timeout=5,
            )

            if response.status_code == 200:
                logger.info("Claude API connection successful")
                return True
            else:
                logger.warning(f"Claude API test failed: {response.status_code}")
                return False

        except Exception as e:
            logger.warning(f"Claude API connection error: {e}")
            return False


# Global instance
_claude_instance = None


def get_claude_client() -> ClaudeClient:
    """Get global Claude client instance"""
    global _claude_instance
    if _claude_instance is None:
        _claude_instance = ClaudeClient()
    return _claude_instance
