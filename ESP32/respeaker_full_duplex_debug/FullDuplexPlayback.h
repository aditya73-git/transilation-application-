#pragma once

#include <AudioTools.h>
#include <WiFi.h>

#include <string.h>

#include "FullDuplexDebugConfig.h"

using namespace audio_tools;

class FullDuplexPlayback {
 public:
  void begin(I2SStream& i2s, SemaphoreHandle_t i2sMutex) {
    i2s_ = &i2s;
    i2sMutex_ = i2sMutex;
    server_.begin();
  }

  void printEndpoint(const IPAddress& ip) const {
    Serial.printf(
        "Playback tcp://%s:%u\n",
        ip.toString().c_str(),
        full_duplex_debug_config::kPlaybackPort);
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
      sessionCount_++;
      startedMs_ = millis();
      lastByteMs_ = startedMs_;
      lastLogMs_ = startedMs_;
      bytes_ = 0;
      sessionOpen_ = true;
      Serial.printf("PLAY session #%lu connected\n", static_cast<unsigned long>(sessionCount_));
    }

    const int availableBytes = client_.available();
    if (availableBytes <= 0) {
      const unsigned long now = millis();
      if ((bytes_ > 0) &&
          ((now - lastByteMs_) > full_duplex_debug_config::kPlaybackIdleTimeoutMs)) {
        closeSession("idle timeout");
      } else if ((now - lastLogMs_) > full_duplex_debug_config::kPlaybackLogIntervalMs) {
        Serial.printf(
            "PLAY waiting, idle=%lums bytes=%lu\n",
            now - lastByteMs_,
            static_cast<unsigned long>(bytes_));
        lastLogMs_ = now;
      }
      return;
    }

    size_t bytesToRead = static_cast<size_t>(availableBytes);
    if (bytesToRead > full_duplex_debug_config::kPlaybackNetBytes) {
      bytesToRead = full_duplex_debug_config::kPlaybackNetBytes;
    }
    bytesToRead -= bytesToRead % 4;
    if (bytesToRead == 0) {
      return;
    }

    const int bytesRead = client_.read(netBuffer_, bytesToRead);
    if (bytesRead <= 0) {
      return;
    }

    lastByteMs_ = millis();
    bytes_ += static_cast<uint32_t>(bytesRead);

    const size_t outBytes = convertStereo16ToStereo32(
        netBuffer_, static_cast<size_t>(bytesRead), i2sBuffer_, sizeof(i2sBuffer_));
    if (outBytes == 0) {
      return;
    }

    size_t written = 0;
    if (xSemaphoreTake(i2sMutex_, pdMS_TO_TICKS(20)) == pdTRUE) {
      written = i2s_->write(i2sBuffer_, outBytes);
      xSemaphoreGive(i2sMutex_);
    }

    if ((millis() - lastLogMs_) > full_duplex_debug_config::kPlaybackLogIntervalMs) {
      Serial.printf(
          "PLAY 16->32 to I2S, in_bytes=%d out_bytes=%lu written=%lu total=%lu\n",
          bytesRead,
          static_cast<unsigned long>(outBytes),
          static_cast<unsigned long>(written),
          static_cast<unsigned long>(bytes_));
      lastLogMs_ = millis();
    }
  }

 private:
  I2SStream* i2s_ = nullptr;
  SemaphoreHandle_t i2sMutex_ = nullptr;
  WiFiServer server_{full_duplex_debug_config::kPlaybackPort};
  WiFiClient client_;

  uint8_t netBuffer_[full_duplex_debug_config::kPlaybackNetBytes];
  uint8_t i2sBuffer_[full_duplex_debug_config::kPlaybackI2SBytes];

  uint32_t sessionCount_ = 0;
  unsigned long startedMs_ = 0;
  unsigned long lastByteMs_ = 0;
  unsigned long lastLogMs_ = 0;
  uint32_t bytes_ = 0;
  bool sessionOpen_ = false;

  void closeSession(const char* reason) {
    Serial.printf(
        "PLAY %s, bytes=%lu, wall=%lums\n",
        reason,
        static_cast<unsigned long>(bytes_),
        millis() - startedMs_);
    sessionOpen_ = false;
    client_.stop();
  }

  static size_t convertStereo16ToStereo32(
      const uint8_t* src,
      size_t len,
      uint8_t* dst,
      size_t dstCap) {
    const size_t usable = len - (len % 4);
    const size_t maxOut = dstCap - (dstCap % 8);
    size_t out = 0;
    for (size_t i = 0; i + 3 < usable && out + 7 < maxOut; i += 4) {
      int16_t left16 = 0;
      int16_t right16 = 0;
      memcpy(&left16, src + i, 2);
      memcpy(&right16, src + i + 2, 2);

      const int32_t left32 = static_cast<int32_t>(left16) << 16;
      const int32_t right32 = static_cast<int32_t>(right16) << 16;

      memcpy(dst + out, &left32, 4);
      memcpy(dst + out + 4, &right32, 4);
      out += 8;
    }
    return out;
  }
};
