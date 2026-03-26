#pragma once

#include <AudioTools.h>
#include <WiFi.h>

#include <string.h>

#include "PlaybackDebugConfig.h"

using namespace audio_tools;

class PlaybackDebugApp {
 public:
  void begin() {
    Serial.begin(115200);
    delay(1000);

    AudioLogger::instance().begin(Serial, AudioLogger::Warning);

    AudioInfo info(
        playback_debug_config::kSampleRate,
        playback_debug_config::kChannels,
        playback_debug_config::kBitsPerSample);

    cfg_ = i2s_.defaultConfig(TX_MODE);
    cfg_.copyFrom(info);
    cfg_.pin_bck = playback_debug_config::kI2SBclkPin;
    cfg_.pin_ws = playback_debug_config::kI2SWsPin;
    cfg_.pin_data = playback_debug_config::kI2SDataOutPin;
    cfg_.pin_data_rx = playback_debug_config::kI2SDataInPin;
    cfg_.is_master = false;

    Serial.println("ReSpeaker playback WiFi debug starting...");
    if (!i2s_.begin(cfg_)) {
      Serial.println("I2S TX begin failed");
      while (true) {
        delay(1000);
      }
    }
    Serial.println("I2S mode -> TX");

    connectWifi();
    server_.begin();

    Serial.printf(
        "Playback tcp://%s:%u\n",
        WiFi.localIP().toString().c_str(),
        playback_debug_config::kPlaybackPort);
    Serial.println("Network format: 16kHz stereo int16 LE");
    Serial.println("I2S format: 16kHz stereo int32 LE");
    Serial.printf(
        "Playback gain: %.2fx, width: %.2fx, prebuffer=%lu bytes\n",
        playback_debug_config::kPlaybackGainQ8 / 256.0f,
        playback_debug_config::kStereoWidthQ8 / 256.0f,
        static_cast<unsigned long>(playback_debug_config::kPlaybackStartThresholdBytes));
  }

  void tick() {
    acceptClient();
    receiveNetworkAudio();
    drainPlaybackRing();

    if (sessionOpen_ && !client_.connected() && ringAvailable() == 0) {
      closeSession("client disconnected");
    }
  }

 private:
  I2SStream i2s_;
  I2SConfig cfg_;
  WiFiServer server_{playback_debug_config::kPlaybackPort};
  WiFiClient client_;

  uint8_t netBuffer_[playback_debug_config::kPlaybackNetBytes];
  uint8_t i2sBuffer_[playback_debug_config::kPlaybackI2SBytes];
  uint8_t ringBuffer_[playback_debug_config::kPlaybackRingBytes];

  uint32_t sessionCount_ = 0;
  unsigned long startedMs_ = 0;
  unsigned long lastByteMs_ = 0;
  unsigned long lastLogMs_ = 0;
  uint32_t bytes_ = 0;
  bool sessionOpen_ = false;
  bool playbackPrimed_ = false;
  size_t ringHead_ = 0;
  size_t ringTail_ = 0;
  int16_t prevMonoSample_ = 0;

  void connectWifi() {
    WiFi.setSleep(false);
    Serial.println("Connecting to WiFi...");
    WiFi.begin(
        playback_debug_config::kWifiSsid,
        playback_debug_config::kWifiPassword);
    while (WiFi.status() != WL_CONNECTED) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
    Serial.printf("WiFi connected, IP=%s\n", WiFi.localIP().toString().c_str());
  }

  void acceptClient() {
    if (client_.connected() || sessionOpen_) {
      return;
    }

    WiFiClient next = server_.available();
    if (!next) {
      delay(10);
      return;
    }

    client_ = next;
    sessionCount_++;
    startedMs_ = millis();
    lastByteMs_ = startedMs_;
    lastLogMs_ = startedMs_;
    bytes_ = 0;
    sessionOpen_ = true;
    playbackPrimed_ = false;
    prevMonoSample_ = 0;
    ringReset();
    Serial.printf("PLAY session #%lu connected\n", static_cast<unsigned long>(sessionCount_));
  }

