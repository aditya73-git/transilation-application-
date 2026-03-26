#pragma once

#include <Arduino.h>

namespace full_duplex_debug_config {

inline constexpr char kWifiSsid[] = "Adityahotspot";
inline constexpr char kWifiPassword[] = "12345678";

inline constexpr uint16_t kMicPort = 12346;
inline constexpr uint16_t kPlaybackPort = 12345;

inline constexpr uint32_t kSampleRate = 16000;
inline constexpr uint8_t kChannels = 2;
inline constexpr uint8_t kBitsPerSample = 32;

inline constexpr int kI2SBclkPin = 8;
inline constexpr int kI2SWsPin = 7;
inline constexpr int kI2SDataOutPin = 43;
inline constexpr int kI2SDataInPin = 44;

inline constexpr size_t kReadBytes = 4096;
inline constexpr size_t kMicNetBytes = 2048;
inline constexpr size_t kPlaybackNetBytes = 2048;
inline constexpr size_t kPlaybackI2SBytes = 4096;
inline constexpr size_t kMicRingBytes = 65536;

inline constexpr unsigned long kMeterIntervalMs = 1000;
inline constexpr unsigned long kPlaybackIdleTimeoutMs = 750;
inline constexpr unsigned long kPlaybackLogIntervalMs = 1000;

}  // namespace full_duplex_debug_config
