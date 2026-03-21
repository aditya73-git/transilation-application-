"""Speech-to-Text Service using OpenAI Whisper"""
import numpy as np
import whisper
from pathlib import Path
from typing import Optional, Tuple
from src.utils.logger import get_logger
from src.config import get_config

logger = get_logger(__name__)


class STTService:
    """Speech-to-Text using offline Whisper model"""

    SUPPORTED_LANGUAGES = {
        "en",
        "zh",
        "de",
        "es",
        "ru",
        "ko",
        "fr",
        "ja",
        "pt",
        "tr",
        "pl",
        "ca",
        "nl",
        "ar",
        "sv",
        "it",
        "id",
        "hi",
        "fi",
        "vi",
        "he",
        "uk",
        "el",
        "hu",
        "ro",
        "sk",
    }

    def __init__(self):
        """Initialize STT service with Whisper model"""
        config = get_config()
        self.config = config.get_whisper_model()
        self.model = None
        self.device = self.config.get("device", "cpu")
        self._load_model()

    def _load_model(self):
        """Load Whisper model"""
        try:
            logger.info(f"Loading Whisper model: {self.config['model']}")
            self.model = whisper.load_model(self.config["model"], device=self.device)
            logger.info("Whisper model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            raise

    def transcribe(self, audio_input, language: Optional[str] = None) -> Tuple[str, str, float]:
        """
        Transcribe audio to text

        Args:
            audio_input: Either numpy array or file path
            language: Optional Whisper language code to constrain transcription

        Returns:
            Tuple of (text, detected_language, confidence)
        """
        if self.model is None:
            logger.error("Model not loaded")
            return "", "unknown", 0.0

        try:
            # Handle file path input
            if isinstance(audio_input, (str, Path)):
                logger.info(f"Loading audio from {audio_input}")
                audio = whisper.load_audio(str(audio_input))
            else:
                # Assume numpy array
                audio = audio_input

            whisper_language = language.lower().strip() if language else None
            if whisper_language and whisper_language not in self.SUPPORTED_LANGUAGES:
                logger.warning(
                    f"Unsupported Whisper language '{whisper_language}', falling back to auto-detect"
                )
                whisper_language = None

            if whisper_language:
                logger.info(f"Starting transcription with constrained language: {whisper_language}")
            else:
                logger.info("Starting transcription with auto-detect...")

            result = self.model.transcribe(audio, language=whisper_language, fp16=False)

            text = result.get("text", "").strip()
            language = result.get("language", "unknown")
            confidence = result.get("segments", [{}])[0].get("confidence", 0.0) if result.get(
                "segments"
            ) else 0.85

            logger.info(f"Transcription complete. Language: {language}, Text: {text[:50]}...")
            return text, language, confidence

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return "", "unknown", 0.0

    def transcribe_file(self, file_path: str, language: Optional[str] = None) -> Tuple[str, str, float]:
        """
        Transcribe audio file

        Args:
            file_path: Path to audio file
            language: Optional Whisper language code

        Returns:
            Tuple of (text, detected_language, confidence)
        """
        return self.transcribe(file_path, language=language)

    def get_supported_languages(self):
        """Get list of supported languages"""
        return sorted(self.SUPPORTED_LANGUAGES)

    def set_device(self, device: str):
        """Change device (cpu or cuda)"""
        self.device = device
        if self.model is not None:
            self.model.to(device)
            logger.info(f"Model moved to {device}")

    def unload_model(self):
        """Unload model to free memory"""
        if self.model is not None:
            self.model = None
            logger.info("Whisper model unloaded")


# Global instance
_stt_instance = None


def get_stt_service() -> STTService:
    """Get global STT service instance"""
    global _stt_instance
    if _stt_instance is None:
        _stt_instance = STTService()
    return _stt_instance
