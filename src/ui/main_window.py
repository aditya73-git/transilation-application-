"""Main GUI window for the offline translator"""
import re
import socket
import sys
import time
import soundfile as sf
import numpy as np
from PyQt5.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QTextEdit,
    QStatusBar,
    QProgressBar,
    QComboBox,
    QDialog,
    QFormLayout,
    QDialogButtonBox,
    QCheckBox,
    QSpinBox,
    QDoubleSpinBox,
    QMessageBox,
    QLineEdit,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QFont, QColor, QIcon
from pathlib import Path

from src.services.stt_service import STTService, get_stt_service
from src.services.translation_service import get_translation_service
from src.services.tts_service import get_tts_service
from src.services.language_service import get_language_service
from src.services.connectivity_service import get_connectivity_service
from src.utils.audio_handler import AudioHandler
from src.utils.cache import TranslationCache
from src.utils.logger import get_logger, setup_logger, create_log_file
from src.utils.performance import format_stage_metrics, stage_metrics, summarize_aggregate, take_perf_sample
from src.config import get_config
from src.cloud.claude_client import get_claude_client

logger = get_logger(__name__)


def _tokenize_words(text: str):
    """Split text into whitespace-delimited tokens."""
    return [word for word in text.strip().split() if word]


def _join_words(words):
    """Join tokens back into normalized text."""
    return " ".join(words).strip()


def _append_text(base: str, extra: str) -> str:
    """Append text with a single separating space when needed."""
    base = (base or "").strip()
    extra = (extra or "").strip()
    if not base:
        return extra
    if not extra:
        return base
    return f"{base} {extra}"


def _normalize_token(token: str) -> str:
    """Normalize a token for fuzzy overlap matching."""
    return re.sub(r"(^\W+|\W+$)", "", token).lower()


def _find_overlap_size(left_words, right_words) -> int:
    """Find the largest suffix/prefix token overlap between two transcripts."""
    max_overlap = min(len(left_words), len(right_words))
    for size in range(max_overlap, 1, -1):
        left_slice = [_normalize_token(token) for token in left_words[-size:]]
        right_slice = [_normalize_token(token) for token in right_words[:size]]
        if left_slice == right_slice:
            return size
    return 0


def _split_committable_words(words, finalize: bool = False):
    """Split a stable text region into committed words and leftover unstable tail."""
    if not words:
        return [], []

    boundary_index = -1
    for index, token in enumerate(words):
        if token and token[-1] in ".?!;:":
            boundary_index = index + 1

    if boundary_index > 0:
        return words[:boundary_index], words[boundary_index:]

    if finalize:
        return words, []

    if len(words) >= 8:
        keep_tail = 3
        return words[:-keep_tail], words[-keep_tail:]

    return [], words


def _advance_incremental_transcript(pending_text: str, latest_text: str):
    """Commit stable prefix words and keep only the unstable live tail."""
    pending_words = _tokenize_words(pending_text)
    latest_words = _tokenize_words(latest_text)

    if not latest_words:
        return "", pending_text
    if not pending_words:
        return "", _join_words(latest_words)

    overlap = _find_overlap_size(pending_words, latest_words)
    if overlap > 0:
        stable_words = pending_words[:-overlap]
        merged_pending = pending_words[-overlap:] + latest_words[overlap:]
    else:
        stable_words = []
        merged_pending = latest_words

    commit_words, leftover_words = _split_committable_words(stable_words, finalize=False)
    next_pending = leftover_words + merged_pending
    return _join_words(commit_words), _join_words(next_pending)


def _finalize_incremental_transcript(pending_text: str, final_tail_text: str) -> str:
    """Merge the last tail transcription into the pending transcript and finalize it."""
    pending_words = _tokenize_words(pending_text)
    final_words = _tokenize_words(final_tail_text)

    if not pending_words:
        return _join_words(final_words)
    if not final_words:
        return _join_words(pending_words)

    overlap = _find_overlap_size(pending_words, final_words)
    if overlap > 0:
        merged_words = pending_words + final_words[overlap:]
    else:
        merged_words = pending_words + final_words

    commit_words, leftover_words = _split_committable_words(merged_words, finalize=True)
    return _join_words(commit_words + leftover_words)


def _save_stt_debug_audio(audio_data, sample_rate: int, label: str) -> str | None:
    """Persist the exact audio clip being sent to STT for listening/debugging."""
    if not get_config().get("ui.audio_log_enabled", True):
        return None
    try:
        debug_dir = Path("logs") / "audio_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_") or "stt_input"
        latest_path = debug_dir / f"{safe_label}.wav"
        archived_path = debug_dir / f"{safe_label}_{time.strftime('%Y%m%d_%H%M%S')}.wav"
        sf.write(latest_path, audio_data, sample_rate)
        sf.write(archived_path, audio_data, sample_rate)
        logger.info("Saved STT debug audio to %s", latest_path)
        return str(latest_path.resolve())
    except Exception as e:
        logger.warning("Failed to save STT debug audio '%s': %s", label, e)
        return None


class RecordingWorker(QThread):
    """Worker thread for microphone capture."""

    work_finished = pyqtSignal()
    error = pyqtSignal(str)
    audio_ready = pyqtSignal(object)

    def __init__(self, audio_handler, max_duration: int):
        super().__init__()
        self.audio_handler = audio_handler
        self.max_duration = max_duration

    def run(self):
        """Capture audio until the button is released or max duration is reached."""
        try:
            audio_data = self.audio_handler.record_audio(duration=self.max_duration)
            self.audio_ready.emit(audio_data)
        except Exception as e:
            logger.error(f"Recording error: {e}")
            self.error.emit(f"Recording failed: {str(e)}")
        finally:
            self.work_finished.emit()


class ESPButtonListener(QThread):
    """Listen for hardware button events from the ESP bridge."""

    button_event = pyqtSignal(str)
    connection_changed = pyqtSignal(bool)

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = port
        self._running = True
        self._sock = None

    def stop(self):
        """Stop the listener and close any active socket."""
        self._running = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def run(self):
        """Reconnect as needed and emit line-based button events."""
        while self._running:
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.settimeout(0.5)
                self._sock = sock
                self.connection_changed.emit(True)
                logger.info("ESP button listener connected to %s:%s", self.host, self.port)
                pending = bytearray()
                while self._running:
                    try:
                        chunk = sock.recv(256)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    pending.extend(chunk)
                    while b"\n" in pending:
                        raw_line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        event = raw_line.decode("utf-8", errors="ignore").strip().upper()
                        if event:
                            self.button_event.emit(event)
            except OSError as exc:
                if self._running:
                    logger.debug("ESP button listener reconnecting: %s", exc)
            finally:
                self._sock = None
                self.connection_changed.emit(False)
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if self._running:
                time.sleep(1.0)


class ConversationRecordingWorker(QThread):
    """Record one conversation turn and stop automatically after trailing silence."""

    work_finished = pyqtSignal()
    error = pyqtSignal(str)
    audio_ready = pyqtSignal(object)
    progress = pyqtSignal(str)
    partial_ready = pyqtSignal(str)

    def __init__(
        self,
        audio_handler,
        stt_service,
        source_lang: str,
        source_lang_code: str,
        max_duration: int,
        silence_threshold: float,
        silence_duration: float,
        partial_interval: float,
        partial_step_seconds: float,
        partial_window_seconds: float,
    ):
        super().__init__()
        self.audio_handler = audio_handler
        self.stt_service = stt_service
        self.source_lang = source_lang
        self.source_lang_code = source_lang_code
        self.max_duration = max_duration
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.stop_requested = False
        self.min_speech_seconds = 0.4
        self.poll_interval = 0.1
        self.partial_interval = partial_interval
        self.partial_step_seconds = partial_step_seconds
        self.partial_window_seconds = partial_window_seconds
        self.speech_detected = False
        self.silence_samples = 0
        self.last_snapshot_samples = 0
        self.last_partial_samples = 0
        self.last_partial_at = 0.0
        self.committed_source_text = ""
        self.pending_source_text = ""
        self.last_display_text = ""

    def stop(self):
        """Stop the current conversation turn capture."""
        self.stop_requested = True
        self.audio_handler.stop_recording()

    def run(self):
        """Capture a single turn until silence is detected."""
        try:
            self.progress.emit(
                f"Conversation mode: listening for {self.source_lang}. Pause to end your turn."
            )
            self.audio_handler.start_stream_recording()
            start_time = time.time()
            silence_limit = int(self.audio_handler.sample_rate * self.silence_duration)
            min_speech_samples = int(self.audio_handler.sample_rate * self.min_speech_seconds)

            while not self.stop_requested:
                if time.time() - start_time >= self.max_duration:
                    self.progress.emit("Conversation turn reached max duration")
                    break

                time.sleep(self.poll_interval)
                snapshot = self.audio_handler.get_recording_snapshot()
                if snapshot is None:
                    continue
                if len(snapshot) <= self.last_snapshot_samples:
                    continue

                new_chunk = snapshot[self.last_snapshot_samples:]
                self.last_snapshot_samples = len(snapshot)
                if new_chunk.size == 0:
                    continue

                amplitude = float(np.max(np.abs(new_chunk)))
                if amplitude >= self.silence_threshold:
                    if not self.speech_detected:
                        self.progress.emit("Speech detected")
                    self.speech_detected = True
                    self.silence_samples = 0
                    continue

                if self.speech_detected:
                    self.silence_samples += len(new_chunk)
                    if len(snapshot) >= min_speech_samples and self.silence_samples >= silence_limit:
                        self.progress.emit("Conversation turn ended by silence")
                        break

                if (
                    self.speech_detected
                    and self.stt_service is not None
                    and len(snapshot) - self.last_partial_samples
                    >= int(self.partial_step_seconds * self.audio_handler.sample_rate)
                    and (time.time() - self.last_partial_at) >= self.partial_interval
                ):
                    window_samples = int(self.partial_window_seconds * self.audio_handler.sample_rate)
                    live_window = snapshot[-window_samples:] if len(snapshot) > window_samples else snapshot
                    text, _, _ = self.stt_service.transcribe(
                        live_window,
                        language=self.source_lang_code,
                    )
                    self.last_partial_samples = len(snapshot)
                    self.last_partial_at = time.time()
                    if text:
                        committed_chunk, self.pending_source_text = _advance_incremental_transcript(
                            self.pending_source_text,
                            text,
                        )
                        if committed_chunk:
                            self.committed_source_text = _append_text(
                                self.committed_source_text,
                                committed_chunk,
                            )

                        display_text = _append_text(
                            self.committed_source_text,
                            self.pending_source_text,
                        )
                        if display_text and display_text != self.last_display_text:
                            self.last_display_text = display_text
                            self.partial_ready.emit(display_text)

            audio_data = self.audio_handler.stop_stream_recording()
            if self.stop_requested:
                return
            if audio_data is None or len(audio_data) == 0:
                self.error.emit("Failed to capture audio. Try again.")
                return
            if not self.speech_detected:
                self.error.emit("No speech detected. Try again.")
                return
            self.audio_ready.emit(audio_data)
        except Exception as e:
            logger.error(f"Conversation recording error: {e}")
            self.error.emit(f"Conversation recording failed: {str(e)}")
        finally:
            self.work_finished.emit()


