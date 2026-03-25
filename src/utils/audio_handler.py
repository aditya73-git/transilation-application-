"""Audio input/output handling with graceful fallback for missing PortAudio"""
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
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

    def __init__(self, sample_rate=16000, esp_config=None):
        self.sample_rate = sample_rate
        esp_config = esp_config or {}
        self.esp_enabled = bool(esp_config.get("enabled")) and bool(
            (esp_config.get("host") or "").strip()
        )
        self.esp_host = (esp_config.get("host") or "").strip()
        self.esp_mic_port = int(esp_config.get("mic_port", 12346))
        self.esp_play_port = int(esp_config.get("playback_port", 12345))
        self.esp_mic_sample_width = int(esp_config.get("mic_sample_width", 4))
        if self.esp_mic_sample_width not in (2, 4):
            logger.warning("Unsupported ESP mic sample width %s; defaulting to 4", self.esp_mic_sample_width)
            self.esp_mic_sample_width = 4
        if esp_config.get("enabled") and not self.esp_host:
            logger.warning("ESP enabled but host empty; ESP audio disabled")

        self.audio_data = None
        self.is_recording = False
        self.recording_process = None
        self.recording_stderr = ""
        self.stream_reader_thread = None
        self.stream_buffer = bytearray()
        self.stream_buffer_lock = threading.Lock()
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

    def start_stream_recording(self):
        """Start recording audio into an internal streaming buffer."""
        self.audio_data = None
        self.recording_stderr = ""
        with self.stream_buffer_lock:
            self.stream_buffer = bytearray()

        if self.esp_enabled:
            self._start_esp_mic_stream()
            return
        if self.use_mock:
            self._start_mock_stream()
            return
        if self.use_pw_record:
            self._start_pw_record_stream()
            return
        if self.use_arecord:
            self._start_arecord_stream()
            return

        self._start_sounddevice_stream()

    def get_recording_snapshot(self):
        """Get the currently buffered recording as float32 mono audio."""
        if self.esp_enabled or self.use_mock or self.use_sounddevice:
            with self.stream_buffer_lock:
                buffer_copy = bytes(self.stream_buffer)
            if not buffer_copy:
                return np.array([], dtype=np.float32)
            return np.frombuffer(buffer_copy, dtype=np.float32).copy()

        with self.stream_buffer_lock:
            buffer_copy = bytes(self.stream_buffer)
        if not buffer_copy:
            return np.array([], dtype=np.float32)
        return self._pcm16_bytes_to_float32(buffer_copy)

    def _start_esp_mic_stream(self):
        """TCP mic stream from ESP (16/32-bit stereo LE @ sample_rate) -> float32 mono buffer."""
        from src.utils.esp_audio_transport import pcm_stereo_bytes_to_mono_float32

        logger.info(
            "ESP mic stream: tcp://%s:%s (%s-bit stereo)",
            self.esp_host,
            self.esp_mic_port,
            self.esp_mic_sample_width * 8,
        )

        def reader():
            try:
                sock = socket.create_connection((self.esp_host, self.esp_mic_port), timeout=10)
            except OSError as e:
                logger.error("ESP mic connect failed: %s", e)
                return
            sock.settimeout(0.35)
            partial = bytearray()
            try:
                while self.is_recording:
                    try:
                        data = sock.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    partial.extend(data)
                    frame_bytes = self.esp_mic_sample_width * 2
                    take = len(partial) - (len(partial) % frame_bytes)
                    if take == 0:
                        continue
                    block = bytes(partial[:take])
                    del partial[:take]
                    mono = pcm_stereo_bytes_to_mono_float32(block, self.esp_mic_sample_width)
                    if mono.size == 0:
                        continue
                    with self.stream_buffer_lock:
                        self.stream_buffer.extend(mono.astype(np.float32).tobytes())
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

        self.stream_reader_thread = threading.Thread(target=reader, daemon=True)
        self.stream_reader_thread.start()

    def stop_stream_recording(self):
        """Stop streaming capture and return the final buffered audio."""
        self.stop_recording()
        if self.recording_process and self.recording_process.poll() is None:
            self.recording_process.send_signal(signal.SIGINT)
            _, stderr = self.recording_process.communicate(timeout=5)
            self.recording_stderr = stderr.strip()
        elif self.recording_process:
            _, stderr = self.recording_process.communicate(timeout=5)
            self.recording_stderr = stderr.strip()

        if self.stream_reader_thread and self.stream_reader_thread.is_alive():
            self.stream_reader_thread.join(timeout=5)

        if self.recording_process and self.recording_process.returncode not in (0, -2, -15):
            logger.warning(
                "Streaming recorder exited with code %s%s",
                self.recording_process.returncode,
                f": {self.recording_stderr}" if self.recording_stderr else "",
            )

        self.recording_process = None
        self.stream_reader_thread = None
        final_audio = self.get_recording_snapshot()
        self.audio_data = final_audio
        if final_audio is not None and len(final_audio) > 0:
            logger.info(f"Recording complete. Duration: {len(final_audio) / self.sample_rate:.2f}s")
        return final_audio

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
        if self.esp_enabled:
            return self._record_audio_esp(duration, threshold, silence_duration)
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

    def _record_audio_esp(self, duration=None, threshold=0.02, silence_duration=0.5):
        """Push-to-talk recording from ESP TCP mic (16/32-bit stereo)."""
        from src.utils.esp_audio_transport import pcm_stereo_bytes_to_mono_float32

        if not self.esp_host:
            logger.error("ESP host not set")
            return np.array([], dtype=np.float32)

        try:
            sock = socket.create_connection((self.esp_host, self.esp_mic_port), timeout=15)
        except OSError as e:
            logger.error("ESP mic connect failed: %s", e)
            return None

        sock.settimeout(0.35)
        logger.info("Recording from ESP mic (%s-bit stereo)...", self.esp_mic_sample_width * 8)
        chunks = []
        partial = bytearray()
        chunk_samples = int(self.sample_rate * 0.1)
        stereo_frame_bytes = self.esp_mic_sample_width * 2
        frame_bytes = chunk_samples * stereo_frame_bytes
        silence_samples = int(self.sample_rate * silence_duration)
        silence_count = 0

        try:
            while self.is_recording:
                try:
                    data = sock.recv(max(frame_bytes, 4096))
                except socket.timeout:
                    continue
                if not data:
                    break
                partial.extend(data)
                take = len(partial) - (len(partial) % stereo_frame_bytes)
                if take == 0:
                    continue
                block = bytes(partial[:take])
                del partial[:take]
                mono = pcm_stereo_bytes_to_mono_float32(block, self.esp_mic_sample_width)
                if mono.size == 0:
                    continue
                chunks.append(mono)

                amplitude = float(np.max(np.abs(mono))) if mono.size else 0.0
                if amplitude < threshold:
                    silence_count += mono.size
                    if silence_count > silence_samples and len(chunks) > 5:
                        logger.info("Silence detected (ESP), stopping recording")
                        break
                else:
                    silence_count = 0

                if duration and sum(len(c) for c in chunks) > self.sample_rate * duration:
                    logger.info("Max duration reached (ESP)")
                    break
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if not chunks:
            logger.warning("No ESP mic data captured")
            return np.array([], dtype=np.float32)

        audio_data = np.concatenate(chunks)
        self.audio_data = audio_data
        logger.info("ESP recording complete. Duration: %.2fs", len(audio_data) / self.sample_rate)
        return audio_data

    def _start_pw_record_stream(self):
        """Start PipeWire recording to stdout for streaming STT."""
        logger.info(f"Recording audio via pw-record (sample_rate: {self.sample_rate}Hz)...")
        cmd = [
            "pw-record",
            "--media-category",
            "Capture",
            "--media-role",
            "Communication",
            "--rate",
            str(self.sample_rate),
            "--channels",
            "1",
            "--format",
            "s16",
            "-",
        ]
        self.recording_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.stream_reader_thread = threading.Thread(
            target=self._read_pcm_process_stream,
            daemon=True,
        )
        self.stream_reader_thread.start()

    def _start_arecord_stream(self):
        """Start arecord to stdout for streaming STT."""
        logger.info(f"Recording audio via arecord (sample_rate: {self.sample_rate}Hz)...")
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
            "raw",
            "-",
        ]
        self.recording_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.stream_reader_thread = threading.Thread(
            target=self._read_pcm_process_stream,
            daemon=True,
        )
        self.stream_reader_thread.start()

    def _read_pcm_process_stream(self):
        """Read raw 16-bit PCM bytes from an audio subprocess."""
        if not self.recording_process or not self.recording_process.stdout:
            return

        try:
            while self.is_recording:
                chunk = self.recording_process.stdout.read(4096)
                if not chunk:
                    break
                with self.stream_buffer_lock:
                    self.stream_buffer.extend(chunk)
        except Exception as e:
            logger.error(f"Streaming process read error: {e}")

    def _start_sounddevice_stream(self):
        """Start live recording with sounddevice and store float32 bytes."""
        logger.info(f"Recording audio (sample_rate: {self.sample_rate}Hz)...")
        self.stream_reader_thread = threading.Thread(
            target=self._sounddevice_capture_loop,
            daemon=True,
        )
        self.stream_reader_thread.start()

    def _sounddevice_capture_loop(self):
        """Capture float32 chunks from sounddevice."""
        try:
            chunk_size = int(self.sample_rate * 0.1)
            with sd.InputStream(
                channels=1,
                samplerate=self.sample_rate,
                blocksize=chunk_size,
                dtype=np.float32,
            ) as stream:
                while self.is_recording:
                    data, _ = stream.read(chunk_size)
                    chunk = np.asarray(data, dtype=np.float32).reshape(-1)
                    with self.stream_buffer_lock:
                        self.stream_buffer.extend(chunk.tobytes())
        except Exception as e:
            logger.error(f"Sounddevice streaming error: {e}")

    def _start_mock_stream(self):
        """Start synthetic streaming audio generation for development."""
        logger.info("MOCK: Simulating streaming audio recording...")
        self.stream_reader_thread = threading.Thread(
            target=self._mock_stream_loop,
            daemon=True,
        )
        self.stream_reader_thread.start()

    def _mock_stream_loop(self):
        """Generate synthetic float32 chunks while recording."""
        chunk_duration = 0.1
        chunk_samples = int(self.sample_rate * chunk_duration)
        frequency = 440
        try:
            while self.is_recording:
                t = np.linspace(0, chunk_duration, chunk_samples, endpoint=False)
                sine_wave = np.sin(2 * np.pi * frequency * t) * 0.3
                noise = np.random.normal(0, 0.05, chunk_samples)
                chunk = (sine_wave + noise).astype(np.float32)
                with self.stream_buffer_lock:
                    self.stream_buffer.extend(chunk.tobytes())
                time.sleep(chunk_duration * 0.5)
        except Exception as e:
            logger.error(f"Mock streaming error: {e}")

    def _pcm16_bytes_to_float32(self, raw_bytes):
        """Convert little-endian 16-bit PCM bytes to float32 mono audio."""
        if not raw_bytes:
            return np.array([], dtype=np.float32)

        usable_length = len(raw_bytes) - (len(raw_bytes) % 2)
        if usable_length <= 0:
            return np.array([], dtype=np.float32)

        pcm = np.frombuffer(raw_bytes[:usable_length], dtype="<i2").astype(np.float32)
        return pcm / 32768.0

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

    def play_wav_file_to_esp(self, wav_path) -> bool:
        """Send a WAV file to the ESP playback TCP port (16 kHz stereo int16)."""
        if not self.esp_enabled:
            logger.warning("play_wav_file_to_esp called but ESP disabled")
            return False
        from src.utils.esp_audio_transport import stream_wav_to_esp

        return stream_wav_to_esp(self.esp_host, self.esp_play_port, wav_path)

    def play_tts_through_esp(self, tts_service, text: str, language: str) -> bool:
        """Synthesize TTS to a temp WAV and stream it to the ESP speaker."""
        if not text.strip():
            return False
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            if not tts_service.speak(text, language, output_file=path):
                return False
            return self.play_wav_file_to_esp(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

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
