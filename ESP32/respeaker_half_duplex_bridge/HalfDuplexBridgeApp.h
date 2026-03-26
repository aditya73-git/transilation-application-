#pragma once

#include <AudioTools.h>
#include <WiFi.h>

#include <string.h>

#include "HalfDuplexBridgeConfig.h"

using namespace audio_tools;

enum class HalfDuplexState : uint8_t {
  ListeningRx,
  RecordingRx,
  AwaitPlaybackTx,
  PlaybackTx,
};

class HalfDuplexBridgeApp {
 public:
  void begin() {
    Serial.begin(115200);
    delay(1000);

    AudioLogger::instance().begin(Serial, AudioLogger::Warning);

    info_ = AudioInfo(
        half_duplex_bridge_config::kSampleRate,
        half_duplex_bridge_config::kChannels,
        half_duplex_bridge_config::kBitsPerSample);

    cfg_ = i2s_.defaultConfig(RX_MODE);
    cfg_.copyFrom(info_);
    cfg_.pin_bck = half_duplex_bridge_config::kI2SBclkPin;
    cfg_.pin_ws = half_duplex_bridge_config::kI2SWsPin;
    cfg_.pin_data = half_duplex_bridge_config::kI2SDataOutPin;
    cfg_.pin_data_rx = half_duplex_bridge_config::kI2SDataInPin;
    cfg_.is_master = false;

    Serial.println("ReSpeaker half-duplex bridge starting...");
    if (!restartI2S(RX_MODE)) {
      Serial.println("I2S RX begin failed");
      while (true) {
        delay(1000);
      }
    }

    connectWifi();
    micServer_.begin();
    playServer_.begin();

    xTaskCreatePinnedToCore(
        micCaptureTaskEntry,
        "half_duplex_mic_capture",
        8192,
        this,
        1,
        &micCaptureTaskHandle_,
        1);

    state_ = HalfDuplexState::ListeningRx;
    lastMicMeterMs_ = millis();

    Serial.printf(
        "Mic stream tcp://%s:%u\n",
        WiFi.localIP().toString().c_str(),
        half_duplex_bridge_config::kMicPort);
    Serial.printf(
        "Playback tcp://%s:%u\n",
        WiFi.localIP().toString().c_str(),
        half_duplex_bridge_config::kPlaybackPort);
    Serial.println("Half-duplex states: LISTEN_RX -> RECORD_RX -> WAIT_TX -> PLAY_TX -> LISTEN_RX");
    Serial.println("Network format: 16kHz stereo int16 LE");
    Serial.println("I2S format: 16kHz stereo int32 LE");
  }

  void tick() {
    switch (state_) {
      case HalfDuplexState::ListeningRx:
        serviceListening();
        break;
      case HalfDuplexState::RecordingRx:
        serviceRecording();
        break;
      case HalfDuplexState::AwaitPlaybackTx:
        serviceAwaitPlayback();
        break;
      case HalfDuplexState::PlaybackTx:
        servicePlayback();
        break;
    }
  }

 private:
  AudioInfo info_;
  I2SStream i2s_;
  I2SConfig cfg_;

  WiFiServer micServer_{half_duplex_bridge_config::kMicPort};
  WiFiServer playServer_{half_duplex_bridge_config::kPlaybackPort};
  WiFiClient micClient_;
  WiFiClient playClient_;

  HalfDuplexState state_ = HalfDuplexState::ListeningRx;
  int i2sMode_ = RX_MODE;

  uint8_t micCaptureBuffer_[half_duplex_bridge_config::kMicReadBytes];
  uint8_t micBuffer_[half_duplex_bridge_config::kMicReadBytes];
  uint8_t micNetBuffer_[half_duplex_bridge_config::kMicNetBytes];
  uint8_t micRingBuffer_[half_duplex_bridge_config::kMicRingBytes];

  uint8_t playNetBuffer_[half_duplex_bridge_config::kPlaybackNetBytes];
  uint8_t playI2SBuffer_[half_duplex_bridge_config::kPlaybackI2SBytes];
  uint8_t playRingBuffer_[half_duplex_bridge_config::kPlaybackRingBytes];

