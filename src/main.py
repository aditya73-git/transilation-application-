"""Terminal entry point for the offline translator."""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.cloud.claude_client import get_claude_client
from src.config import get_config
from src.services.connectivity_service import get_connectivity_service
from src.services.language_service import get_language_service
from src.services.stt_service import get_stt_service
from src.services.translation_service import get_translation_service
from src.services.tts_service import get_tts_service
from src.startup_preflight import prime_required_assets
from src.utils.audio_handler import AudioHandler
from src.utils.cache import TranslationCache
from src.utils.logger import create_log_file, setup_logger
from src.utils.performance import format_stage_metrics, take_perf_sample
from src.utils.system_monitor import collect_system_snapshot, format_system_snapshot, log_system_snapshot


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(description="Offline terminal translator for Raspberry Pi")
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip model and voice preflight checks/downloads during startup",
    )
    parser.add_argument(
        "--stt-only",
        action="store_true",
        help="Run in speech-to-text only mode and skip translation/TTS",
    )
    return parser


class TerminalTranslatorApp:
    """Interactive terminal translator shell."""

    def __init__(self, *, stt_only: bool = False):
        self.config = get_config()
        self.logger = logging.getLogger(__name__)
        self.language_service = get_language_service()
        self.connectivity_service = get_connectivity_service()
        self.claude_client = get_claude_client()
        self.audio_config = self.config.get_audio_config()
        self.audio_handler = AudioHandler(sample_rate=self.audio_config.get("sample_rate", 16000))
        self.cache = TranslationCache(db_path="cache.db")
        self.stt_only = stt_only

        self.stt_service = None
        self.translation_service = None
        self.tts_service = None

    def run(self) -> int:
        """Run the interactive command loop."""
        self.connectivity_service.update_status()
        self._print_banner()
        self._print_help()

        while True:
            try:
                raw_command = input("\ntranslator> ").strip()
            except EOFError:
                print()
                return 0
            except KeyboardInterrupt:
                print("\nExiting.")
                return 0

            if not raw_command:
                continue

            command, _, remainder = raw_command.partition(" ")
            command = command.lower()
            args = remainder.strip()

            if command in {"quit", "exit", "q"}:
                print("Exiting.")
                return 0
            if command in {"help", "h", "?"}:
                self._print_help()
                continue
            if command == "status":
                self._print_status()
                continue
            if command == "health":
                self._print_health()
                continue
            if command == "pairs":
                self._print_pairs()
                continue
            if command == "set":
                self._handle_set_pair(args)
                continue
            if command == "next":
                self.language_service.switch_language_next()
                print(f"Active pair: {self.language_service.display_pair()}")
                continue
            if command == "prev":
                self.language_service.switch_language_prev()
                print(f"Active pair: {self.language_service.display_pair()}")
                continue
            if command == "devices":
                self._print_devices()
                continue
            if command == "record":
                self._run_record_pipeline()
                continue
            if command == "text":
                self._run_text_pipeline(args)
                continue
            if command == "stt-only":
                self.stt_only = not self.stt_only
                state = "ON" if self.stt_only else "OFF"
                print(f"STT-only mode: {state}")
                continue

            print(f"Unknown command: {command}. Type 'help' to see available commands.")

    def _print_banner(self):
        """Print startup banner."""
        pair = self.language_service.display_pair()
        online = "ONLINE" if self.connectivity_service.is_online else "OFFLINE"
        print("Offline Translator - Terminal Mode")
        print(f"Active pair: {pair}")
        print(f"Connectivity: {online}")

    def _print_help(self):
        """Print interactive help."""
        print(
            "\nCommands:\n"
            "  help                 Show this help\n"
            "  status               Show current mode, pair, and connectivity\n"
            "  health               Show Pi telemetry snapshot\n"
            "  pairs                List available language pairs\n"
            "  set <src> <tgt>      Set active language pair\n"
            "  next / prev          Cycle language pairs\n"
            "  devices              List available audio devices\n"
            "  record               Record from microphone until Enter is pressed\n"
            "  text <message>       Translate typed text using the active pair\n"
            "  stt-only             Toggle STT-only mode\n"
            "  quit                 Exit terminal mode"
        )

    def _print_status(self):
        """Print current runtime status."""
        self.connectivity_service.update_status()
        print(f"Pair: {self.language_service.display_pair()}")
        print(f"Connectivity: {'ONLINE' if self.connectivity_service.is_online else 'OFFLINE'}")
        print(f"STT-only mode: {'ON' if self.stt_only else 'OFF'}")
        self._print_health()

    def _print_health(self):
        """Print a current Pi telemetry snapshot."""
        snapshot = collect_system_snapshot()
        print(f"Health: {format_system_snapshot(snapshot)}")
        self.logger.info("Pi telemetry [status] %s", format_system_snapshot(snapshot))

    def _print_pairs(self):
        """Print all configured language pairs."""
        active = self.language_service.get_current_pair()
        for source, target in self.language_service.get_all_pairs():
            marker = "*" if (source, target) == active else " "
            print(f"{marker} {source} -> {target}")

    def _handle_set_pair(self, args: str):
        """Set the active language pair from a command string."""
        parts = args.split()
        if len(parts) != 2:
            print("Usage: set <source_language> <target_language>")
            return

        source, target = parts
        if self.language_service.set_language_pair(source, target):
            print(f"Active pair: {self.language_service.display_pair()}")
        else:
            print(f"Unknown pair: {source} -> {target}")

    def _print_devices(self):
        """Print available audio devices."""
        devices = self.audio_handler.list_devices()
        if not devices:
            print("No audio devices found.")
            return

        for index, device in enumerate(devices, start=1):
            print(f"{index}. {device}")

    def _ensure_services(self):
        """Load required backend services lazily."""
        if self.stt_service is None:
            start = take_perf_sample()
            self.stt_service = get_stt_service()
            end = take_perf_sample()
            print(format_stage_metrics("STT load", start, end))

        if self.stt_only:
            return

        if self.translation_service is None:
            start = take_perf_sample()
            self.translation_service = get_translation_service()
            end = take_perf_sample()
            print(format_stage_metrics("Translation load", start, end))

        if self.tts_service is None:
            start = take_perf_sample()
            self.tts_service = get_tts_service()
            end = take_perf_sample()
            print(format_stage_metrics("TTS load", start, end))

    def _run_record_pipeline(self):
        """Record speech and run the active pipeline."""
        try:
            self._ensure_services()
        except Exception as exc:
            print(f"Service initialization failed: {exc}")
            return

        log_system_snapshot(self.logger, "before_record")

        max_duration = self.audio_config.get("max_duration", 30)
        source_lang, target_lang = self.language_service.get_current_pair()
        source_lang_code = self.language_service.get_language_code(source_lang)

        print(f"Recording for {source_lang}. Press Enter to stop. Max duration: {max_duration}s")
        self.audio_handler.start_stream_recording()

        stop_event = threading.Event()

        def wait_for_stop():
            try:
                input()
            finally:
                stop_event.set()

        stopper = threading.Thread(target=wait_for_stop, daemon=True)
        stopper.start()

        started_at = time.time()
        try:
            while not stop_event.is_set():
                if time.time() - started_at >= max_duration:
                    print("Max duration reached.")
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nStopping recording.")
        finally:
            audio_data = self.audio_handler.stop_stream_recording()

        if audio_data is None or len(audio_data) == 0:
            print("No audio captured.")
            return

        print(f"Captured {len(audio_data) / self.audio_handler.sample_rate:.2f}s of audio")

        stt_start = take_perf_sample()
        text, _, _ = self.stt_service.transcribe(audio_data, language=source_lang_code)
        stt_end = take_perf_sample()
        print(format_stage_metrics("STT", stt_start, stt_end))

        if not text:
            print("Failed to recognize speech.")
            return

        print(f"Source: {text}")

        if self.stt_only:
            log_system_snapshot(self.logger, "after_stt_only_record")
            return

        self._finish_translation_pipeline(text, source_lang, target_lang)

    def _run_text_pipeline(self, args: str):
        """Translate provided text or prompt for it."""
        try:
            self._ensure_services()
        except Exception as exc:
            print(f"Service initialization failed: {exc}")
            return

        log_system_snapshot(self.logger, "before_text_translation")

        source_lang, target_lang = self.language_service.get_current_pair()
        text = args or input("Enter source text: ").strip()
        if not text:
            print("No text entered.")
            return

        print(f"Source: {text}")
        if self.stt_only:
            print("STT-only mode is enabled, so typed translation is skipped.")
            return

        self._finish_translation_pipeline(text, source_lang, target_lang)

    def _finish_translation_pipeline(self, text: str, source_lang: str, target_lang: str):
        """Translate, cache, speak, and optionally refine."""
        cached = self.cache.get_best(text, source_lang, target_lang)
        if cached:
            translated_text = cached["translated_text"]
            print("Using cached translation.")
        else:
            translation_start = take_perf_sample()
            translated_text, confidence = self.translation_service.translate(text, source_lang, target_lang)
            translation_end = take_perf_sample()
            print(format_stage_metrics("Translation", translation_start, translation_end))
            if not translated_text:
                print("Translation failed.")
                return
            self.cache.set(text, source_lang, target_lang, translated_text, confidence=confidence)

        print(f"Translated: {translated_text}")

        tts_start = take_perf_sample()
        if self.tts_service.speak(translated_text, target_lang):
            tts_end = take_perf_sample()
            print(format_stage_metrics("TTS", tts_start, tts_end))
        else:
            print("Text-to-speech failed.")
            return

        log_system_snapshot(self.logger, f"after_pipeline_{source_lang}_to_{target_lang}")

        self._maybe_refine_translation(text, translated_text, source_lang, target_lang)

    def _maybe_refine_translation(self, text: str, translated_text: str, source_lang: str, target_lang: str):
        """Optionally queue cloud refinement."""
        if not self.claude_client.is_enabled():
            return

        cached = self.cache.get(text, source_lang, target_lang)
        if cached and cached["cloud_refined"]:
            return

        print("Cloud refinement queued in background.")

        def notify_refined(refined_text: str):
            self.cache.set_cloud_refinement(text, source_lang, target_lang, refined_text)
            print(f"\nCloud refined: {refined_text}")
            print("translator> ", end="", flush=True)

        self.claude_client.refine_translation_async(
            text,
            translated_text,
            source_lang,
            target_lang,
            callback=notify_refined,
        )


def main():
    """Main application entry point."""
    args = build_parser().parse_args()

    log_file = create_log_file("logs")
    config = get_config()
    debug_mode = config.is_debug_mode()
    setup_logger(
        "",
        debug_mode=debug_mode,
        log_file=log_file,
        log_level=config.get_log_level(),
    )

    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Starting Offline Translator Terminal Application")
    logger.info("=" * 60)
    log_system_snapshot(logger, "startup")

    if sys.version_info < (3, 9):
        logger.error("Python 3.9+ required")
        sys.exit(1)

    if not args.skip_preflight:
        try:
            logger.info("Running startup preflight for offline model assets...")
            prime_required_assets(config)
        except Exception as exc:
            logger.error(f"Startup preflight failed: {exc}", exc_info=True)
            sys.exit(1)

    app = TerminalTranslatorApp(stt_only=args.stt_only)
    try:
        sys.exit(app.run())
    finally:
        log_system_snapshot(logger, "shutdown")
        if app.tts_service is not None:
            app.tts_service.shutdown()
        if app.stt_service is not None:
            app.stt_service.unload_model()
        if app.translation_service is not None:
            app.translation_service.unload_model()


if __name__ == "__main__":
    main()
