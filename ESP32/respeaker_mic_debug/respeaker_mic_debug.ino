#include "MicDebugApp.h"

MicDebugApp app;

void setup() {
  app.begin();
}

void loop() {
  app.tick();
}
