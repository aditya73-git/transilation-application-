#pragma once

#include <AudioTools.h>
#include <WiFi.h>

#include <string.h>

#include "MicWifiDebugConfig.h"

using namespace audio_tools;

class MicWifiDebugApp {
 public:
  void begin() {
    Serial.begin(115200);
    delay(1000);

    AudioLogger::instance().begin(Serial, AudioLogger::Warning);

    info_ = AudioInfo(
        mic_wifi_debug_config::kSampleRate,
        mic_wifi_debug_config::kChannels,
        mic_wifi_debug_config::kBitsPerSample);

    cfg_ = i2s_.defaultConfig(RX_MODE);
    cfg_.copyFrom(info_);
    cfg_.pin_bck = mic_wifi_debug_config::kI2SBclkPin;
    cfg_.pin_ws = mic_wifi_debug_config::kI2SWsPin;
    cfg_.pin_data = mic_wifi_debug_config::kI2SDataOutPin;
    cfg_.pin_data_rx = mic_wifi_debug_config::kI2SDataInPin;
    cfg_.is_master = false;

    Serial.println("ReSpeaker mic WiFi debug starting...");
    if (!restartI2S()) {
      Serial.println("I2S RX begin failed");
      while (true) {
        delay(1000);
      }
    }

    connectWifi();
    micServer_.begin();
    lastMeterMs_ = millis();

    xTaskCreatePinnedToCore(
        micCaptureTaskEntry,
        "mic_capture_task",
        8192,
        this,
        1,
        &micCaptureTaskHandle_,
        1);

    Serial.printf(
        "Mic stream tcp://%s:%u\n",
        WiFi.localIP().toString().c_str(),
        mic_wifi_debug_config::kMicPort);
    Serial.println("Network format: 16kHz stereo int16 LE");
    Serial.println("I2S format: 16kHz stereo int32 LE");
    Serial.println("Mic capture is always on; network send is on-demand");
  }

  void tick() {
    serviceMic();
  }

 private:
  AudioInfo info_;
  I2SStream i2s_;
  I2SConfig cfg_;
  WiFiServer micServer_{mic_wifi_debug_config::kMicPort};
  WiFiClient micClient_;

  uint8_t micCaptureBuffer_[mic_wifi_debug_config::kReadBytes];
  uint8_t micBuffer_[mic_wifi_debug_config::kReadBytes];
  uint8_t micNetBuffer_[mic_wifi_debug_config::kNetMicBytes];
  uint8_t micRingBuffer_[mic_wifi_debug_config::kMicRingBytes];

  TaskHandle_t micCaptureTaskHandle_ = nullptr;
  portMUX_TYPE micRingMux_ = portMUX_INITIALIZER_UNLOCKED;
  volatile size_t micRingHead_ = 0;
  volatile size_t micRingTail_ = 0;

  unsigned long lastMeterMs_ = 0;
  uint32_t peak_ = 0;
  uint64_t absSum_ = 0;
  uint32_t frames_ = 0;

  uint32_t micSessionCount_ = 0;
  unsigned long micStartedMs_ = 0;
  uint32_t micBytes_ = 0;
  bool micSessionOpen_ = false;

  void connectWifi() {
    WiFi.setSleep(false);
    Serial.println("Connecting to WiFi...");
    WiFi.begin(
        mic_wifi_debug_config::kWifiSsid,
        mic_wifi_debug_config::kWifiPassword);
    while (WiFi.status() != WL_CONNECTED) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
    Serial.printf("WiFi connected, IP=%s\n", WiFi.localIP().toString().c_str());
  }

  bool restartI2S() {
    i2s_.end();
    delay(10);
    cfg_.rx_tx_mode = RX_MODE;
    if (!i2s_.begin(cfg_)) {
      return false;
    }
    Serial.println("I2S mode -> RX");
    delay(20);
    return true;
  }

  static void micCaptureTaskEntry(void* arg) {
    static_cast<MicWifiDebugApp*>(arg)->micCaptureTask();
  }

  void micCaptureTask() {
    for (;;) {
      const size_t n = i2s_.readBytes(micCaptureBuffer_, sizeof(micCaptureBuffer_));
      if (n > 0) {
        micRingWrite(micCaptureBuffer_, n);
      } else {
        vTaskDelay(pdMS_TO_TICKS(1));
      }
    }
  }

  size_t micRingAvailableLocked() const {
    if (micRingHead_ >= micRingTail_) {
      return micRingHead_ - micRingTail_;
    }
    return mic_wifi_debug_config::kMicRingBytes - (micRingTail_ - micRingHead_);
  }

  size_t micRingFreeLocked() const {
    return (mic_wifi_debug_config::kMicRingBytes - 1) - micRingAvailableLocked();
  }

  void micRingReset() {
    portENTER_CRITICAL(&micRingMux_);
    micRingHead_ = 0;
    micRingTail_ = 0;
    portEXIT_CRITICAL(&micRingMux_);
  }

  void micRingWrite(const uint8_t* data, size_t len) {
    len -= len % 8;
    if (len == 0) {
      return;
    }

    portENTER_CRITICAL(&micRingMux_);
    size_t freeSpace = micRingFreeLocked();
    if (len > freeSpace) {
      size_t drop = len - freeSpace;
      drop += (8 - (drop % 8)) % 8;
      micRingTail_ = (micRingTail_ + drop) % mic_wifi_debug_config::kMicRingBytes;
    }

    const size_t first = min(len, mic_wifi_debug_config::kMicRingBytes - micRingHead_);
    memcpy(micRingBuffer_ + micRingHead_, data, first);
    if (len > first) {
      memcpy(micRingBuffer_, data + first, len - first);
    }
    micRingHead_ = (micRingHead_ + len) % mic_wifi_debug_config::kMicRingBytes;
    portEXIT_CRITICAL(&micRingMux_);
  }

