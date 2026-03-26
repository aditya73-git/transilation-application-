#pragma once

#include <Arduino.h>

namespace i2c_safe_scanner_config {

// ReSpeaker Lite exposes the XIAO ESP32S3 I2C bus on these pins.
inline constexpr int kSdaPin = 5;
inline constexpr int kSclPin = 6;

inline constexpr uint32_t kBaudRate = 115200;
inline constexpr unsigned long kScanIntervalMs = 2000;

}  // namespace i2c_safe_scanner_config
