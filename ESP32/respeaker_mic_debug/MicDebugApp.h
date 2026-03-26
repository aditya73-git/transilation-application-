#pragma once

#include <AudioTools.h>

#include <string.h>

#include "MicDebugConfig.h"

using namespace audio_tools;

class MicDebugApp {
 public:
  void begin() {
    Serial.begin(115200);
    delay(1000);

    AudioLogger::instance().begin(Serial, AudioLogger::Warning);

    info_ = AudioInfo(
        mic_debug_config::kSampleRate,
        mic_debug_config::kChannels,
        mic_debug_config::kBitsPerSample);

    cfg_ = i2s_.defaultConfig(RX_MODE);
    cfg_.copyFrom(info_);
    cfg_.pin_bck = mic_debug_config::kI2SBclkPin;
    cfg_.pin_ws = mic_debug_config::kI2SWsPin;
    cfg_.pin_data = mic_debug_config::kI2SDataOutPin;
    cfg_.pin_data_rx = mic_debug_config::kI2SDataInPin;
    cfg_.is_master = false;

    Serial.println("ReSpeaker mic debug starting...");
    if (!i2s_.begin(cfg_)) {
      Serial.println("I2S RX begin failed");
      while (true) {
        delay(1000);
      }
    }

    lastReportMs_ = millis();
    lastAudioMs_ = millis();

    Serial.println("I2S RX ready");
    Serial.println("Expected firmware mode: XMOS I2S firmware");
    Serial.println("Reading mic over I2S at 16kHz, stereo, 32-bit");
    Serial.println("Speak near the board and watch peak/avg/nonzero values");
  }

  void tick() {
    const size_t n = i2s_.readBytes(buffer_, sizeof(buffer_));
    if (n > 0) {
      consume(n);
      lastAudioMs_ = millis();
    }

    const unsigned long now = millis();
    if ((now - lastReportMs_) >= mic_debug_config::kReportIntervalMs) {
      report(now);
    }

    if ((now - lastAudioMs_) > mic_debug_config::kSilenceWarnMs) {
      Serial.printf("WARN no mic data for %lums\n", now - lastAudioMs_);
      lastAudioMs_ = now;
    }
  }

 private:
  AudioInfo info_;
  I2SStream i2s_;
  I2SConfig cfg_;
  uint8_t buffer_[mic_debug_config::kReadBytes];

  unsigned long lastReportMs_ = 0;
  unsigned long lastAudioMs_ = 0;

  uint32_t leftPeak_ = 0;
  uint32_t rightPeak_ = 0;
  uint64_t leftAbsSum_ = 0;
  uint64_t rightAbsSum_ = 0;
  uint32_t frames_ = 0;
  uint32_t nonZeroFrames_ = 0;

  void consume(size_t n) {
    const size_t usable = n - (n % 8);
    for (size_t i = 0; i + 7 < usable; i += 8) {
      int32_t left32 = 0;
      int32_t right32 = 0;
      memcpy(&left32, buffer_ + i, sizeof(left32));
      memcpy(&right32, buffer_ + i + 4, sizeof(right32));

      const int16_t left16 = static_cast<int16_t>(left32 >> 16);
      const int16_t right16 = static_cast<int16_t>(right32 >> 16);

      const uint32_t leftMag =
          left16 < 0 ? static_cast<uint32_t>(-static_cast<int32_t>(left16))
                     : static_cast<uint32_t>(left16);
      const uint32_t rightMag =
          right16 < 0 ? static_cast<uint32_t>(-static_cast<int32_t>(right16))
                      : static_cast<uint32_t>(right16);

      if (leftMag > leftPeak_) {
        leftPeak_ = leftMag;
      }
      if (rightMag > rightPeak_) {
        rightPeak_ = rightMag;
      }

      leftAbsSum_ += leftMag;
      rightAbsSum_ += rightMag;
      frames_++;

      if (leftMag > 8 || rightMag > 8) {
        nonZeroFrames_++;
      }
    }
  }

  void report(unsigned long now) {
    const float leftPeak = leftPeak_ / 32768.0f;
    const float rightPeak = rightPeak_ / 32768.0f;
    const float leftAvg =
        frames_ ? (leftAbsSum_ / static_cast<float>(frames_)) / 32768.0f : 0.0f;
    const float rightAvg =
        frames_ ? (rightAbsSum_ / static_cast<float>(frames_)) / 32768.0f : 0.0f;
    const float activeRatio = frames_ ? nonZeroFrames_ / static_cast<float>(frames_) : 0.0f;

    Serial.printf(
        "MIC peak L=%.4f R=%.4f avg L=%.4f R=%.4f active=%.2f frames=%lu\n",
        leftPeak,
        rightPeak,
        leftAvg,
        rightAvg,
        activeRatio,
        static_cast<unsigned long>(frames_));

    leftPeak_ = 0;
    rightPeak_ = 0;
    leftAbsSum_ = 0;
    rightAbsSum_ = 0;
    frames_ = 0;
    nonZeroFrames_ = 0;
    lastReportMs_ = now;
  }
};
