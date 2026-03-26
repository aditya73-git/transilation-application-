#!/usr/bin/env python3
import argparse
import audioop
import socket
import sys
import time
import wave


TARGET_RATE = 16000
CHUNK_FRAMES = 320


def mono16_chunk(wav_file, state):
    raw = wav_file.readframes(CHUNK_FRAMES)
    if not raw:
        return b"", state

    sample_width = wav_file.getsampwidth()
    channels = wav_file.getnchannels()
    rate = wav_file.getframerate()

    if sample_width != 2:
        raw = audioop.lin2lin(raw, sample_width, 2)
        sample_width = 2

    if channels == 2:
        raw = audioop.tomono(raw, sample_width, 0.5, 0.5)
        channels = 1
    elif channels != 1:
        raise ValueError(f"Unsupported channel count: {channels}")

    if rate != TARGET_RATE:
        raw, state = audioop.ratecv(raw, sample_width, channels, rate, TARGET_RATE, state)

    return raw, state


def mono16_to_stereo16le(raw):
    return audioop.tostereo(raw, 2, 1.0, 1.0)


def main():
    parser = argparse.ArgumentParser(description="Stream a WAV file to ReSpeaker Lite over TCP.")
    parser.add_argument("wav_file", help="Path to a WAV file")
    parser.add_argument("--host", default="10.42.0.27", help="ESP32 IP address")
    parser.add_argument("--port", type=int, default=12345, help="ESP32 TCP port")
    args = parser.parse_args()

    with wave.open(args.wav_file, "rb") as wav_file:
        print(
            f"Input: {wav_file.getnchannels()}ch, "
            f"{wav_file.getsampwidth() * 8}-bit, {wav_file.getframerate()}Hz"
        )
        state = None

        with socket.create_connection((args.host, args.port), timeout=10) as sock:
            print(f"Streaming to tcp://{args.host}:{args.port}")
            while True:
                raw, state = mono16_chunk(wav_file, state)
                if not raw:
                    break
                payload = mono16_to_stereo16le(raw)
                sock.sendall(payload)
                chunk_samples = len(raw) // 2
                time.sleep(chunk_samples / TARGET_RATE)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
