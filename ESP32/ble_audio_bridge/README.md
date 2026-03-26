# BLE Audio Bridge for ReSpeaker Lite

This sketch is a Bluetooth Low Energy audio transport for the ReSpeaker Lite / XIAO ESP32S3.

Important limitation: the `ESP32-S3` supports Bluetooth LE, but not Bluetooth Classic audio profiles such as `A2DP` or `HFP`. That means this sketch does not make the board behave like a normal Bluetooth speaker or headset. Instead, it exposes a custom BLE GATT service for low-bandwidth audio exchange with your own app.

## What it does

- Sends microphone audio from the ReSpeaker Lite to a BLE central with notifications
- Receives playback audio from the BLE central with GATT writes
- Reuses the existing ReSpeaker Lite I2S pinout already used in this repo:
  - `BCK`: `8`
  - `WS`: `7`
  - `DOUT`: `43`
  - `DIN`: `44`

## Audio format

- BLE mic stream: `8 kHz`, `mono`, `16-bit little-endian PCM`
- BLE speaker input: `8 kHz`, `mono`, `16-bit little-endian PCM`
- ReSpeaker Lite I2S side: `16 kHz`, `stereo`, `32-bit`

The sketch downsamples microphone audio from I2S to BLE and upsamples playback audio from BLE back to I2S. This keeps the BLE packets small enough to be practical on the `ESP32-S3`.

## Arduino dependencies

- `ESP32 Arduino Core`
- `AudioTools` by Phil Schatzmann

## Files

- `ble_audio_bridge.ino`: main sketch

## BLE service layout

- Service UUID: `7a1c1000-4f9f-4c4e-9f6d-8f84d0051000`
- Mic notify/read characteristic: `7a1c1001-4f9f-4c4e-9f6d-8f84d0051000`
- Speaker write/write-without-response characteristic: `7a1c1002-4f9f-4c4e-9f6d-8f84d0051000`
- Status read/notify characteristic: `7a1c1003-4f9f-4c4e-9f6d-8f84d0051000`

## How to use it

1. Flash `ble_audio_bridge.ino` to the XIAO ESP32S3 on the ReSpeaker Lite.
2. From your phone or desktop app, connect to `ReSpeaker-BLE-Audio`.
3. Request a larger MTU such as `247`.
4. Subscribe to the mic characteristic for incoming PCM chunks.
5. Write raw PCM chunks to the speaker characteristic for playback.

## Practical notes

- This is best for push-to-talk or short low-rate audio, not hi-fi streaming.
- When playback data is arriving, the sketch pauses mic notifications and switches the I2S path to speaker output.
- After playback goes idle, it switches back to mic mode automatically.
