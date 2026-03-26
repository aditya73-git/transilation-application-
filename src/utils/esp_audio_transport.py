"""Wi-Fi and BLE transports for ReSpeaker ESP audio."""
from __future__ import annotations

import asyncio
import audioop
import socket
import struct
import time
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)

TARGET_RATE = 16000
BLE_AUDIO_RATE = 8000
BLE_WRITE_CHUNK_SAMPLES = 80  # 10 ms @ 8 kHz fits comfortably in one BLE write

BLE_SERVICE_UUID = "7a1c1000-4f9f-4c4e-9f6d-8f84d0051000"
BLE_MIC_CHAR_UUID = "7a1c1001-4f9f-4c4e-9f6d-8f84d0051000"
BLE_SPEAKER_CHAR_UUID = "7a1c1002-4f9f-4c4e-9f6d-8f84d0051000"
BLE_STATUS_CHAR_UUID = "7a1c1003-4f9f-4c4e-9f6d-8f84d0051000"

try:
    from bleak import BleakClient, BleakScanner

    BLEAK_AVAILABLE = True
except ImportError:
    BleakClient = None
    BleakScanner = None
    BLEAK_AVAILABLE = False


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


def pcm16_mono_bytes_to_float32(pcm: bytes) -> np.ndarray:
    """Little-endian int16 mono PCM -> float32 mono [-1, 1]."""
    if not pcm or len(pcm) < 2:
        return np.array([], dtype=np.float32)
    n = len(pcm) - (len(pcm) % 2)
    if n <= 0:
        return np.array([], dtype=np.float32)
    arr = np.frombuffer(pcm[:n], dtype="<i2").astype(np.float32)
    return arr / 32768.0


def pcm_stereo_bytes_to_mono_float32(pcm: bytes, sample_width: int) -> np.ndarray:
    """Little-endian stereo PCM -> float32 mono for 16-bit or 32-bit sample widths."""
    if sample_width == 4:
        return pcm32_stereo_bytes_to_mono_float32(pcm)
    return pcm16_stereo_bytes_to_mono_float32(pcm)


def resample_pcm16_mono(
    pcm: bytes,
    source_rate: int,
    target_rate: int,
    state: Optional[object] = None,
) -> tuple[bytes, Optional[object]]:
    """Resample 16-bit mono PCM bytes to a new sample rate."""
    if not pcm or source_rate == target_rate:
        return pcm, state
    converted, state = audioop.ratecv(pcm, 2, 1, source_rate, target_rate, state)
    return converted, state


def ble_pcm16_bytes_to_mono_float32(
    pcm: bytes,
    source_rate: int = BLE_AUDIO_RATE,
    target_rate: int = TARGET_RATE,
) -> np.ndarray:
    """BLE mono PCM16 -> float32 mono at the application's target sample rate."""
    resampled, _ = resample_pcm16_mono(pcm, source_rate, target_rate)
    return pcm16_mono_bytes_to_float32(resampled)


def mono16_to_stereo16le(raw: bytes) -> bytes:
    out = bytearray()
    for (sample,) in struct.iter_unpack("<h", raw):
        out.extend(struct.pack("<hh", sample, sample))
    return bytes(out)


def _ble_target_label(device_name: str = "", device_address: str = "") -> str:
    if device_address:
        return device_address
    if device_name:
        return device_name
    return "<unknown BLE device>"


async def _find_ble_device(device_name: str = "", device_address: str = "", scan_timeout: float = 8.0):
    if not BLEAK_AVAILABLE:
        raise RuntimeError("Bleak is not installed. Install the 'bleak' Python package first.")

    if device_address:
        device = await BleakScanner.find_device_by_address(device_address, timeout=scan_timeout)
        if device is not None:
            return device

    if device_name:
        lowered = device_name.strip().lower()

        def match_name(device, _advertisement_data):
            return bool(device.name and device.name.strip().lower() == lowered)

        device = await BleakScanner.find_device_by_filter(match_name, timeout=scan_timeout)
        if device is not None:
            return device

    raise RuntimeError(
        f"Unable to find BLE device {_ble_target_label(device_name, device_address)}"
    )


async def _open_ble_client(
    device_name: str = "",
    device_address: str = "",
    scan_timeout: float = 8.0,
    connect_timeout: float = 15.0,
):
    device = await _find_ble_device(device_name=device_name, device_address=device_address, scan_timeout=scan_timeout)
    client = BleakClient(device, timeout=connect_timeout)
    await client.connect()
    logger.info(
        "Connected to ESP BLE device %s (mtu=%s)",
        _ble_target_label(device_name, device_address),
        getattr(client, "mtu_size", "unknown"),
    )
    return client


async def _stream_ble_mic_audio_async(
    device_name: str = "",
    device_address: str = "",
    stop_event=None,
    on_chunk: Optional[Callable[[bytes], None]] = None,
    scan_timeout: float = 8.0,
    connect_timeout: float = 15.0,
) -> bool:
    client = None
    try:
        client = await _open_ble_client(
            device_name=device_name,
            device_address=device_address,
            scan_timeout=scan_timeout,
            connect_timeout=connect_timeout,
        )

        def handle_chunk(_handle: int, data: bytearray):
            if on_chunk is not None:
                on_chunk(bytes(data))

        await client.start_notify(BLE_MIC_CHAR_UUID, handle_chunk)
        logger.info("ESP BLE mic notifications started")

        while client.is_connected and (stop_event is None or not stop_event.is_set()):
            await asyncio.sleep(0.05)

        if client.is_connected:
            await client.stop_notify(BLE_MIC_CHAR_UUID)
        return True
    except Exception as exc:
        logger.error("ESP BLE mic streaming failed: %s", exc)
        return False
    finally:
        if client is not None:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass


