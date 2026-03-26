"""Translation service with Marian, M2M100, and NLLB backends."""
from collections import OrderedDict
import gc
import threading
from typing import Dict, Optional, Tuple

import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

from src.config import get_config
from src.startup_preflight import ensure_required_assets, get_cached_snapshot_path
from src.utils.logger import get_logger

logger = get_logger(__name__)


class TranslationService:
    """Translate between the offline GUI languages."""

    def __init__(self):
        """Initialize translation service."""
        config = get_config()
        self.config = config.get_translation_config()
        self.device = self.config.get("device", "cpu")
        self.mode = self.config.get("mode", "quality")
        self.strategy = self.config.get("strategy", "pivot_english")
        self.pivot_language = self.config.get("pivot_language", "english").lower().strip()
        self.max_loaded_models = max(1, int(self.config.get("max_loaded_models", 2)))
        self.model_specs = self.config.get("models", {})
        self.m2m_model_name = self.config.get("m2m_model", "facebook/m2m100_418M")
        self.m2m_language_codes = self.config.get("m2m_language_codes", {})
        self.quality_model_name = self.config.get("quality_model", "facebook/nllb-200-distilled-600M")
        self.quality_language_codes = self.config.get("quality_language_codes", {})
        self.loaded_pipelines = OrderedDict()
        self._load_lock = threading.RLock()

    def _normalize_language(self, language: str) -> str:
        """Normalize a language name from the GUI."""
        return language.lower().strip()

    def _get_model_spec(self, source_lang: str, target_lang: str) -> Optional[Dict[str, str]]:
        """Return the configured model spec for one direct translation hop."""
        source_lang = self._normalize_language(source_lang)
        target_lang = self._normalize_language(target_lang)
        return self.model_specs.get(source_lang, {}).get(target_lang)

    def _load_pipeline(self, model_name: str):
        """Load or reuse a translation pipeline."""
        with self._load_lock:
            if model_name in self.loaded_pipelines:
                tokenizer, model = self.loaded_pipelines.pop(model_name)
                self.loaded_pipelines[model_name] = (tokenizer, model)
                return tokenizer, model

            logger.info("Loading translation model: %s", model_name)
            local_model_path = get_cached_snapshot_path(model_name)
            if local_model_path is None:
                local_model_path = ensure_required_assets(get_config(), local_files_only=True)[model_name]

            tokenizer = AutoTokenizer.from_pretrained(local_model_path, local_files_only=True)
            model = AutoModelForSeq2SeqLM.from_pretrained(
                local_model_path,
                local_files_only=True,
                use_safetensors=False,
            )
            model = model.to(self.device)
            model.eval()
            self.loaded_pipelines[model_name] = (tokenizer, model)

            while len(self.loaded_pipelines) > self.max_loaded_models:
                old_model_name, (_, old_model) = self.loaded_pipelines.popitem(last=False)
                logger.info("Unloading translation model: %s", old_model_name)
                del old_model
                gc.collect()
                if self.device.startswith("cuda") and torch.cuda.is_available():
                    torch.cuda.empty_cache()

            logger.info("Translation model ready on %s", self.device)
            return tokenizer, model

    def _m2m_lang_code(self, language: str) -> str:
        """Return the M2M100 language code for one GUI language."""
        language = self._normalize_language(language)
        code = self.m2m_language_codes.get(language)
        if not code:
            raise ValueError(f"No M2M100 translation code configured for {language}")
        return code

    def _translate_m2m(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate with M2M100."""
        tokenizer, model = self._load_pipeline(self.m2m_model_name)
        source_code = self._m2m_lang_code(source_lang)
        target_code = self._m2m_lang_code(target_lang)

        tokenizer.src_lang = source_code
        inputs = tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
        ).to(self.device)

        forced_bos_token_id = tokenizer.get_lang_id(target_code)

        with torch.no_grad():
            generated_tokens = model.generate(
                **inputs,
                forced_bos_token_id=forced_bos_token_id,
                max_length=512,
                num_beams=4,
            )

        return tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)[0].strip()

    def _quality_lang_code(self, language: str) -> str:
        """Return the NLLB language code for one GUI language."""
        language = self._normalize_language(language)
        code = self.quality_language_codes.get(language)
        if not code:
            raise ValueError(f"No quality translation code configured for {language}")
        return code

    def _translate_quality(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate with the higher-quality NLLB model."""
        tokenizer, model = self._load_pipeline(self.quality_model_name)
        source_code = self._quality_lang_code(source_lang)
        target_code = self._quality_lang_code(target_lang)

        tokenizer.src_lang = source_code
        inputs = tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
        ).to(self.device)

        forced_bos_token_id = None
        if hasattr(tokenizer, "lang_code_to_id") and target_code in tokenizer.lang_code_to_id:
            forced_bos_token_id = tokenizer.lang_code_to_id[target_code]
        else:
            forced_bos_token_id = tokenizer.convert_tokens_to_ids(target_code)

        with torch.no_grad():
            generated_tokens = model.generate(
                **inputs,
                forced_bos_token_id=forced_bos_token_id,
                max_length=512,
                num_beams=4,
            )

        return tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)[0].strip()

    def get_cache_namespace(self) -> str:
        """Return a cache namespace for the active translation backend."""
        if self.mode == "balanced":
            return f"balanced:{self.m2m_model_name}"
        if self.mode == "quality":
            return f"quality:{self.quality_model_name}"
        return "fast:marian"

    def get_route_model_names(self, source_lang: str, target_lang: str) -> list[str]:
        """Return the direct or pivot model ids needed for one pair."""
        source_lang = self._normalize_language(source_lang)
        target_lang = self._normalize_language(target_lang)

        if source_lang == target_lang:
            return []

        if self.mode == "balanced":
            self._m2m_lang_code(source_lang)
            self._m2m_lang_code(target_lang)
            return [self.m2m_model_name]

        if self.mode == "quality":
            self._quality_lang_code(source_lang)
            self._quality_lang_code(target_lang)
            return [self.quality_model_name]

        direct = self._get_model_spec(source_lang, target_lang)
        if direct is not None:
            return [direct["model"]]

        if self.strategy != "pivot_english":
            raise ValueError(f"No direct translation route configured for {source_lang} -> {target_lang}")

        source_to_pivot = self._get_model_spec(source_lang, self.pivot_language)
        pivot_to_target = self._get_model_spec(self.pivot_language, target_lang)
        if source_to_pivot is None:
            raise ValueError(f"No offline route configured for {source_lang} -> {self.pivot_language}")
        if pivot_to_target is None:
            raise ValueError(f"No offline route configured for {self.pivot_language} -> {target_lang}")

        return [source_to_pivot["model"], pivot_to_target["model"]]

    def warm_pair(self, source_lang: str, target_lang: str):
        """Preload the direct or pivot models for a selected pair."""
        model_names = self.get_route_model_names(source_lang, target_lang)
        if not model_names:
            return
        logger.info(
            "Warming translation route for %s -> %s using %s",
            source_lang,
            target_lang,
            ", ".join(model_names),
        )
        for model_name in model_names:
            self._load_pipeline(model_name)

    def _translate_direct(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate a single hop using a configured pair model."""
        spec = self._get_model_spec(source_lang, target_lang)
        if spec is None:
            raise ValueError(f"No offline translation model configured for {source_lang} -> {target_lang}")

        tokenizer, model = self._load_pipeline(spec["model"])
        input_prefix = spec.get("input_prefix", "")
        input_text = f"{input_prefix}{text}" if input_prefix else text
        inputs = tokenizer(
            input_text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
        ).to(self.device)

        with torch.no_grad():
            generated_tokens = model.generate(
                **inputs,
                max_length=512,
                num_beams=1,
            )

        return tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)[0].strip()

    def translate(self, text: str, source_lang: str, target_lang: str) -> Tuple[str, float]:
        """Translate text from source to target language."""
        try:
            source_lang = self._normalize_language(source_lang)
            target_lang = self._normalize_language(target_lang)
            text = text.strip()

            if not text:
                return "", 0.0

            if source_lang == target_lang:
                return text, 1.0

            logger.info("Translating (%s -> %s): %s...", source_lang, target_lang, text[:50])

            if self.mode == "balanced":
                translated_text = self._translate_m2m(text, source_lang, target_lang)
                confidence = 0.89
            elif self.mode == "quality":
                translated_text = self._translate_quality(text, source_lang, target_lang)
                confidence = 0.92
            elif self._get_model_spec(source_lang, target_lang):
                translated_text = self._translate_direct(text, source_lang, target_lang)
                confidence = 0.85
            else:
                if self.strategy != "pivot_english":
                    raise ValueError(f"No direct translation route configured for {source_lang} -> {target_lang}")

                if self._get_model_spec(source_lang, self.pivot_language) is None:
                    raise ValueError(f"No offline route configured for {source_lang} -> {self.pivot_language}")
                if self._get_model_spec(self.pivot_language, target_lang) is None:
                    raise ValueError(f"No offline route configured for {self.pivot_language} -> {target_lang}")

                pivot_text = self._translate_direct(text, source_lang, self.pivot_language)
                translated_text = self._translate_direct(pivot_text, self.pivot_language, target_lang)
                confidence = 0.72
                logger.info(
                    "Pivot translation used (%s -> %s -> %s)",
                    source_lang,
                    self.pivot_language,
                    target_lang,
                )

            logger.info("Translation complete: %s...", translated_text[:50])
            return translated_text, confidence

        except Exception as e:
            logger.error(f"Translation error: {e}")
            return "", 0.0

    def translate_batch(self, texts: list, source_lang: str, target_lang: str) -> list:
        """Translate multiple texts one by one."""
        results = []
        for text in texts:
            translated, _ = self.translate(text, source_lang, target_lang)
            results.append(translated)
        return results

    def get_supported_languages(self):
        """Get list of supported languages."""
        languages = set(self.model_specs.keys())
        for targets in self.model_specs.values():
            languages.update(targets.keys())
        return sorted(languages)

    def set_device(self, device: str):
        """Change device (cpu or cuda)."""
        self.device = device
        self.unload_model()
        logger.info("Translation service moved to %s", device)

    def set_mode(self, mode: str):
        """Switch translation backend mode and clear loaded models."""
        mode = mode.lower().strip()
        if mode not in {"fast", "balanced", "quality"}:
            raise ValueError(f"Unsupported translation mode: {mode}")
        if self.mode != mode:
            self.mode = mode
            self.unload_model()
            logger.info("Translation mode switched to %s", mode)

    def unload_model(self):
        """Unload translation models to free memory."""
        if self.loaded_pipelines:
            self.loaded_pipelines.clear()
            gc.collect()
            if self.device.startswith("cuda") and torch.cuda.is_available():
                torch.cuda.empty_cache()
            logger.info("Translation models unloaded")


_translation_instance = None


def get_translation_service() -> TranslationService:
    """Get global translation service instance."""
    global _translation_instance
    if _translation_instance is None:
        _translation_instance = TranslationService()
    return _translation_instance
