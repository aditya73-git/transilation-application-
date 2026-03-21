"""Language pair management service."""
from typing import List, Tuple

from src.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class LanguageService:
    """Manage the offline GUI language matrix and active pair."""

    def __init__(self):
        """Initialize language service."""
        config = get_config()
        self.supported_languages = config.get_languages()
        self.language_codes = config.get_language_codes()
        self.default_pair = config.get_default_pair()

        self.language_pairs = self._create_language_pairs()
        if self.default_pair in self.language_pairs:
            self.current_pair_index = self.language_pairs.index(self.default_pair)
        else:
            self.current_pair_index = 0
        self.current_pair = self.language_pairs[self.current_pair_index]

        logger.info("Language service initialized with %s pairs", len(self.language_pairs))

    def _create_language_pairs(self) -> List[Tuple[str, str]]:
        """Create all directed language pairs except same-language pairs."""
        pairs = []
        for source in self.supported_languages:
            for target in self.supported_languages:
                if source != target:
                    pairs.append((source, target))
        return pairs

    def get_current_pair(self) -> Tuple[str, str]:
        """Get current source and target language."""
        return self.current_pair

    def get_language_code(self, language: str) -> str:
        """Get language code for a language name."""
        return self.language_codes.get(language.lower(), language.lower())

    def switch_language_next(self):
        """Switch to next language pair."""
        self.current_pair_index = (self.current_pair_index + 1) % len(self.language_pairs)
        self.current_pair = self.language_pairs[self.current_pair_index]
        logger.info("Language pair switched to: %s → %s", self.current_pair[0], self.current_pair[1])
        return self.current_pair

    def switch_language_prev(self):
        """Switch to previous language pair."""
        self.current_pair_index = (self.current_pair_index - 1) % len(self.language_pairs)
        self.current_pair = self.language_pairs[self.current_pair_index]
        logger.info("Language pair switched to: %s → %s", self.current_pair[0], self.current_pair[1])
        return self.current_pair

    def set_language_pair(self, source: str, target: str) -> bool:
        """Manually set the active language pair."""
        source = source.lower().strip()
        target = target.lower().strip()
        pair = (source, target)

        if pair in self.language_pairs:
            self.current_pair = pair
            self.current_pair_index = self.language_pairs.index(pair)
            logger.info("Language pair set to: %s → %s", source, target)
            return True

        logger.warning("Language pair not found: %s → %s", source, target)
        return False

    def get_all_pairs(self) -> List[Tuple[str, str]]:
        """Get all available language pairs."""
        return self.language_pairs

    def get_pair_index(self) -> int:
        """Get current pair index."""
        return self.current_pair_index

    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages."""
        return self.supported_languages

    def is_language_supported(self, language: str) -> bool:
        """Check if language is supported."""
        return language.lower() in [lang.lower() for lang in self.supported_languages]

    def display_pair(self) -> str:
        """Get human-readable pair string."""
        source, target = self.current_pair
        return f"{source.capitalize()} → {target.capitalize()}"


_language_instance = None


def get_language_service() -> LanguageService:
    """Get global language service instance."""
    global _language_instance
    if _language_instance is None:
        _language_instance = LanguageService()
    return _language_instance
