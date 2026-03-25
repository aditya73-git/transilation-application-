"""TCP transport for ReSpeaker ESP: mic stream (16/32-bit stereo LE) and playback to device."""
from __future__ import annotations

import audioop
import socket
import struct
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)

TARGET_RATE = 16000


def pcm16_stereo_bytes_to_mono_float32(pcm: bytes) -> np.ndarray:
    """Little-endian int16 stereo interleaved -> float32 mono [-1, 1]."""
    if not pcm or len(pcm) < 4:
        return np.array([], dtype=np.float32)
    n = len(pcm) - (len(pcm) % 4)
    if n <= 0:
        return np.array([], dtype=np.float32)
    arr = np.frombuffer(pcm[:n], dtype="<i2").reshape(-1, 2)
    mono = arr.astype(np.float32).mean(axis=1) / 32768.0
    return mono


def pcm32_stereo_bytes_to_mono_float32(pcm: bytes) -> np.ndarray:
    """Little-endian int32 stereo interleaved -> float32 mono [-1, 1]."""
    if not pcm or len(pcm) < 8:
        return np.array([], dtype=np.float32)
    n = len(pcm) - (len(pcm) % 8)
    if n <= 0:
        return np.array([], dtype=np.float32)
    arr = np.frombuffer(pcm[:n], dtype="<i4").reshape(-1, 2)
    mono = arr.astype(np.float32).mean(axis=1) / 2147483648.0
    return mono


def pcm_stereo_bytes_to_mono_float32(pcm: bytes, sample_width: int) -> np.ndarray:
    """Little-endian stereo PCM -> float32 mono for 16-bit or 32-bit sample widths."""
    if sample_width == 4:
        return pcm32_stereo_bytes_to_mono_float32(pcm)
    return pcm16_stereo_bytes_to_mono_float32(pcm)


def mono16_to_stereo16le(raw: bytes) -> bytes:
    out = bytearray()
    for (s,) in struct.iter_unpack("<h", raw):
        out.extend(struct.pack("<hh", s, s))
    return bytes(out)


def stream_wav_to_esp(host: str, port: int, wav_path: str | Path, timeout: float = 30.0) -> bool:
    """
    Stream WAV as raw 16 kHz stereo int16 LE (4 bytes/frame) to ESP playback TCP server.
    Resamples / converts like tools/stream_wav_to_respeaker.py but 16-bit stereo output.
    """
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        logger.error("WAV not found: %s", wav_path)
        return False

    CHUNK = 320  # 20 ms at 16 kHz mono before stereo duplicate

    try:
        with wave.open(str(wav_path), "rb") as wf, socket.create_connection(
            (host, port), timeout=timeout
        ) as sock:
            state: Optional[object] = None
            while True:
                raw = wf.readframes(CHUNK)
                if not raw:
                    break
                sample_width = wf.getsampwidth()
                channels = wf.getnchannels()
                rate = wf.getframerate()

                if sample_width != 2:
                    raw = audioop.lin2lin(raw, sample_width, 2)
                    sample_width = 2

                if channels == 2:
                    raw = audioop.tomono(raw, sample_width, 0.5, 0.5)
                    channels = 1
                elif channels != 1:
                    logger.error("Unsupported channels: %s", channels)
                    return False

                if rate != TARGET_RATE:
                    raw, state = audioop.ratecv(
                        raw, sample_width, channels, rate, TARGET_RATE, state
                    )

                stereo = mono16_to_stereo16le(raw)
                sock.sendall(stereo)
                # Pace roughly real-time to avoid overrunning ESP buffers
                import time

                time.sleep(len(raw) / 2 / TARGET_RATE)
        return True
    except OSError as e:
        logger.error("ESP playback failed: %s", e)
        return False


def test_connection(host: str, mic_port: int, play_port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """Quick TCP connect check to ESP mic and play ports (no audio)."""
    if not host:
        return False, "Host empty"
    for label, port in (("mic", mic_port), ("play", play_port)):
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
        except OSError as e:
            return False, f"{label} port {port}: {e}"
    return True, "ok"
