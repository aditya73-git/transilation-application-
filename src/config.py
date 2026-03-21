"""Configuration loader module"""
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Configuration handler for the application"""

    def __init__(self, config_path="src/config.yaml"):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self._resolve_env_variables()

    def _load_config(self):
        """Load configuration from YAML file"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(self.config_path, "r") as f:
            return yaml.safe_load(f)

    def _resolve_env_variables(self):
        """Replace ${VAR} with environment variables"""

        def resolve_dict(d):
            if isinstance(d, dict):
                for key, value in d.items():
                    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                        env_var = value[2:-1]
                        d[key] = os.getenv(env_var, "")
                    elif isinstance(value, (dict, list)):
                        resolve_dict(value)
            elif isinstance(d, list):
                for i, item in enumerate(d):
                    if isinstance(item, str) and item.startswith("${") and item.endswith("}"):
                        env_var = item[2:-1]
                        d[i] = os.getenv(env_var, "")
                    elif isinstance(item, (dict, list)):
                        resolve_dict(item)

        resolve_dict(self.config)

    def get(self, key, default=None):
        """Get configuration value using dot notation (e.g., 'languages.default_pair')"""
        keys = key.split(".")
        value = self.config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default

        return value

    def get_languages(self):
        """Get list of supported languages"""
        return self.config.get("languages", {}).get("supported", [])

    def get_language_codes(self):
        """Get language code mappings"""
        return self.config.get("languages", {}).get("code_mappings", {})

    def get_default_pair(self):
        """Get default language pair"""
        pair = self.config.get("languages", {}).get("default_pair", {})
        return (pair.get("source"), pair.get("target"))

    def get_whisper_model(self):
        """Get Whisper model configuration"""
        return {
            "model": self.config.get("offline", {}).get("whisper_model", "base"),
            "device": self.config.get("offline", {}).get("whisper_device", "cpu"),
            "compute_type": self.config.get("offline", {}).get("whisper_compute_type", "int8"),
            "cpu_threads": self.config.get("offline", {}).get("whisper_cpu_threads", 0),
            "num_workers": self.config.get("offline", {}).get("whisper_num_workers", 1),
            "beam_size": self.config.get("offline", {}).get("whisper_beam_size", 1),
            "vad_filter": self.config.get("offline", {}).get("whisper_vad_filter", True),
        }

    def get_translation_config(self):
        """Get translation model configuration."""
        return {
            "strategy": self.config.get("offline", {}).get("translation_strategy", "pivot_english"),
            "pivot_language": self.config.get("offline", {}).get("translation_pivot_language", "english"),
            "device": self.config.get("offline", {}).get("translation_device", "cpu"),
            "max_loaded_models": self.config.get("offline", {}).get("translation_max_loaded_models", 2),
            "models": self.config.get("offline", {}).get("translation_models", {}),
        }

    def get_m2m_model(self):
        """Backward-compatible alias for translation configuration."""
        return self.get_translation_config()

    def get_tts_config(self):
        """Get TTS configuration"""
        return {
            "engine": self.config.get("offline", {}).get("tts_engine", "piper"),
            "speed": self.config.get("offline", {}).get("tts_voice_speed", 1.0),
            "volume": self.config.get("offline", {}).get("tts_voice_volume", 1.0),
            "piper_binary": self.config.get("offline", {}).get("piper_binary", "piper"),
            "piper_play_command": self.config.get("offline", {}).get("piper_play_command", "auto"),
            "piper_voice_models": self.config.get("offline", {}).get("piper_voice_models", {}),
        }

    def get_audio_config(self):
        """Get audio configuration"""
        return self.config.get("audio", {})

    def get_cloud_config(self):
        """Get cloud configuration"""
        return self.config.get("cloud", {})

    def get_cache_config(self):
        """Get cache configuration"""
        return self.config.get("cache", {})

    def is_debug_mode(self):
        """Check if debug mode is enabled"""
        return self.config.get("device", {}).get("debug_mode", False)

    def get_log_level(self):
        """Get configured application log level."""
        return self.config.get("device", {}).get("log_level", "INFO")

    def __repr__(self):
        return f"<Config: {self.config_path}>"


# Global config instance
_config_instance = None


def get_config():
    """Get global config instance"""
    global _config_instance
    if _config_instance is None:
        _config_instance = Config()
    return _config_instance
