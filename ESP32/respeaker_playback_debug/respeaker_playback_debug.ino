#include "PlaybackDebugApp.h"

PlaybackDebugApp app;

void setup() {
  app.begin();
}

void loop() {
  app.tick();
}
