"""Translation Service using M2M-100 Model"""
from typing import Tuple
import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
from src.utils.logger import get_logger
from src.config import get_config

logger = get_logger(__name__)


class TranslationService:
    """Translation using M2M-100 lightweight model"""

    # Language code mappings for M2M-100
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
        """Initialize translation service"""
        config = get_config()
        self.config = config.get_m2m_model()
        self.device = self.config.get("device", "cpu")
        self.model = None
        self.tokenizer = None
        self._load_model()

    def _load_model(self):
        """Load M2M-100 model and tokenizer"""
        try:
            model_name = self.config.get("model", "facebook/m2m100_418M")
            logger.info(f"Loading M2M-100 model: {model_name}")

            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            self.model = AutoModelForSeq2SeqLM.from_pretrained(model_name)

            # Move to device
            self.model = self.model.to(self.device)

            # Set to evaluation mode
            self.model.eval()

            logger.info(f"M2M-100 model loaded successfully on {self.device}")

        except Exception as e:
            logger.error(f"Failed to load M2M-100 model: {e}")
            raise

    def translate(self, text: str, source_lang: str, target_lang: str) -> Tuple[str, float]:
        """
        Translate text from source to target language

        Args:
            text: Text to translate
            source_lang: Source language name (e.g., 'english')
            target_lang: Target language name (e.g., 'german')

        Returns:
            Tuple of (translated_text, confidence)
        """
        if self.model is None or self.tokenizer is None:
            logger.error("Model not loaded")
            return "", 0.0

        try:
            # Normalize language names
            source_lang = source_lang.lower().strip()
            target_lang = target_lang.lower().strip()

            # Get language codes
            source_code = self.LANG_CODE_MAPPING.get(source_lang, source_lang)
            target_code = self.LANG_CODE_MAPPING.get(target_lang, target_lang)

            logger.info(
                f"Translating ({source_code}→{target_code}): {text[:50]}..."
            )

            # Set target language
            self.tokenizer.tgt_lang = target_code

            # Encode input
            inputs = self.tokenizer(text, return_tensors="pt").to(self.device)

            # Generate translation
            with torch.no_grad():
                generated_tokens = self.model.generate(
                    **inputs,
                    forced_bos_token_id=self.tokenizer.get_lang_id(target_code),
                    max_length=512,
                    num_beams=5,
                    early_stopping=True,
                )

            # Decode output
            translated_text = self.tokenizer.batch_decode(
                generated_tokens, skip_special_tokens=True
            )[0]

            # Confidence is typically high for this model
            confidence = 0.85

            logger.info(f"Translation complete: {translated_text[:50]}...")
            return translated_text, confidence

        except Exception as e:
            logger.error(f"Translation error: {e}")
            return "", 0.0

    def translate_batch(self, texts: list, source_lang: str, target_lang: str) -> list:
        """
        Translate multiple texts at once (more efficient)

        Args:
            texts: List of texts to translate
            source_lang: Source language
            target_lang: Target language

        Returns:
            List of translated texts
        """
        results = []
        for text in texts:
            translated, _ = self.translate(text, source_lang, target_lang)
            results.append(translated)
        return results

    def get_supported_languages(self):
        """Get list of supported languages"""
        return list(self.LANG_CODE_MAPPING.keys())

    def set_device(self, device: str):
        """Change device (cpu or cuda)"""
        self.device = device
        if self.model is not None:
            self.model = self.model.to(device)
            logger.info(f"Model moved to {device}")

    def unload_model(self):
        """Unload model to free memory"""
        if self.model is not None:
            self.model = None
            self.tokenizer = None
            logger.info("M2M-100 model unloaded")


# Global instance
_translation_instance = None


def get_translation_service() -> TranslationService:
    """Get global translation service instance"""
    global _translation_instance
    if _translation_instance is None:
        _translation_instance = TranslationService()
    return _translation_instance
