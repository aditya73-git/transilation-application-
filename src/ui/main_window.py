"""Main GUI window for the offline translator"""
import sys
import time
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
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QFont, QColor, QIcon
from pathlib import Path

from src.services.stt_service import get_stt_service
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


class StreamingSTTWorker(QThread):
    """Worker thread for near-real-time STT updates while recording."""

    work_finished = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    partial_ready = pyqtSignal(str)
    final_ready = pyqtSignal(str, str)

    def __init__(self, audio_handler, stt_service, source_lang, source_lang_code, max_duration: int):
        super().__init__()
        self.audio_handler = audio_handler
        self.stt_service = stt_service
        self.source_lang = source_lang
        self.source_lang_code = source_lang_code
        self.max_duration = max_duration
        self.stop_requested = False
        self.partial_interval = 1.0
        self.min_audio_seconds = 0.75
        self.last_partial = ""
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
                if len(snapshot) - self.last_processed_samples < int(0.5 * self.audio_handler.sample_rate):
                    continue

                text, _, _ = self.stt_service.transcribe(snapshot, language=self.source_lang_code)
                self.last_processed_samples = len(snapshot)
                if text and text != self.last_partial:
                    self.last_partial = text
                    self.partial_ready.emit(text)

            final_audio = self.audio_handler.stop_stream_recording()
            if final_audio is None or len(final_audio) == 0:
                self.error.emit("Failed to capture audio. Try again.")
                return

            self.progress.emit("Finalizing transcription...")
            final_text, _, _ = self.stt_service.transcribe(final_audio, language=self.source_lang_code)
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
        self.partial_interval = 1.0
        self.min_audio_seconds = 0.75
        self.last_processed_samples = 0
        self.last_source_text = ""
        self.last_translated_text = ""
        self.partial_stt_runs = 0
        self.partial_stt_wall = 0.0
        self.partial_stt_cpu = 0.0
        self.partial_translation_runs = 0
        self.partial_translation_wall = 0.0
        self.partial_translation_cpu = 0.0

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

    def run(self):
        """Capture audio, emit partial transcripts/translations, then finalize."""
        start_time = time.time()
        pipeline_start = take_perf_sample()
        source_lang, target_lang = self.language_service.get_current_pair()
        source_lang_code = self.language_service.get_language_code(source_lang)

        try:
            self.progress.emit(f"Listening for {source_lang}...")
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
                if len(snapshot) - self.last_processed_samples < int(0.5 * self.audio_handler.sample_rate):
                    continue

                stt_start = take_perf_sample()
                text, _, _ = self.stt_service.transcribe(snapshot, language=source_lang_code)
                stt_end = take_perf_sample()
                stt_metrics = stage_metrics(stt_start, stt_end)
                self.partial_stt_runs += 1
                self.partial_stt_wall += stt_metrics["wall_seconds"]
                self.partial_stt_cpu += stt_metrics["total_cpu_seconds"]
                self.last_processed_samples = len(snapshot)
                if not text or text == self.last_source_text:
                    continue

                translate_start = take_perf_sample()
                translated_text = self._translate_for_display(text, source_lang, target_lang)
                translate_end = take_perf_sample()
                translate_metrics = stage_metrics(translate_start, translate_end)
                self.partial_translation_runs += 1
                self.partial_translation_wall += translate_metrics["wall_seconds"]
                self.partial_translation_cpu += translate_metrics["total_cpu_seconds"]
                if not translated_text:
                    translated_text = "Translating..."

                self.last_source_text = text
                self.last_translated_text = translated_text
                self.partial_update.emit(text, translated_text)

            final_audio = self.audio_handler.stop_stream_recording()
            if final_audio is None or len(final_audio) == 0:
                self.error.emit("Failed to capture audio. Try again.")
                return

            self.progress.emit("Finalizing transcription...")
            final_stt_start = take_perf_sample()
            final_text, _, _ = self.stt_service.transcribe(final_audio, language=source_lang_code)
            final_stt_end = take_perf_sample()
            if not final_text:
                self.error.emit("Failed to recognize speech. Try again.")
                return

            final_translation_start = take_perf_sample()
            final_translated = self._translate_for_display(final_text, source_lang, target_lang)
            final_translation_end = take_perf_sample()
            if not final_translated:
                self.error.emit("Translation failed. Try again.")
                return

            if not self.stt_only:
                self.progress.emit("Converting to speech...")
                tts_start = take_perf_sample()
                if not self.tts_service.speak(final_translated, target_lang):
                    self.error.emit("Text-to-speech failed.")
                    return
                tts_end = take_perf_sample()
            else:
                tts_start = None
                tts_end = None

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

    def run(self):
        """Run translation pipeline in background"""
        start_time = time.time()
        pipeline_start = take_perf_sample()

        try:
            source_lang, target_lang = self.language_service.get_current_pair()
            source_lang_code = self.language_service.get_language_code(source_lang)

            # Step 1: Speech-to-Text
            self.progress.emit(f"Running speech-to-text for {source_lang}...")
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
            self.progress.emit("Converting to speech...")
            tts_start = take_perf_sample()
            if not self.tts_service.speak(translated_text, target_lang):
                self.error.emit("Text-to-speech failed.")
                return
            tts_end = take_perf_sample()

            elapsed = time.time() - start_time
            pipeline_end = take_perf_sample()
            self.progress.emit(format_stage_metrics("STT", stt_start, stt_end))
            self.progress.emit(format_stage_metrics("Translation", translation_start, translation_end))
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


