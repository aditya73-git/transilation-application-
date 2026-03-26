#pragma once

#include <Arduino.h>

inline void logLine(const char* tag, const char* message) {
  Serial.printf("[%s] %s\n", tag, message);
}

template <typename... Args>
inline void logf(const char* tag, const char* fmt, Args... args) {
  Serial.printf("[%s] ", tag);
  Serial.printf(fmt, args...);
  Serial.println();
}
