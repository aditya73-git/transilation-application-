#include <Arduino.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include "AudioTools.h"

using namespace audio_tools;

namespace {

constexpr char kDeviceName[] = "ReSpeaker-BLE-Audio";
constexpr char kServiceUuid[] = "7a1c1000-4f9f-4c4e-9f6d-8f84d0051000";
constexpr char kMicCharUuid[] = "7a1c1001-4f9f-4c4e-9f6d-8f84d0051000";
constexpr char kSpeakerCharUuid[] = "7a1c1002-4f9f-4c4e-9f6d-8f84d0051000";
constexpr char kStatusCharUuid[] = "7a1c1003-4f9f-4c4e-9f6d-8f84d0051000";

constexpr uint32_t kI2SSampleRate = 16000;
constexpr uint32_t kBleAudioSampleRate = 8000;
constexpr uint8_t kBleBitsPerSample = 16;
constexpr uint8_t kBleChannels = 1;

constexpr uint32_t kMicChunkMs = 10;
constexpr size_t kI2SFrameBytes = 8;      // int32 stereo
constexpr size_t kI2SFramesPerChunk = (kI2SSampleRate * kMicChunkMs) / 1000;
constexpr size_t kMicI2SChunkBytes = kI2SFramesPerChunk * kI2SFrameBytes;
constexpr size_t kBleMicChunkBytes = ((kBleAudioSampleRate * kMicChunkMs) / 1000) * 2;

constexpr uint32_t kPlaybackIdleTimeoutMs = 250;
constexpr uint32_t kStatusUpdateMs = 1000;

AudioInfo i2sInfo(kI2SSampleRate, 2, 32);
I2SStream i2s;
I2SConfig i2sConfig;

BLEServer* bleServer = nullptr;
BLECharacteristic* micCharacteristic = nullptr;
BLECharacteristic* speakerCharacteristic = nullptr;
BLECharacteristic* statusCharacteristic = nullptr;

bool bleClientConnected = false;
bool bleNotificationsEnabled = false;
int currentI2SMode = TX_MODE;
unsigned long lastPlaybackMs = 0;
unsigned long lastStatusMs = 0;

uint8_t micI2SBuffer[kMicI2SChunkBytes];
uint8_t bleMicBuffer[kBleMicChunkBytes];
uint8_t playbackI2SBuffer[kBleMicChunkBytes * 8];

const char* mode_label() {
  return currentI2SMode == RX_MODE ? "mic" : "speaker";
}

void update_status() {
  if (statusCharacteristic == nullptr) {
    return;
  }

  char status[128];
  snprintf(
      status,
      sizeof(status),
      "{\"connected\":%s,\"notify\":%s,\"mode\":\"%s\",\"ble_hz\":%lu,\"bits\":%u,\"channels\":%u}",
      bleClientConnected ? "true" : "false",
      bleNotificationsEnabled ? "true" : "false",
      mode_label(),
      static_cast<unsigned long>(kBleAudioSampleRate),
      static_cast<unsigned>(kBleBitsPerSample),
      static_cast<unsigned>(kBleChannels));
  statusCharacteristic->setValue(reinterpret_cast<const uint8_t*>(status), strlen(status));
}

bool switch_i2s_mode(int mode, unsigned long settleMs = 12) {
  if (currentI2SMode == mode) {
    return true;
  }

  i2s.end();
  delay(5);
  i2sConfig.rx_tx_mode = static_cast<decltype(i2sConfig.rx_tx_mode)>(mode);
  if (!i2s.begin(i2sConfig)) {
    Serial.println(mode == RX_MODE ? "I2S RX begin failed" : "I2S TX begin failed");
    return false;
  }
  currentI2SMode = mode;
  delay(settleMs);
  Serial.printf("I2S mode -> %s\n", mode == RX_MODE ? "RX" : "TX");
  update_status();
  return true;
}

void advertise_again() {
  BLEAdvertising* advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(kServiceUuid);
  advertising->setScanResponse(true);
  advertising->setMinPreferred(0x06);
  advertising->setMinPreferred(0x12);
  BLEDevice::startAdvertising();
  Serial.println("BLE advertising");
}

// ReSpeaker Lite produces 16 kHz stereo int32 frames. For BLE we collapse to 8 kHz mono int16
// by averaging L/R and decimating every other frame so the payload stays within a single BLE packet.
size_t convert_i2s_rx_to_ble_mono16(const uint8_t* src, size_t len, uint8_t* dst) {
  const size_t frames = len / kI2SFrameBytes;
  size_t out = 0;

  for (size_t frame = 0; frame < frames; frame += 2) {
    int32_t left32 = 0;
    int32_t right32 = 0;
    memcpy(&left32, src + (frame * kI2SFrameBytes), sizeof(left32));
    memcpy(&right32, src + (frame * kI2SFrameBytes) + 4, sizeof(right32));

    const int16_t left16 = static_cast<int16_t>(left32 >> 16);
    const int16_t right16 = static_cast<int16_t>(right32 >> 16);
    const int32_t mixed = (static_cast<int32_t>(left16) + static_cast<int32_t>(right16)) / 2;
    const int16_t mono = static_cast<int16_t>(mixed);

    memcpy(dst + out, &mono, sizeof(mono));
    out += sizeof(mono);
  }

  return out;
}

// Playback from the central arrives as 8 kHz mono int16. We upsample back to 16 kHz and duplicate
// to stereo int32 because the ReSpeaker Lite I2S path is already known-good in that format.
size_t convert_ble_mono16_to_i2s_tx(const uint8_t* src, size_t len, uint8_t* dst) {
  const size_t samples = len / 2;
  size_t out = 0;

  for (size_t i = 0; i < samples; ++i) {
    int16_t sample16 = 0;
    memcpy(&sample16, src + (i * 2), sizeof(sample16));
    const int32_t sample32 = static_cast<int32_t>(sample16) << 16;

    for (int repeat = 0; repeat < 2; ++repeat) {
      memcpy(dst + out, &sample32, sizeof(sample32));
      memcpy(dst + out + 4, &sample32, sizeof(sample32));
      out += kI2SFrameBytes;
    }
  }

  return out;
}

class ServerCallbacks final : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    bleClientConnected = true;
    bleNotificationsEnabled = false;
    lastPlaybackMs = 0;
    Serial.println("BLE client connected");
    update_status();
  }

  void onDisconnect(BLEServer*) override {
    bleClientConnected = false;
    bleNotificationsEnabled = false;
    lastPlaybackMs = 0;
    switch_i2s_mode(TX_MODE, 5);
    Serial.println("BLE client disconnected");
    advertise_again();
  }
};

