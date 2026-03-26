#include <Wire.h>

#include "I2C48ProbeConfig.h"

using namespace i2c_48_probe_config;

unsigned long last_poll_ms = 0;
bool last_present = false;

bool device_present() {
  Wire.beginTransmission(kDeviceAddress);
  return Wire.endTransmission() == 0;
}

void try_raw_read() {
  const uint8_t requested = 4;
  const uint8_t received = Wire.requestFrom(static_cast<int>(kDeviceAddress), static_cast<int>(requested));
  if (received == 0) {
    Serial.println("  Raw read: no bytes returned");
    return;
  }

  Serial.print("  Raw read bytes:");
  while (Wire.available()) {
    const uint8_t value = Wire.read();
    Serial.printf(" 0x%02X", value);
  }
  Serial.println();
}

void poll_device() {
  const bool present = device_present();
  if (present != last_present) {
    Serial.printf("Device 0x%02X %s\n", kDeviceAddress, present ? "appeared" : "disappeared");
    last_present = present;
  }

  if (!present) {
    return;
  }

  Serial.printf("Device 0x%02X ACKed\n", kDeviceAddress);
  try_raw_read();
}

void setup() {
  Serial.begin(kBaudRate);
  delay(1000);

  Serial.println("I2C 0x48 read-only probe starting...");
  Serial.printf("Using SDA=%d SCL=%d target=0x%02X\n", kSdaPin, kSclPin, kDeviceAddress);
  Serial.println("This sketch does not write registers. It only checks ACK and attempts a raw read.");

  Wire.begin(kSdaPin, kSclPin);
  Wire.setClock(100000);

  poll_device();
  last_poll_ms = millis();
}

void loop() {
  if ((millis() - last_poll_ms) >= kPollIntervalMs) {
    poll_device();
    last_poll_ms = millis();
  }
}