class MainWindow(QMainWindow):
    """Main application window"""

    connectivity_changed_signal = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎤 Offline Translator - Desktop v0.1")
        self.setGeometry(100, 100, 1000, 800)

        # Initialize config and lightweight services only.
        self.config = get_config()
        logger.info("Initializing GUI services...")

        self.stt_service = None
        self.translation_service = None
        self.tts_service = None
        self.language_service = get_language_service()
        self.connectivity_service = get_connectivity_service()
        self.claude_client = get_claude_client()
        self.audio_handler = AudioHandler(sample_rate=16000)
        self.cache = TranslationCache(db_path="cache.db")
        self.audio_config = self.config.get_audio_config()
        self.stt_only_mode = False

        # State
        self.is_recording = False
        self.is_processing = False
        self.is_streaming = False
        self.recording_thread = None
        self.streaming_stt_worker = None
        self.streaming_pipeline_worker = None
        self.worker_thread = None
        self.last_live_log_text = ""

        # UI Setup
        self._init_ui()
        self._connect_signals()

        # Start connectivity monitoring
        self.connectivity_service.start_monitoring()

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

        # Language pair
        self.language_label = QLabel()
        self.language_label.setFont(QFont("Arial", 12, QFont.Bold))
        header_layout.addWidget(self.language_label)

        header_layout.addStretch()
        main_layout.addLayout(header_layout)

        # ===== Text Display Areas =====
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
        self.ptt_button = QPushButton("🎙️  PRESS TO TALK  🔊")
        self.ptt_button.setFont(QFont("Arial", 14, QFont.Bold))
        self.ptt_button.setMinimumHeight(60)
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

        # Language switching
        lang_layout = QHBoxLayout()
        self.prev_lang_btn = QPushButton("← Prev")
        self.prev_lang_btn.setMinimumWidth(100)
        self.prev_lang_btn.setMinimumHeight(40)

        self.next_lang_btn = QPushButton("Next →")
        self.next_lang_btn.setMinimumWidth(100)
        self.next_lang_btn.setMinimumHeight(40)

        self.settings_btn = QPushButton("⚙️ Settings")
        self.settings_btn.setMinimumWidth(100)
        self.settings_btn.setMinimumHeight(40)

        lang_layout.addWidget(self.prev_lang_btn)
        lang_layout.addStretch()
        lang_layout.addWidget(self.next_lang_btn)
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

        central_widget.setLayout(main_layout)

        # Status bar
        self.statusBar().showMessage("Ready")

        # Update initial state
        self.status_label.setText("Ready")
        self._update_language_display()
        self._update_connectivity_display()

    def _connect_signals(self):
        """Connect UI signals to slots"""
        # PTT Button
        self.ptt_button.mousePressEvent = self._on_ptt_pressed
        self.ptt_button.mouseReleaseEvent = self._on_ptt_released

        # Language buttons
        self.prev_lang_btn.clicked.connect(self._on_prev_language)
        self.next_lang_btn.clicked.connect(self._on_next_language)
        self.settings_btn.clicked.connect(self._on_settings)

        # Connectivity callback
        self.connectivity_changed_signal.connect(self._on_connectivity_changed)
        self.connectivity_service.add_callback(self.connectivity_changed_signal.emit)

        # Log signal
        logger.info("All signals connected")

    def _on_ptt_pressed(self, event):
        """Handle PTT button press"""
        if self.is_recording or self.is_processing or self.is_streaming:
            return

        if not self._ensure_pipeline_services():
            return

        self.is_recording = True
        self.audio_handler.is_recording = True
        self.source_text.clear()
        self.translated_text.setText("Streaming STT active...")
        self.ptt_button.setText("🎙️  RECORDING...  🔊")
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
        )
        self.streaming_pipeline_worker.progress.connect(self._on_progress)
        self.streaming_pipeline_worker.partial_update.connect(self._on_partial_pipeline_update)
        self.streaming_pipeline_worker.final_result_ready.connect(self._on_result_ready)
        self.streaming_pipeline_worker.cloud_refinement_ready.connect(self._on_cloud_refinement_ready)
        self.streaming_pipeline_worker.error.connect(self._on_error)
        self.streaming_pipeline_worker.finished.connect(self._on_streaming_pipeline_finished)
        self.streaming_pipeline_worker.start()

    def _on_ptt_released(self, event):
        """Handle PTT button release"""
        if not self.is_recording:
            return

        self.audio_handler.stop_recording()
        self.ptt_button.setText("⏳  FINALIZING...  🔊")
        self.statusBar().showMessage("Stopping recording...")
        self.ptt_button.setEnabled(False)
        if self.streaming_stt_worker is not None:
            self.streaming_stt_worker.stop()
        if self.streaming_pipeline_worker is not None:
            self.streaming_pipeline_worker.stop()

    def _reset_ptt_button(self):
        """Restore the default push-to-talk button style."""
        self.is_recording = False
        self.ptt_button.setText("🎙️  PRESS TO TALK  🔊")
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

    def _on_recording_error(self, error: str):
        """Handle recording failures."""
        self._reset_ptt_button()
        self.ptt_button.setEnabled(True)
        self.statusBar().showMessage("Recording failed")
        self._log(f"ERROR: {error}")

    def _process_audio(self, audio_data):
        """Process recorded audio through the active pipeline stage."""
        self._reset_ptt_button()

        if audio_data is None or len(audio_data) == 0:
            self.statusBar().showMessage("Recording failed")
            self._log("ERROR: Failed to record audio")
            self.ptt_button.setEnabled(True)
            return

        if not self._ensure_pipeline_services():
            self.ptt_button.setEnabled(True)
            return

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
        self._log(f"ERROR: {error}")
        self.statusBar().showMessage(f"Error: {error}")

    def _on_result_ready(self, source_text: str, translated_text: str, elapsed: str):
        """Handle successful pipeline output"""
        self.source_text.setText(source_text)
        self.translated_text.setText(translated_text)
        self._log(f"✓ Complete in {elapsed}")

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
        self.ptt_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_label.setText("STT Ready" if self.stt_only_mode else "Ready")
        self.worker_thread = None

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
        self._reset_ptt_button()
        self.ptt_button.setEnabled(True)
        self.status_label.setText("Ready")
        self.streaming_pipeline_worker = None

    def _on_prev_language(self):
        """Switch to previous language pair"""
        self.language_service.switch_language_prev()
        self._update_language_display()
        self._log(f"Switched to: {self.language_service.display_pair()}")

    def _on_next_language(self):
        """Switch to next language pair"""
        self.language_service.switch_language_next()
        self._update_language_display()
        self._log(f"Switched to: {self.language_service.display_pair()}")

    def _on_settings(self):
        """Open settings dialog"""
        self._log("Settings: Not yet implemented")
        logger.info("Settings button clicked")

    def _on_connectivity_changed(self, is_online: bool):
        """Handle connectivity changes"""
        self._update_connectivity_display()
        status = "ONLINE" if is_online else "OFFLINE"
        self._log(f"Connectivity: {status}")

    def _update_language_display(self):
        """Update language pair display"""
        pair_str = self.language_service.display_pair()
        self.language_label.setText(pair_str)

    def _update_connectivity_display(self):
        """Update connectivity indicator"""
        is_online = self.connectivity_service.is_online
        if is_online:
            self.connectivity_label.setText("🟢 Online")
            self.connectivity_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.connectivity_label.setText("🔴 Offline")
            self.connectivity_label.setStyleSheet("color: red; font-weight: bold;")

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

        if self.stt_service is None:
            try:
                self.status_label.setText("Loading STT...")
                self.statusBar().showMessage("Loading speech-to-text model...")
                load_start = take_perf_sample()
                self.stt_service = get_stt_service()
                load_end = take_perf_sample()
                self._log(format_stage_metrics("STT load", load_start, load_end))
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

        if self.tts_service is None:
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
        if self.recording_thread is not None and self.recording_thread.isRunning():
            self.recording_thread.wait(5000)
        if self.worker_thread is not None and self.worker_thread.isRunning():
            self.worker_thread.wait(5000)
        self.connectivity_service.stop_monitoring()
        if self.tts_service is not None:
            self.tts_service.shutdown()
        if self.stt_service is not None:
            self.stt_service.unload_model()
        if self.translation_service is not None:
            self.translation_service.unload_model()
        event.accept()
