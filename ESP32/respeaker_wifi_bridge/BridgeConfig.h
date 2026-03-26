#pragma once

#include <Arduino.h>

namespace bridge_config {

inline constexpr char kWifiSsid[] = "Adityahotspot";
inline constexpr char kWifiPassword[] = "12345678";

inline constexpr uint16_t kPlaybackPort = 12345;
inline constexpr uint16_t kMicPort = 12346;

inline constexpr uint32_t kSampleRate = 16000;
inline constexpr uint8_t kChannels = 2;
inline constexpr uint8_t kBitsPerSample = 32;

inline constexpr int kI2SBclkPin = 8;
inline constexpr int kI2SWsPin = 7;
inline constexpr int kI2SDataOutPin = 43;
inline constexpr int kI2SDataInPin = 44;

inline constexpr size_t kMicChunkBytes = 4096;
inline constexpr size_t kPlaybackChunkBytes = 2048;

inline constexpr uint32_t kPlaybackFirstByteTimeoutMs = 500;
inline constexpr uint32_t kPlaybackIdleTimeoutMs = 250;
inline constexpr uint32_t kMicIdleRestartMs = 750;

}  // namespace bridge_config
