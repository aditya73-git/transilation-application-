# Offline Translator - Terminal Pi Version

A terminal-first offline translator application designed for Raspberry Pi hardware.

## Features

- 🎙️ Terminal-driven recording and translation flow
- 🌐 4 supported languages:
  - English ↔ German
  - English ↔ Italian
  - English ↔ Hindi
  - German ↔ Italian/Hindi via English pivot
  - Italian ↔ Hindi via English pivot
- 📴 Fully offline translation (no internet required)
- ☁️ Optional cloud refinement for better quality translations
- 💾 Translation caching for repeated phrases
- 🔊 Text-to-speech output
- ⚙️ Configurable models and settings

## Architecture

```
Audio Input (Microphone)
    ↓
[STT] Speech-to-Text (faster-whisper)
    ↓
[Detect Language]
    ↓
[Translation] Marian / OPUS models
    ↓
[TTS] Text-to-Speech
    ↓
Audio Output (Speakers)
    ↓
[Optional] Cloud Refinement (Background)
```

## Installation

### Prerequisites
- Python 3.9+
- 8GB+ RAM (for comfortable model loading)
- Audio devices (microphone and speakers)

### Setup

1. **Clone or navigate to the project directory**
   ```bash
   cd /path/to/offline-translator
   ```

2. **Create virtual environment** (if not already done)
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   ```bash
   cp src/.env.example .env
   # Edit .env and add your API keys (optional)
   ```

5. **Configure settings** (optional)
   Edit `src/config.yaml` to customize:
   - Model sizes (for faster/slower hardware)
   - Language pairs
   - Audio settings
   - Cloud API keys

## Usage

### Run the Terminal Application
```bash
python -m src.main
```

This will:
1. Load language models (first run takes 2-3 minutes)
2. Initialize audio system
3. Start the terminal command loop
4. Await terminal commands

### Using the Application

1. Start the application
2. Use `pairs`, `set english german`, `next`, or `prev` to choose a language pair
3. Use `record` to capture speech from the microphone until you press Enter
4. Use `text hello world` to translate typed input
5. Use `status`, `devices`, and `stt-only` as needed

## Configuration

Edit `src/config.yaml` to customize:

### Model Selection
```yaml
offline:
  # For laptop: use 'base' or 'small'
  # For Pi: will use 'tiny'
  whisper_model: base
  whisper_compute_type: int8

  # Translation model routing
  translation_strategy: pivot_english

  # TTS engine
  tts_engine: piper
```

### Audio Settings
```yaml
audio:
  sample_rate: 16000  # Whisper requirement
  channels: 1         # Mono
  max_duration: 30    # Max recording time
```

### Cloud Integration
```yaml
cloud:
  enabled: false      # Set to true to enable Claude refinements
  use_refinement: false  # Background translation improvement
```

## Performance

Expected timing on modern laptop:
- STT (10 seconds audio): 1-3 seconds depending on CPU and model cache
- Translation (20 words): 1-2 seconds
- TTS (20 words): 2-3 seconds
- **Total E2E**: 5-8 seconds

## Troubleshooting

### Models won't download
- Check internet connection
- Set `HF_HOME` environment variable to custom cache location
- Models (~3GB total) require significant disk space

### Microphone not detected
- Check ALSA / PipeWire device availability
- Run the `devices` command in the terminal app
- Verify `pw-record`, `arecord`, or `sounddevice` works on the Pi

### High latency
- Reduce model size in config (tiny → base)
- Use GPU if available (cuda in config)
- Close other applications to free RAM

### Poor translation quality
- Enable cloud refinement if online
- Pivot routes (for example German → Hindi) may be less natural than direct language-pair models

## Project Structure

```
src/
├── main.py                      # Application entry point
├── config.py                    # Configuration loader
├── config.yaml                  # Configuration file
│
├── services/
│   ├── stt_service.py          # Speech-to-text
│   ├── translation_service.py  # Translation
│   ├── tts_service.py          # Text-to-speech
│   ├── language_service.py     # Language management
│   └── connectivity_service.py # Internet detection
│
├── utils/
│   ├── logger.py               # Logging setup
│   ├── cache.py                # Translation cache
│   └── audio_handler.py        # Audio I/O
│
└── cloud/
    └── claude_client.py        # Cloud refinement client
```

## Next Steps - Pi Deployment

When moving to Raspberry Pi:
1. Switch to quantized models (`tiny-int8`, `418M-int8`)
2. Add GPIO button handling
3. Integrate with system audio (USB soundcard)
4. Create systemd service for auto-start
5. Optimize for power consumption

## Development Notes

- **Models are cached** in `./models/` after first download
- **Translations cached** in SQLite database (`cache.db`)
- **Logs stored** in `./logs/` directory
- **Thread-safe** design for async cloud refinement

## License

MIT License

## Contributing

Pull requests welcome!

## Support

For issues or questions, check:
1. Configuration in `src/config.yaml`
2. Logs in `logs/` directory
3. GitHub issues

---

**Status**: Alpha (terminal Pi testing phase)
**Target**: Raspberry Pi 4/5 with bone-conduction earpiece
