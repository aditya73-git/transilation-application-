"""Speech-to-Text Service using faster-whisper."""
import math
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel
from src.utils.logger import get_logger
from src.config import get_config

logger = get_logger(__name__)


class STTService:
    """Speech-to-Text using offline faster-whisper."""

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
        """Initialize STT service with faster-whisper model."""
        config = get_config()
        self.config = config.get_whisper_model()
        self.model = None
        self.device = self.config.get("device", "cpu")
        self.compute_type = self.config.get("compute_type", "int8")
        self.cpu_threads = self.config.get("cpu_threads", 0)
        self.num_workers = self.config.get("num_workers", 1)
        self.beam_size = self.config.get("beam_size", 1)
        self.vad_filter = self.config.get("vad_filter", True)
        self._load_model()

    def _load_model(self):
        """Load the faster-whisper model."""
        try:
            logger.info(
                "Loading faster-whisper model: %s (device=%s, compute_type=%s)",
                self.config["model"],
                self.device,
                self.compute_type,
            )
            model_kwargs = {
                "model_size_or_path": self.config["model"],
                "device": self.device,
                "compute_type": self.compute_type,
                "num_workers": self.num_workers,
            }
            if self.cpu_threads:
                model_kwargs["cpu_threads"] = self.cpu_threads

            self.model = WhisperModel(**model_kwargs)
            logger.info("faster-whisper model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load faster-whisper model: {e}")
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
            temp_path = None
            if isinstance(audio_input, (str, Path)):
                logger.info(f"Loading audio from {audio_input}")
                audio = str(audio_input)
            else:
                # faster-whisper is most reliable when given a real audio file path.
                fd, temp_path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                sf.write(temp_path, np.asarray(audio_input, dtype=np.float32), 16000)
                audio = temp_path

            whisper_language = language.lower().strip() if language else None
            if whisper_language and whisper_language not in self.SUPPORTED_LANGUAGES:
                logger.warning(
                    f"Unsupported faster-whisper language '{whisper_language}', falling back to auto-detect"
                )
                whisper_language = None

            if whisper_language:
                logger.info(f"Starting transcription with constrained language: {whisper_language}")
            else:
                logger.info("Starting transcription with auto-detect...")

            segments, info = self.model.transcribe(
                audio,
                language=whisper_language,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
            )
            segments = list(segments)
            text = " ".join(segment.text.strip() for segment in segments if segment.text).strip()
            detected_language = getattr(info, "language", "unknown")

            avg_confidence = 0.85
            if segments:
                probs = []
                for segment in segments:
                    avg_logprob = getattr(segment, "avg_logprob", None)
                    if avg_logprob is not None:
                        probs.append(max(0.0, min(1.0, math.exp(avg_logprob))))
                if probs:
                    avg_confidence = sum(probs) / len(probs)

            logger.info(
                "Transcription complete. Language: %s, Text: %s...",
                detected_language,
                text[:50],
            )
            return text, detected_language, avg_confidence

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return "", "unknown", 0.0
        finally:
            if "temp_path" in locals() and temp_path:
                Path(temp_path).unlink(missing_ok=True)

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
        """Change device (cpu or cuda) and reload the model."""
        self.device = device
        self.unload_model()
        self._load_model()
        logger.info(f"Model reloaded on {device}")

    def unload_model(self):
        """Unload model to free memory."""
        if self.model is not None:
            self.model = None
            logger.info("faster-whisper model unloaded")


# Global instance
_stt_instance = None


def get_stt_service() -> STTService:
    """Get global STT service instance"""
    global _stt_instance
    if _stt_instance is None:
        _stt_instance = STTService()
    return _stt_instance
