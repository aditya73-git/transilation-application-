#pragma once

#include <Arduino.h>

namespace step16_serial_test_config {

inline constexpr int kSdaPin = 5;
inline constexpr int kSclPin = 6;
inline constexpr uint8_t kAddress = 0x48;
inline constexpr uint32_t kBaudRate = 115200;
inline constexpr unsigned long kPollIntervalMs = 50;

}  // namespace step16_serial_test_config
