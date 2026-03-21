"""Text-to-Speech Service using pyttsx3"""
import pyttsx3
from pathlib import Path
from typing import Optional
from src.utils.logger import get_logger
from src.config import get_config

logger = get_logger(__name__)


class TTSService:
    """Text-to-Speech using offline pyttsx3 engine"""

    # Language code mappings for TTS
    LANG_CODE_MAPPING = {
        "english": "en",
        "german": "de",
        "arabic": "ar",
        "romanian": "ro",
        "slovakian": "sk",
        "turkish": "tr",
        "polish": "pl",
    }

    def __init__(self):
        """Initialize TTS service"""
        config = get_config()
        self.config = config.get_tts_config()
        self.engine = None
        self._init_engine()

    def _init_engine(self):
        """Initialize pyttsx3 engine"""
        try:
            logger.info("Initializing pyttsx3 engine...")
            self.engine = pyttsx3.init()

            # Set properties
            self.engine.setProperty("rate", 150)  # Speed of speech
            self.engine.setProperty("volume", self.config.get("volume", 1.0))

            logger.info("pyttsx3 engine initialized")

        except Exception as e:
            logger.error(f"Failed to initialize TTS engine: {e}")
            raise

    def speak(self, text: str, language: str = "english", output_file: Optional[str] = None):
        """
        Convert text to speech

        Args:
            text: Text to convert
            language: Target language
            output_file: Optional file path to save audio

        Returns:
            True if successful, False otherwise
        """
        if self.engine is None:
            logger.error("TTS engine not initialized")
            return False

        try:
            # Normalize language
            language = language.lower().strip()
            lang_code = self.LANG_CODE_MAPPING.get(language, language)

            logger.info(f"Converting to speech ({lang_code}): {text[:50]}...")

            # Set language (basic - pyttsx3 has limited language support)
            # This is more for future enhancement
            # Current version works best with system default

            if output_file:
                # Save to file
                output_path = Path(output_file)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                self.engine.save_to_file(text, str(output_path))
                self.engine.runAndWait()
                logger.info(f"Audio saved to {output_path}")
                return True
            else:
                # Play directly
                self.engine.say(text)
                self.engine.runAndWait()
                logger.info("Speech completed")
                return True

        except Exception as e:
            logger.error(f"TTS error: {e}")
            return False

    def get_voices(self) -> list:
        """Get available voices"""
        if self.engine is None:
            return []

        try:
            voices = self.engine.getProperty("voices")
            voice_list = []
            for voice in voices:
                voice_list.append({"id": voice.id, "name": voice.name, "languages": voice.languages})
            return voice_list
        except Exception as e:
            logger.error(f"Error getting voices: {e}")
            return []

    def set_rate(self, rate: float):
        """
        Set speech rate (0.5 to 2.0)

        Args:
            rate: Speed multiplier
        """
        if self.engine:
            self.engine.setProperty("rate", int(150 * rate))
            logger.info(f"Speech rate set to {rate}x")

    def set_volume(self, volume: float):
        """
        Set volume (0.0 to 1.0)

        Args:
            volume: Volume level
        """
        if self.engine:
            self.engine.setProperty("volume", max(0.0, min(1.0, volume)))
            logger.info(f"Volume set to {volume}")

    def set_voice(self, voice_id: int = 0):
        """
        Set voice

        Args:
            voice_id: Index of voice to use
        """
        if self.engine:
            try:
                voices = self.engine.getProperty("voices")
                if 0 <= voice_id < len(voices):
                    self.engine.setProperty("voice", voices[voice_id].id)
                    logger.info(f"Voice set to {voices[voice_id].name}")
            except Exception as e:
                logger.error(f"Error setting voice: {e}")

    def stop(self):
        """Stop current speech"""
        if self.engine:
            self.engine.stop()

    def shutdown(self):
        """Shutdown TTS engine"""
        if self.engine:
            self.engine.stop()
            logger.info("TTS engine shutdown")


# Global instance
_tts_instance = None


def get_tts_service() -> TTSService:
    """Get global TTS service instance"""
    global _tts_instance
    if _tts_instance is None:
        _tts_instance = TTSService()
    return _tts_instance
