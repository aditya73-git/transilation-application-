#include <Wire.h>

#include "I2CSafeScannerConfig.h"

using namespace i2c_safe_scanner_config;

unsigned long last_scan_ms = 0;

void scan_i2c_bus() {
  int found = 0;

  Serial.println("Scanning I2C bus...");
  for (uint8_t address = 0x03; address <= 0x77; ++address) {
    Wire.beginTransmission(address);
    const uint8_t error = Wire.endTransmission();

    if (error == 0) {
      Serial.printf("  Found device at 0x%02X\n", address);
      found++;
    } else if (error == 4) {
      Serial.printf("  Unknown response at 0x%02X\n", address);
    }
  }

  if (found == 0) {
    Serial.println("  No I2C devices found.");
  }
  Serial.println();
}

void setup() {
  Serial.begin(kBaudRate);
  delay(1000);

  Serial.println("Safe I2C scanner starting...");
  Serial.printf("Using SDA=%d SCL=%d\n", kSdaPin, kSclPin);
  Serial.println("This sketch only probes addresses. It does not write device registers.");

  Wire.begin(kSdaPin, kSclPin);
  Wire.setClock(100000);

  scan_i2c_bus();
  last_scan_ms = millis();
}

void loop() {
  if ((millis() - last_scan_ms) >= kScanIntervalMs) {
    scan_i2c_bus();
    last_scan_ms = millis();
  }
}