class TranslationWarmupWorker(QThread):
    """Warm the translation route for the currently selected pair."""

    warmed = pyqtSignal(str, str)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, translation_service, source_lang: str, target_lang: str):
        super().__init__()
        self.translation_service = translation_service
        self.source_lang = source_lang
        self.target_lang = target_lang

    def run(self):
        """Load direct or pivot translation models in the background."""
        try:
            self.progress.emit(f"Warming translation route for {self.source_lang} -> {self.target_lang}...")
            self.translation_service.warm_pair(self.source_lang, self.target_lang)
            self.warmed.emit(self.source_lang, self.target_lang)
        except Exception as e:
            logger.error(f"Translation warmup error: {e}")
            self.error.emit(f"Translation warmup failed: {str(e)}")


class STTBenchmarkWorker(QThread):
    """Benchmark the same captured audio on CPU and CUDA STT backends."""

    work_finished = pyqtSignal()
    progress = pyqtSignal(str)

    def __init__(
        self,
        audio_data,
        source_lang_code: str,
        model_name: str,
        active_device: str,
        active_compute_type: str,
        cpu_compute_type: str,
        gpu_compute_type: str,
        cpu_threads: int,
        num_workers: int,
        beam_size: int,
        vad_filter: bool,
    ):
        super().__init__()
        self.audio_data = audio_data
        self.source_lang_code = source_lang_code
        self.model_name = model_name
        self.active_device = active_device
        self.active_compute_type = active_compute_type
        self.cpu_compute_type = cpu_compute_type
        self.gpu_compute_type = gpu_compute_type
        self.cpu_threads = cpu_threads
        self.num_workers = num_workers
        self.beam_size = beam_size
        self.vad_filter = vad_filter

    def run(self):
        """Benchmark CPU and CUDA STT sequentially on the same audio."""
        results = {
            self.active_device: {
                "wall_seconds": 0.0,
                "self_cpu_seconds": 0.0,
                "child_cpu_seconds": 0.0,
                "total_cpu_seconds": 0.0,
                "avg_cpu_percent": 0.0,
                "current_rss_mib": 0.0,
                "self_peak_rss_mib": 0.0,
                "child_peak_rss_mib": 0.0,
            }
        }
        target_devices = []
        if self.active_device == "cuda":
            target_devices.append(("cpu", self.cpu_compute_type))
        elif self.active_device == "cpu":
            target_devices.append(("cuda", self.gpu_compute_type))
        else:
            target_devices.extend((("cpu", self.cpu_compute_type), ("cuda", self.gpu_compute_type)))

        for device, compute_type in target_devices:
            service = None
            try:
                self.progress.emit(f"Benchmarking STT on {device} ({compute_type})...")
                load_start = take_perf_sample()
                service = STTService(
                    device=device,
                    compute_type=compute_type,
                    model_name=self.model_name,
                    cpu_threads=self.cpu_threads,
                    num_workers=self.num_workers,
                    beam_size=self.beam_size,
                    vad_filter=self.vad_filter,
                )
                load_end = take_perf_sample()

                transcribe_start = take_perf_sample()
                text, detected_language, confidence = service.transcribe(
                    self.audio_data,
                    language=self.source_lang_code,
                )
                transcribe_end = take_perf_sample()

                load_metrics = stage_metrics(load_start, load_end)
                run_metrics = stage_metrics(transcribe_start, transcribe_end)
                results[device] = run_metrics

                self.progress.emit(format_stage_metrics(f"Benchmark STT {device} load", load_start, load_end))
                self.progress.emit(
                    format_stage_metrics(f"Benchmark STT {device}", transcribe_start, transcribe_end)
                )
                preview = text[:80] + ("..." if len(text) > 80 else "")
                self.progress.emit(
                    f"Benchmark | STT {device} result: lang={detected_language} conf={confidence:.2f} "
                    f"text={preview}"
                )
                if load_metrics["wall_seconds"] > 0:
                    self.progress.emit(
                        f"Benchmark | STT {device} load summary: wall={load_metrics['wall_seconds']:.2f}s "
                        f"peak={load_metrics['self_peak_rss_mib']:.1f}MiB"
                    )
            except Exception as e:
                logger.warning("STT benchmark failed on %s: %s", device, e)
                self.progress.emit(f"Benchmark | STT {device} unavailable: {e}")
            finally:
                if service is not None:
                    service.unload_model()

        cpu_metrics = results.get("cpu")
        gpu_metrics = results.get("cuda")
        if cpu_metrics and gpu_metrics and cpu_metrics["wall_seconds"] > 0 and gpu_metrics["wall_seconds"] > 0:
            speedup = cpu_metrics["wall_seconds"] / gpu_metrics["wall_seconds"]
            self.progress.emit(
                f"Benchmark | STT cuda_vs_cpu speedup={speedup:.2f}x "
                f"(cpu={cpu_metrics['wall_seconds']:.2f}s, cuda={gpu_metrics['wall_seconds']:.2f}s)"
            )

        self.work_finished.emit()


class StreamingSTTWorker(QThread):
    """Worker thread for near-real-time STT updates while recording."""

    work_finished = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    partial_ready = pyqtSignal(str)
    final_ready = pyqtSignal(str, str)

    def __init__(
        self,
        audio_handler,
        stt_service,
        source_lang,
        source_lang_code,
        max_duration: int,
        partial_interval: float,
        partial_step_seconds: float,
        partial_window_seconds: float,
    ):
        super().__init__()
        self.audio_handler = audio_handler
        self.stt_service = stt_service
        self.source_lang = source_lang
        self.source_lang_code = source_lang_code
        self.max_duration = max_duration
        self.stop_requested = False
        self.partial_interval = partial_interval
        self.min_audio_seconds = 0.75
        self.partial_step_seconds = partial_step_seconds
        self.partial_window_seconds = partial_window_seconds
        self.committed_source_text = ""
        self.pending_source_text = ""
        self.last_display_text = ""
        self.last_processed_samples = 0

    def stop(self):
        """Request the streaming loop to stop."""
        self.stop_requested = True
        self.audio_handler.stop_recording()

    def run(self):
        """Capture audio and emit partial transcripts while recording."""
        start_time = time.time()
        final_audio = None

        try:
            self.progress.emit(f"Listening for {self.source_lang}...")
            self.audio_handler.start_stream_recording()

            while not self.stop_requested:
                if time.time() - start_time >= self.max_duration:
                    self.progress.emit("Max recording duration reached")
                    break

                time.sleep(self.partial_interval)
                snapshot = self.audio_handler.get_recording_snapshot()
                if snapshot is None:
                    continue
                if len(snapshot) < int(self.min_audio_seconds * self.audio_handler.sample_rate):
                    continue
                if len(snapshot) - self.last_processed_samples < int(self.partial_step_seconds * self.audio_handler.sample_rate):
                    continue

                window_samples = int(self.partial_window_seconds * self.audio_handler.sample_rate)
                live_window = snapshot[-window_samples:] if len(snapshot) > window_samples else snapshot
                text, _, _ = self.stt_service.transcribe(live_window, language=self.source_lang_code)
                self.last_processed_samples = len(snapshot)
                if not text:
                    continue

                committed_chunk, self.pending_source_text = _advance_incremental_transcript(
                    self.pending_source_text,
                    text,
                )
                if committed_chunk:
                    self.committed_source_text = _append_text(self.committed_source_text, committed_chunk)

                display_text = _append_text(self.committed_source_text, self.pending_source_text)
                if display_text and display_text != self.last_display_text:
                    self.last_display_text = display_text
                    self.partial_ready.emit(display_text)

            final_audio = self.audio_handler.stop_stream_recording()
            if final_audio is None or len(final_audio) == 0:
                self.error.emit("Failed to capture audio. Try again.")
                return

            self.progress.emit("Finalizing transcription...")
            debug_audio_path = _save_stt_debug_audio(
                final_audio,
                self.audio_handler.sample_rate,
                "last_streaming_stt_input",
            )
            if debug_audio_path:
                self.progress.emit(f"Saved STT input audio: {debug_audio_path}")
            final_text, _, _ = self.stt_service.transcribe(final_audio, language=self.source_lang_code)
            if not final_text:
                tail_samples = int(
                    max(
                        self.partial_window_seconds + self.partial_step_seconds,
                        self.partial_window_seconds,
                    )
                    * self.audio_handler.sample_rate
                )
                final_tail_audio = (
                    final_audio[-tail_samples:] if len(final_audio) > tail_samples else final_audio
                )
                tail_audio_path = _save_stt_debug_audio(
                    final_tail_audio,
                    self.audio_handler.sample_rate,
                    "last_streaming_stt_tail_fallback",
                )
                if tail_audio_path:
                    self.progress.emit(f"Saved STT tail fallback audio: {tail_audio_path}")
                final_tail_text, _, _ = self.stt_service.transcribe(
                    final_tail_audio,
                    language=self.source_lang_code,
                )
                final_text = _append_text(
                    self.committed_source_text,
                    _finalize_incremental_transcript(self.pending_source_text, final_tail_text),
                )
            if not final_text:
                self.error.emit("Failed to recognize speech. Try again.")
                return

            self.final_ready.emit(final_text, f"{time.time() - start_time:.2f}s")

        except Exception as e:
            logger.error(f"Streaming STT error: {e}")
            self.error.emit(f"Error: {str(e)}")
        finally:
            self.audio_handler.stop_recording()
            self.work_finished.emit()


