#include <Wire.h>
#include <M5UnitStep16.h>

#include "Step16SerialTestConfig.h"

using namespace step16_serial_test_config;

UnitStep16 step16(kAddress, &Wire);
uint8_t last_value = 0;
bool first_read = true;
unsigned long last_poll_ms = 0;

void setup() {
  Serial.begin(kBaudRate);
  delay(1000);

  Serial.println("Step16 serial test starting...");
  Serial.printf("Using SDA=%d SCL=%d addr=0x%02X\n", kSdaPin, kSclPin, kAddress);

  Wire.begin(kSdaPin, kSclPin);

  while (!step16.begin()) {
    Serial.println("Step16 not found, retrying...");
    delay(1000);
  }

  last_value = step16.getValue();
  Serial.printf("Initial Step16 value: %u (0x%X)\n", last_value, last_value);
  Serial.println("Rotate the dial and watch for value changes.");
}

void loop() {
  const unsigned long now = millis();
  if ((now - last_poll_ms) < kPollIntervalMs) {
    return;
  }
  last_poll_ms = now;

  const uint8_t value = step16.getValue();
  if (first_read || value != last_value) {
    Serial.printf("Step16 value: %u (0x%X)\n", value, value);
    last_value = value;
    first_read = false;
  }
}
