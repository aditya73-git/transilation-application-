# Offline Translator (desktop GUI)

Push-to-talk offline translator: **faster-whisper** (STT) → **Helsinki-NLP Marian/OPUS** models (translation) → **Piper** (TTS). Designed for laptops and Raspberry Pi–class hardware.

## Features

- Push-to-talk PyQt5 GUI
- Offline STT, translation, and TTS (no cloud required for core pipeline)
- Optional Claude API refinement when online (`src/config.yaml` + `.env`)
- Translation cache (SQLite) and configurable models in `src/config.yaml`

## Requirements

| Resource | Guidance |
|----------|----------|
| **Python** | 3.9 or newer |
| **RAM** | ~4 GB minimum for `whisper_model: tiny`; **8 GB** recommended for `base` + Marian + Piper |
| **Disk** | Several GB for Hugging Face caches and Piper voices (see startup preflight) |
| **Display** | Graphical session for the GUI (`python -m src.main`) |
| **Audio** | Microphone + speakers; on Linux, PipeWire (`pw-record` / `pw-play`) or PortAudio (`sounddevice`) |

## Install on a new machine (from scratch)

### 1. Get the code

```bash
git clone <your-repo-url> transilation-application
cd transilation-application
```

(Or unpack a ZIP and `cd` into the project folder.)

### 2. System packages (Linux)

Install build/audio helpers so `sounddevice` and PyTorch-friendly wheels resolve cleanly:

**Debian / Ubuntu / Raspberry Pi OS**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev build-essential \
  libportaudio2 portaudio19-dev libsndfile1
```

For the same audio path the app often uses on desktop Linux (PipeWire):

```bash
sudo apt install -y pipewire pipewire-audio-client-libraries
```

Optional: `ffmpeg` is useful for general audio tooling; faster-whisper does not require it for basic use.

### 3. Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Environment file (optional)

Used for API keys referenced in `src/config.yaml` (cloud refinement, etc.):

```bash
cp src/.env.example .env
# Edit .env if you enable cloud features
```

### 5. Configuration

- Main settings: **`src/config.yaml`** (Whisper size, language pairs, Piper paths, cache, cloud flags).
- First launch with **`models.auto_download: true`** (default) will download STT/translation assets via Hugging Face when online; Piper voice files are validated/downloaded per your preflight settings.

### 6. Run the application

From the project root, with the venv activated:

```bash
python -m src.main
```

On first run, startup **preflight** may take several minutes while models are cached. Later starts are faster.

### 7. Optional: verify the pipeline without the GUI

```bash
python test_pipeline.py
```

## How it works (high level)

```
Microphone → STT (faster-whisper) → Translation (Marian/OPUS, pivot via English if needed)
    → TTS (Piper) → speakers
         ↘ optional: Claude refinement (if enabled + API key)
```

## Troubleshooting

| Issue | What to try |
|-------|-------------|
| **Imports / missing wheels** | Ensure Python 3.9+, upgraded `pip`, and on Linux the `apt` packages above. |
| **PyQt5 / display** | Run under a desktop session; for SSH use X11 forwarding or run on the device console. |
| **Microphone** | Check PipeWire/PulseAudio; if `pw-record` is missing, install PipeWire packages or rely on `sounddevice` + PortAudio. |
| **Piper not found** | `piper` should be on `PATH` (venv’s `bin` after `pip install piper-tts`). Voice `.onnx` paths must match `src/config.yaml`. |
| **Hugging Face / downloads** | First run needs network; set `HF_HOME` in `.env` to move the cache. |
| **RAM / swap** | In `src/config.yaml`, reduce `whisper_model` (e.g. `tiny`), lower `translation_max_loaded_models`, or disable heavy pairs. |

## Project layout (important paths)

```
src/
  main.py              # Entry point
  config.py            # Loads config.yaml + .env
  config.yaml          # Models, audio, cloud, UI
  services/            # STT, translation, TTS, languages, connectivity
  ui/main_window.py    # GUI
  utils/               # Audio, cache, logging, performance, Pi check
  startup_preflight.py # Offline asset checks / downloads
logs/                  # Application logs
```

## Development

Install in editable mode (optional):

```bash
pip install -e .
```

Console script (if configured in `setup.py`): `translator` → `src.main:main`.

## License

MIT License