class StreamingPipelineWorker(QThread):
    """Worker thread for streaming STT plus live translation updates."""

    work_finished = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    partial_update = pyqtSignal(str, str)  # source_text, translated_text
    final_result_ready = pyqtSignal(str, str, str)  # source_text, translated_text, elapsed
    cloud_refinement_ready = pyqtSignal(str, str, str, str, str)  # source_text, refined_text, source_lang, target_lang, elapsed

    def __init__(
        self,
        audio_handler,
        stt_service,
        translation_service,
        tts_service,
        language_service,
        cache,
        claude_client,
        stt_only,
        max_duration: int,
        partial_interval: float,
        partial_step_seconds: float,
        partial_window_seconds: float,
        auto_play_output: bool,
        conversation_mode: bool = False,
    ):
        super().__init__()
        self.audio_handler = audio_handler
        self.stt_service = stt_service
        self.translation_service = translation_service
        self.tts_service = tts_service
        self.language_service = language_service
        self.cache = cache
        self.claude_client = claude_client
        self.stt_only = stt_only
        self.max_duration = max_duration
        self.stop_requested = False
        self.partial_interval = partial_interval
        self.min_audio_seconds = 0.75
        self.partial_step_seconds = partial_step_seconds
        self.partial_window_seconds = partial_window_seconds
        self.auto_play_output = auto_play_output
        self.conversation_mode = conversation_mode
        self.last_processed_samples = 0
        self.last_source_text = ""
        self.last_translated_text = ""
        self.partial_stt_runs = 0
        self.partial_stt_wall = 0.0
        self.partial_stt_cpu = 0.0
        self.partial_translation_runs = 0
        self.partial_translation_wall = 0.0
        self.partial_translation_cpu = 0.0
        self.committed_source_text = ""
        self.pending_source_text = ""
        self.committed_translation_text = ""
        self.last_display_source = ""
        self.last_display_translation = ""

    def stop(self):
        """Request the streaming loop to stop."""
        self.stop_requested = True
        self.audio_handler.stop_recording()

    def _translate_for_display(self, text: str, source_lang: str, target_lang: str):
        """Translate text for live display, preferring cached results."""
        if self.stt_only:
            return "STT-only mode: translation skipped"

        cached = self.cache.get_best(text, source_lang, target_lang)
        if cached:
            return cached["translated_text"]

        translated_text, _ = self.translation_service.translate(text, source_lang, target_lang)
        if translated_text:
            self.cache.set(text, source_lang, target_lang, translated_text)
        return translated_text

    def _emit_partial_display(self):
        """Update the GUI with committed text and the current unstable tail."""
        source_text = _append_text(self.committed_source_text, self.pending_source_text)
        translated_text = self.committed_translation_text or "Listening..."
        if self.pending_source_text:
            translated_text = _append_text(translated_text, "...")

        if (
            source_text
            and (
                source_text != self.last_display_source
                or translated_text != self.last_display_translation
            )
        ):
            self.last_display_source = source_text
            self.last_display_translation = translated_text
            self.partial_update.emit(source_text, translated_text)

    def run(self):
        """Capture audio, emit partial transcripts/translations, then finalize."""
        start_time = time.time()
        pipeline_start = take_perf_sample()
        source_lang, target_lang = self.language_service.get_current_pair()
        source_lang_code = self.language_service.get_language_code(source_lang)

        try:
            self.progress.emit(f"Listening for {source_lang}...")
            self.audio_handler.start_stream_recording()

            self.speech_detected = False
            self.silence_samples = 0

            while not self.stop_requested:
                if time.time() - start_time >= self.max_duration:
                    self.progress.emit("Max recording duration reached")
                    break

                time.sleep(self.partial_interval)
                snapshot = self.audio_handler.get_recording_snapshot()
                if snapshot is None:
                    continue
                if len(snapshot) < int(self.min_audio_seconds * self.audio_handler.sample_rate):
                    continue
                if len(snapshot) - self.last_processed_samples < int(self.partial_step_seconds * self.audio_handler.sample_rate):
                    continue

                window_samples = int(self.partial_window_seconds * self.audio_handler.sample_rate)
                live_window = snapshot[-window_samples:] if len(snapshot) > window_samples else snapshot
                stt_start = take_perf_sample()

                if self.conversation_mode:
                    amplitude = float(np.max(np.abs(live_window))) if live_window.size else 0.0
                    if amplitude > 0.02:
                        self.speech_detected = True
                        self.silence_samples = 0
                    elif getattr(self, "speech_detected", False):
                        self.silence_samples += (len(snapshot) - self.last_processed_samples)
                        if self.silence_samples > int(1.5 * self.audio_handler.sample_rate):
                            self.progress.emit("End of turn detected (silence)")
                            break

                transcribe_lang = None if self.conversation_mode else source_lang_code
                text, _, _ = self.stt_service.transcribe(live_window, language=transcribe_lang)
                stt_end = take_perf_sample()
                stt_metrics = stage_metrics(stt_start, stt_end)
                self.partial_stt_runs += 1
                self.partial_stt_wall += stt_metrics["wall_seconds"]
                self.partial_stt_cpu += stt_metrics["total_cpu_seconds"]
                self.last_processed_samples = len(snapshot)
                if not text:
                    continue

                committed_chunk, self.pending_source_text = _advance_incremental_transcript(
                    self.pending_source_text,
                    text,
                )

                if committed_chunk:
                    self.committed_source_text = _append_text(self.committed_source_text, committed_chunk)
                    translate_start = take_perf_sample()
                    translated_chunk = self._translate_for_display(committed_chunk, source_lang, target_lang)
                    translate_end = take_perf_sample()
                    translate_metrics = stage_metrics(translate_start, translate_end)
                    self.partial_translation_runs += 1
                    self.partial_translation_wall += translate_metrics["wall_seconds"]
                    self.partial_translation_cpu += translate_metrics["total_cpu_seconds"]
                    if translated_chunk:
                        self.committed_translation_text = _append_text(
                            self.committed_translation_text,
                            translated_chunk,
                        )

                self._emit_partial_display()

            final_audio = self.audio_handler.stop_stream_recording()
            if final_audio is None or len(final_audio) == 0:
                self.error.emit("Failed to capture audio. Try again.")
                return

            self.progress.emit("Finalizing transcription...")
            debug_audio_path = _save_stt_debug_audio(
                final_audio,
                self.audio_handler.sample_rate,
                "last_streaming_pipeline_stt_input",
            )
            if debug_audio_path:
                self.progress.emit(f"Saved STT input audio: {debug_audio_path}")
            final_stt_start = take_perf_sample()
            transcribe_lang = None if self.conversation_mode else source_lang_code
            final_text, final_detected_lang, _ = self.stt_service.transcribe(final_audio, language=transcribe_lang)
            if not final_text:
                tail_samples = int(
                    max(
                        self.partial_window_seconds + self.partial_step_seconds,
                        self.partial_window_seconds,
                    )
                    * self.audio_handler.sample_rate
                )
                final_tail_audio = (
                    final_audio[-tail_samples:] if len(final_audio) > tail_samples else final_audio
                )
                tail_audio_path = _save_stt_debug_audio(
                    final_tail_audio,
                    self.audio_handler.sample_rate,
                    "last_streaming_pipeline_stt_tail_fallback",
                )
                if tail_audio_path:
                    self.progress.emit(f"Saved STT tail fallback audio: {tail_audio_path}")
                final_tail_text, tail_detected_lang, _ = self.stt_service.transcribe(
                    final_tail_audio,
                    language=transcribe_lang,
                )
                if not final_text:
                    final_detected_lang = tail_detected_lang
                final_text = _append_text(
                    self.committed_source_text,
                    _finalize_incremental_transcript(self.pending_source_text, final_tail_text),
                )
            final_stt_end = take_perf_sample()
            if not final_text:
                self.error.emit("Failed to recognize speech. Try again.")
                return

            if self.conversation_mode:
                target_code = self.language_service.get_language_code(target_lang)
                if final_detected_lang == target_code:
                    source_lang, target_lang = target_lang, source_lang

            final_translation_start = take_perf_sample()
            final_translated = self._translate_for_display(
                final_text,
                source_lang,
                target_lang,
            )
            final_translation_end = take_perf_sample()
            if not final_translated:
                self.error.emit("Translation failed. Try again.")
                return

            if not self.stt_only and self.auto_play_output:
                self.progress.emit("Converting to speech...")
                tts_start = take_perf_sample()
                if self.audio_handler.esp_enabled:
                    ok = self.audio_handler.play_tts_through_esp(
                        self.tts_service, final_translated, target_lang
                    )
                else:
                    ok = self.tts_service.speak(final_translated, target_lang)
                if not ok:
                    self.error.emit("Text-to-speech failed.")
                    return
                tts_end = take_perf_sample()
            else:
                tts_start = None
                tts_end = None
                if not self.stt_only and not self.auto_play_output:
                    self.progress.emit("Auto-play disabled; skipping speech output")

            elapsed = f"{time.time() - start_time:.2f}s"
            pipeline_end = take_perf_sample()

            self.progress.emit(
                format_stage_metrics("Final STT", final_stt_start, final_stt_end)
            )
            self.progress.emit(
                format_stage_metrics("Final translation", final_translation_start, final_translation_end)
            )
            if self.partial_stt_runs:
                self.progress.emit(
                    summarize_aggregate(
                        "Live STT",
                        self.partial_stt_wall,
                        self.partial_stt_cpu,
                        self.partial_stt_runs,
                    )
                )
            if self.partial_translation_runs:
                self.progress.emit(
                    summarize_aggregate(
                        "Live translation",
                        self.partial_translation_wall,
                        self.partial_translation_cpu,
                        self.partial_translation_runs,
                    )
                )
            if tts_start is not None and tts_end is not None:
                self.progress.emit(format_stage_metrics("TTS", tts_start, tts_end))
            self.progress.emit(format_stage_metrics("Pipeline total", pipeline_start, pipeline_end))

            cached = self.cache.get_best(final_text, source_lang, target_lang)
            if self.claude_client.is_enabled() and not (cached and cached["cloud_refined"]):
                self.progress.emit("Queuing for cloud refinement...")
                refine_started = time.time()

                def notify_refined(refined):
                    refine_elapsed = f"{time.time() - refine_started:.2f}s"
                    self.cloud_refinement_ready.emit(
                        final_text,
                        refined,
                        source_lang,
                        target_lang,
                        refine_elapsed,
                    )

                self.claude_client.refine_translation_async(
                    final_text,
                    final_translated,
                    source_lang,
                    target_lang,
                    callback=notify_refined,
                )

            self.final_result_ready.emit(final_text, final_translated, elapsed)

        except Exception as e:
            logger.error(f"Streaming pipeline error: {e}")
            self.error.emit(f"Error: {str(e)}")
        finally:
            self.audio_handler.stop_recording()
            self.work_finished.emit()


