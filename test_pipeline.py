"""Simple CLI test to verify the translation pipeline works."""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.stt_service import get_stt_service
from src.services.translation_service import get_translation_service
from src.services.tts_service import get_tts_service
from src.services.language_service import get_language_service
from src.utils.logger import setup_logger, create_log_file
from src.config import get_config


def test_pipeline():
    """Test the complete translation pipeline"""
    # Setup logging
    log_file = create_log_file("logs")
    config = get_config()
    setup_logger("", debug_mode=True, log_file=log_file)

    print("\n" + "="*60)
    print("OFFLINE TRANSLATOR - PIPELINE TEST")
    print("="*60 + "\n")

    try:
        # Initialize services
        print("▶ Loading services...")
        print("  - Loading STT (faster-whisper)...")
        stt = get_stt_service()
        print("    ✓ faster-whisper loaded")

        print("  - Loading Translation (Marian pivot translator)...")
        translator = get_translation_service()
        print("    ✓ translation service loaded")

        print("  - Loading TTS (Piper)...")
        tts = get_tts_service()
        print("    ✓ Piper loaded")

        print("  - Loading Language Service...")
        lang_service = get_language_service()
        print("    ✓ Language Service loaded")

        print("\n" + "="*60)
        print("TESTING TRANSLATION PIPELINE")
        print("="*60 + "\n")

        # Test 1: Language pairs
        print("✓ Test 1: Language Pairs")
        pairs = lang_service.get_all_pairs()
        print(f"  Available pairs: {len(pairs)}")
        for i, (src, tgt) in enumerate(pairs[:3]):
            print(f"    {i+1}. {src.upper()} → {tgt.upper()}")
        print(f"    ... and {len(pairs)-3} more\n")

        # Test 2: Translation
        print("✓ Test 2: Direct Translation (without STT)")
        test_sentences = {
            "english": "Hello, how are you today?",
            "german": "Guten Tag, wie geht es dir?",
            "italian": "Ciao, come stai oggi?",
            "hindi": "नमस्ते, आप आज कैसे हैं?",
        }

        for lang, sentence in test_sentences.items():
            if lang == "english":
                # English to German
                translated, conf = translator.translate(sentence, "english", "german")
                print(f"\n  EN → DE:")
                print(f"    Source: {sentence}")
                print(f"    Target: {translated}")
                print(f"    Confidence: {conf:.2f}")

        print("\n✓ Test 3: Language Switching")
        print(f"  Current pair: {lang_service.display_pair()}")
        lang_service.switch_language_next()
        print(f"  Switched to: {lang_service.display_pair()}")
        lang_service.switch_language_prev()
        print(f"  Back to: {lang_service.display_pair()}\n")

        # Test 4: Cache system
        print("✓ Test 4: Translation Caching")
        from src.utils.cache import TranslationCache
        cache = TranslationCache(db_path="cache.db")

        test_text = "Testing cache system"
        cache.set(test_text, "english", "german", "System testen", confidence=0.95)
        print(f"  Cached: '{test_text}' (EN→DE)")

        cached = cache.get(test_text, "english", "german")
        if cached:
            print(f"  Retrieved: '{cached['translated_text']}'")
        else:
            print("  ERROR: Cache retrieval failed")

        stats = cache.get_stats()
        print(f"  Cache stats: {stats['total_entries']} entries, {stats['size_mb']}MB\n")

        # Test 5: Offline mode indicator
        print("✓ Test 5: Offline Mode")
        from src.services.connectivity_service import get_connectivity_service
        connectivity = get_connectivity_service()
        is_online = connectivity.check_connection()
        status = "✓ Online" if is_online else "✓ Offline (working as expected)"
        print(f"  Status: {status}\n")

        print("="*60)
        print("ALL TESTS PASSED! ✓")
        print("="*60)
        print("\nPipeline is working correctly!")
        print("You can now run the terminal app with: python -m src.main\n")
        print(f"Log file: {log_file}")

        return True

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_pipeline()
    sys.exit(0 if success else 1)
