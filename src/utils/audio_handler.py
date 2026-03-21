"""Audio input/output handling with graceful fallback for missing PortAudio"""
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Try to import audio libraries, but provide fallbacks.
try:
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except (ImportError, OSError) as e:
    logger.warning(f"Audio libraries not available: {e}")
    SOUNDDEVICE_AVAILABLE = False
    sd = None

try:
    import soundfile as sf
    SOUNDFILE_AVAILABLE = True
except (ImportError, OSError) as e:
    logger.warning(f"SoundFile not available: {e}")
    SOUNDFILE_AVAILABLE = False
    sf = None

PW_RECORD_AVAILABLE = shutil.which("pw-record") is not None
ARECORD_AVAILABLE = shutil.which("arecord") is not None


class AudioHandler:
    """Handle microphone recording and speaker playback (with fallback for dev mode)"""

    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self.audio_data = None
        self.is_recording = False
        self.recording_process = None
        self.use_sounddevice = SOUNDDEVICE_AVAILABLE
        self.use_pw_record = (not self.use_sounddevice) and PW_RECORD_AVAILABLE and SOUNDFILE_AVAILABLE
        self.use_arecord = (
            (not self.use_sounddevice)
            and (not self.use_pw_record)
            and ARECORD_AVAILABLE
            and SOUNDFILE_AVAILABLE
        )
        self.use_mock = not self.use_sounddevice and not self.use_pw_record and not self.use_arecord

        if self.use_sounddevice:
            logger.info("Operating in SOUNDDEVICE mode")
        elif self.use_pw_record:
            logger.info("Operating in PW-RECORD mode")
        elif self.use_arecord:
            logger.info("Operating in ARECORD mode")
        else:
            logger.warning("Using mock audio handler - audio I/O will be simulated")
            logger.info("Operating in MOCK MODE (no real audio I/O)")

    def list_devices(self):
        """List available audio devices"""
        if self.use_sounddevice and sd:
            devices = sd.query_devices()
            return devices
        if self.use_pw_record:
            return ["PipeWire default source"]
        if self.use_arecord:
            result = subprocess.run(
                ["arecord", "-l"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return [line for line in result.stdout.splitlines() if line.strip()]
            logger.warning(f"Could not list arecord devices: {result.stderr.strip()}")
            return []

        logger.warning("Audio devices not available in mock mode")
        return []

    def record_audio(self, duration=None, threshold=0.02, silence_duration=0.5):
        """
        Record audio from microphone

        Args:
            duration: Maximum duration in seconds (None for manual stop)
            threshold: Amplitude threshold for silence detection (0-1)
            silence_duration: Seconds of silence to stop recording

        Returns:
            numpy array of audio data
        """
        if self.use_mock:
            return self._mock_record_audio(duration)
        if self.use_pw_record:
            return self._record_audio_pw_record(duration)
        if self.use_arecord:
            return self._record_audio_arecord(duration)

        logger.info(f"Recording audio (sample_rate: {self.sample_rate}Hz)...")
        chunks = []

        try:
            # Stream for real-time recording
            chunk_size = int(self.sample_rate * 0.1)  # 100ms chunks
            silence_samples = int(self.sample_rate * silence_duration)
            silence_count = 0

            with sd.InputStream(
                channels=1,
                samplerate=self.sample_rate,
                blocksize=chunk_size,
                dtype=np.float32,
            ) as stream:
                while self.is_recording:
                    data, _ = stream.read(chunk_size)
                    chunks.append(data)

                    # Simple silence detection
                    amplitude = np.max(np.abs(data))
                    if amplitude < threshold:
                        silence_count += chunk_size
                        if silence_count > silence_samples and len(chunks) > 5:
                            logger.info("Silence detected, stopping recording")
                            break
                    else:
                        silence_count = 0

                    # Check duration
                    if duration and len(chunks) * chunk_size > self.sample_rate * duration:
                        logger.info("Max duration reached")
                        break

            if not chunks:
                logger.warning("Recording stopped before any audio was captured")
                return np.array([], dtype=np.float32)

            # Concatenate all chunks
            audio_data = np.concatenate(chunks)
            self.audio_data = audio_data
            logger.info(f"Recording complete. Duration: {len(audio_data) / self.sample_rate:.2f}s")
            return audio_data

        except Exception as e:
            logger.error(f"Recording error: {e}")
            return None

    def _record_audio_pw_record(self, duration=None):
        """Record audio using PipeWire's pw-record command."""
        if not SOUNDFILE_AVAILABLE or sf is None:
            logger.error("SoundFile is required to load recorded audio from pw-record")
            return None

        logger.info(f"Recording audio via pw-record (sample_rate: {self.sample_rate}Hz)...")
        fd, temp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        Path(temp_path).unlink(missing_ok=True)

        cmd = [
            "pw-record",
            "--rate",
            str(self.sample_rate),
            "--channels",
            "1",
            "--format",
            "s16",
            temp_path,
        ]

        try:
            self.recording_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

            start_time = time.time()
            while self.is_recording:
                if duration and time.time() - start_time >= duration:
                    logger.info("Max duration reached")
                    break
                time.sleep(0.05)

            if self.recording_process.poll() is None:
                self.recording_process.send_signal(signal.SIGINT)

            _, stderr = self.recording_process.communicate(timeout=5)

            temp_file = Path(temp_path)
            if not temp_file.exists() or temp_file.stat().st_size == 0:
                error_message = stderr.strip() or f"exit code {self.recording_process.returncode}"
                logger.error(f"pw-record failed: {error_message}")
                return None

            if self.recording_process.returncode not in (0, -2, -15):
                logger.warning(
                    "pw-record exited with code %s after capture; using recorded audio",
                    self.recording_process.returncode,
                )

            if temp_file.stat().st_size == 0:
                logger.warning("pw-record did not capture any audio")
                return np.array([], dtype=np.float32)

            audio_data, sr = sf.read(temp_path, dtype="float32")
            audio_data = np.asarray(audio_data, dtype=np.float32)
            if audio_data.ndim > 1:
                audio_data = audio_data[:, 0]
            if sr != self.sample_rate:
                logger.warning(f"Sample rate mismatch: {sr} != {self.sample_rate}")

            self.audio_data = audio_data
            logger.info(f"Recording complete. Duration: {len(audio_data) / self.sample_rate:.2f}s")
            return audio_data

        except subprocess.TimeoutExpired:
            logger.error("Timed out while stopping pw-record")
            if self.recording_process and self.recording_process.poll() is None:
                self.recording_process.kill()
            return None
        except Exception as e:
            logger.error(f"pw-record recording error: {e}")
            return None
        finally:
            self.recording_process = None
            Path(temp_path).unlink(missing_ok=True)

    def _record_audio_arecord(self, duration=None):
        """Record audio using the system's arecord command."""
        if not SOUNDFILE_AVAILABLE or sf is None:
            logger.error("SoundFile is required to load recorded audio from arecord")
            return None

        logger.info(f"Recording audio via arecord (sample_rate: {self.sample_rate}Hz)...")
        fd, temp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        Path(temp_path).unlink(missing_ok=True)

        cmd = [
            "arecord",
            "-q",
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            "1",
            "-t",
            "wav",
            temp_path,
        ]

        try:
            self.recording_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

            start_time = time.time()
            while self.is_recording:
                if duration and time.time() - start_time >= duration:
                    logger.info("Max duration reached")
                    break
                time.sleep(0.05)

            if self.recording_process.poll() is None:
                self.recording_process.send_signal(signal.SIGINT)

            _, stderr = self.recording_process.communicate(timeout=5)

            if self.recording_process.returncode not in (0, -15):
                logger.error(f"arecord failed: {stderr.strip()}")
                return None

            if not Path(temp_path).exists() or Path(temp_path).stat().st_size == 0:
                logger.warning("arecord did not capture any audio")
                return np.array([], dtype=np.float32)

            audio_data, sr = sf.read(temp_path, dtype="float32")
            audio_data = np.asarray(audio_data, dtype=np.float32)
            if audio_data.ndim > 1:
                audio_data = audio_data[:, 0]
            if sr != self.sample_rate:
                logger.warning(f"Sample rate mismatch: {sr} != {self.sample_rate}")

            self.audio_data = audio_data
            logger.info(f"Recording complete. Duration: {len(audio_data) / self.sample_rate:.2f}s")
            return audio_data

        except subprocess.TimeoutExpired:
            logger.error("Timed out while stopping arecord")
            if self.recording_process and self.recording_process.poll() is None:
                self.recording_process.kill()
            return None
        except Exception as e:
            logger.error(f"arecord recording error: {e}")
            return None
        finally:
            self.recording_process = None
            Path(temp_path).unlink(missing_ok=True)

    def _mock_record_audio(self, duration=None):
        """Mock audio recording for testing with push-to-talk style behavior."""
        logger.info("MOCK: Simulating audio recording...")
        max_duration = duration or 10
        chunk_duration = 0.1
        chunk_samples = int(self.sample_rate * chunk_duration)
        frequency = 440  # A4 note
        start_time = time.time()
        chunks = []

        while self.is_recording:
            elapsed = time.time() - start_time
            if elapsed >= max_duration:
                logger.info("MOCK: Max duration reached")
                break

            t = np.linspace(0, chunk_duration, chunk_samples, endpoint=False)
            sine_wave = np.sin(2 * np.pi * frequency * t) * 0.3
            noise = np.random.normal(0, 0.05, chunk_samples)
            chunks.append((sine_wave + noise).astype(np.float32))
            time.sleep(chunk_duration * 0.5)

        if not chunks:
            logger.warning("MOCK: Recording stopped before any audio was captured")
            return np.array([], dtype=np.float32)

        audio_data = np.concatenate(chunks)
        self.audio_data = audio_data
        logger.info(f"MOCK: Recording complete. Duration: {len(audio_data) / self.sample_rate:.2f}s")
        return audio_data

    def stop_recording(self):
        """Stop recording"""
        self.is_recording = False
        if self.recording_process and self.recording_process.poll() is None:
            self.recording_process.send_signal(signal.SIGINT)

    def play_audio(self, audio_data, blocking=True):
        """
        Play audio through speakers

        Args:
            audio_data: numpy array of audio data or file path
            blocking: Wait for playback to finish
        """
        if self.use_mock:
            return self._mock_play_audio(audio_data)

        try:
            if isinstance(audio_data, (str, Path)):
                # Load from file
                audio_data, _ = sf.read(audio_data)

            logger.info(f"Playing audio ({len(audio_data) / self.sample_rate:.2f}s)...")
            sd.play(audio_data, self.sample_rate, blocking=blocking)
            logger.info("Playback finished")

        except Exception as e:
            logger.error(f"Playback error: {e}")

    def _mock_play_audio(self, audio_data):
        """Mock audio playback"""
        if isinstance(audio_data, (str, Path)):
            logger.info(f"MOCK: Would play audio from {audio_data}")
        else:
            duration = len(audio_data) / self.sample_rate
            logger.info(f"MOCK: Playing audio ({duration:.2f}s)...")
            time.sleep(duration * 0.5)  # Simulate playback

        logger.info("MOCK: Playback finished")

    def save_audio(self, audio_data, file_path):
        """Save audio to file"""
        if self.use_mock:
            logger.info(f"MOCK: Would save audio to {file_path}")
            return str(file_path)

        try:
            file_path = Path(file_path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(file_path, audio_data, self.sample_rate)
            logger.info(f"Audio saved to {file_path}")
            return str(file_path)
        except Exception as e:
            logger.error(f"Save audio error: {e}")
            return None

    def load_audio(self, file_path):
        """Load audio from file"""
        if self.use_mock:
            logger.info(f"MOCK: Would load audio from {file_path}")
            # Return mock audio data
            duration = 2
            num_samples = int(self.sample_rate * duration)
            return np.random.normal(0, 0.05, num_samples).astype(np.float32)

        try:
            audio_data, sr = sf.read(file_path)
            if sr != self.sample_rate:
                logger.warning(f"Sample rate mismatch: {sr} != {self.sample_rate}")
            return audio_data
        except Exception as e:
            logger.error(f"Load audio error: {e}")
            return None