  void receiveNetworkAudio() {
    if (!client_.connected()) {
      return;
    }

    const int availableBytes = client_.available();
    if (availableBytes <= 0) {
      const unsigned long now = millis();
      if ((bytes_ > 0) &&
          ((now - lastByteMs_) > playback_debug_config::kPlaybackIdleTimeoutMs)) {
        client_.stop();
      } else if ((now - lastLogMs_) > playback_debug_config::kPlaybackLogIntervalMs) {
        Serial.printf(
            "PLAY waiting, idle=%lums bytes=%lu ring=%lu\n",
            now - lastByteMs_,
            static_cast<unsigned long>(bytes_),
            static_cast<unsigned long>(ringAvailable()));
        lastLogMs_ = now;
      }
      return;
    }

    size_t bytesToRead = static_cast<size_t>(availableBytes);
    if (bytesToRead > playback_debug_config::kPlaybackNetBytes) {
      bytesToRead = playback_debug_config::kPlaybackNetBytes;
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
    if (outBytes > 0) {
      ringWrite(i2sBuffer_, outBytes);
    }
  }

  void drainPlaybackRing() {
    if (!sessionOpen_) {
      return;
    }

    if (!playbackPrimed_ &&
        ringAvailable() >= playback_debug_config::kPlaybackStartThresholdBytes) {
      playbackPrimed_ = true;
    }
    if (!playbackPrimed_ && !client_.connected() && ringAvailable() > 0) {
      playbackPrimed_ = true;
    }
    if (!playbackPrimed_) {
      return;
    }

    const size_t outBytes = ringPeek(i2sBuffer_, sizeof(i2sBuffer_));
    if (outBytes == 0) {
      return;
    }

    const size_t written = i2s_.write(i2sBuffer_, outBytes);
    if (written > 0) {
      ringDiscard(written - (written % 8));
    }

    if ((millis() - lastLogMs_) > playback_debug_config::kPlaybackLogIntervalMs) {
      Serial.printf(
          "PLAY ring->I2S out_bytes=%lu written=%lu total=%lu ring=%lu primed=%s\n",
          static_cast<unsigned long>(outBytes),
          static_cast<unsigned long>(written),
          static_cast<unsigned long>(bytes_),
          static_cast<unsigned long>(ringAvailable()),
          playbackPrimed_ ? "yes" : "no");
      lastLogMs_ = millis();
    }
  }

  void closeSession(const char* reason) {
    Serial.printf(
        "PLAY %s, bytes=%lu, wall=%lums\n",
        reason,
        static_cast<unsigned long>(bytes_),
        millis() - startedMs_);
    sessionOpen_ = false;
    playbackPrimed_ = false;
    prevMonoSample_ = 0;
    ringReset();
    client_.stop();
  }

  size_t ringAvailable() const {
    if (ringHead_ >= ringTail_) {
      return ringHead_ - ringTail_;
    }
    return playback_debug_config::kPlaybackRingBytes - (ringTail_ - ringHead_);
  }

  size_t ringFree() const {
    return (playback_debug_config::kPlaybackRingBytes - 1) - ringAvailable();
  }

  void ringReset() {
    ringHead_ = 0;
    ringTail_ = 0;
  }

  void ringWrite(const uint8_t* data, size_t len) {
    len -= len % 8;
    if (len == 0) {
      return;
    }

    size_t freeSpace = ringFree();
    if (len > freeSpace) {
      size_t drop = len - freeSpace;
      drop += (8 - (drop % 8)) % 8;
      ringDiscard(drop);
    }

    const size_t first = min(len, playback_debug_config::kPlaybackRingBytes - ringHead_);
    memcpy(ringBuffer_ + ringHead_, data, first);
    if (len > first) {
      memcpy(ringBuffer_, data + first, len - first);
    }
    ringHead_ = (ringHead_ + len) % playback_debug_config::kPlaybackRingBytes;
  }

  size_t ringPeek(uint8_t* out, size_t maxLen) const {
    maxLen -= maxLen % 8;
    if (maxLen == 0) {
      return 0;
    }

    size_t take = min(maxLen, ringAvailable());
    take -= take % 8;
    if (take == 0) {
      return 0;
    }

    const size_t first = min(take, playback_debug_config::kPlaybackRingBytes - ringTail_);
    memcpy(out, ringBuffer_ + ringTail_, first);
    if (take > first) {
      memcpy(out + first, ringBuffer_, take - first);
    }
    return take;
  }

  void ringDiscard(size_t len) {
    len -= len % 8;
    if (len == 0) {
      return;
    }

    len = min(len, ringAvailable());
    len -= len % 8;
    ringTail_ = (ringTail_ + len) % playback_debug_config::kPlaybackRingBytes;
  }

  size_t convertStereo16ToStereo32(
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

      const int32_t mono = (static_cast<int32_t>(left16) + static_cast<int32_t>(right16)) / 2;
      const int32_t side =
          ((mono - static_cast<int32_t>(prevMonoSample_)) *
           playback_debug_config::kStereoWidthQ8) >>
          8;
      prevMonoSample_ = static_cast<int16_t>(mono);

      int32_t leftScaled =
          ((mono + side) * playback_debug_config::kPlaybackGainQ8) >> 8;
      int32_t rightScaled =
          ((mono - side) * playback_debug_config::kPlaybackGainQ8) >> 8;

      if (leftScaled > 32767) leftScaled = 32767;
      if (leftScaled < -32768) leftScaled = -32768;
      if (rightScaled > 32767) rightScaled = 32767;
      if (rightScaled < -32768) rightScaled = -32768;

      const int32_t left32 = leftScaled << 16;
      const int32_t right32 = rightScaled << 16;

      memcpy(dst + out, &left32, 4);
      memcpy(dst + out + 4, &right32, 4);
      out += 8;
    }
    return out;
  }
};
