#!/usr/bin/env python3
"""
Cycle-test helper for the ReSpeaker mic/playback debug sketch.

1. Ask the ESP for mic audio for a fixed duration.
2. Save the captured 16-bit stereo audio to a WAV file.
3. Wait briefly for the ESP to switch into TX mode.
4. Send the same raw bytes back to the ESP playback port.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import wave


SAMPLE_RATE = 16000
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_BYTES = CHANNELS * SAMPLE_WIDTH


def record_raw(host: str, port: int, duration: float) -> bytes:
    deadline = time.monotonic() + duration
    chunks: list[bytes] = []

    with socket.create_connection((host, port), timeout=15) as sock:
        sock.settimeout(1.0)
        print(f"MIC connect tcp://{host}:{port}")
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                break
            usable = len(chunk) - (len(chunk) % FRAME_BYTES)
            if usable > 0:
                chunks.append(chunk[:usable])

    raw = b"".join(chunks)
    duration_s = len(raw) / FRAME_BYTES / SAMPLE_RATE
    print(f"Captured {len(raw)} bytes ({duration_s:.2f}s)")
    return raw


def save_wav(path: str, raw: bytes) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(raw)


def play_raw(host: str, port: int, raw: bytes) -> None:
    chunk_bytes = 320 * FRAME_BYTES
    with socket.create_connection((host, port), timeout=15) as sock:
        print(f"PLAY connect tcp://{host}:{port}")
        for i in range(0, len(raw), chunk_bytes):
            chunk = raw[i : i + chunk_bytes]
            if not chunk:
                break
            sock.sendall(chunk)
            time.sleep(len(chunk) / FRAME_BYTES / SAMPLE_RATE)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    print("Playback send complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Round-trip ReSpeaker cycle debug")
    parser.add_argument("--host", required=True, help="ESP IP")
    parser.add_argument("--mic-port", type=int, default=12346, help="ESP mic port")
    parser.add_argument("--play-port", type=int, default=12345, help="ESP playback port")
    parser.add_argument("--duration", type=float, default=5.0, help="Capture duration in seconds")
    parser.add_argument(
        "--play-delay",
        type=float,
        default=0.35,
        help="Delay after mic capture before playback starts",
    )
    parser.add_argument(
        "--output",
        default="/tmp/respeaker_cycle_debug.wav",
        help="Path to save the captured 16-bit WAV",
    )
    args = parser.parse_args()

    raw = record_raw(args.host, args.mic_port, args.duration)
    if not raw:
        print("No mic data captured")
        sys.exit(1)

    save_wav(args.output, raw)
    print(f"Saved {args.output}")
    if args.play_delay > 0:
        print(f"Waiting {args.play_delay:.2f}s for ESP TX state")
        time.sleep(args.play_delay)
    play_raw(args.host, args.play_port, raw)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