def stream_ble_mic_audio(
    device_name: str = "",
    device_address: str = "",
    stop_event=None,
    on_chunk: Optional[Callable[[bytes], None]] = None,
    scan_timeout: float = 8.0,
    connect_timeout: float = 15.0,
) -> bool:
    """Subscribe to the BLE mic characteristic until stop_event is set."""
    return asyncio.run(
        _stream_ble_mic_audio_async(
            device_name=device_name,
            device_address=device_address,
            stop_event=stop_event,
            on_chunk=on_chunk,
            scan_timeout=scan_timeout,
            connect_timeout=connect_timeout,
        )
    )


async def _write_ble_audio_chunk(client, payload: bytes) -> None:
    try:
        await client.write_gatt_char(BLE_SPEAKER_CHAR_UUID, payload, response=False)
    except Exception:
        await client.write_gatt_char(BLE_SPEAKER_CHAR_UUID, payload, response=True)


async def _stream_wav_to_esp_ble_async(
    device_name: str,
    device_address: str,
    wav_path: str | Path,
    timeout: float = 30.0,
    scan_timeout: float = 8.0,
) -> bool:
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        logger.error("WAV not found: %s", wav_path)
        return False

    client = None
    try:
        client = await _open_ble_client(
            device_name=device_name,
            device_address=device_address,
            scan_timeout=scan_timeout,
            connect_timeout=min(timeout, 15.0),
        )

        state: Optional[object] = None
        with wave.open(str(wav_path), "rb") as wf:
            while True:
                raw = wf.readframes(BLE_WRITE_CHUNK_SAMPLES)
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
                    logger.error("Unsupported channels for BLE playback: %s", channels)
                    return False

                if rate != BLE_AUDIO_RATE:
                    raw, state = audioop.ratecv(raw, sample_width, channels, rate, BLE_AUDIO_RATE, state)

                usable = len(raw) - (len(raw) % 2)
                if usable <= 0:
                    continue

                payload = raw[:usable]
                await _write_ble_audio_chunk(client, payload)
                await asyncio.sleep((usable / 2) / BLE_AUDIO_RATE)

        logger.info("ESP BLE playback completed")
        return True
    except Exception as exc:
        logger.error("ESP BLE playback failed: %s", exc)
        return False
    finally:
        if client is not None:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass


def stream_wav_to_esp_ble(
    device_name: str,
    device_address: str,
    wav_path: str | Path,
    timeout: float = 30.0,
    scan_timeout: float = 8.0,
) -> bool:
    """Stream a WAV file to the BLE speaker characteristic as 8 kHz mono PCM16."""
    return asyncio.run(
        _stream_wav_to_esp_ble_async(
            device_name=device_name,
            device_address=device_address,
            wav_path=wav_path,
            timeout=timeout,
            scan_timeout=scan_timeout,
        )
    )


def stream_wav_to_esp(host: str, port: int, wav_path: str | Path, timeout: float = 30.0) -> bool:
    """
    Stream WAV as raw 16 kHz stereo int16 LE (4 bytes/frame) to ESP playback TCP server.
    Resamples / converts like tools/stream_wav_to_respeaker.py but 16-bit stereo output.
    """
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        logger.error("WAV not found: %s", wav_path)
        return False

    chunk_frames = 320  # 20 ms at 16 kHz mono before stereo duplicate

    try:
        with wave.open(str(wav_path), "rb") as wf, socket.create_connection(
            (host, port), timeout=timeout
        ) as sock:
            state: Optional[object] = None
            while True:
                raw = wf.readframes(chunk_frames)
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
                    raw, state = audioop.ratecv(raw, sample_width, channels, rate, TARGET_RATE, state)

                stereo = mono16_to_stereo16le(raw)
                sock.sendall(stereo)
                time.sleep(len(raw) / 2 / TARGET_RATE)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        return True
    except OSError as exc:
        logger.error("ESP playback failed: %s", exc)
        return False


async def _test_ble_connection_async(
    device_name: str = "",
    device_address: str = "",
    scan_timeout: float = 8.0,
) -> tuple[bool, str]:
    client = None
    try:
        client = await _open_ble_client(
            device_name=device_name,
            device_address=device_address,
            scan_timeout=scan_timeout,
            connect_timeout=15.0,
        )
        try:
            status = await client.read_gatt_char(BLE_STATUS_CHAR_UUID)
            return True, status.decode("utf-8", errors="replace")
        except Exception:
            return True, "connected"
    except Exception as exc:
        return False, str(exc)
    finally:
        if client is not None:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass


def test_connection(
    transport: str = "wifi",
    host: str = "",
    mic_port: int = 12346,
    play_port: int = 12345,
    device_name: str = "",
    device_address: str = "",
    scan_timeout: float = 8.0,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """Quick ESP transport connectivity check."""
    if transport == "ble":
        return asyncio.run(
            _test_ble_connection_async(
                device_name=device_name,
                device_address=device_address,
                scan_timeout=scan_timeout,
            )
        )

    if not host:
        return False, "Host empty"

    for label, port in (("mic", mic_port), ("play", play_port)):
        try:
            connection = socket.create_connection((host, port), timeout=timeout)
            connection.close()
        except OSError as exc:
            return False, f"{label} port {port}: {exc}"
    return True, "ok"
