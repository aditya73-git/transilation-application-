#pragma once

#include <Arduino.h>

namespace i2c_48_probe_config {

inline constexpr int kSdaPin = 5;
inline constexpr int kSclPin = 6;
inline constexpr uint8_t kDeviceAddress = 0x48;
inline constexpr uint32_t kBaudRate = 115200;
inline constexpr unsigned long kPollIntervalMs = 1000;

}  // namespace i2c_48_probe_config
