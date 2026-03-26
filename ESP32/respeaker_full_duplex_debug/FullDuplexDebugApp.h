#pragma once

#include <AudioTools.h>
#include <WiFi.h>

#include "FullDuplexDebugConfig.h"
#include "FullDuplexMic.h"
#include "FullDuplexPlayback.h"

using namespace audio_tools;

class FullDuplexDebugApp {
 public:
  void begin() {
    Serial.begin(115200);
    delay(1000);

    AudioLogger::instance().begin(Serial, AudioLogger::Warning);

    AudioInfo info(
        full_duplex_debug_config::kSampleRate,
        full_duplex_debug_config::kChannels,
        full_duplex_debug_config::kBitsPerSample);

    cfg_ = i2s_.defaultConfig(RXTX_MODE);
    cfg_.copyFrom(info);
    cfg_.pin_bck = full_duplex_debug_config::kI2SBclkPin;
    cfg_.pin_ws = full_duplex_debug_config::kI2SWsPin;
    cfg_.pin_data = full_duplex_debug_config::kI2SDataOutPin;
    cfg_.pin_data_rx = full_duplex_debug_config::kI2SDataInPin;
    cfg_.is_master = false;

    Serial.println("ReSpeaker full-duplex WiFi debug starting...");
    if (!i2s_.begin(cfg_)) {
      Serial.println("I2S RXTX begin failed");
      while (true) {
        delay(1000);
      }
    }
    Serial.println("I2S mode -> RXTX");

    i2sMutex_ = xSemaphoreCreateMutex();
    if (i2sMutex_ == nullptr) {
      Serial.println("I2S mutex create failed");
      while (true) {
        delay(1000);
      }
    }

    connectWifi();
    mic_.begin(i2s_, i2sMutex_);
    playback_.begin(i2s_, i2sMutex_);

    mic_.printEndpoint(WiFi.localIP());
    playback_.printEndpoint(WiFi.localIP());
    Serial.println("Network format: 16kHz stereo int16 LE");
    Serial.println("I2S format: 16kHz stereo int32 LE");
    Serial.println("Mic capture is always on; playback can run at the same time");
  }

  void tick() {
    mic_.tick();
    playback_.tick();
  }

 private:
  I2SStream i2s_;
  I2SConfig cfg_;
  SemaphoreHandle_t i2sMutex_ = nullptr;
  FullDuplexMic mic_;
  FullDuplexPlayback playback_;

  void connectWifi() {
    WiFi.setSleep(false);
    Serial.println("Connecting to WiFi...");
    WiFi.begin(
        full_duplex_debug_config::kWifiSsid,
        full_duplex_debug_config::kWifiPassword);
    while (WiFi.status() != WL_CONNECTED) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
    Serial.printf("WiFi connected, IP=%s\n", WiFi.localIP().toString().c_str());
  }
};