  TaskHandle_t micCaptureTaskHandle_ = nullptr;
  portMUX_TYPE micRingMux_ = portMUX_INITIALIZER_UNLOCKED;
  volatile size_t micRingHead_ = 0;
  volatile size_t micRingTail_ = 0;
  volatile bool micCaptureEnabled_ = false;
  volatile bool micCapturePaused_ = true;

  size_t playRingHead_ = 0;
  size_t playRingTail_ = 0;
  bool playbackPrimed_ = false;
  int16_t prevMonoSample_ = 0;

  unsigned long stateStartedMs_ = 0;
  unsigned long lastMicMeterMs_ = 0;
  unsigned long playLastByteMs_ = 0;
  unsigned long playLastLogMs_ = 0;

  uint32_t micSessionCount_ = 0;
  uint32_t playSessionCount_ = 0;
  uint32_t micBytes_ = 0;
  uint32_t playBytes_ = 0;
  uint32_t micPeak_ = 0;
  uint64_t micAbsSum_ = 0;
  uint32_t micFrames_ = 0;

  void connectWifi() {
    WiFi.setSleep(false);
    Serial.println("Connecting to WiFi...");
    WiFi.begin(
        half_duplex_bridge_config::kWifiSsid,
        half_duplex_bridge_config::kWifiPassword);
    while (WiFi.status() != WL_CONNECTED) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
    Serial.printf("WiFi connected, IP=%s\n", WiFi.localIP().toString().c_str());
  }

  static void micCaptureTaskEntry(void* arg) {
    static_cast<HalfDuplexBridgeApp*>(arg)->micCaptureTask();
  }

  void micCaptureTask() {
    for (;;) {
      if (!micCaptureEnabled_ || i2sMode_ != RX_MODE) {
        micCapturePaused_ = true;
        vTaskDelay(pdMS_TO_TICKS(1));
        continue;
      }

      micCapturePaused_ = false;
      const size_t n = i2s_.readBytes(micCaptureBuffer_, sizeof(micCaptureBuffer_));
      if (n > 0) {
        micRingWrite(micCaptureBuffer_, n);
      } else {
        vTaskDelay(pdMS_TO_TICKS(1));
      }
    }
  }

  bool restartI2S(int mode) {
    if (mode == TX_MODE) {
      setMicCaptureEnabled(false);
    }

    i2s_.end();
    delay(10);
    cfg_.rx_tx_mode = static_cast<decltype(cfg_.rx_tx_mode)>(mode);
    if (!i2s_.begin(cfg_)) {
      Serial.printf("I2S %s begin failed\n", mode == RX_MODE ? "RX" : "TX");
      return false;
    }

    i2sMode_ = mode;
    Serial.printf("I2S mode -> %s\n", mode == RX_MODE ? "RX" : "TX");
    delay(half_duplex_bridge_config::kI2SSettleMs);

    if (mode == RX_MODE) {
      setMicCaptureEnabled(true);
    }

    return true;
  }

  void setMicCaptureEnabled(bool enabled) {
    micCaptureEnabled_ = enabled;
    if (enabled) {
      return;
    }

    const unsigned long start = millis();
    while (!micCapturePaused_ && (millis() - start) < 250) {
      delay(1);
    }
  }

  void transitionToListeningRx() {
    closeMicClient();
    closePlayClient();
    if (restartI2S(RX_MODE)) {
      micRingReset();
      state_ = HalfDuplexState::ListeningRx;
      stateStartedMs_ = millis();
      Serial.println("STATE -> LISTEN_RX");
    }
  }

  void startRecording(WiFiClient client) {
    if (i2sMode_ != RX_MODE && !restartI2S(RX_MODE)) {
      client.stop();
      return;
    }

    micClient_ = client;
    micRingReset();
    micBytes_ = 0;
    micPeak_ = 0;
    micAbsSum_ = 0;
    micFrames_ = 0;
    lastMicMeterMs_ = millis();
    state_ = HalfDuplexState::RecordingRx;
    stateStartedMs_ = millis();
    micSessionCount_++;
    Serial.printf("STATE -> RECORD_RX (session #%lu)\n", static_cast<unsigned long>(micSessionCount_));
  }

