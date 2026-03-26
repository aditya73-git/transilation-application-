#pragma once

#include <AudioTools.h>
#include <WiFi.h>

#include <string.h>

#include "FullDuplexDebugConfig.h"

using namespace audio_tools;

class FullDuplexMic {
 public:
  void begin(I2SStream& i2s, SemaphoreHandle_t i2sMutex) {
    i2s_ = &i2s;
    i2sMutex_ = i2sMutex;
    server_.begin();
    lastMeterMs_ = millis();

    xTaskCreatePinnedToCore(
        captureTaskEntry,
        "fd_mic_capture",
        8192,
        this,
        1,
        &captureTaskHandle_,
        1);
  }

  void printEndpoint(const IPAddress& ip) const {
    Serial.printf(
        "Mic stream tcp://%s:%u\n",
        ip.toString().c_str(),
        full_duplex_debug_config::kMicPort);
  }

  void tick() {
    if (!client_.connected()) {
      if (sessionOpen_) {
        closeSession("client disconnected");
      }
      client_.stop();
      WiFiClient next = server_.available();
      if (!next) {
        return;
      }

      client_ = next;
      ringReset();
      sessionCount_++;
      startedMs_ = millis();
      bytes_ = 0;
      peak_ = 0;
      absSum_ = 0;
      frames_ = 0;
      lastMeterMs_ = millis();
      sessionOpen_ = true;
      Serial.printf("MIC session #%lu connected\n", static_cast<unsigned long>(sessionCount_));
    }

    const size_t n = ringRead(readBuffer_, sizeof(readBuffer_));
    if (n == 0) {
      return;
    }

    bytes_ += static_cast<uint32_t>(n);
    updateMeter(n);
    maybeReportMeter();

    const size_t netBytes = convertStereo32ToStereo16(
        readBuffer_, n, netBuffer_, sizeof(netBuffer_));
    if (netBytes == 0) {
      return;
    }

    if (!writeAll(netBuffer_, netBytes)) {
      closeSession("socket write failed");
    }
  }

 private:
  I2SStream* i2s_ = nullptr;
  SemaphoreHandle_t i2sMutex_ = nullptr;
  WiFiServer server_{full_duplex_debug_config::kMicPort};
  WiFiClient client_;

  uint8_t captureBuffer_[full_duplex_debug_config::kReadBytes];
  uint8_t readBuffer_[full_duplex_debug_config::kReadBytes];
  uint8_t netBuffer_[full_duplex_debug_config::kMicNetBytes];
  uint8_t ringBuffer_[full_duplex_debug_config::kMicRingBytes];

  TaskHandle_t captureTaskHandle_ = nullptr;
  portMUX_TYPE ringMux_ = portMUX_INITIALIZER_UNLOCKED;
  volatile size_t ringHead_ = 0;
  volatile size_t ringTail_ = 0;

  unsigned long lastMeterMs_ = 0;
  uint32_t peak_ = 0;
  uint64_t absSum_ = 0;
  uint32_t frames_ = 0;

  uint32_t sessionCount_ = 0;
  unsigned long startedMs_ = 0;
  uint32_t bytes_ = 0;
  bool sessionOpen_ = false;

  static void captureTaskEntry(void* arg) {
    static_cast<FullDuplexMic*>(arg)->captureTask();
  }

  void captureTask() {
    for (;;) {
      size_t n = 0;
      if (xSemaphoreTake(i2sMutex_, pdMS_TO_TICKS(20)) == pdTRUE) {
        n = i2s_->readBytes(captureBuffer_, sizeof(captureBuffer_));
        xSemaphoreGive(i2sMutex_);
      }

      if (n > 0) {
        ringWrite(captureBuffer_, n);
      } else {
        vTaskDelay(pdMS_TO_TICKS(1));
      }
    }
  }

  size_t ringAvailableLocked() const {
    if (ringHead_ >= ringTail_) {
      return ringHead_ - ringTail_;
    }
    return full_duplex_debug_config::kMicRingBytes - (ringTail_ - ringHead_);
  }

  size_t ringFreeLocked() const {
    return (full_duplex_debug_config::kMicRingBytes - 1) - ringAvailableLocked();
  }

  void ringReset() {
    portENTER_CRITICAL(&ringMux_);
    ringHead_ = 0;
    ringTail_ = 0;
    portEXIT_CRITICAL(&ringMux_);
  }

  void ringWrite(const uint8_t* data, size_t len) {
    len -= len % 8;
    if (len == 0) {
      return;
    }

    portENTER_CRITICAL(&ringMux_);
    size_t freeSpace = ringFreeLocked();
    if (len > freeSpace) {
      size_t drop = len - freeSpace;
      drop += (8 - (drop % 8)) % 8;
      ringTail_ = (ringTail_ + drop) % full_duplex_debug_config::kMicRingBytes;
    }

    const size_t first = min(len, full_duplex_debug_config::kMicRingBytes - ringHead_);
    memcpy(ringBuffer_ + ringHead_, data, first);
    if (len > first) {
      memcpy(ringBuffer_, data + first, len - first);
    }
    ringHead_ = (ringHead_ + len) % full_duplex_debug_config::kMicRingBytes;
    portEXIT_CRITICAL(&ringMux_);
  }

  size_t ringRead(uint8_t* out, size_t maxLen) {
    maxLen -= maxLen % 8;
    if (maxLen == 0) {
      return 0;
    }

    portENTER_CRITICAL(&ringMux_);
    size_t available = ringAvailableLocked();
    size_t take = min(maxLen, available);
    take -= take % 8;
    if (take == 0) {
      portEXIT_CRITICAL(&ringMux_);
      return 0;
    }

    const size_t first = min(take, full_duplex_debug_config::kMicRingBytes - ringTail_);
    memcpy(out, ringBuffer_ + ringTail_, first);
    if (take > first) {
      memcpy(out + first, ringBuffer_, take - first);
    }
    ringTail_ = (ringTail_ + take) % full_duplex_debug_config::kMicRingBytes;
    portEXIT_CRITICAL(&ringMux_);
    return take;
  }

  void updateMeter(size_t n) {
    const size_t usable = n - (n % 8);
    for (size_t i = 0; i + 7 < usable; i += 8) {
      int32_t left32 = 0;
      int32_t right32 = 0;
      memcpy(&left32, readBuffer_ + i, 4);
      memcpy(&right32, readBuffer_ + i + 4, 4);

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
    if ((now - lastMeterMs_) < full_duplex_debug_config::kMeterIntervalMs) {
      return;
    }

    const float peak = peak_ / 32768.0f;
    const float avg = frames_ ? (absSum_ / static_cast<float>(frames_)) / 32768.0f : 0.0f;
    Serial.printf(
        "MIC meter peak=%.4f avg=%.4f frames=%lu connected=%s bytes=%lu\n",
        peak,
        avg,
        static_cast<unsigned long>(frames_),
        client_.connected() ? "yes" : "no",
        static_cast<unsigned long>(bytes_));
    peak_ = 0;
    absSum_ = 0;
    frames_ = 0;
    lastMeterMs_ = now;
  }

  void closeSession(const char* reason) {
    Serial.printf(
        "MIC %s, bytes=%lu, wall=%lums\n",
        reason,
        static_cast<unsigned long>(bytes_),
        millis() - startedMs_);
    sessionOpen_ = false;
    client_.stop();
  }

  bool writeAll(const uint8_t* data, size_t len) {
    size_t sent = 0;
    while (sent < len && client_.connected()) {
      const int n = client_.write(data + sent, len - sent);
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
