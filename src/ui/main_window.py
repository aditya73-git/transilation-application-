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
from src.config import get_config
from src.cloud.claude_client import get_claude_client

logger = get_logger(__name__)


class RecordingWorker(QThread):
    """Worker thread for microphone capture."""

    finished = pyqtSignal()
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
            self.finished.emit()


class TranslationWorker(QThread):
    """Worker thread for running translation pipeline"""

    finished = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    result_ready = pyqtSignal(str, str, str)  # source_text, translated_text, time_taken

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

        try:
            source_lang, target_lang = self.language_service.get_current_pair()
            source_lang_code = self.language_service.get_language_code(source_lang)

            # Step 1: Speech-to-Text
            self.progress.emit(f"Running speech-to-text for {source_lang}...")
            text, detected_lang, confidence = self.stt_service.transcribe(
                self.audio_data,
                language=source_lang_code,
            )

            if not text:
                self.error.emit("Failed to recognize speech. Try again.")
                return

            self.progress.emit(f"Recognized: {text[:50]}...")

            if self.stt_only:
                elapsed = time.time() - start_time
                self.progress.emit("STT test complete")
                self.result_ready.emit(text, "STT-only mode: translation skipped", f"{elapsed:.2f}s")
                return

            # Step 2: Translation
            self.progress.emit("Translating...")

            # Check cache first
            cached = self.cache.get(text, source_lang, target_lang)
            if cached:
                translated_text = cached["translated_text"]
                self.progress.emit("Using cached translation")
            else:
                translated_text, _ = self.translation_service.translate(text, source_lang, target_lang)

                if not translated_text:
                    self.error.emit("Translation failed. Try again.")
                    return

                # Cache the translation
                self.cache.set(text, source_lang, target_lang, translated_text)

            self.progress.emit(f"Translated: {translated_text[:50]}...")

            # Step 3: Text-to-Speech
            self.progress.emit("Converting to speech...")
            if not self.tts_service.speak(translated_text, target_lang):
                self.error.emit("Text-to-speech failed.")
                return

            elapsed = time.time() - start_time
            self.progress.emit(f"Complete in {elapsed:.2f}s")

            # Step 4: Cloud refinement (async, non-blocking)
            if self.claude_client.is_enabled():
                self.progress.emit("Queuing for cloud refinement...")

                def update_cache_with_refined(refined):
                    self.cache.set_cloud_refinement(text, source_lang, target_lang, refined)
                    self.progress.emit(f"Cloud refinement complete")

                self.claude_client.refine_translation_async(
                    text, translated_text, source_lang, target_lang, callback=update_cache_with_refined
                )

            # Emit result
            self.result_ready.emit(text, translated_text, f"{elapsed:.2f}s")

        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            self.error.emit(f"Error: {str(e)}")

        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    """Main application window"""

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
        self.stt_only_mode = True

        # State
        self.is_recording = False
        self.is_processing = False
        self.recording_thread = None
        self.worker_thread = None

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
        self.status_label.setText("STT Test Mode")
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
        self.connectivity_service.add_callback(self._on_connectivity_changed)

        # Log signal
        logger.info("All signals connected")

    def _on_ptt_pressed(self, event):
        """Handle PTT button press"""
        if self.is_recording or self.is_processing:
            return

        self.is_recording = True
        self.audio_handler.is_recording = True
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

        self.recording_thread = RecordingWorker(
            self.audio_handler,
            self.audio_config.get("max_duration", 10),
        )
        self.recording_thread.audio_ready.connect(self._on_audio_recorded)
        self.recording_thread.error.connect(self._on_recording_error)
        self.recording_thread.start()

    def _on_ptt_released(self, event):
        """Handle PTT button release"""
        if not self.is_recording:
            return

        self.audio_handler.stop_recording()
        self.ptt_button.setText("⏳  PROCESSING...  🔊")
        self.statusBar().showMessage("Stopping recording...")
        self.ptt_button.setEnabled(False)

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
        self.worker_thread.finished.connect(self._on_processing_finished)

        self.worker_thread.start()

    def _on_progress(self, message: str):
        """Handle progress updates"""
        self._log(message)
        self.statusBar().showMessage(message)

    def _on_error(self, error: str):
        """Handle errors"""
        self._log(f"ERROR: {error}")
        self.statusBar().showMessage(f"Error: {error}")

    def _on_result_ready(self, source_text: str, translated_text: str, elapsed: str):
        """Handle successful pipeline output"""
        self.source_text.setText(source_text)
        self.translated_text.setText(translated_text)
        self._log(f"✓ Complete in {elapsed}")

    def _on_processing_finished(self):
        """Handle when processing finishes"""
        self.is_processing = False
        self.ptt_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_label.setText("STT Ready" if self.stt_only_mode else "GUI Ready")

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
                self.stt_service = get_stt_service()
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
                self.translation_service = get_translation_service()
            except Exception as e:
                logger.error(f"Failed to initialize translation service: {e}")
                errors.append(f"Translation unavailable: {e}")

        if self.tts_service is None:
            try:
                self.status_label.setText("Loading TTS...")
                self.statusBar().showMessage("Loading text-to-speech engine...")
                self.tts_service = get_tts_service()
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
        self.connectivity_service.stop_monitoring()
        if self.tts_service is not None:
            self.tts_service.shutdown()
        if self.stt_service is not None:
            self.stt_service.unload_model()
        if self.translation_service is not None:
            self.translation_service.unload_model()
        event.accept()
