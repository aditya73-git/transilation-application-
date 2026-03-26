#pragma once

#include <AudioTools.h>
#include <WiFi.h>

#include <string.h>

#include "BridgeConfig.h"
#include "BridgeLog.h"
#include "BridgeTypes.h"

using namespace audio_tools;

class WifiAudioBridgeApp {
 public:
  void begin() {
    Serial.begin(115200);
    delay(1000);

    AudioLogger::instance().begin(Serial, AudioLogger::Warning);

    info_ = AudioInfo(
        bridge_config::kSampleRate,
        bridge_config::kChannels,
        bridge_config::kBitsPerSample);

    cfg_ = i2s_.defaultConfig(TX_MODE);
    cfg_.copyFrom(info_);
    cfg_.pin_bck = bridge_config::kI2SBclkPin;
    cfg_.pin_ws = bridge_config::kI2SWsPin;
    cfg_.pin_data = bridge_config::kI2SDataOutPin;
    cfg_.pin_data_rx = bridge_config::kI2SDataInPin;
    cfg_.is_master = false;

    if (!restartI2S(TX_MODE, 20)) {
      logLine("BOOT", "I2S TX init failed");
      while (true) {
        delay(1000);
      }
    }

    connectWifi();
    micServer_.begin();
    playServer_.begin();

    logf("NET", "Mic stream tcp://%s:%u", WiFi.localIP().toString().c_str(), bridge_config::kMicPort);
    logf(
        "NET",
        "Playback tcp://%s:%u",
        WiFi.localIP().toString().c_str(),
        bridge_config::kPlaybackPort);
    logLine("BOOT", "Idle in TX mode; waiting for mic or playback client");
  }

  void tick() {
    servicePlayback();
    if (playClient_.connected()) {
      return;
    }
    serviceMic();
  }

 private:
  AudioInfo info_;
  I2SStream i2s_;
  I2SConfig cfg_;

  WiFiServer micServer_{bridge_config::kMicPort};
  WiFiServer playServer_{bridge_config::kPlaybackPort};
  WiFiClient micClient_;
  WiFiClient playClient_;

  BridgeMode mode_ = BridgeMode::IdleTx;
  int i2sMode_ = TX_MODE;

  uint8_t micBuf_[bridge_config::kMicChunkBytes];
  uint8_t playBuf_[bridge_config::kPlaybackChunkBytes];
  uint8_t playI2SBuf_[bridge_config::kPlaybackChunkBytes * 2];

  unsigned long micStartedMs_ = 0;
  unsigned long micLastByteMs_ = 0;
  unsigned long micLastStallLogMs_ = 0;
  uint32_t micBytes_ = 0;

  unsigned long playStartedMs_ = 0;
  unsigned long playLastByteMs_ = 0;
  unsigned long playLastWaitLogMs_ = 0;
  uint32_t playBytes_ = 0;

  bool connectWifi() {
    WiFi.setSleep(false);
    logLine("NET", "Connecting to WiFi...");
    WiFi.begin(bridge_config::kWifiSsid, bridge_config::kWifiPassword);
    while (WiFi.status() != WL_CONNECTED) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
    logf("NET", "WiFi connected, IP=%s", WiFi.localIP().toString().c_str());
    return true;
  }

  bool restartI2S(int mode, unsigned long settleMs) {
    i2s_.end();
    delay(10);
    cfg_.rx_tx_mode = static_cast<decltype(cfg_.rx_tx_mode)>(mode);
    if (!i2s_.begin(cfg_)) {
      logf("I2S", "%s begin failed", mode == RX_MODE ? "RX" : "TX");
      return false;
    }
    i2sMode_ = mode;
    mode_ = (mode == RX_MODE) ? BridgeMode::MicRx : BridgeMode::IdleTx;
    logf("I2S", "mode -> %s", mode == RX_MODE ? "RX" : "TX");
    delay(settleMs);
    return true;
  }

  bool ensureI2SMode(int mode) {
    if (i2sMode_ == mode) {
      return true;
    }
    return restartI2S(mode, 10);
  }

