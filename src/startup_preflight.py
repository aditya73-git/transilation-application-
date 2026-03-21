"""Startup preflight for downloading and validating required offline models."""
from __future__ import annotations

import logging
from pathlib import Path
import shutil
import subprocess
from typing import Dict, List
from urllib.request import Request, urlopen

from huggingface_hub import snapshot_download
from huggingface_hub.utils import disable_progress_bars


logger = logging.getLogger(__name__)

_SNAPSHOT_CACHE: Dict[str, str] = {}
TRANSLATION_REQUIRED_ANY = (
    "model.safetensors",
    "model.safetensors.index.json",
    "pytorch_model.bin",
    "pytorch_model.bin.index.json",
)
TRANSLATION_REQUIRED_ALL = (
    "config.json",
    "tokenizer_config.json",
)
WHISPER_REQUIRED_ALL = (
    "config.json",
    "tokenizer.json",
    "vocabulary.txt",
    "model.bin",
)


def _is_path_like(value: str) -> bool:
    """Return True if the value already looks like a local filesystem path."""
    return Path(value).exists() or value.startswith(".") or value.startswith("/")


def resolve_whisper_repo_id(model_name: str) -> str:
    """Map the configured faster-whisper model name to a Hugging Face repo id."""
    if _is_path_like(model_name):
        return model_name
    if "/" in model_name:
        return model_name
    return f"Systran/faster-whisper-{model_name}"


def _required_translation_model_ids(config) -> List[str]:
    """Collect unique translation repo ids from config."""
    model_specs = config.get_translation_config().get("models", {})
    model_ids = []
    for targets in model_specs.values():
        for spec in targets.values():
            model_id = spec.get("model")
            if model_id and model_id not in model_ids:
                model_ids.append(model_id)
    return model_ids


def _snapshot_is_complete(path: str, required_all=(), required_any=()) -> bool:
    """Return True if the downloaded snapshot contains the files we need."""
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        return False

    for filename in required_all:
        if not (snapshot_path / filename).exists():
            return False

    if required_any and not any((snapshot_path / filename).exists() for filename in required_any):
        return False

    return True


def _format_size(num_bytes: int | None) -> str:
    """Return a human-friendly file size string."""
    if not num_bytes:
        return "unknown size"

    value = float(num_bytes)
    units = ("B", "KB", "MB", "GB")
    unit_index = 0
    while value >= 1024.0 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    return f"{value:.2f} {units[unit_index]}"