class MicDescriptorCallbacks final : public BLEDescriptorCallbacks {
  void onWrite(BLEDescriptor* descriptor) override {
    const uint8_t* value = descriptor->getValue();
    const size_t valueLen = descriptor->getLength();
    bleNotificationsEnabled = (value != nullptr && valueLen >= 2 && value[0] == 0x01);
    Serial.printf("Mic notify -> %s\n", bleNotificationsEnabled ? "enabled" : "disabled");
    update_status();
  }
};

class SpeakerCallbacks final : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    const uint8_t* value = characteristic->getData();
    const size_t valueLen = characteristic->getLength();
    if (value == nullptr || valueLen == 0) {
      return;
    }

    if (!switch_i2s_mode(TX_MODE, 5)) {
      return;
    }

    const size_t usable = valueLen - (valueLen % 2);
    if (usable == 0) {
      return;
    }

    const size_t outBytes = convert_ble_mono16_to_i2s_tx(value, usable, playbackI2SBuffer);
    if (outBytes > 0) {
      i2s.write(playbackI2SBuffer, outBytes);
      lastPlaybackMs = millis();
    }
  }
};

void setup_ble() {
  BLEDevice::init(kDeviceName);
  BLEDevice::setMTU(247);

  bleServer = BLEDevice::createServer();
  bleServer->setCallbacks(new ServerCallbacks());

  BLEService* service = bleServer->createService(kServiceUuid);

  micCharacteristic = service->createCharacteristic(
      kMicCharUuid,
      BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_READ);
  auto* cccd = new BLE2902();
  cccd->setCallbacks(new MicDescriptorCallbacks());
  micCharacteristic->addDescriptor(cccd);

  speakerCharacteristic = service->createCharacteristic(
      kSpeakerCharUuid,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  speakerCharacteristic->setCallbacks(new SpeakerCallbacks());

  statusCharacteristic = service->createCharacteristic(
      kStatusCharUuid,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  auto* statusCccd = new BLE2902();
  statusCharacteristic->addDescriptor(statusCccd);

  service->start();
  advertise_again();
}

void setup_i2s() {
  AudioLogger::instance().begin(Serial, AudioLogger::Warning);
  i2sConfig = i2s.defaultConfig(TX_MODE);
  i2sConfig.copyFrom(i2sInfo);
  i2sConfig.pin_bck = 8;
  i2sConfig.pin_ws = 7;
  i2sConfig.pin_data = 43;
  i2sConfig.pin_data_rx = 44;
  i2sConfig.is_master = false;

  if (!i2s.begin(i2sConfig)) {
    Serial.println("I2S TX begin failed");
    while (true) {
      delay(1000);
    }
  }

  currentI2SMode = TX_MODE;
  Serial.println("I2S ready");
}

void stream_mic_if_possible() {
  if (!bleClientConnected) {
    return;
  }

  if (lastPlaybackMs != 0 && (millis() - lastPlaybackMs) < kPlaybackIdleTimeoutMs) {
    return;
  }

  if (!switch_i2s_mode(RX_MODE, 20)) {
    return;
  }

  const size_t bytesRead = i2s.readBytes(micI2SBuffer, sizeof(micI2SBuffer));
  if (bytesRead == 0) {
    delay(1);
    return;
  }

  const size_t bleBytes = convert_i2s_rx_to_ble_mono16(micI2SBuffer, bytesRead, bleMicBuffer);
  if (bleBytes == 0) {
    return;
  }

  micCharacteristic->setValue(bleMicBuffer, bleBytes);
  micCharacteristic->notify();
}

void recover_from_playback_idle() {
  if (lastPlaybackMs == 0) {
    return;
  }
  if ((millis() - lastPlaybackMs) < kPlaybackIdleTimeoutMs) {
    return;
  }

  lastPlaybackMs = 0;
  switch_i2s_mode(RX_MODE, 20);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("ReSpeaker Lite BLE audio bridge");
  Serial.println("BLE audio format: 8 kHz mono 16-bit PCM");
  Serial.println("I2S audio format: 16 kHz stereo 32-bit PCM");

  setup_i2s();
  setup_ble();
  update_status();
}

void loop() {
  recover_from_playback_idle();
  stream_mic_if_possible();

  if ((millis() - lastStatusMs) > kStatusUpdateMs) {
    lastStatusMs = millis();
    update_status();
  }

  delay(1);
}