class TranslationWorker(QThread):
    """Worker thread for running translation pipeline"""

    work_finished = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    result_ready = pyqtSignal(str, str, str)  # source_text, translated_text, time_taken
    cloud_refinement_ready = pyqtSignal(str, str, str, str, str)  # source_text, refined_text, source_lang, target_lang, elapsed

    def __init__(
        self,
        audio_data,
        stt_service,
        translation_service,
        tts_service,
        language_service,
        audio_handler,
        cache,
        claude_client,
        stt_only=False,
        auto_play_output=True,
    ):
        super().__init__()
        self.audio_data = audio_data
        self.stt_service = stt_service
        self.translation_service = translation_service
        self.tts_service = tts_service
        self.language_service = language_service
        self.audio_handler = audio_handler
        self.cache = cache
        self.claude_client = claude_client
        self.stt_only = stt_only
        self.auto_play_output = auto_play_output

    def run(self):
        """Run translation pipeline in background"""
        start_time = time.time()
        pipeline_start = take_perf_sample()

        try:
            source_lang, target_lang = self.language_service.get_current_pair()
            source_lang_code = self.language_service.get_language_code(source_lang)

            # Step 1: Speech-to-Text
            self.progress.emit(f"Running speech-to-text for {source_lang}...")
            debug_audio_path = _save_stt_debug_audio(
                self.audio_data,
                self.audio_handler.sample_rate,
                "last_stt_input",
            )
            if debug_audio_path:
                self.progress.emit(f"Saved STT input audio: {debug_audio_path}")
            stt_start = take_perf_sample()
            text, detected_lang, confidence = self.stt_service.transcribe(
                self.audio_data,
                language=source_lang_code,
            )
            stt_end = take_perf_sample()

            if not text:
                self.error.emit("Failed to recognize speech. Try again.")
                return

            self.progress.emit(f"Recognized: {text[:50]}...")

            if self.stt_only:
                elapsed = time.time() - start_time
                pipeline_end = take_perf_sample()
                self.progress.emit(format_stage_metrics("STT", stt_start, stt_end))
                self.progress.emit(format_stage_metrics("Pipeline total", pipeline_start, pipeline_end))
                self.progress.emit("STT test complete")
                self.result_ready.emit(text, "STT-only mode: translation skipped", f"{elapsed:.2f}s")
                return

            # Step 2: Translation
            self.progress.emit("Translating...")

            # Check cache first
            cached = self.cache.get_best(text, source_lang, target_lang)
            if cached:
                translated_text = cached["translated_text"]
                if cached["cloud_refined"]:
                    self.progress.emit("Using cached cloud-refined translation")
                else:
                    self.progress.emit("Using cached translation")
            else:
                translation_start = take_perf_sample()
                translated_text, _ = self.translation_service.translate(text, source_lang, target_lang)
                translation_end = take_perf_sample()

                if not translated_text:
                    self.error.emit("Translation failed. Try again.")
                    return

                # Cache the translation
                self.cache.set(text, source_lang, target_lang, translated_text)
            if cached:
                translation_start = take_perf_sample()
                translation_end = translation_start

            self.progress.emit(f"Translated: {translated_text[:50]}...")

            # Step 3: Text-to-Speech
            if self.auto_play_output:
                self.progress.emit("Converting to speech...")
                tts_start = take_perf_sample()
                if self.audio_handler.esp_enabled:
                    ok = self.audio_handler.play_tts_through_esp(
                        self.tts_service, translated_text, target_lang
                    )
                else:
                    ok = self.tts_service.speak(translated_text, target_lang)
                if not ok:
                    self.error.emit("Text-to-speech failed.")
                    return
                tts_end = take_perf_sample()
            else:
                self.progress.emit("Auto-play disabled; skipping speech output")
                tts_start = None
                tts_end = None

            elapsed = time.time() - start_time
            pipeline_end = take_perf_sample()
            self.progress.emit(format_stage_metrics("STT", stt_start, stt_end))
            self.progress.emit(format_stage_metrics("Translation", translation_start, translation_end))
            if tts_start is not None and tts_end is not None:
                self.progress.emit(format_stage_metrics("TTS", tts_start, tts_end))
            self.progress.emit(format_stage_metrics("Pipeline total", pipeline_start, pipeline_end))
            self.progress.emit(f"Complete in {elapsed:.2f}s")

            # Step 4: Cloud refinement (async, non-blocking)
            if self.claude_client.is_enabled() and not (cached and cached["cloud_refined"]):
                self.progress.emit("Queuing for cloud refinement...")
                refine_started = time.time()

                def notify_refined(refined):
                    elapsed = f"{time.time() - refine_started:.2f}s"
                    self.cloud_refinement_ready.emit(
                        text,
                        refined,
                        source_lang,
                        target_lang,
                        elapsed,
                    )

                self.claude_client.refine_translation_async(
                    text,
                    translated_text,
                    source_lang,
                    target_lang,
                    callback=notify_refined,
                )

            # Emit result
            self.result_ready.emit(text, translated_text, f"{elapsed:.2f}s")

        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            self.error.emit(f"Error: {str(e)}")

        finally:
            self.work_finished.emit()


class SettingsDialog(QDialog):
    """Application settings dialog."""

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(520, 460)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.stt_only_checkbox = QCheckBox("Run STT only")
        self.stt_only_checkbox.setChecked(settings["stt_only_mode"])
        form.addRow("Mode", self.stt_only_checkbox)

        self.stt_device_combo = QComboBox()
        self.stt_device_combo.addItem("CPU", "cpu")
        self.stt_device_combo.addItem("CUDA / GPU", "cuda")
        index = self.stt_device_combo.findData(settings["stt_device"])
        self.stt_device_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("STT device", self.stt_device_combo)

        self.stt_benchmark_checkbox = QCheckBox("Benchmark same audio on CPU and GPU after each run")
        self.stt_benchmark_checkbox.setChecked(settings["stt_benchmark_cpu_gpu"])
        form.addRow("STT benchmark", self.stt_benchmark_checkbox)

        self.live_streaming_checkbox = QCheckBox("Stream STT/translation while holding button")
        self.live_streaming_checkbox.setChecked(settings["live_streaming"])
        form.addRow("Live mode", self.live_streaming_checkbox)

        self.auto_play_checkbox = QCheckBox("Speak translated output automatically")
        self.auto_play_checkbox.setChecked(settings["auto_play_output"])
        form.addRow("Auto-play", self.auto_play_checkbox)

        self.audio_log_checkbox = QCheckBox("Save input audio clips for debugging")
        self.audio_log_checkbox.setChecked(settings["audio_log_enabled"])
        form.addRow("Audio log", self.audio_log_checkbox)

        self.show_logs_checkbox = QCheckBox("Show log panel")
        self.show_logs_checkbox.setChecked(settings["show_logs"])
        form.addRow("Logs", self.show_logs_checkbox)

        self.max_duration_spin = QSpinBox()
        self.max_duration_spin.setRange(5, 300)
        self.max_duration_spin.setValue(settings["max_duration"])
        self.max_duration_spin.setSuffix(" s")
        form.addRow("Max recording", self.max_duration_spin)

        self.silence_threshold_spin = QDoubleSpinBox()
        self.silence_threshold_spin.setRange(0.001, 0.200)
        self.silence_threshold_spin.setSingleStep(0.001)
        self.silence_threshold_spin.setDecimals(3)
        self.silence_threshold_spin.setValue(settings["silence_threshold"])
        form.addRow("Silence threshold", self.silence_threshold_spin)

        self.silence_duration_spin = QDoubleSpinBox()
        self.silence_duration_spin.setRange(0.2, 5.0)
        self.silence_duration_spin.setSingleStep(0.1)
        self.silence_duration_spin.setValue(settings["silence_duration"])
        self.silence_duration_spin.setSuffix(" s")
        form.addRow("Silence end delay", self.silence_duration_spin)

        self.partial_interval_spin = QDoubleSpinBox()
        self.partial_interval_spin.setRange(0.5, 10.0)
        self.partial_interval_spin.setSingleStep(0.5)
        self.partial_interval_spin.setValue(settings["partial_interval"])
        self.partial_interval_spin.setSuffix(" s")
        form.addRow("Live poll interval", self.partial_interval_spin)

        self.partial_step_spin = QDoubleSpinBox()
        self.partial_step_spin.setRange(0.5, 15.0)
        self.partial_step_spin.setSingleStep(0.5)
        self.partial_step_spin.setValue(settings["partial_step_seconds"])
        self.partial_step_spin.setSuffix(" s")
        form.addRow("Live chunk advance", self.partial_step_spin)

        self.partial_window_spin = QDoubleSpinBox()
        self.partial_window_spin.setRange(2.0, 30.0)
        self.partial_window_spin.setSingleStep(1.0)
        self.partial_window_spin.setValue(settings["partial_window_seconds"])
        self.partial_window_spin.setSuffix(" s")
        form.addRow("Live window size", self.partial_window_spin)

        self.connectivity_interval_spin = QSpinBox()
        self.connectivity_interval_spin.setRange(5, 300)
        self.connectivity_interval_spin.setValue(settings["connectivity_interval"])
        self.connectivity_interval_spin.setSuffix(" s")
        form.addRow("Connectivity check", self.connectivity_interval_spin)

        self.esp_enable_checkbox = QCheckBox("Use ReSpeaker ESP bridge")
        self.esp_enable_checkbox.setChecked(settings.get("esp_enabled", False))
        form.addRow("ESP bridge", self.esp_enable_checkbox)
        self.esp_transport_combo = QComboBox()
        self.esp_transport_combo.addItem("Bluetooth LE", "ble")
        self.esp_transport_combo.addItem("Wi-Fi TCP", "wifi")
        transport_index = self.esp_transport_combo.findData(settings.get("esp_transport", "ble"))
        self.esp_transport_combo.setCurrentIndex(transport_index if transport_index >= 0 else 0)
        form.addRow("ESP transport", self.esp_transport_combo)
        self.esp_ble_name_edit = QLineEdit(settings.get("esp_ble_device_name", "ReSpeaker-BLE-Audio"))
        form.addRow("ESP BLE name", self.esp_ble_name_edit)
        self.esp_ble_address_edit = QLineEdit(settings.get("esp_ble_device_address", ""))
        form.addRow("ESP BLE address", self.esp_ble_address_edit)
        self.esp_ble_scan_timeout_spin = QDoubleSpinBox()
        self.esp_ble_scan_timeout_spin.setRange(2.0, 30.0)
        self.esp_ble_scan_timeout_spin.setSingleStep(0.5)
        self.esp_ble_scan_timeout_spin.setValue(float(settings.get("esp_ble_scan_timeout", 8.0)))
        self.esp_ble_scan_timeout_spin.setSuffix(" s")
        form.addRow("BLE scan timeout", self.esp_ble_scan_timeout_spin)
        self.esp_host_edit = QLineEdit(settings.get("esp_host", "10.42.0.27"))
        form.addRow("ESP IP / host", self.esp_host_edit)
        self.esp_mic_port_spin = QSpinBox()
        self.esp_mic_port_spin.setRange(1024, 65535)
        self.esp_mic_port_spin.setValue(int(settings.get("esp_mic_port", 12346)))
        form.addRow("ESP mic TCP port", self.esp_mic_port_spin)
        self.esp_play_port_spin = QSpinBox()
        self.esp_play_port_spin.setRange(1024, 65535)
        self.esp_play_port_spin.setValue(int(settings.get("esp_play_port", 12345)))
        form.addRow("ESP playback TCP port", self.esp_play_port_spin)

        layout.addLayout(form)

        self.esp_enable_checkbox.toggled.connect(self._update_esp_fields)
        self.esp_transport_combo.currentIndexChanged.connect(self._update_esp_fields)
        self._update_esp_fields()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_esp_fields(self):
        """Enable only the ESP settings relevant to the selected transport."""
        esp_enabled = self.esp_enable_checkbox.isChecked()
        transport = self.esp_transport_combo.currentData()

        ble_enabled = esp_enabled and transport == "ble"
        wifi_enabled = esp_enabled and transport == "wifi"

        self.esp_transport_combo.setEnabled(esp_enabled)
        self.esp_ble_name_edit.setEnabled(ble_enabled)
        self.esp_ble_address_edit.setEnabled(ble_enabled)
        self.esp_ble_scan_timeout_spin.setEnabled(ble_enabled)
        self.esp_host_edit.setEnabled(wifi_enabled)
        self.esp_mic_port_spin.setEnabled(wifi_enabled)
        self.esp_play_port_spin.setEnabled(wifi_enabled)

    def get_values(self) -> dict:
        """Return dialog values."""
        return {
            "stt_only_mode": self.stt_only_checkbox.isChecked(),
            "stt_device": self.stt_device_combo.currentData(),
            "stt_benchmark_cpu_gpu": self.stt_benchmark_checkbox.isChecked(),
            "live_streaming": self.live_streaming_checkbox.isChecked(),
            "auto_play_output": self.auto_play_checkbox.isChecked(),
            "audio_log_enabled": self.audio_log_checkbox.isChecked(),
            "show_logs": self.show_logs_checkbox.isChecked(),
            "max_duration": self.max_duration_spin.value(),
            "silence_threshold": self.silence_threshold_spin.value(),
            "silence_duration": self.silence_duration_spin.value(),
            "partial_interval": self.partial_interval_spin.value(),
            "partial_step_seconds": self.partial_step_spin.value(),
            "partial_window_seconds": self.partial_window_spin.value(),
            "connectivity_interval": self.connectivity_interval_spin.value(),
            "esp_enabled": self.esp_enable_checkbox.isChecked(),
            "esp_transport": self.esp_transport_combo.currentData(),
            "esp_ble_device_name": self.esp_ble_name_edit.text().strip(),
            "esp_ble_device_address": self.esp_ble_address_edit.text().strip(),
            "esp_ble_scan_timeout": self.esp_ble_scan_timeout_spin.value(),
            "esp_host": self.esp_host_edit.text().strip(),
            "esp_mic_port": self.esp_mic_port_spin.value(),
            "esp_play_port": self.esp_play_port_spin.value(),
        }


