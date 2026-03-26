#pragma once

#include <Arduino.h>

namespace mic_debug_config {

inline constexpr uint32_t kSampleRate = 16000;
inline constexpr uint8_t kChannels = 2;
inline constexpr uint8_t kBitsPerSample = 32;

inline constexpr int kI2SBclkPin = 8;
inline constexpr int kI2SWsPin = 7;
inline constexpr int kI2SDataOutPin = 43;
inline constexpr int kI2SDataInPin = 44;

inline constexpr size_t kReadBytes = 4096;
inline constexpr unsigned long kReportIntervalMs = 1000;
inline constexpr unsigned long kSilenceWarnMs = 1500;

}  // namespace mic_debug_config