  size_t micRingRead(uint8_t* out, size_t maxLen) {
    maxLen -= maxLen % 8;
    if (maxLen == 0) {
      return 0;
    }

    portENTER_CRITICAL(&micRingMux_);
    size_t available = micRingAvailableLocked();
    size_t take = min(maxLen, available);
    take -= take % 8;
    if (take == 0) {
      portEXIT_CRITICAL(&micRingMux_);
      return 0;
    }

    const size_t first = min(take, mic_wifi_debug_config::kMicRingBytes - micRingTail_);
    memcpy(out, micRingBuffer_ + micRingTail_, first);
    if (take > first) {
      memcpy(out + first, micRingBuffer_, take - first);
    }
    micRingTail_ = (micRingTail_ + take) % mic_wifi_debug_config::kMicRingBytes;
    portEXIT_CRITICAL(&micRingMux_);
    return take;
  }

  void serviceMic() {
    if (!micClient_.connected()) {
      if (micSessionOpen_) {
        closeMicSession("client disconnected");
      }
      micClient_.stop();
      WiFiClient next = micServer_.available();
      if (!next) {
        delay(10);
        return;
      }

      micClient_ = next;
      micRingReset();
      micSessionCount_++;
      micStartedMs_ = millis();
      micBytes_ = 0;
      peak_ = 0;
      absSum_ = 0;
      frames_ = 0;
      lastMeterMs_ = millis();
      micSessionOpen_ = true;
      Serial.printf("MIC session #%lu connected\n", static_cast<unsigned long>(micSessionCount_));
    }

    const size_t n = micRingRead(micBuffer_, sizeof(micBuffer_));
    if (n == 0) {
      delay(1);
      return;
    }

    micBytes_ += static_cast<uint32_t>(n);
    updateMeter(n);
    maybeReportMeter();

    const size_t netBytes = convertStereo32ToStereo16(
        micBuffer_, n, micNetBuffer_, sizeof(micNetBuffer_));
    if (netBytes == 0) {
      delay(1);
      return;
    }

    if (!writeAll(micNetBuffer_, netBytes)) {
      closeMicSession("client disconnected");
    }
  }

  void updateMeter(size_t n) {
    const size_t usable = n - (n % 8);
    for (size_t i = 0; i + 7 < usable; i += 8) {
      int32_t left32 = 0;
      int32_t right32 = 0;
      memcpy(&left32, micBuffer_ + i, sizeof(left32));
      memcpy(&right32, micBuffer_ + i + 4, sizeof(right32));

      const int16_t left16 = static_cast<int16_t>(left32 >> 16);
      const int16_t right16 = static_cast<int16_t>(right32 >> 16);

      const uint32_t leftMag =
          left16 < 0 ? static_cast<uint32_t>(-static_cast<int32_t>(left16))
                     : static_cast<uint32_t>(left16);
      const uint32_t rightMag =
          right16 < 0 ? static_cast<uint32_t>(-static_cast<int32_t>(right16))
                      : static_cast<uint32_t>(right16);

      const uint32_t mag = leftMag > rightMag ? leftMag : rightMag;
      if (mag > peak_) {
        peak_ = mag;
      }
      absSum_ += leftMag;
      absSum_ += rightMag;
      frames_ += 2;
    }
  }

  void maybeReportMeter() {
    const unsigned long now = millis();
    if ((now - lastMeterMs_) < mic_wifi_debug_config::kMeterIntervalMs) {
      return;
    }

    const float peak = peak_ / 32768.0f;
    const float avg = frames_ ? (absSum_ / static_cast<float>(frames_)) / 32768.0f : 0.0f;
    Serial.printf(
        "MIC meter peak=%.4f avg=%.4f frames=%lu connected=%s bytes=%lu\n",
        peak,
        avg,
        static_cast<unsigned long>(frames_),
        micClient_.connected() ? "yes" : "no",
        static_cast<unsigned long>(micBytes_));
    peak_ = 0;
    absSum_ = 0;
    frames_ = 0;
    lastMeterMs_ = now;
  }

  void closeMicSession(const char* reason) {
    Serial.printf(
        "MIC %s, bytes=%lu, wall=%lums\n",
        reason,
        static_cast<unsigned long>(micBytes_),
        millis() - micStartedMs_);
    micSessionOpen_ = false;
    micClient_.stop();
  }

  bool writeAll(const uint8_t* data, size_t len) {
    size_t sent = 0;
    while (sent < len && micClient_.connected()) {
      const int n = micClient_.write(data + sent, len - sent);
      if (n <= 0) {
        return false;
      }
      sent += static_cast<size_t>(n);
    }
    return sent == len;
  }

  static size_t convertStereo32ToStereo16(
      const uint8_t* src,
      size_t len,
      uint8_t* dst,
      size_t dstCap) {
    const size_t usable = len - (len % 8);
    const size_t maxOut = dstCap - (dstCap % 4);
    size_t out = 0;
    for (size_t i = 0; i + 7 < usable && out + 3 < maxOut; i += 8) {
      int32_t left32 = 0;
      int32_t right32 = 0;
      memcpy(&left32, src + i, 4);
      memcpy(&right32, src + i + 4, 4);

      const int16_t left16 = static_cast<int16_t>(left32 >> 16);
      const int16_t right16 = static_cast<int16_t>(right32 >> 16);

      memcpy(dst + out, &left16, 2);
      memcpy(dst + out + 2, &right16, 2);
      out += 4;
    }
    return out;
  }
};
