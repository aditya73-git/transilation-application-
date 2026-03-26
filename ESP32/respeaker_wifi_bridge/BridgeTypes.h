#pragma once

#include <Arduino.h>

enum class BridgeMode : uint8_t {
  IdleTx,
  MicRx,
  PlaybackTx,
};