class MainWindow(QMainWindow):
    """Main application window"""

    connectivity_changed_signal = pyqtSignal(bool)
    esp_button_event_signal = pyqtSignal(str)
    esp_connection_signal = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎤 Offline Translator - Desktop v0.1")
        self.setGeometry(100, 100, 1000, 800)

        # Initialize config and lightweight services only.
        self.config = get_config()
        logger.info("Initializing GUI services...")
        whisper_config = self.config.get_whisper_model()

        self.stt_service = None
        self.translation_service = None
        self.tts_service = None
        self.language_service = get_language_service()
        self.connectivity_service = get_connectivity_service()
        self.claude_client = get_claude_client()
        self.audio_handler = AudioHandler(
            sample_rate=16000,
            esp_config=self.config.get_esp_config(),
        )
        self.cache = TranslationCache(db_path="cache.db")
        self.audio_config = self.config.get_audio_config()
        self.stt_model_name = whisper_config.get("model", "base")
        self.stt_device = self.config.get("offline.whisper_device", whisper_config.get("device", "cpu"))
        self.stt_cpu_compute_type = whisper_config.get("compute_type", "int8")
        self.stt_gpu_compute_type = whisper_config.get("gpu_compute_type", "float16")
        self.stt_cpu_threads = whisper_config.get("cpu_threads", 0)
        self.stt_num_workers = whisper_config.get("num_workers", 1)
        self.stt_beam_size = whisper_config.get("beam_size", 1)
        self.stt_vad_filter = whisper_config.get("vad_filter", True)
        self.stt_benchmark_cpu_gpu = self.config.get("ui.stt_benchmark_cpu_gpu", False)
        self.stt_only_mode = self.config.get("ui.stt_only_mode", False)
        self.live_streaming_enabled = self.config.get("ui.live_streaming", False)
        self.conversation_mode_enabled = self.config.get("ui.conversation_mode", False)
        self.auto_play_output = self.config.get("ui.auto_play_output", True)
        self.audio_log_enabled = self.config.get("ui.audio_log_enabled", True)
        self.show_logs = self.config.get("ui.show_logs", True)
        self.partial_interval = float(self.config.get("ui.live_partial_interval", 1.0))
        self.partial_step_seconds = float(self.config.get("ui.live_partial_step", 2.0))
        self.partial_window_seconds = float(self.config.get("ui.live_partial_window", 6.0))
        self.connectivity_interval = int(self.config.get("ui.connectivity_interval", 10))

        # State
        self.is_recording = False
        self.is_processing = False
        self.is_streaming = False
        self.recording_thread = None
        self.conversation_recording_worker = None
        self.streaming_stt_worker = None
        self.streaming_pipeline_worker = None
        self.translation_warmup_worker = None
        self.translation_warming_pair = None
        self.stt_benchmark_worker = None
        self.worker_thread = None
        self.esp_button_listener = None
        self.esp_bridge_connected = False
        self.last_live_log_text = ""
        self.pending_stt_benchmark = None
        self.conversation_session_active = False

        # UI Setup
        self._init_ui()
        self._connect_signals()
        self._restart_esp_button_listener()

        # Start connectivity monitoring
        self.connectivity_service.start_monitoring(interval=self.connectivity_interval)

        logger.info("GUI initialized")

    def _init_ui(self):
        """Initialize user interface"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Main layout
        main_layout = QVBoxLayout()

        # ===== Header =====
        header_layout = QHBoxLayout()

        # Status indicators
        self.status_label = QLabel()
        self.status_label.setFont(QFont("Arial", 12, QFont.Bold))
        header_layout.addWidget(QLabel("Status:"))
        header_layout.addWidget(self.status_label)

        # Connectivity indicator
        self.connectivity_label = QLabel()
        self.connectivity_label.setStyleSheet("color: green; font-weight: bold;")
        header_layout.addWidget(QLabel("Connectivity:"))
        header_layout.addWidget(self.connectivity_label)

        self.esp_status_label = QLabel()
        self.esp_status_label.setStyleSheet("color: #666; font-weight: bold;")
        header_layout.addWidget(QLabel("ESP:"))
        header_layout.addWidget(self.esp_status_label)

        # Language pair
        self.language_label = QLabel()
        self.language_label.setFont(QFont("Arial", 12, QFont.Bold))
        header_layout.addWidget(self.language_label)

        header_layout.addStretch()
        main_layout.addLayout(header_layout)

        # ===== Text Display Areas =====
        self.turn_label = QLabel()
        self.turn_label.setFont(QFont("Arial", 11, QFont.Bold))
        self.turn_label.setStyleSheet("color: #1b5e20;")
        main_layout.addWidget(self.turn_label)

        self.turn_hint_label = QLabel()
        self.turn_hint_label.setWordWrap(True)
        self.turn_hint_label.setStyleSheet("color: #555;")
        main_layout.addWidget(self.turn_hint_label)

        # Source text
        main_layout.addWidget(QLabel("SOURCE TEXT:"))
        self.source_text = QTextEdit()
        self.source_text.setReadOnly(True)
        self.source_text.setMaximumHeight(120)
        main_layout.addWidget(self.source_text)

        # Translated text
        main_layout.addWidget(QLabel("TRANSLATED OUTPUT:"))
        self.translated_text = QTextEdit()
        self.translated_text.setReadOnly(True)
        self.translated_text.setMaximumHeight(120)
        main_layout.addWidget(self.translated_text)

        # ===== Controls =====
        control_layout = QVBoxLayout()

        # PTT Button (main control)
        self.ptt_button = QPushButton("🎙️  START RECORDING  🔊")
        self.ptt_button.setFont(QFont("Arial", 14, QFont.Bold))
        self.ptt_button.setMinimumHeight(60)
        self.ptt_button.setCheckable(True)
        self.ptt_button.setStyleSheet(
            """
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:pressed {
                background-color: #45a049;
            }
            QPushButton:hover {
                background-color: #5DB960;
            }
            """
        )
        self.ptt_button.setMouseTracking(True)
        self.ptt_button.setFocusPolicy(Qt.NoFocus)
        control_layout.addWidget(self.ptt_button)

        # Language selection
        lang_layout = QHBoxLayout()
        self.source_lang_combo = QComboBox()
        self.source_lang_combo.setMinimumWidth(160)
        self.source_lang_combo.setMinimumHeight(40)
        self.target_lang_combo = QComboBox()
        self.target_lang_combo.setMinimumWidth(160)
        self.target_lang_combo.setMinimumHeight(40)
        for language in self.language_service.get_supported_languages():
            label = language.capitalize()
            self.source_lang_combo.addItem(label, language)
            self.target_lang_combo.addItem(label, language)

        self.settings_btn = QPushButton("⚙️ Settings")
        self.settings_btn.setMinimumWidth(100)
        self.settings_btn.setMinimumHeight(40)

        lang_layout.addWidget(QLabel("From"))
        lang_layout.addWidget(self.source_lang_combo)
        lang_layout.addWidget(QLabel("To"))
        lang_layout.addWidget(self.target_lang_combo)
        
        self.conversation_mode_checkbox = QCheckBox("Conversation Mode")
        self.conversation_mode_checkbox.setStyleSheet("margin-left: 15px; font-weight: bold;")
        self.conversation_mode_checkbox.setChecked(self.conversation_mode_enabled)
        lang_layout.addWidget(self.conversation_mode_checkbox)

        lang_layout.addStretch()
        lang_layout.addWidget(self.settings_btn)

        control_layout.addLayout(lang_layout)

        main_layout.addLayout(control_layout)

        # ===== Progress and Logging =====
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        # Log display
        main_layout.addWidget(QLabel("LOG:"))
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setMaximumHeight(150)
        self.log_display.setStyleSheet("background-color: #f5f5f5; font-family: monospace;")
        main_layout.addWidget(self.log_display)
        self.log_display.setVisible(self.show_logs)

        central_widget.setLayout(main_layout)

        # Status bar
        self.statusBar().showMessage("Ready")

        # Update initial state
        self.status_label.setText("Ready")
        self._update_language_display()
        self._update_connectivity_display()
        self._update_esp_status_display()
        self._update_conversation_labels()

    def _connect_signals(self):
        """Connect UI signals to slots"""
        # PTT Button
        self.ptt_button.clicked.connect(self._on_ptt_clicked)

        # Language selectors
        self.source_lang_combo.currentIndexChanged.connect(self._on_source_language_changed)
        self.target_lang_combo.currentIndexChanged.connect(self._on_target_language_changed)
        self.conversation_mode_checkbox.toggled.connect(self._on_conversation_mode_toggled)
        self.settings_btn.clicked.connect(self._on_settings)

        # Connectivity callback
        self.connectivity_changed_signal.connect(self._on_connectivity_changed)
        self.connectivity_service.add_callback(self.connectivity_changed_signal.emit)
        self.esp_button_event_signal.connect(self._on_esp_button_event)
        self.esp_connection_signal.connect(self._on_esp_connection_changed)

        # Log signal
        logger.info("All signals connected")

    def _restart_esp_button_listener(self):
        """Restart the ESP hardware button listener with current settings."""
        self._stop_esp_button_listener()
        if not self.audio_handler.esp_enabled or self.audio_handler.esp_transport != "wifi":
            return

        button_port = int(self.config.get("esp.button_port", 12347))
        if not self.audio_handler.esp_host or button_port <= 0:
            return

        self.esp_button_listener = ESPButtonListener(self.audio_handler.esp_host, button_port)
        self.esp_button_listener.button_event.connect(self.esp_button_event_signal.emit)
        self.esp_button_listener.connection_changed.connect(self.esp_connection_signal.emit)
        self.esp_button_listener.start()
        logger.info(
            "ESP button listener starting for tcp://%s:%s",
            self.audio_handler.esp_host,
            button_port,
        )

    def _stop_esp_button_listener(self):
        """Stop the ESP hardware button listener thread if it exists."""
        if self.esp_button_listener is None:
            self.esp_bridge_connected = False
            self._update_esp_status_display()
            return
        self.esp_button_listener.stop()
        self.esp_button_listener.wait(2000)
        self.esp_button_listener = None
        self.esp_bridge_connected = False
        self._update_esp_status_display()

    def _on_esp_button_event(self, event: str):
        """Map a hardware button press onto the GUI's toggle button."""
        if event != "PRESS":
            return
        self.ptt_button.click()

    def _on_esp_connection_changed(self, connected: bool):
        """Update GUI status when the ESP button socket connects or drops."""
        self.esp_bridge_connected = connected
        self._update_esp_status_display()

    def _on_ptt_clicked(self, checked: bool):
        """Toggle recording on/off with a single button."""
        if self.conversation_mode_enabled and not self.live_streaming_enabled:
            if checked:
                self._start_conversation_session()
            else:
                self._stop_conversation_session()
            return
        if checked:
            self._on_ptt_pressed(None)
        else:
            self._on_ptt_released(None)

    def _start_conversation_session(self):
        """Start a continuous turn-based conversation session."""
        if self.conversation_session_active:
            self.ptt_button.setChecked(True)
            return
        if self.is_processing or self.is_streaming or self.is_recording:
            self.ptt_button.setChecked(False)
            return
        self.conversation_session_active = True
        self._log("Conversation session started")
        self._update_conversation_labels()
        self._begin_conversation_turn()

    def _stop_conversation_session(self):
        """Stop the current conversation session."""
        self.conversation_session_active = False
        self.pending_stt_benchmark = None
        if self.conversation_recording_worker is not None:
            self.conversation_recording_worker.stop()
        elif self.recording_thread is not None and self.recording_thread.isRunning():
            self.audio_handler.stop_recording()
        self._log("Conversation session stopped")
        if not self.is_processing:
            self._reset_ptt_button()
            self.ptt_button.setEnabled(True)
            self.status_label.setText("Ready")
            self.statusBar().showMessage("Conversation stopped")
        self._update_conversation_labels()

    def _begin_conversation_turn(self):
        """Begin listening for the next speaker turn in conversation mode."""
        if not self.conversation_session_active:
            return
        if self.is_processing or self.is_recording or self.is_streaming:
            return
        if not self._ensure_pipeline_services():
            self.conversation_session_active = False
            self.ptt_button.setChecked(False)
            return

        source_lang, _ = self.language_service.get_current_pair()
        self.is_recording = True
        self.audio_handler.is_recording = True
        self.source_text.clear()
        self.translated_text.setText(
            f"Conversation mode active. Speak in {source_lang.capitalize()} and pause to end your turn."
        )
        self.ptt_button.setChecked(True)
        self.ptt_button.setEnabled(True)
        self.ptt_button.setText("⏹️  STOP CONVERSATION  🔊")
        self.ptt_button.setStyleSheet(
            """
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                border-radius: 5px;
                font-weight: bold;
                font-size: 14px;
            }
            """
        )
        self.status_label.setText(f"Listening: {source_lang.capitalize()}")
        self.statusBar().showMessage(
            f"Conversation mode: listening for {source_lang.capitalize()}"
        )
        self._update_conversation_labels()
        self._start_translation_warmup()

        self.conversation_recording_worker = ConversationRecordingWorker(
            self.audio_handler,
            self.stt_service,
            source_lang,
            self.language_service.get_language_code(source_lang),
            int(self.audio_config.get("max_duration", 10)),
            float(self.audio_config.get("silence_threshold", 0.01)),
            float(self.audio_config.get("silence_duration", 0.5)),
            self.partial_interval,
            self.partial_step_seconds,
            self.partial_window_seconds,
        )
        self.conversation_recording_worker.progress.connect(self._on_progress)
        self.conversation_recording_worker.partial_ready.connect(self._on_partial_stt_ready)
        self.conversation_recording_worker.audio_ready.connect(self._on_audio_recorded)
        self.conversation_recording_worker.error.connect(self._on_recording_error)
        self.conversation_recording_worker.finished.connect(self._on_conversation_recording_finished)
        self.conversation_recording_worker.start()

    def _on_ptt_pressed(self, event):
        """Handle recording start."""
        if self.is_recording or self.is_processing or self.is_streaming:
            self.ptt_button.setChecked(self.is_recording)
            return

        if not self._ensure_pipeline_services():
            self.ptt_button.setChecked(False)
            return

        self.is_recording = True
        self.audio_handler.is_recording = True
        self.source_text.clear()
        self.translated_text.setText("Recording in progress...")
        self.ptt_button.setChecked(True)
        self.ptt_button.setText("⏹️  STOP RECORDING  🔊")
        self.ptt_button.setStyleSheet(
            """
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                border-radius: 5px;
                font-weight: bold;
                font-size: 14px;
            }
            """
        )
        self.statusBar().showMessage("Recording...")
        self._log("Recording audio...")
        self.last_live_log_text = ""
        self.status_label.setText("Listening...")
        self._start_translation_warmup()

        if not self.live_streaming_enabled:
            self.recording_thread = RecordingWorker(
                self.audio_handler,
                self.audio_config.get("max_duration", 10),
            )
            self.recording_thread.audio_ready.connect(self._on_audio_recorded)
            self.recording_thread.error.connect(self._on_recording_error)
            self.recording_thread.finished.connect(self._on_recording_finished)
            self.recording_thread.start()
            return

        self.is_streaming = True
        if self.stt_only_mode:
            source_lang, _ = self.language_service.get_current_pair()
            source_lang_code = self.language_service.get_language_code(source_lang)
            self.streaming_stt_worker = StreamingSTTWorker(
                self.audio_handler,
                self.stt_service,
                source_lang,
                source_lang_code,
                self.audio_config.get("max_duration", 10),
                self.partial_interval,
                self.partial_step_seconds,
                self.partial_window_seconds,
            )
            self.streaming_stt_worker.progress.connect(self._on_progress)
            self.streaming_stt_worker.partial_ready.connect(self._on_partial_stt_ready)
            self.streaming_stt_worker.final_ready.connect(self._on_final_stt_ready)
            self.streaming_stt_worker.error.connect(self._on_error)
            self.streaming_stt_worker.finished.connect(self._on_streaming_stt_finished)
            self.streaming_stt_worker.start()
            return

        self.streaming_pipeline_worker = StreamingPipelineWorker(
            self.audio_handler,
            self.stt_service,
            self.translation_service,
            self.tts_service,
            self.language_service,
            self.cache,
            self.claude_client,
            self.stt_only_mode,
            self.audio_config.get("max_duration", 10),
            self.partial_interval,
            self.partial_step_seconds,
            self.partial_window_seconds,
            self.auto_play_output,
            self.conversation_mode_checkbox.isChecked(),
        )
        self.streaming_pipeline_worker.progress.connect(self._on_progress)
        self.streaming_pipeline_worker.partial_update.connect(self._on_partial_pipeline_update)
        self.streaming_pipeline_worker.final_result_ready.connect(self._on_result_ready)
        self.streaming_pipeline_worker.cloud_refinement_ready.connect(self._on_cloud_refinement_ready)
        self.streaming_pipeline_worker.error.connect(self._on_error)
        self.streaming_pipeline_worker.finished.connect(self._on_streaming_pipeline_finished)
        self.streaming_pipeline_worker.start()

    def _on_ptt_released(self, event):
        """Handle recording stop."""
        if not self.is_recording:
            self.ptt_button.setChecked(False)
            return

        self.audio_handler.stop_recording()
        self.ptt_button.setText("⏳  FINALIZING...  🔊")
        self.statusBar().showMessage("Stopping recording...")
        self.ptt_button.setEnabled(False)
        if self.recording_thread is not None and self.recording_thread.isRunning():
            return
        if self.streaming_stt_worker is not None:
            self.streaming_stt_worker.stop()
        if self.streaming_pipeline_worker is not None:
            self.streaming_pipeline_worker.stop()

    def _reset_ptt_button(self):
        """Restore the default toggle button style."""
        self.is_recording = False
        if self.conversation_session_active and self.conversation_mode_enabled:
            self.ptt_button.setChecked(True)
            self.ptt_button.setText("⏹️  STOP CONVERSATION  🔊")
            self.ptt_button.setStyleSheet(
                """
                QPushButton {
                    background-color: #f44336;
                    color: white;
                    border: none;
                    border-radius: 5px;
                    font-weight: bold;
                    font-size: 14px;
                }
                """
            )
            return
        else:
            self.ptt_button.setChecked(False)
            self.ptt_button.setText("🎙️  START RECORDING  🔊")
        self.ptt_button.setStyleSheet(
            """
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:pressed {
                background-color: #45a049;
            }
            QPushButton:hover {
                background-color: #5DB960;
            }
            """
        )

    def _on_audio_recorded(self, audio_data):
        """Handle recorded audio coming back from the capture thread."""
        self._process_audio(audio_data)

    def _on_recording_finished(self):
        """Handle when batch audio capture completes."""
        self.recording_thread = None

    def _on_conversation_recording_finished(self):
        """Handle when one silence-ended conversation turn capture finishes."""
        self.conversation_recording_worker = None

    def _on_recording_error(self, error: str):
        """Handle recording failures."""
        self.pending_stt_benchmark = None
        self.is_recording = False
        if self.conversation_session_active and self.conversation_mode_enabled:
            self.status_label.setText("Conversation paused")
            self.translated_text.setText(error)
            self.ptt_button.setEnabled(True)
        else:
            self._reset_ptt_button()
            self.ptt_button.setEnabled(True)
        self.statusBar().showMessage("Recording failed")
        self._log(f"ERROR: {error}")
        self._update_conversation_labels()

    def _start_translation_warmup(self, force: bool = False):
        """Warm the currently selected translation route in the background."""
        if self.stt_only_mode:
            return

        if self.translation_service is None:
            try:
                self.translation_service = get_translation_service()
            except Exception as e:
                logger.error(f"Failed to initialize translation service for warmup: {e}")
                return

        source_lang, target_lang = self.language_service.get_current_pair()
        pair = (source_lang, target_lang)

        try:
            route_models = self.translation_service.get_route_model_names(source_lang, target_lang)
        except Exception as e:
            logger.error(f"Cannot determine warmup route for {source_lang} -> {target_lang}: {e}")
            return

        if not force and all(model_name in self.translation_service.loaded_pipelines for model_name in route_models):
            return

        if self.translation_warmup_worker is not None and self.translation_warmup_worker.isRunning():
            if self.translation_warming_pair == pair:
                return
            return

        self.translation_warming_pair = pair
        self.translation_warmup_worker = TranslationWarmupWorker(
            self.translation_service,
            source_lang,
            target_lang,
        )
        self.translation_warmup_worker.progress.connect(self._on_progress)
        self.translation_warmup_worker.error.connect(self._on_error)
        self.translation_warmup_worker.warmed.connect(self._on_translation_warmup_finished)
        self.translation_warmup_worker.finished.connect(self._on_translation_warmup_thread_finished)
        self.translation_warmup_worker.start()

    def _on_translation_warmup_finished(self, source_lang: str, target_lang: str):
        """Handle successful completion of translation model warmup."""
        if self.translation_warming_pair == (source_lang, target_lang):
            self._log(f"Translation route ready: {source_lang.capitalize()} -> {target_lang.capitalize()}")

    def _on_translation_warmup_thread_finished(self):
        """Clear warmup worker state once it exits."""
        warmed_pair = self.translation_warming_pair
        self.translation_warmup_worker = None
        self.translation_warming_pair = None
        if not self.stt_only_mode:
            current_pair = self.language_service.get_current_pair()
            if warmed_pair is not None and current_pair != warmed_pair:
                self._start_translation_warmup(force=True)

    def _process_audio(self, audio_data):
        """Process recorded audio through the active pipeline stage."""
        self.is_recording = False
        if self.conversation_session_active and self.conversation_mode_enabled:
            self.ptt_button.setChecked(True)
            self.ptt_button.setText("⏳  PROCESSING TURN...  🔊")
            self.status_label.setText("Processing turn...")
            self._update_conversation_labels()
        else:
            self._reset_ptt_button()

        if audio_data is None or len(audio_data) == 0:
            self.statusBar().showMessage("Recording failed")
            self._log("ERROR: Failed to record audio")
            self.ptt_button.setEnabled(True)
            return

        if not self._ensure_pipeline_services():
            self.ptt_button.setEnabled(True)
            return

        source_lang, _ = self.language_service.get_current_pair()
        source_lang_code = self.language_service.get_language_code(source_lang)
        self.pending_stt_benchmark = None
        if self.stt_benchmark_cpu_gpu and not self.live_streaming_enabled:
            self.pending_stt_benchmark = {
                "audio_data": audio_data,
                "source_lang_code": source_lang_code,
            }

        # Start worker thread for processing
        self.is_processing = True
        self.ptt_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self.worker_thread = TranslationWorker(
            audio_data,
            self.stt_service,
            self.translation_service,
            self.tts_service,
            self.language_service,
            self.audio_handler,
            self.cache,
            self.claude_client,
            stt_only=self.stt_only_mode,
            auto_play_output=self.auto_play_output,
        )

        self.worker_thread.progress.connect(self._on_progress)
        self.worker_thread.error.connect(self._on_error)
        self.worker_thread.result_ready.connect(self._on_result_ready)
        self.worker_thread.cloud_refinement_ready.connect(self._on_cloud_refinement_ready)
        self.worker_thread.finished.connect(self._on_processing_finished)

        self.worker_thread.start()

    def _on_progress(self, message: str):
        """Handle progress updates"""
        self._log(message)
        self.statusBar().showMessage(message)

    def _on_partial_stt_ready(self, text: str):
        """Show partial STT output while the user is still speaking."""
        self.source_text.setText(text)
        if self.conversation_session_active and self.conversation_mode_enabled:
            self.translated_text.setText("Listening... pause to end the turn and translate.")
        else:
            self.translated_text.setText("Streaming STT active...")

    def _on_partial_pipeline_update(self, source_text: str, translated_text: str):
        """Show partial STT and translation output while the user is still speaking."""
        self.source_text.setText(source_text)
        self.translated_text.setText(translated_text)
        self.statusBar().showMessage("Streaming translation...")
        if source_text and source_text != self.last_live_log_text:
            self.last_live_log_text = source_text
            self._log(f"Live update: {source_text[:60]}...")

    def _on_final_stt_ready(self, text: str, elapsed: str):
        """Handle the finalized streaming STT result."""
        self.source_text.setText(text)
        self.translated_text.setText("STT-only mode: translation skipped")
        self._log(f"✓ STT complete in {elapsed}")

    def _on_error(self, error: str):
        """Handle errors"""
        self.pending_stt_benchmark = None
        self._log(f"ERROR: {error}")
        self.statusBar().showMessage(f"Error: {error}")

    def _on_result_ready(self, source_text: str, translated_text: str, elapsed: str):
        """Handle successful pipeline output"""
        self.source_text.setText(source_text)
        self.translated_text.setText(translated_text)
        self._log(f"✓ Complete in {elapsed}")
        if self.conversation_session_active and self.conversation_mode_enabled and not self.stt_only_mode:
            self._advance_conversation_turn()
        if self.pending_stt_benchmark and self.stt_benchmark_worker is None:
            self._start_stt_benchmark()

    def _on_cloud_refinement_ready(
        self,
        source_text: str,
        refined_text: str,
        source_lang: str,
        target_lang: str,
        elapsed: str,
    ):
        """Handle async cloud refinement on the GUI thread."""
        self.cache.set_cloud_refinement(source_text, source_lang, target_lang, refined_text)
        if self.source_text.toPlainText().strip() == source_text.strip():
            self.translated_text.setText(refined_text)
        self._log(f"Cloud refinement complete in {elapsed}")
        self.statusBar().showMessage("Cloud refinement complete")

    def _on_processing_finished(self):
        """Handle when processing finishes"""
        self.is_processing = False
        self.progress_bar.setVisible(False)
        self.worker_thread = None
        if self.conversation_session_active and self.conversation_mode_enabled and not self.stt_only_mode:
            self.ptt_button.setEnabled(True)
            self.status_label.setText(f"Next turn: {self.language_service.display_pair()}")
            self._update_conversation_labels()
            QTimer.singleShot(150, self._begin_conversation_turn)
            return

        self.ptt_button.setEnabled(True)
        self.status_label.setText("STT Ready" if self.stt_only_mode else "Ready")
        self._update_conversation_labels()

    def _on_streaming_stt_finished(self):
        """Handle when streaming STT stops."""
        self.is_streaming = False
        self._reset_ptt_button()
        self.ptt_button.setEnabled(True)
        self.status_label.setText("STT Ready")
        self.streaming_stt_worker = None

    def _on_streaming_pipeline_finished(self):
        """Handle when the streaming translation pipeline stops."""
        self.is_streaming = False
        self.streaming_pipeline_worker = None
        self._reset_ptt_button()
        self.ptt_button.setEnabled(True)
        if self.conversation_mode_enabled and not self.stt_only_mode:
            self.status_label.setText(f"Next turn: {self.language_service.display_pair()}")
        else:
            self.status_label.setText("Ready")
        self._update_conversation_labels()

    def _on_source_language_changed(self, index: int):
        """Handle source language dropdown changes."""
        source = self.source_lang_combo.itemData(index)
        target = self.target_lang_combo.currentData()
        self._apply_language_selection(source, target, changed="source")

    def _on_target_language_changed(self, index: int):
        """Handle target language dropdown changes."""
        source = self.source_lang_combo.currentData()
        target = self.target_lang_combo.itemData(index)
        self._apply_language_selection(source, target, changed="target")

    def _apply_language_selection(self, source: str, target: str, changed: str):
        """Apply the selected source/target language pair."""
        if not source or not target:
            return

        supported = self.language_service.get_supported_languages()
        if source == target:
            for language in supported:
                if language != source:
                    if changed == "source":
                        target = language
                    else:
                        source = language
                    break

        if not self.language_service.set_language_pair(source, target):
            return

        self._update_language_display()
        self._log(f"Switched to: {self.language_service.display_pair()}")
        self._start_translation_warmup(force=True)

    def _on_conversation_mode_toggled(self, checked: bool):
        """Enable or disable conversation mode."""
        self.conversation_mode_enabled = checked
        if checked and self.live_streaming_enabled:
            self.live_streaming_enabled = False
            self.config.set("ui.live_streaming", False, persist=True)
            self._log("Conversation mode uses automatic turn detection; live mode was disabled")
        self.config.set("ui.conversation_mode", checked, persist=True)
        if checked:
            self.statusBar().showMessage("Conversation mode enabled")
            self._log("Conversation mode enabled")
            if not self.stt_only_mode:
                self._start_translation_warmup(force=True)
        else:
            if self.conversation_session_active:
                self._stop_conversation_session()
            self.statusBar().showMessage("Conversation mode disabled")
            self._log("Conversation mode disabled")
        self._update_conversation_labels()

    def _advance_conversation_turn(self):
        """Swap source and target languages for the next speaker turn."""
        source, target = self.language_service.get_current_pair()
        if not self.language_service.set_language_pair(target, source):
            return
        self._update_language_display()
        self._start_translation_warmup(force=True)
        next_pair = self.language_service.display_pair()
        self.statusBar().showMessage(f"Conversation mode: next turn {next_pair}")
        self.status_label.setText(f"Next turn: {next_pair}")
        self._log(f"Conversation mode: next turn {next_pair}")
        self._update_conversation_labels()

    def _on_settings(self):
        """Open settings dialog and apply runtime settings."""
        dialog = SettingsDialog(
            {
                "stt_only_mode": self.stt_only_mode,
                "stt_device": self.stt_device,
                "stt_benchmark_cpu_gpu": self.stt_benchmark_cpu_gpu,
                "live_streaming": self.live_streaming_enabled,
                "auto_play_output": self.auto_play_output,
                "audio_log_enabled": self.audio_log_enabled,
                "show_logs": self.show_logs,
                "max_duration": int(self.audio_config.get("max_duration", 10)),
                "silence_threshold": float(self.audio_config.get("silence_threshold", 0.01)),
                "silence_duration": float(self.audio_config.get("silence_duration", 0.5)),
                "partial_interval": self.partial_interval,
                "partial_step_seconds": self.partial_step_seconds,
                "partial_window_seconds": self.partial_window_seconds,
                "connectivity_interval": self.connectivity_interval,
                "esp_enabled": self.audio_handler.esp_requested_enabled,
                "esp_transport": self.audio_handler.esp_transport,
                "esp_ble_device_name": self.audio_handler.esp_ble_device_name,
                "esp_ble_device_address": self.audio_handler.esp_ble_device_address,
                "esp_ble_scan_timeout": self.audio_handler.esp_ble_scan_timeout,
                "esp_host": self.audio_handler.esp_host,
                "esp_mic_port": self.audio_handler.esp_mic_port,
                "esp_play_port": self.audio_handler.esp_play_port,
            },
            self,
        )
        if dialog.exec_() != QDialog.Accepted:
            return

        settings = dialog.get_values()
        if settings["live_streaming"] and settings["partial_window_seconds"] < settings["partial_step_seconds"]:
            QMessageBox.warning(
                self,
                "Invalid Settings",
                "Live window size must be greater than or equal to live chunk advance.",
            )
            return

        self.stt_only_mode = settings["stt_only_mode"]
        self.stt_device = settings["stt_device"]
        self.stt_benchmark_cpu_gpu = settings["stt_benchmark_cpu_gpu"]
        self.live_streaming_enabled = settings["live_streaming"]
        self.auto_play_output = settings["auto_play_output"]
        self.audio_log_enabled = settings["audio_log_enabled"]
        self.show_logs = settings["show_logs"]
        self.audio_config["max_duration"] = settings["max_duration"]
        self.audio_config["silence_threshold"] = settings["silence_threshold"]
        self.audio_config["silence_duration"] = settings["silence_duration"]
        self.partial_interval = settings["partial_interval"]
        self.partial_step_seconds = settings["partial_step_seconds"]
        self.partial_window_seconds = settings["partial_window_seconds"]
        self.connectivity_interval = settings["connectivity_interval"]
        self.log_display.setVisible(self.show_logs)

        if self.connectivity_service.check_interval != self.connectivity_interval:
            self.connectivity_service.stop_monitoring()
            self.connectivity_service.start_monitoring(interval=self.connectivity_interval)

        self.config.update(
            {
                "offline.whisper_device": self.stt_device,
                "ui.stt_only_mode": self.stt_only_mode,
                "ui.stt_benchmark_cpu_gpu": self.stt_benchmark_cpu_gpu,
                "ui.live_streaming": self.live_streaming_enabled,
                "ui.auto_play_output": self.auto_play_output,
                "ui.audio_log_enabled": self.audio_log_enabled,
                "ui.show_logs": self.show_logs,
                "ui.conversation_mode": self.conversation_mode_enabled,
                "ui.live_partial_interval": self.partial_interval,
                "ui.live_partial_step": self.partial_step_seconds,
                "ui.live_partial_window": self.partial_window_seconds,
                "ui.connectivity_interval": self.connectivity_interval,
                "audio.max_duration": settings["max_duration"],
                "audio.silence_threshold": settings["silence_threshold"],
                "audio.silence_duration": settings["silence_duration"],
                "esp.enabled": settings["esp_enabled"],
                "esp.transport": settings["esp_transport"],
                "esp.ble_device_name": settings["esp_ble_device_name"],
                "esp.ble_device_address": settings["esp_ble_device_address"],
                "esp.ble_scan_timeout": settings["esp_ble_scan_timeout"],
                "esp.host": settings["esp_host"],
                "esp.mic_port": settings["esp_mic_port"],
                "esp.playback_port": settings["esp_play_port"],
            },
            persist=True,
        )

        self.audio_handler.update_esp_config(
            {
                "enabled": settings["esp_enabled"],
                "transport": settings["esp_transport"],
                "ble_device_name": settings["esp_ble_device_name"],
                "ble_device_address": settings["esp_ble_device_address"],
                "ble_scan_timeout": settings["esp_ble_scan_timeout"],
                "host": settings["esp_host"],
                "mic_port": settings["esp_mic_port"],
                "playback_port": settings["esp_play_port"],
            }
        )
        self._restart_esp_button_listener()

        desired_compute_type = self._get_stt_compute_type_for_device(self.stt_device)
        if self.stt_service is not None:
            if (
                self.stt_service.device != self.stt_device
                or self.stt_service.compute_type != desired_compute_type
            ):
                self.stt_service.unload_model()
                self.stt_service = None
                self._log(
                    f"STT backend set to {self.stt_device} ({desired_compute_type}); model will reload on next use"
                )

        self.status_label.setText("STT Ready" if self.stt_only_mode else "Ready")
        self.statusBar().showMessage("Settings updated")
        self._log("Settings updated")
        logger.info("Settings button clicked")

    def _on_connectivity_changed(self, is_online: bool):
        """Handle connectivity changes"""
        self._update_connectivity_display()
        status = "ONLINE" if is_online else "OFFLINE"
        self._log(f"Connectivity: {status}")

    def _update_language_display(self):
        """Update language pair display"""
        source, target = self.language_service.get_current_pair()
        pair_str = self.language_service.display_pair()
        self.language_label.setText(pair_str)
        self.source_lang_combo.blockSignals(True)
        self.target_lang_combo.blockSignals(True)
        source_index = self.source_lang_combo.findData(source)
        target_index = self.target_lang_combo.findData(target)
        if source_index >= 0:
            self.source_lang_combo.setCurrentIndex(source_index)
        if target_index >= 0:
            self.target_lang_combo.setCurrentIndex(target_index)
        self.source_lang_combo.blockSignals(False)
        self.target_lang_combo.blockSignals(False)
        self._update_conversation_labels()

    def _update_conversation_labels(self):
        """Show clear turn guidance for normal mode and conversation mode."""
        source, target = self.language_service.get_current_pair()
        if self.conversation_mode_enabled:
            self.turn_label.setText(
                f"Turn: Speaker ({source.capitalize()}) -> Listener ({target.capitalize()})"
            )
            if self.conversation_session_active:
                self.turn_hint_label.setText(
                    f"Speak in {source.capitalize()}. Pause briefly to end your turn. "
                    f"After playback, the app will switch to {target.capitalize()} -> {source.capitalize()}."
                )
            else:
                self.turn_hint_label.setText(
                    f"Conversation mode is on. Click start to begin with {source.capitalize()} speaking."
                )
        else:
            self.turn_label.setText(
                f"Current direction: {source.capitalize()} -> {target.capitalize()}"
            )
            self.turn_hint_label.setText(
                "Record one turn at a time. Use Conversation Mode for automatic turn swapping."
            )

    def _update_connectivity_display(self):
        """Update connectivity indicator"""
        is_online = self.connectivity_service.is_online
        if is_online:
            self.connectivity_label.setText("🟢 Online")
            self.connectivity_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.connectivity_label.setText("🔴 Offline")
            self.connectivity_label.setStyleSheet("color: red; font-weight: bold;")

    def _update_esp_status_display(self):
        """Update the dedicated ESP bridge status indicator."""
        if not self.audio_handler.esp_requested_enabled:
            self.esp_status_label.setText("Disabled")
            self.esp_status_label.setStyleSheet("color: #666; font-weight: bold;")
            return
        if not self.audio_handler.esp_enabled:
            self.esp_status_label.setText("Misconfigured")
            self.esp_status_label.setStyleSheet("color: #d9822b; font-weight: bold;")
            return
        if self.audio_handler.esp_transport != "wifi":
            self.esp_status_label.setText(self.audio_handler.esp_transport.upper())
            self.esp_status_label.setStyleSheet("color: #2b6cb0; font-weight: bold;")
            return
        if self.esp_bridge_connected:
            self.esp_status_label.setText("🟢 Connected")
            self.esp_status_label.setStyleSheet("color: green; font-weight: bold;")
            return
        self.esp_status_label.setText("🟠 Connecting...")
        self.esp_status_label.setStyleSheet("color: #d9822b; font-weight: bold;")

    def _get_stt_compute_type_for_device(self, device: str) -> str:
        """Return the preferred compute type for one STT device."""
        if device == "cuda":
            return self.stt_gpu_compute_type
        return self.stt_cpu_compute_type

    def _get_effective_stt_vad_filter(self) -> bool:
        """Disable Whisper VAD for ESP input because the ReSpeaker already filters speech."""
        if self.audio_handler.esp_enabled:
            return False
        return self.stt_vad_filter

    def _start_stt_benchmark(self):
        """Run an optional STT CPU/GPU benchmark on the latest audio clip."""
        if not self.pending_stt_benchmark or self.stt_benchmark_worker is not None:
            return

        benchmark = self.pending_stt_benchmark
        self.pending_stt_benchmark = None
        self.stt_benchmark_worker = STTBenchmarkWorker(
            benchmark["audio_data"],
            benchmark["source_lang_code"],
            self.stt_model_name,
            self.stt_device,
            self._get_stt_compute_type_for_device(self.stt_device),
            self.stt_cpu_compute_type,
            self.stt_gpu_compute_type,
            self.stt_cpu_threads,
            self.stt_num_workers,
            self.stt_beam_size,
            self._get_effective_stt_vad_filter(),
        )
        self.stt_benchmark_worker.progress.connect(self._on_progress)
        self.stt_benchmark_worker.work_finished.connect(self._on_stt_benchmark_finished)
        self.stt_benchmark_worker.start()

    def _on_stt_benchmark_finished(self):
        """Handle completion of the optional STT benchmark."""
        self.stt_benchmark_worker = None

    def _log(self, message: str):
        """Add message to log display"""
        timestamp = time.strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        self.log_display.append(log_message)
        if message.startswith("ERROR: "):
            logger.error(message[7:])
        else:
            logger.info(message)
        # Auto-scroll to bottom
        self.log_display.verticalScrollBar().setValue(
            self.log_display.verticalScrollBar().maximum()
        )

    def _ensure_pipeline_services(self) -> bool:
        """Load only the services needed for the current test stage."""
        errors = []
        desired_compute_type = self._get_stt_compute_type_for_device(self.stt_device)
        desired_vad_filter = self._get_effective_stt_vad_filter()

        if (
            self.stt_service is None
            or self.stt_service.device != self.stt_device
            or self.stt_service.compute_type != desired_compute_type
            or self.stt_service.vad_filter != desired_vad_filter
        ):
            try:
                self.status_label.setText("Loading STT...")
                self.statusBar().showMessage("Loading speech-to-text model...")
                load_start = take_perf_sample()
                self.stt_service = get_stt_service(
                    device=self.stt_device,
                    compute_type=desired_compute_type,
                    vad_filter=desired_vad_filter,
                    force_reload=self.stt_service is not None,
                )
                load_end = take_perf_sample()
                self._log(format_stage_metrics("STT load", load_start, load_end))
                self._log(
                    "STT VAD filter: "
                    + ("disabled for ReSpeaker ESP input" if not desired_vad_filter and self.audio_handler.esp_enabled else str(desired_vad_filter))
                )
            except Exception as e:
                logger.error(f"Failed to initialize STT service: {e}")
                errors.append(f"Speech-to-text unavailable: {e}")

        if self.stt_only_mode:
            if errors:
                self.status_label.setText("GUI Ready")
                self.statusBar().showMessage("Backend setup incomplete")
                for error in errors:
                    self._log(f"ERROR: {error}")
                return False

            self.status_label.setText("STT Ready")
            self.statusBar().showMessage("STT test mode")
            return True

        if self.translation_service is None:
            try:
                self.status_label.setText("Loading Translation...")
                self.statusBar().showMessage("Loading translation model...")
                load_start = take_perf_sample()
                self.translation_service = get_translation_service()
                load_end = take_perf_sample()
                self._log(format_stage_metrics("Translation load", load_start, load_end))
            except Exception as e:
                logger.error(f"Failed to initialize translation service: {e}")
                errors.append(f"Translation unavailable: {e}")

        if self.auto_play_output and self.tts_service is None:
            try:
                self.status_label.setText("Loading TTS...")
                self.statusBar().showMessage("Loading text-to-speech engine...")
                load_start = take_perf_sample()
                self.tts_service = get_tts_service()
                load_end = take_perf_sample()
                self._log(format_stage_metrics("TTS load", load_start, load_end))
            except Exception as e:
                logger.error(f"Failed to initialize TTS service: {e}")
                errors.append(f"Text-to-speech unavailable: {e}")

        if errors:
            self.status_label.setText("GUI Ready")
            self.statusBar().showMessage("Backend setup incomplete")
            for error in errors:
                self._log(f"ERROR: {error}")
            return False

        self.status_label.setText("Ready")
        return True

    def closeEvent(self, event):
        """Handle application close"""
        logger.info("Shutting down...")
        if self.streaming_stt_worker is not None:
            self.streaming_stt_worker.stop()
            self.streaming_stt_worker.wait(5000)
        if self.streaming_pipeline_worker is not None:
            self.streaming_pipeline_worker.stop()
            self.streaming_pipeline_worker.wait(5000)
        if self.translation_warmup_worker is not None and self.translation_warmup_worker.isRunning():
            self.translation_warmup_worker.wait(5000)
        if self.stt_benchmark_worker is not None and self.stt_benchmark_worker.isRunning():
            self.stt_benchmark_worker.wait(5000)
        if self.conversation_recording_worker is not None and self.conversation_recording_worker.isRunning():
            self.conversation_recording_worker.stop()
            self.conversation_recording_worker.wait(5000)
        if self.recording_thread is not None and self.recording_thread.isRunning():
            self.recording_thread.wait(5000)
        if self.worker_thread is not None and self.worker_thread.isRunning():
            self.worker_thread.wait(5000)
        self._stop_esp_button_listener()
        self.connectivity_service.stop_monitoring()
        if self.tts_service is not None:
            self.tts_service.shutdown()
        if self.stt_service is not None:
            self.stt_service.unload_model()
        if self.translation_service is not None:
            self.translation_service.unload_model()
        event.accept()
