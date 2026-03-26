#include "MicWifiDebugApp.h"

MicWifiDebugApp app;

void setup() {
  app.begin();
}

void loop() {
  app.tick();
}
