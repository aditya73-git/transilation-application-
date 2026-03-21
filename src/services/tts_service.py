"""Text-to-Speech service using Piper."""
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Optional

from src.config import get_config
from src.utils.logger import get_logger
from src.utils.performance import format_stage_metrics, take_perf_sample

logger = get_logger(__name__)


class TTSService:
    """Text-to-Speech using Piper voice models."""

    def __init__(self):
        """Initialize Piper TTS configuration."""
        config = get_config()
        self.config = config.get_tts_config()
        self.engine = self.config.get("engine", "piper")
        self.piper_binary = self.config.get("piper_binary", "piper")
        self.play_command = self.config.get("piper_play_command", "auto")
        self.voice_models = self.config.get("piper_voice_models", {})
        self.current_process = None
        self._process_lock = threading.Lock()
        self.last_run_metrics = {}
        self._init_engine()

    def _init_engine(self):
        """Validate Piper executable availability."""
        if self.engine != "piper":
            raise RuntimeError(f"Unsupported TTS engine configured: {self.engine}")

        resolved_binary = shutil.which(self.piper_binary)
        if not resolved_binary:
            raise RuntimeError(
                f"Piper binary not found: {self.piper_binary}. Install Piper or update offline.piper_binary."
            )

        self.piper_binary = resolved_binary
        logger.info("Piper TTS initialized: %s", self.piper_binary)

    def _get_voice_spec(self, language: str):
        """Return configured Piper voice files for a language."""
        language = language.lower().strip()
        spec = self.voice_models.get(language)
        if not spec:
            raise RuntimeError(f"No Piper voice configured for language: {language}")

        model_path = Path(spec.get("model", "")).expanduser().resolve()
        config_path = Path(spec.get("config", "")).expanduser().resolve()

        if not model_path.exists():
            raise RuntimeError(f"Piper voice model not found: {model_path}")
        if not config_path.exists():
            raise RuntimeError(f"Piper voice config not found: {config_path}")

        return {"model": model_path, "config": config_path}

    def _get_player_command(self, audio_path: Path):
        """Build the local playback command for the generated WAV file."""
        if self.play_command == "auto":
            if shutil.which("pw-play"):
                return ["pw-play", str(audio_path)]
            if shutil.which("aplay"):
                return ["aplay", str(audio_path)]
            raise RuntimeError("No audio playback command available. Install pw-play or aplay.")

        return [self.play_command, str(audio_path)]

    def speak(self, text: str, language: str = "english", output_file: Optional[str] = None):
        """
        Convert text to speech using Piper.

        Args:
            text: Text to convert
            language: Target language
            output_file: Optional file path to save audio

        Returns:
            True if successful, False otherwise
        """
        if not text.strip():
            logger.warning("No text provided for TTS")
            return False

        try:
            self.last_run_metrics = {}
            voice_spec = self._get_voice_spec(language)
            logger.info("Converting to speech (%s): %s...", language, text[:50])

            if output_file:
                output_path = Path(output_file).expanduser().resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                temp_output = output_path
            else:
                temp_dir = Path(tempfile.mkdtemp(prefix="piper_tts_"))
                temp_output = temp_dir / "speech.wav"

            synth_cmd = [
                self.piper_binary,
                "--model",
                str(voice_spec["model"]),
                "--config",
                str(voice_spec["config"]),
                "--output_file",
                str(temp_output),
            ]

            synth_start = take_perf_sample()
            with self._process_lock:
                self.current_process = subprocess.Popen(
                    synth_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                _, stderr = self.current_process.communicate(text, timeout=120)
                return_code = self.current_process.returncode
                self.current_process = None
            synth_end = take_perf_sample()

            if return_code != 0:
                logger.error("Piper synthesis failed: %s", stderr.strip())
                return False

            audio_duration = self._get_wav_duration_seconds(temp_output)
            synth_rtf = (synth_end.wall_time - synth_start.wall_time) / audio_duration if audio_duration > 0 else 0.0
            self.last_run_metrics["synthesis"] = {
                "duration_seconds": audio_duration,
                "rtf": synth_rtf,
            }
            logger.info(format_stage_metrics("TTS synthesis", synth_start, synth_end))
            logger.info(
                "Perf | TTS audio: duration=%.2fs synth_rtf=%.2f chars=%s",
                audio_duration,
                synth_rtf,
                len(text),
            )

            if output_file:
                logger.info("Audio saved to %s", temp_output)
                return True

            play_cmd = self._get_player_command(temp_output)
            playback_start = take_perf_sample()
            with self._process_lock:
                self.current_process = subprocess.Popen(
                    play_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                _, stderr = self.current_process.communicate(timeout=120)
                return_code = self.current_process.returncode
                self.current_process = None
            playback_end = take_perf_sample()

            try:
                temp_output.unlink(missing_ok=True)
                temp_output.parent.rmdir()
            except OSError:
                pass

            if return_code != 0:
                logger.error("Audio playback failed: %s", stderr.strip())
                return False

            logger.info(format_stage_metrics("TTS playback", playback_start, playback_end))
            logger.info("Speech completed")
            return True

        except Exception as e:
            logger.error(f"TTS error: {e}")
            return False

    def _get_wav_duration_seconds(self, audio_path: Path) -> float:
        """Return WAV duration in seconds."""
        with wave.open(str(audio_path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            num_frames = wav_file.getnframes()
            if frame_rate <= 0:
                return 0.0
            return num_frames / float(frame_rate)

    def get_voices(self) -> list:
        """Get configured Piper voices."""
        voice_list = []
        for language, spec in self.voice_models.items():
            voice_list.append(
                {
                    "id": language,
                    "name": language.capitalize(),
                    "languages": [language],
                    "model": spec.get("model"),
                }
            )
        return voice_list

    def set_rate(self, rate: float):
        """Store speech rate preference for future Piper tuning."""
        self.config["speed"] = rate
        logger.info("Piper speech rate preference set to %sx", rate)

    def set_volume(self, volume: float):
        """Store volume preference for future playback tuning."""
        self.config["volume"] = max(0.0, min(1.0, volume))
        logger.info("Piper volume preference set to %s", self.config["volume"])

    def set_voice(self, voice_id: int = 0):
        """Compatibility shim for the old TTS interface."""
        voices = list(self.voice_models.keys())
        if 0 <= voice_id < len(voices):
            logger.info("Configured Piper voice selected: %s", voices[voice_id])

    def stop(self):
        """Stop current synthesis or playback process."""
        with self._process_lock:
            if self.current_process and self.current_process.poll() is None:
                self.current_process.terminate()
                try:
                    self.current_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.current_process.kill()
                finally:
                    self.current_process = None

    def shutdown(self):
        """Shutdown TTS engine."""
        self.stop()
        logger.info("Piper TTS shutdown")


_tts_instance = None


def get_tts_service() -> TTSService:
    """Get global TTS service instance."""
    global _tts_instance
    if _tts_instance is None:
        _tts_instance = TTSService()
    return _tts_instance