  void finishRecording(const char* reason) {
    Serial.printf(
        "MIC %s, bytes=%lu, wall=%lums\n",
        reason,
        static_cast<unsigned long>(micBytes_),
        millis() - stateStartedMs_);
    closeMicClient();
    if (restartI2S(TX_MODE)) {
      state_ = HalfDuplexState::AwaitPlaybackTx;
      stateStartedMs_ = millis();
      Serial.println("STATE -> WAIT_TX");
    }
  }

  void startPlayback(WiFiClient client) {
    if (i2sMode_ != TX_MODE && !restartI2S(TX_MODE)) {
      client.stop();
      return;
    }

    playClient_ = client;
    playBytes_ = 0;
    playLastByteMs_ = millis();
    playLastLogMs_ = playLastByteMs_;
    playbackPrimed_ = false;
    prevMonoSample_ = 0;
    playRingReset();
    state_ = HalfDuplexState::PlaybackTx;
    stateStartedMs_ = millis();
    playSessionCount_++;
    Serial.printf("STATE -> PLAY_TX (session #%lu)\n", static_cast<unsigned long>(playSessionCount_));
  }

  void finishPlayback(const char* reason) {
    Serial.printf(
        "PLAY %s, bytes=%lu, wall=%lums\n",
        reason,
        static_cast<unsigned long>(playBytes_),
        millis() - stateStartedMs_);
    closePlayClient();
    transitionToListeningRx();
  }

  void serviceListening() {
    WiFiClient nextPlay = playServer_.available();
    if (nextPlay) {
      startPlayback(nextPlay);
      return;
    }

    WiFiClient nextMic = micServer_.available();
    if (nextMic) {
      startRecording(nextMic);
      return;
    }

    delay(5);
  }

  void serviceRecording() {
    if (!micClient_.connected()) {
      finishRecording("client disconnected");
      return;
    }

    const size_t n = micRingRead(micBuffer_, sizeof(micBuffer_));
    if (n == 0) {
      delay(1);
      return;
    }

    micBytes_ += static_cast<uint32_t>(n);
    updateMicMeter(n);
    maybeReportMicMeter();

    const size_t netBytes = convertStereo32ToStereo16(
        micBuffer_, n, micNetBuffer_, sizeof(micNetBuffer_));
    if (netBytes == 0) {
      return;
    }

    if (!writeAll(micClient_, micNetBuffer_, netBytes)) {
      finishRecording("socket write failed");
    }
  }

  void serviceAwaitPlayback() {
    WiFiClient nextPlay = playServer_.available();
    if (nextPlay) {
      startPlayback(nextPlay);
      return;
    }

    if ((millis() - stateStartedMs_) > half_duplex_bridge_config::kAwaitPlaybackMs) {
      transitionToListeningRx();
      return;
    }

    delay(5);
  }

  void servicePlayback() {
    receivePlaybackAudio();
    drainPlaybackRing();

    if (playClient_.connected()) {
      return;
    }

    if (playRingAvailable() == 0) {
      finishPlayback("client disconnected");
    }
  }

  void receivePlaybackAudio() {
    if (!playClient_.connected()) {
      return;
    }

    const int availableBytes = playClient_.available();
    if (availableBytes <= 0) {
      const unsigned long now = millis();
      if ((playBytes_ > 0) &&
          ((now - playLastByteMs_) > half_duplex_bridge_config::kPlaybackIdleTimeoutMs)) {
        playClient_.stop();
      } else if ((now - playLastLogMs_) > half_duplex_bridge_config::kPlaybackLogIntervalMs) {
        Serial.printf(
            "PLAY waiting, idle=%lums bytes=%lu ring=%lu\n",
            now - playLastByteMs_,
            static_cast<unsigned long>(playBytes_),
            static_cast<unsigned long>(playRingAvailable()));
        playLastLogMs_ = now;
      }
      return;
    }

    size_t bytesToRead = static_cast<size_t>(availableBytes);
    if (bytesToRead > half_duplex_bridge_config::kPlaybackNetBytes) {
      bytesToRead = half_duplex_bridge_config::kPlaybackNetBytes;
    }
    bytesToRead -= bytesToRead % 4;
    if (bytesToRead == 0) {
      return;
    }

    const int bytesRead = playClient_.read(playNetBuffer_, bytesToRead);
    if (bytesRead <= 0) {
      return;
    }

    playLastByteMs_ = millis();
    playBytes_ += static_cast<uint32_t>(bytesRead);

    const size_t outBytes = convertStereo16ToStereo32(
        playNetBuffer_,
        static_cast<size_t>(bytesRead),
        playI2SBuffer_,
        sizeof(playI2SBuffer_));
    if (outBytes > 0) {
      playRingWrite(playI2SBuffer_, outBytes);
    }
  }