  void servicePlayback() {
    if (!playClient_.connected()) {
      playClient_.stop();
      WiFiClient next = playServer_.available();
      if (next) {
        if (micClient_.connected()) {
          closeMicSession("handoff to playback");
        }
        playClient_ = next;
        if (!restartI2S(TX_MODE, 20)) {
          playClient_.stop();
          return;
        }
        mode_ = BridgeMode::PlaybackTx;
        playStartedMs_ = millis();
        playLastByteMs_ = playStartedMs_;
        playLastWaitLogMs_ = 0;
        playBytes_ = 0;
        logLine("PLAY", "client connected");
      }
    }

    if (!playClient_.connected()) {
      return;
    }

    if (!ensureI2SMode(TX_MODE)) {
      delay(1);
      return;
    }

    int availableBytes = playClient_.available();
    if (availableBytes <= 0) {
      const unsigned long now = millis();
      const bool firstByteTimeout =
          (playBytes_ == 0) &&
          ((now - playStartedMs_) > bridge_config::kPlaybackFirstByteTimeoutMs);
      const bool idleTimeout =
          (playBytes_ > 0) &&
          ((now - playLastByteMs_) > bridge_config::kPlaybackIdleTimeoutMs);

      if (!playClient_.connected() || firstByteTimeout || idleTimeout) {
        closePlaySession("client disconnected");
      } else if (playLastWaitLogMs_ == 0 || (now - playLastWaitLogMs_) > 500) {
        logf("PLAY", "waiting for audio, idle=%lums", now - playLastByteMs_);
        playLastWaitLogMs_ = now;
      }
      delay(1);
      return;
    }

    size_t bytesToRead = static_cast<size_t>(availableBytes);
    if (bytesToRead > bridge_config::kPlaybackChunkBytes) {
      bytesToRead = bridge_config::kPlaybackChunkBytes;
    }
    bytesToRead -= bytesToRead % 4;
    if (bytesToRead == 0) {
      delay(1);
      return;
    }

    const int bytesRead = playClient_.read(playBuf_, bytesToRead);
    if (bytesRead <= 0) {
      delay(1);
      return;
    }

    playLastByteMs_ = millis();
    playBytes_ += static_cast<uint32_t>(bytesRead);

    const size_t outBytes =
        convertStereo16ToStereo32(playBuf_, static_cast<size_t>(bytesRead), playI2SBuf_);
    if (outBytes > 0) {
      i2s_.write(playI2SBuf_, outBytes);
    }
  }

  void serviceMic() {
    if (!micClient_.connected()) {
      micClient_.stop();
      if (mode_ != BridgeMode::IdleTx || i2sMode_ != TX_MODE) {
        restartI2S(TX_MODE, 5);
      }

      WiFiClient next = micServer_.available();
      if (!next) {
        delay(10);
        return;
      }

      micClient_ = next;
      if (!restartI2S(RX_MODE, 40)) {
        micClient_.stop();
        restartI2S(TX_MODE, 5);
        return;
      }

      mode_ = BridgeMode::MicRx;
      micStartedMs_ = millis();
      micLastByteMs_ = micStartedMs_;
      micLastStallLogMs_ = micStartedMs_;
      micBytes_ = 0;
      logLine("MIC", "client connected");
    }

    if (!ensureI2SMode(RX_MODE)) {
      delay(1);
      return;
    }

    const size_t n = i2s_.readBytes(micBuf_, bridge_config::kMicChunkBytes);
    if (n == 0) {
      const unsigned long now = millis();
      if ((now - micLastStallLogMs_) > 1000) {
        logf("MIC", "no I2S data for %lums", now - micLastByteMs_);
        micLastStallLogMs_ = now;
      }
      if ((now - micLastByteMs_) > bridge_config::kMicIdleRestartMs) {
        logLine("MIC", "RX stalled, reinitializing");
        restartI2S(RX_MODE, 40);
        micLastByteMs_ = now;
      }
      delay(1);
      return;
    }

    micLastByteMs_ = millis();
    micBytes_ += static_cast<uint32_t>(n);

    if (!writeAll(micClient_, micBuf_, n)) {
      closeMicSession("client disconnected");
    }
  }

  void closeMicSession(const char* reason) {
    const unsigned long now = millis();
    logf(
        "MIC",
        "%s, bytes=%lu, wall=%lums, idle=%lums",
        reason,
        static_cast<unsigned long>(micBytes_),
        now - micStartedMs_,
        now - micLastByteMs_);
    micClient_.stop();
    restartI2S(TX_MODE, 5);
    mode_ = BridgeMode::IdleTx;
  }

  void closePlaySession(const char* reason) {
    const unsigned long now = millis();
    logf(
        "PLAY",
        "%s, bytes=%lu, wall=%lums",
        reason,
        static_cast<unsigned long>(playBytes_),
        now - playStartedMs_);
    playClient_.stop();
    restartI2S(TX_MODE, 5);
    mode_ = BridgeMode::IdleTx;
  }

  static bool writeAll(WiFiClient& client, const uint8_t* data, size_t len) {
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

  static size_t convertStereo16ToStereo32(const uint8_t* src, size_t len, uint8_t* dst) {
    const size_t usable = len - (len % 4);
    size_t out = 0;
    for (size_t i = 0; i < usable; i += 4) {
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