def _download_file(url: str, destination: Path) -> int:
    """Download one file to destination atomically and return its size."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".part")

    request = Request(url, headers={"User-Agent": "offline-translator-preflight/1.0"})
    with urlopen(request, timeout=120) as response, temp_path.open("wb") as output_file:
        total_bytes = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output_file.write(chunk)
            total_bytes += len(chunk)

    temp_path.replace(destination)
    return total_bytes


def _download_repo(
    repo_id: str,
    *,
    local_files_only: bool,
    required_all=(),
    required_any=(),
) -> str:
    """Download or resolve a cached snapshot for one repo."""
    if _is_path_like(repo_id):
        path = str(Path(repo_id).resolve())
        _SNAPSHOT_CACHE[repo_id] = path
        return path

    cached_path = _SNAPSHOT_CACHE.get(repo_id)
    if cached_path and _snapshot_is_complete(cached_path, required_all=required_all, required_any=required_any):
        return cached_path

    try:
        path = snapshot_download(
            repo_id=repo_id,
            local_files_only=True,
        )
        if _snapshot_is_complete(path, required_all=required_all, required_any=required_any):
            _SNAPSHOT_CACHE[repo_id] = path
            return path
    except Exception:
        path = None

    if local_files_only:
        raise RuntimeError(f"Cached snapshot is missing required files for {repo_id}")

    path = snapshot_download(
        repo_id=repo_id,
        local_files_only=False,
        force_download=True,
    )
    if not _snapshot_is_complete(path, required_all=required_all, required_any=required_any):
        if local_files_only:
            raise RuntimeError(f"Cached snapshot is missing required files for {repo_id}")
        raise RuntimeError(f"Downloaded snapshot is still incomplete for {repo_id}")

    _SNAPSHOT_CACHE[repo_id] = path
    return path


def ensure_required_assets(config, *, local_files_only: bool = False) -> Dict[str, str]:
    """
    Ensure all startup-required model assets are present locally.

    Returns:
        Mapping of repo id -> local snapshot path.
    """
    disable_progress_bars()

    resolved_paths: Dict[str, str] = {}

    whisper_model = config.get_whisper_model().get("model", "base")
    whisper_repo = resolve_whisper_repo_id(whisper_model)
    logger.info("Checking offline STT assets...")
    try:
        resolved_paths[whisper_repo] = _download_repo(
            whisper_repo,
            local_files_only=local_files_only,
            required_all=WHISPER_REQUIRED_ALL,
        )
    except Exception as e:
        raise RuntimeError(f"Missing required STT model: {whisper_repo}") from e
    logger.info("STT assets ready: %s", whisper_repo)

    translation_repos = _required_translation_model_ids(config)
    logger.info("Checking %s offline translation models...", len(translation_repos))
    for index, repo_id in enumerate(translation_repos, start=1):
        logger.info("Preparing translation model %s/%s: %s", index, len(translation_repos), repo_id)
        try:
            resolved_paths[repo_id] = _download_repo(
                repo_id,
                local_files_only=local_files_only,
                required_all=TRANSLATION_REQUIRED_ALL,
                required_any=TRANSLATION_REQUIRED_ANY,
            )
        except Exception as e:
            raise RuntimeError(f"Missing required translation model: {repo_id}") from e

    logger.info("All required offline models are available locally")
    return resolved_paths


def ensure_required_tts_assets(config) -> Dict[str, str]:
    """Ensure Piper binary and configured voice files exist locally."""
    tts_config = config.get_tts_config()
    engine = tts_config.get("engine", "piper")
    if engine != "piper":
        raise RuntimeError(f"Unsupported TTS engine configured: {engine}")

    voice_models = tts_config.get("piper_voice_models", {})
    if not voice_models:
        raise RuntimeError("No Piper voices configured in offline.piper_voice_models")

    auto_download = config.get("models.auto_download", True)
    resolved_paths: Dict[str, str] = {}
    logger.info("Checking Piper TTS voices...")
    for language, spec in voice_models.items():
        model_path = Path(spec.get("model", "")).expanduser().resolve()
        config_path = Path(spec.get("config", "")).expanduser().resolve()

        model_url = spec.get("model_url", "")
        config_url = spec.get("config_url", "")

        if not model_path.exists():
            if not auto_download or not model_url:
                raise RuntimeError(f"Missing Piper voice model for {language}: {model_path}")
            logger.info("Downloading Piper voice model for %s -> %s", language, model_path)
            model_size = _download_file(model_url, model_path)
            logger.info(
                "Downloaded Piper voice model for %s (%s)",
                language,
                _format_size(model_size),
            )

        if not config_path.exists():
            if not auto_download or not config_url:
                raise RuntimeError(f"Missing Piper voice config for {language}: {config_path}")
            logger.info("Downloading Piper voice config for %s -> %s", language, config_path)
            config_size = _download_file(config_url, config_path)
            logger.info(
                "Downloaded Piper voice config for %s (%s)",
                language,
                _format_size(config_size),
            )

        resolved_paths[f"{language}_model"] = str(model_path)
        resolved_paths[f"{language}_config"] = str(config_path)

    piper_binary = tts_config.get("piper_binary", "piper")
    resolved_binary = shutil.which(piper_binary)
    if not resolved_binary:
        raise RuntimeError(
            f"Missing Piper binary: {piper_binary}. Install Piper or update offline.piper_binary."
        )

    try:
        probe = subprocess.run(
            [resolved_binary, "--help"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to execute Piper binary: {resolved_binary}") from e
    if probe.returncode != 0:
        stderr = (probe.stderr or "").strip()
        raise RuntimeError(
            f"Piper binary is installed but unusable: {stderr or f'exit code {probe.returncode}'}"
        )

    resolved_paths["piper_binary"] = resolved_binary
    logger.info("All required Piper voices are available locally")
    return resolved_paths


def get_cached_snapshot_path(repo_id: str) -> str | None:
    """Return the cached snapshot path for a repo, if known."""
    return _SNAPSHOT_CACHE.get(repo_id)


def prime_required_assets(config) -> Dict[str, str]:
    """Download all required assets up front during startup."""
    resolved = ensure_required_assets(config, local_files_only=False)
    resolved.update(ensure_required_tts_assets(config))
    return resolved