  void drainPlaybackRing() {
    if (!playbackPrimed_ &&
        playRingAvailable() >= half_duplex_bridge_config::kPlaybackStartThresholdBytes) {
      playbackPrimed_ = true;
    }
    if (!playbackPrimed_ && !playClient_.connected() && playRingAvailable() > 0) {
      playbackPrimed_ = true;
    }
    if (!playbackPrimed_) {
      return;
    }

    const size_t outBytes = playRingPeek(playI2SBuffer_, sizeof(playI2SBuffer_));
    if (outBytes == 0) {
      return;
    }

    const size_t written = i2s_.write(playI2SBuffer_, outBytes);
    if (written > 0) {
      playRingDiscard(written - (written % 8));
    }

    if ((millis() - playLastLogMs_) > half_duplex_bridge_config::kPlaybackLogIntervalMs) {
      Serial.printf(
          "PLAY ring->I2S out_bytes=%lu written=%lu total=%lu ring=%lu primed=%s\n",
          static_cast<unsigned long>(outBytes),
          static_cast<unsigned long>(written),
          static_cast<unsigned long>(playBytes_),
          static_cast<unsigned long>(playRingAvailable()),
          playbackPrimed_ ? "yes" : "no");
      playLastLogMs_ = millis();
    }
  }

  void closeMicClient() {
    micClient_.stop();
  }

  void closePlayClient() {
    playClient_.stop();
    playbackPrimed_ = false;
    prevMonoSample_ = 0;
    playRingReset();
  }

  size_t micRingAvailableLocked() const {
    if (micRingHead_ >= micRingTail_) {
      return micRingHead_ - micRingTail_;
    }
    return half_duplex_bridge_config::kMicRingBytes - (micRingTail_ - micRingHead_);
  }

  size_t micRingFreeLocked() const {
    return (half_duplex_bridge_config::kMicRingBytes - 1) - micRingAvailableLocked();
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
      micRingTail_ = (micRingTail_ + drop) % half_duplex_bridge_config::kMicRingBytes;
    }

    const size_t first = min(len, half_duplex_bridge_config::kMicRingBytes - micRingHead_);
    memcpy(micRingBuffer_ + micRingHead_, data, first);
    if (len > first) {
      memcpy(micRingBuffer_, data + first, len - first);
    }
    micRingHead_ = (micRingHead_ + len) % half_duplex_bridge_config::kMicRingBytes;
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

