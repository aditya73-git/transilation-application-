#!/usr/bin/env python3
"""
Receive raw PCM from ReSpeaker ESP mic TCP port (default 12346).

WIFI_DUAL_AUDIO (current default): 16 kHz stereo int32 LE (8 bytes/frame).
MIC_STREAM_WIFI (AudioTools): 16 kHz stereo int16 LE (4 bytes/frame).

Usage:
  python3 receive_mic_stream.py --host 10.42.0.27 --pcm-width 4 --convert-16 -o captured.wav
  python3 receive_mic_stream.py --host 10.42.0.27 --pcm-width 4 -o raw32.wav
"""

import argparse
import socket
import struct
import sys
import wave

SAMPLE_RATE = 16000
CHANNELS = 2


def update_meter_from_pcm16le(chunk: bytes, meter: dict) -> None:
    """Track simple peak/rms-like stats for int16 PCM."""
    usable = len(chunk) - (len(chunk) % 2)
    if usable <= 0:
        return
    peak = meter["peak"]
    sum_abs = meter["sum_abs"]
    samples = meter["samples"]
    for (sample,) in struct.iter_unpack("<h", chunk[:usable]):
        value = abs(sample)
        if value > peak:
            peak = value
        sum_abs += value
        samples += 1
    meter["peak"] = peak
    meter["sum_abs"] = sum_abs
    meter["samples"] = samples


def update_meter_from_pcm32le(chunk: bytes, meter: dict) -> None:
    """Track simple peak/rms-like stats for int32 PCM."""
    usable = len(chunk) - (len(chunk) % 4)
    if usable <= 0:
        return
    peak = meter["peak"]
    sum_abs = meter["sum_abs"]
    samples = meter["samples"]
    for (sample,) in struct.iter_unpack("<i", chunk[:usable]):
        value = abs(sample) / 65536.0
        if value > peak:
            peak = value
        sum_abs += value
        samples += 1
    meter["peak"] = peak
    meter["sum_abs"] = sum_abs
    meter["samples"] = samples


def maybe_print_meter(meter: dict, label: str) -> None:
    """Print a once-per-second signal meter."""
    import time

    now = time.monotonic()
    if now - meter["last_print"] < 1.0:
        return
    samples = meter["samples"]
    peak = meter["peak"] / 32768.0 if meter["peak"] else 0.0
    avg = (meter["sum_abs"] / samples) / 32768.0 if samples else 0.0
    print(f"{label} meter: peak={peak:.4f} avg_abs={avg:.4f} samples={samples}")
    meter["last_print"] = now
    meter["peak"] = 0
    meter["sum_abs"] = 0
    meter["samples"] = 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Record mic stream from ESP to WAV")
    parser.add_argument("--host", required=True, help="ESP32 IP (same WiFi as laptop)")
    parser.add_argument("--port", type=int, default=12346, help="Mic TCP port (default 12346)")
    parser.add_argument("-o", "--output", default="mic_from_esp.wav", help="Output WAV path")
    parser.add_argument(
        "--pcm-width",
        type=int,
        choices=(2, 4),
        default=4,
        help="Bytes per sample per channel (4=int32 WIFI_DUAL, 2=int16 AudioTools mic-only)",
    )
    parser.add_argument(
        "--convert-16",
        action="store_true",
        help="If --pcm-width 4: convert to 16-bit stereo in the WAV",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Print a simple once-per-second peak/average level meter while receiving",
    )
    args = parser.parse_args()

    samp_width = args.pcm_width
    frame_bytes = CHANNELS * samp_width
    meter = {"peak": 0, "sum_abs": 0, "samples": 0, "last_print": 0.0}

    def i32_to_i16(x: int) -> int:
        x >>= 16
        if x > 32767:
            return 32767
        if x < -32768:
            return -32768
        return x

    with socket.create_connection((args.host, args.port), timeout=15) as sock:
        print(f"Connected to {args.host}:{args.port} (PCM {samp_width * 8}-bit stereo)")
        print(f"Recording to {args.output} — Ctrl+C to stop\n")

        if samp_width == 4 and args.convert_16:
            with wave.open(args.output, "wb") as wav:
                wav.setnchannels(CHANNELS)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                try:
                    while True:
                        chunk = sock.recv(8192)
                        if not chunk:
                            break
                        if len(chunk) < 8:
                            continue
                        n = len(chunk) - (len(chunk) % 8)
                        out = bytearray()
                        for i in range(0, n, 8):
                            L, R = struct.unpack_from("<ii", chunk, i)
                            out.extend(struct.pack("<hh", i32_to_i16(L), i32_to_i16(R)))
                        out_bytes = bytes(out)
                        wav.writeframes(out_bytes)
                        if args.analyze:
                            update_meter_from_pcm16le(out_bytes, meter)
                            maybe_print_meter(meter, "RX")
                except KeyboardInterrupt:
                    print("\nStopped.")
        else:
            with wave.open(args.output, "wb") as wav:
                wav.setnchannels(CHANNELS)
                wav.setsampwidth(samp_width)
                wav.setframerate(SAMPLE_RATE)
                try:
                    while True:
                        chunk = sock.recv(8192)
                        if not chunk:
                            break
                        align = frame_bytes
                        chunk = chunk[: len(chunk) - (len(chunk) % align)]
                        wav.writeframes(chunk)
                        if args.analyze:
                            if samp_width == 2:
                                update_meter_from_pcm16le(chunk, meter)
                            else:
                                update_meter_from_pcm32le(chunk, meter)
                            maybe_print_meter(meter, "RX")
                except KeyboardInterrupt:
                    print("\nStopped.")

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
