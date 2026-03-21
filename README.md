# Offline Translator - Desktop GUI Version

A push-to-talk offline translator application designed for testing and development before deployment on Raspberry Pi hardware.

## Features

- 🎙️ Push-to-talk interface (GUI button replaces physical button)
- 🌐 7 supported language pairs:
  - English ↔ German
  - English ↔ Arabic
  - English ↔ Romanian
  - English ↔ Slovakian
  - English ↔ Turkish
  - English ↔ Polish
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
[Translation] M2M-100
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

### Run the GUI Application
```bash
python -m src.main
```

This will:
1. Load language models (first run takes 2-3 minutes)
2. Initialize audio system
3. Open the GUI window
4. Await button presses

### Using the Application

1. **Start Application** → Models load and display "Ready"
2. **Click "Press to Talk" Button** → Start recording
3. **Release Button** → Processing begins:
   - Speech-to-text (2-3 seconds)
   - Translation (1-2 seconds)
   - Text-to-speech (2-3 seconds)
   - Audio plays through speakers
4. **Switch Languages** ← Click/→ Buttons → Cycle through language pairs

### GUI Layout

```
┌─────────────────────────────────────────┐
│  🎤 OFFLINE TRANSLATOR - Desktop v0.1   │
├─────────────────────────────────────────┤
│  Status: Ready | 🟢 Offline | EN→DE     │
├─────────────────────────────────────────┤
│  SOURCE TEXT:                           │
│  [Recognized speech]                    │
│                                         │
│  TRANSLATED OUTPUT:                     │
│  [Translation result]                   │
├─────────────────────────────────────────┤
│        [Press to Talk]                  │
│  [← >>] Language Pair [>> →]            │
│        [Settings]                       │
├─────────────────────────────────────────┤
│  Log: Ready...                          │
└─────────────────────────────────────────┘
```

## Configuration

Edit `src/config.yaml` to customize:

### Model Selection
```yaml
offline:
  # For laptop: use 'base' or 'small'
  # For Pi: will use 'tiny'
  whisper_model: base
  whisper_compute_type: int8

  # Translation model (lightweight option)
  m2m_model: facebook/m2m100_418M

  # TTS engine
  tts_engine: pyttsx3
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
- Check audio settings in system preferences
- Run `sounddevice` test to list devices
- Select correct device in GUI settings

### High latency
- Reduce model size in config (tiny → base)
- Use GPU if available (cuda in config)
- Close other applications to free RAM

### Poor translation quality
- Enable cloud refinement if online
- This model trade-off is expected for offline-only operation

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
├── ui/
│   ├── main_window.py          # Main GUI window
│   ├── components.py           # Reusable widgets
│   └── styles.qss              # Stylesheet
│
├── utils/
│   ├── logger.py               # Logging setup
│   ├── cache.py                # Translation cache
│   └── audio_handler.py        # Audio I/O
│
└── cloud/
    └── claude_client.py        # Claude API client
```

## Next Steps - Pi Deployment

When moving to Raspberry Pi:
1. Switch to quantized models (`tiny-int8`, `418M-int8`)
2. Remove PyQt5 GUI → Use CLI or buttons
3. Add GPIO button handling
4. Integrate with system audio (USB soundcard)
5. Create systemd service for auto-start
6. Optimize for power consumption

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

**Status**: Alpha (Desktop testing phase)
**Target**: Raspberry Pi 4/5 with bone-conduction earpiece