    const size_t first = min(take, half_duplex_bridge_config::kMicRingBytes - micRingTail_);
    memcpy(out, micRingBuffer_ + micRingTail_, first);
    if (take > first) {
      memcpy(out + first, micRingBuffer_, take - first);
    }
    micRingTail_ = (micRingTail_ + take) % half_duplex_bridge_config::kMicRingBytes;
    portEXIT_CRITICAL(&micRingMux_);
    return take;
  }

  size_t playRingAvailable() const {
    if (playRingHead_ >= playRingTail_) {
      return playRingHead_ - playRingTail_;
    }
    return half_duplex_bridge_config::kPlaybackRingBytes - (playRingTail_ - playRingHead_);
  }

  size_t playRingFree() const {
    return (half_duplex_bridge_config::kPlaybackRingBytes - 1) - playRingAvailable();
  }

  void playRingReset() {
    playRingHead_ = 0;
    playRingTail_ = 0;
  }

  void playRingWrite(const uint8_t* data, size_t len) {
    len -= len % 8;
    if (len == 0) {
      return;
    }

    size_t freeSpace = playRingFree();
    if (len > freeSpace) {
      size_t drop = len - freeSpace;
      drop += (8 - (drop % 8)) % 8;
      playRingDiscard(drop);
    }

    const size_t first = min(len, half_duplex_bridge_config::kPlaybackRingBytes - playRingHead_);
    memcpy(playRingBuffer_ + playRingHead_, data, first);
    if (len > first) {
      memcpy(playRingBuffer_, data + first, len - first);
    }
    playRingHead_ = (playRingHead_ + len) % half_duplex_bridge_config::kPlaybackRingBytes;
  }

  size_t playRingPeek(uint8_t* out, size_t maxLen) const {
    maxLen -= maxLen % 8;
    if (maxLen == 0) {
      return 0;
    }

    size_t take = min(maxLen, playRingAvailable());
    take -= take % 8;
    if (take == 0) {
      return 0;
    }

    const size_t first = min(take, half_duplex_bridge_config::kPlaybackRingBytes - playRingTail_);
    memcpy(out, playRingBuffer_ + playRingTail_, first);
    if (take > first) {
      memcpy(out + first, playRingBuffer_, take - first);
    }
    return take;
  }

  void playRingDiscard(size_t len) {
    len -= len % 8;
    if (len == 0) {
      return;
    }

    len = min(len, playRingAvailable());
    len -= len % 8;
    playRingTail_ = (playRingTail_ + len) % half_duplex_bridge_config::kPlaybackRingBytes;
  }

  void updateMicMeter(size_t n) {
    const size_t usable = n - (n % 8);
    for (size_t i = 0; i + 7 < usable; i += 8) {
      int32_t left32 = 0;
      int32_t right32 = 0;
      memcpy(&left32, micBuffer_ + i, 4);
      memcpy(&right32, micBuffer_ + i + 4, 4);

      const int16_t left16 = static_cast<int16_t>(left32 >> 16);
      const int16_t right16 = static_cast<int16_t>(right32 >> 16);

      const uint32_t leftMag =
          left16 < 0 ? static_cast<uint32_t>(-static_cast<int32_t>(left16))
                     : static_cast<uint32_t>(left16);
      const uint32_t rightMag =
          right16 < 0 ? static_cast<uint32_t>(-static_cast<int32_t>(right16))
                      : static_cast<uint32_t>(right16);

      const uint32_t mag = leftMag > rightMag ? leftMag : rightMag;
      if (mag > micPeak_) {
        micPeak_ = mag;
      }
      micAbsSum_ += leftMag;
      micAbsSum_ += rightMag;
      micFrames_ += 2;
    }
  }

  void maybeReportMicMeter() {
    const unsigned long now = millis();
    if ((now - lastMicMeterMs_) < half_duplex_bridge_config::kMeterIntervalMs) {
      return;
    }

    const float peak = micPeak_ / 32768.0f;
    const float avg =
        micFrames_ ? (micAbsSum_ / static_cast<float>(micFrames_)) / 32768.0f : 0.0f;
    Serial.printf(
        "MIC meter peak=%.4f avg=%.4f frames=%lu bytes=%lu\n",
        peak,
        avg,
        static_cast<unsigned long>(micFrames_),
        static_cast<unsigned long>(micBytes_));
    micPeak_ = 0;
    micAbsSum_ = 0;
    micFrames_ = 0;
    lastMicMeterMs_ = now;
  }

  bool writeAll(WiFiClient& client, const uint8_t* data, size_t len) {
    size_t sent = 0;
    while (sent < len && client.connected()) {
      const int n = client.write(data + sent, len - sent);
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

      const int32_t mono =
          (static_cast<int32_t>(left16) + static_cast<int32_t>(right16)) / 2;
      const int32_t side =
          ((mono - static_cast<int32_t>(prevMonoSample_)) *
           half_duplex_bridge_config::kStereoWidthQ8) >>
          8;
      prevMonoSample_ = static_cast<int16_t>(mono);

      int32_t leftScaled =
          ((mono + side) * half_duplex_bridge_config::kPlaybackGainQ8) >> 8;
      int32_t rightScaled =
          ((mono - side) * half_duplex_bridge_config::kPlaybackGainQ8) >> 8;

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
