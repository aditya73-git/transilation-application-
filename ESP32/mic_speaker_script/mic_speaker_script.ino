// Translation / desktop app: mic (12346) + playback (12345) over WiFi.
#ifndef WIFI_DUAL_AUDIO
#define WIFI_DUAL_AUDIO 0
#endif

// Mic-only stream (AudioTools); use when WIFI_DUAL_AUDIO is 0.
#ifndef MIC_STREAM_WIFI
#define MIC_STREAM_WIFI 1
#endif

// Mic loopback: onboard mics -> speaker (AudioTools).
#ifndef MIC_LOOPBACK_TEST
#define MIC_LOOPBACK_TEST 0
#endif

#ifndef MIC_LOOPBACK_USE_TOGGLE
#define MIC_LOOPBACK_USE_TOGGLE 0
#endif

#ifndef PLAY_SINE_TEST
#define PLAY_SINE_TEST 0
#endif

#include <WiFi.h>
#include <string.h>

const char* ssid = "Wifi 1";//"Adityahotspot";
const char* password = "9347575778";//"12345678";

constexpr int kUsrButtonPin = D3;
constexpr uint32_t kButtonDebounceMs = 40;

constexpr uint16_t AUDIO_PORT = 12345;
constexpr uint16_t MIC_STREAM_PORT = 12346;
constexpr size_t AUDIO_BUFFER_SIZE = 2048;

constexpr uint32_t SAMPLE_RATE = 16000;

WiFiServer server(AUDIO_PORT);
WiFiClient client;

uint8_t audio_buffer[AUDIO_BUFFER_SIZE];

struct PcmMeterState {
  unsigned long last_report_ms = 0;
  uint32_t peak = 0;
  uint64_t sum_abs = 0;
  uint32_t samples = 0;
};

static void update_pcm16_meter(PcmMeterState& meter, const uint8_t* data, size_t len) {
  const size_t usable = len - (len % 2);
  for (size_t i = 0; i + 1 < usable; i += 2) {
    int16_t sample = 0;
    memcpy(&sample, data + i, sizeof(sample));
    uint32_t magnitude = (sample < 0) ? static_cast<uint32_t>(-static_cast<int32_t>(sample))
                                      : static_cast<uint32_t>(sample);
    if (magnitude > meter.peak) {
      meter.peak = magnitude;
    }
    meter.sum_abs += magnitude;
    meter.samples++;
  }
}

static void maybe_report_meter(PcmMeterState& meter, const char* label) {
  const unsigned long now = millis();
  if (now - meter.last_report_ms < 1000) {
    return;
  }
  const float peak = meter.peak / 32768.0f;
  const float avg_abs = meter.samples ? (meter.sum_abs / static_cast<float>(meter.samples)) / 32768.0f : 0.0f;
  Serial.printf("%s meter: peak=%.4f avg_abs=%.4f samples=%lu\n", label, peak, avg_abs, meter.samples);
  meter.last_report_ms = now;
  meter.peak = 0;
  meter.sum_abs = 0;
  meter.samples = 0;
}

static void update_pcm32_meter_top16(PcmMeterState& meter, const uint8_t* data, size_t len) {
  const size_t usable = len - (len % 8);
  for (size_t i = 0; i + 7 < usable; i += 8) {
    int32_t left32 = 0;
    int32_t right32 = 0;
    memcpy(&left32, data + i, sizeof(left32));
    memcpy(&right32, data + i + 4, sizeof(right32));
    int16_t left16 = static_cast<int16_t>(left32 >> 16);
    int16_t right16 = static_cast<int16_t>(right32 >> 16);
    const int16_t pair[2] = {left16, right16};
    update_pcm16_meter(meter, reinterpret_cast<const uint8_t*>(pair), sizeof(pair));
  }
}

static void report_send_failure(WiFiClient& c, const char* disconnected_msg, const char* failed_msg) {
  const bool was_connected = c.connected();
  c.stop();
  Serial.println(was_connected ? failed_msg : disconnected_msg);
}


void poll_usr_button() {
  static int last_raw = HIGH;
  static unsigned long last_change_ms = 0;
  static bool reported_hold = false;

  const int raw = digitalRead(kUsrButtonPin);
  const unsigned long now = millis();
  if (raw != last_raw) {
    last_change_ms = now;
    last_raw = raw;
  }
  if ((now - last_change_ms) < kButtonDebounceMs) {
    return;
  }

  const bool down = (raw == LOW);
  if (down && !reported_hold) {
    Serial.println("Button pressed");
    reported_hold = true;
  } else if (!down) {
    reported_hold = false;
  }
}

/* ----------- WiFi dual audio: Seeed-style AudioTools RX/TX toggle ----------- */
#if WIFI_DUAL_AUDIO

#include "AudioTools.h"

using namespace audio_tools;

AudioInfo info_dual(16000, 2, 32);
I2SStream i2s_dual;
I2SConfig cfg_dual;

WiFiServer play_server(AUDIO_PORT);
WiFiServer mic_server(MIC_STREAM_PORT);
WiFiClient play_client;
WiFiClient mic_client;

uint8_t mic_i2s_buf[4096];
uint8_t play_i2s_buf[AUDIO_BUFFER_SIZE * 2];
PcmMeterState mic_meter;
int current_i2s_mode = TX_MODE;

static bool write_all_client(WiFiClient& c, const uint8_t* data, size_t len) {
  size_t sent = 0;
  while (sent < len && c.connected()) {
    int w = c.write(data + sent, len - sent);
    if (w <= 0) {
      return false;
    }
    sent += (size_t)w;
  }
  return sent == len;
}

static size_t convert_i16_stereo_to_i32_stereo(const uint8_t* src, size_t len, uint8_t* dst) {
  size_t usable = len - (len % 4);
  size_t out = 0;
  for (size_t i = 0; i < usable; i += 4) {
    int16_t left16;
    int16_t right16;
    memcpy(&left16, src + i, 2);
    memcpy(&right16, src + i + 2, 2);
    int32_t left32 = static_cast<int32_t>(left16) << 16;
    int32_t right32 = static_cast<int32_t>(right16) << 16;
    memcpy(dst + out, &left32, 4);
    memcpy(dst + out + 4, &right32, 4);
    out += 8;
  }
  return out;
}

static bool switch_dual_i2s_mode(int mode) {
  if (current_i2s_mode == mode) {
    return true;
  }
  i2s_dual.end();
  cfg_dual.rx_tx_mode = static_cast<decltype(cfg_dual.rx_tx_mode)>(mode);
  if (!i2s_dual.begin(cfg_dual)) {
    Serial.println(mode == RX_MODE ? "I2S RX begin failed" : "I2S TX begin failed");
    return false;
  }
  current_i2s_mode = mode;
  return true;
}


void setup_wifi_dual() {
  Serial.println("Mode: WIFI_DUAL_AUDIO (AudioTools toggle)");
  AudioLogger::instance().begin(Serial, AudioLogger::Info);

  cfg_dual = i2s_dual.defaultConfig(TX_MODE);
  cfg_dual.copyFrom(info_dual);
  cfg_dual.pin_bck = 8;
  cfg_dual.pin_ws = 7;
  cfg_dual.pin_data = 43;
  cfg_dual.pin_data_rx = 44;
  cfg_dual.is_master = false;
  if (!i2s_dual.begin(cfg_dual)) {
    Serial.println("I2S TX begin failed");
    while (1) {
      delay(1000);
    }
  }
  current_i2s_mode = TX_MODE;

  WiFi.setSleep(false);
  Serial.println("Connecting to WiFi...");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("");
  Serial.println("WiFi connected!");
  Serial.print("IP Address: ");
  Serial.println(WiFi.localIP());

  play_server.begin();
  mic_server.begin();
  Serial.print("Playback (send PCM here) tcp://");
  Serial.print(WiFi.localIP());
  Serial.print(":");
  Serial.println(AUDIO_PORT);
  Serial.print("Mic stream (read PCM here) tcp://");
  Serial.print(WiFi.localIP());
  Serial.print(":");
  Serial.println(MIC_STREAM_PORT);
  Serial.println("Format: 16 kHz stereo int32 LE (8 bytes/frame) from AudioTools I2S");
}

void loop_wifi_dual() {
  poll_usr_button();

  if (!mic_client.connected()) {
    mic_client.stop();
    WiFiClient c = mic_server.available();
    if (c) {
      mic_client = c;
      Serial.println("Mic client connected");
    }
  }
  if (mic_client.connected()) {
    if (switch_dual_i2s_mode(RX_MODE)) {
      size_t n = i2s_dual.readBytes(mic_i2s_buf, sizeof(mic_i2s_buf));
      if (n > 0) {
        update_pcm32_meter_top16(mic_meter, mic_i2s_buf, n);
        maybe_report_meter(mic_meter, "ESP TX");
        if (!write_all_client(mic_client, mic_i2s_buf, n)) {
          report_send_failure(
              mic_client, "Mic client disconnected", "Mic send failed (socket write error)");
        }
      }
    }
  }

  if (!play_client.connected()) {
    play_client.stop();
    WiFiClient c = play_server.available();
    if (c) {
      play_client = c;
      Serial.println("Playback client connected");
    }
  }
  if (play_client.connected() && play_client.available()) {
    int available_bytes = play_client.available();
    size_t bytes_to_read =
        available_bytes > (int)AUDIO_BUFFER_SIZE ? AUDIO_BUFFER_SIZE : (size_t)available_bytes;
    
    // FIX applied here: Ensure exact multiples of 4 bytes are read for 16-bit stereo
    bytes_to_read -= bytes_to_read % 4;
    
    if (bytes_to_read > 0) {
      size_t total_read = 0;
      while (total_read < bytes_to_read && play_client.connected()) {
        int n = play_client.read(audio_buffer + total_read, bytes_to_read - total_read);
        if (n > 0) {
          total_read += n;
        } else {
          delay(1);
        }
      }
      
      if (total_read > 0) {
        size_t out_n = convert_i16_stereo_to_i32_stereo(audio_buffer, total_read, play_i2s_buf);
        if (out_n > 0 && switch_dual_i2s_mode(TX_MODE)) {
          i2s_dual.write(play_i2s_buf, out_n);
        }
      }
    }
  }
}

/* ----------- Mic -> WiFi -> laptop (AudioTools I2S RX only) ----------- */
#elif MIC_STREAM_WIFI

#include "AudioTools.h"

using namespace audio_tools;

AudioInfo info_mic(16000, 2, 32);
I2SStream i2s_mic;
I2SConfig cfg_mic;

constexpr size_t kMicChunkBytes = 4096;
constexpr uint32_t kPlaybackIdleTimeoutMs = 250;
constexpr uint32_t kMicIdleRestartMs = 750;
uint8_t mic_capture_buf[kMicChunkBytes];

WiFiServer mic_server(MIC_STREAM_PORT);
WiFiClient stream_client;
WiFiServer play_server(AUDIO_PORT);
WiFiClient play_client;
PcmMeterState mic_meter;
int current_mic_mode = TX_MODE;
uint8_t play_i2s_buf[AUDIO_BUFFER_SIZE * 2];
unsigned long playback_last_activity_ms = 0;
unsigned long mic_session_started_ms = 0;
unsigned long mic_last_data_ms = 0;
unsigned long mic_last_stall_log_ms = 0;
uint32_t mic_session_bytes = 0;
unsigned long play_session_started_ms = 0;
unsigned long play_last_stall_log_ms = 0;
uint32_t play_session_bytes = 0;

static void log_mode_change(const char* label) {
  Serial.printf("I2S mode -> %s\n", label);
}

static size_t convert_i16_stereo_to_i32_stereo(const uint8_t* src, size_t len, uint8_t* dst) {
  size_t usable = len - (len % 4);
  size_t out = 0;
  for (size_t i = 0; i < usable; i += 4) {
    int16_t left16 = 0;
    int16_t right16 = 0;
    memcpy(&left16, src + i, 2);
    memcpy(&right16, src + i + 2, 2);
    int32_t left32 = static_cast<int32_t>(left16) << 16;
    int32_t right32 = static_cast<int32_t>(right16) << 16;
    memcpy(dst + out, &left32, 4);
    memcpy(dst + out + 4, &right32, 4);
    out += 8;
  }
  return out;
}

static bool switch_mic_i2s_mode(int mode) {
  if (current_mic_mode == mode) {
    return true;
  }
  i2s_mic.end();
  delay(10);
  cfg_mic.rx_tx_mode = static_cast<decltype(cfg_mic.rx_tx_mode)>(mode);
  if (!i2s_mic.begin(cfg_mic)) {
    Serial.println(mode == RX_MODE ? "I2S RX begin failed" : "I2S TX begin failed");
    return false;
  }
  current_mic_mode = mode;
  log_mode_change(mode == RX_MODE ? "RX" : "TX");
  return true;
}

static bool restart_mic_i2s_mode(int mode, unsigned long settle_ms = 20) {
  i2s_mic.end();
  delay(10);
  cfg_mic.rx_tx_mode = static_cast<decltype(cfg_mic.rx_tx_mode)>(mode);
  if (!i2s_mic.begin(cfg_mic)) {
    Serial.println(mode == RX_MODE ? "I2S RX begin failed" : "I2S TX begin failed");
    return false;
  }
  current_mic_mode = mode;
  log_mode_change(mode == RX_MODE ? "RX (reinit)" : "TX (reinit)");
  delay(settle_ms);
  return true;
}

void setup_mic_wifi_stream() {
  Serial.println("Mode: MIC_STREAM_WIFI");
  AudioLogger::instance().begin(Serial, AudioLogger::Warning);
  cfg_mic = i2s_mic.defaultConfig(TX_MODE);
  cfg_mic.copyFrom(info_mic);
  cfg_mic.pin_bck = 8;
  cfg_mic.pin_ws = 7;
  cfg_mic.pin_data = 43;
  cfg_mic.pin_data_rx = 44;
  cfg_mic.is_master = false;
  if (!i2s_mic.begin(cfg_mic)) {
    Serial.println("I2S TX begin failed");
    while (1) {
      delay(1000);
    }
  }

  WiFi.setSleep(false);
  Serial.println("Connecting to WiFi...");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("");
  Serial.println("WiFi connected!");
  Serial.print("IP Address: ");
  Serial.println(WiFi.localIP());

  mic_server.begin();
  play_server.begin();
  current_mic_mode = TX_MODE;
  Serial.print("Mic stream (raw PCM) tcp://");
  Serial.print(WiFi.localIP());
  Serial.print(":");
  Serial.println(MIC_STREAM_PORT);
  Serial.print("Playback (send PCM here) tcp://");
  Serial.print(WiFi.localIP());
  Serial.print(":");
  Serial.println(AUDIO_PORT);
  Serial.println("Idle in speaker-ready TX mode; mic starts only when laptop connects");
}

static bool write_all_mic(WiFiClient& c, const uint8_t* data, size_t len) {
  size_t sent = 0;
  while (sent < len && c.connected()) {
    int w = c.write(data + sent, len - sent);
    if (w <= 0) {
      return false;
    }
    sent += (size_t)w;
  }
  return sent == len;
}

void loop_mic_wifi_stream() {
  poll_usr_button();

  if (!play_client.connected()) {
    play_client.stop();
    WiFiClient next_play_client = play_server.available();
    if (next_play_client) {
      play_client = next_play_client;
      if (stream_client.connected()) {
        stream_client.stop();
        Serial.println("MIC: client closed for playback handoff");
      }
      if (restart_mic_i2s_mode(TX_MODE)) {
        playback_last_activity_ms = millis();
        play_session_started_ms = millis();
        play_last_stall_log_ms = play_session_started_ms;
        play_session_bytes = 0;
        Serial.println("PLAY: client connected");
      } else {
        play_client.stop();
        restart_mic_i2s_mode(TX_MODE);
      }
    }
  }

  if (play_client.connected()) {
    if (!switch_mic_i2s_mode(TX_MODE)) {
      delay(1);
      return;
    }

    int available_bytes = play_client.available();
    if (available_bytes <= 0) {
      if (!play_client.connected() ||
          (millis() - playback_last_activity_ms) > kPlaybackIdleTimeoutMs) {
        play_client.stop();
        Serial.printf(
            "PLAY: client disconnected, bytes=%lu, wall=%lums\n",
            static_cast<unsigned long>(play_session_bytes),
            millis() - play_session_started_ms);
        restart_mic_i2s_mode(TX_MODE);
      } else if ((millis() - play_last_stall_log_ms) > 1000) {
        Serial.printf(
            "PLAY: waiting for audio, idle=%lums\n", millis() - playback_last_activity_ms);
        play_last_stall_log_ms = millis();
      }
      delay(1);
      return;
    }

    size_t bytes_to_read =
        available_bytes > (int)AUDIO_BUFFER_SIZE ? AUDIO_BUFFER_SIZE : (size_t)available_bytes;
    bytes_to_read -= bytes_to_read % 4;
    if (bytes_to_read == 0) {
      delay(1);
      return;
    }

    int bytes_read = play_client.read(audio_buffer, bytes_to_read);
    if (bytes_read > 0) {
      playback_last_activity_ms = millis();
      play_session_bytes += static_cast<uint32_t>(bytes_read);
      size_t out_n = convert_i16_stereo_to_i32_stereo(
          audio_buffer, static_cast<size_t>(bytes_read), play_i2s_buf);
      if (out_n > 0) {
        i2s_mic.write(play_i2s_buf, out_n);
      }
    }
    return;
  }

  if (!stream_client.connected()) {
    stream_client.stop();
    if (current_mic_mode != TX_MODE) {
      restart_mic_i2s_mode(TX_MODE, 5);
    }
    WiFiClient next_client = mic_server.available();
    if (next_client) {
      stream_client = next_client;
      if (restart_mic_i2s_mode(RX_MODE, 40)) {
        mic_session_started_ms = millis();
        mic_last_data_ms = mic_session_started_ms;
        mic_last_stall_log_ms = mic_session_started_ms;
        mic_session_bytes = 0;
        Serial.println("MIC: client connected");
      } else {
        stream_client.stop();
      }
    } else {
      delay(10);
      return;
    }
  }

  if (!switch_mic_i2s_mode(RX_MODE)) {
    delay(1);
    return;
  }

  size_t n = i2s_mic.readBytes(mic_capture_buf, kMicChunkBytes);
  if (n > 0) {
    mic_last_data_ms = millis();
    mic_session_bytes += static_cast<uint32_t>(n);
    update_pcm32_meter_top16(mic_meter, mic_capture_buf, n);
    maybe_report_meter(mic_meter, "ESP TX");
    if (!write_all_mic(stream_client, mic_capture_buf, n)) {
      const unsigned long now = millis();
      Serial.printf(
          "MIC: client disconnected, bytes=%lu, wall=%lums, idle=%lums\n",
          static_cast<unsigned long>(mic_session_bytes),
          now - mic_session_started_ms,
          now - mic_last_data_ms);
      report_send_failure(
          stream_client, "Mic client disconnected", "Mic send failed (socket write error)");
      switch_mic_i2s_mode(TX_MODE);
    }
  } else {
    if ((millis() - mic_last_stall_log_ms) > 1000) {
      Serial.printf("MIC: no I2S data for %lums\n", millis() - mic_last_data_ms);
      mic_last_stall_log_ms = millis();
    }
    if ((millis() - mic_last_data_ms) > kMicIdleRestartMs) {
      Serial.println("MIC: RX stalled, reinitializing");
      restart_mic_i2s_mode(RX_MODE, 40);
      mic_last_data_ms = millis();
    }
    delay(1);
  }
}

/* ----------- Mic loopback (AudioTools) ----------- */
#elif MIC_LOOPBACK_TEST

#include "AudioTools.h"

using namespace audio_tools;

AudioInfo info_loop(16000, 2, 32);
I2SStream i2s_at;
I2SConfig cfg_loop;

#if MIC_LOOPBACK_USE_TOGGLE

constexpr size_t kLoopBufBytes = 8192;
uint8_t loop_buf[kLoopBufBytes];

void setup_mic_loopback() {
  AudioLogger::instance().begin(Serial, AudioLogger::Info);
  cfg_loop = i2s_at.defaultConfig(TX_MODE);
  cfg_loop.copyFrom(info_loop);
  cfg_loop.pin_bck = 8;
  cfg_loop.pin_ws = 7;
  cfg_loop.pin_data = 43;
  cfg_loop.pin_data_rx = 44;
  cfg_loop.is_master = false;
  if (!i2s_at.begin(cfg_loop)) {
    Serial.println("I2S (TX) begin failed");
    while (1) {
      delay(1000);
    }
  }
  Serial.println("Mic loopback (toggle RX/TX): speak into mics; audio to speaker.");
}

void loop_mic_loopback() {
  poll_usr_button();

  i2s_at.end();
  cfg_loop.rx_tx_mode = RX_MODE;
  i2s_at.begin(cfg_loop);
  size_t bytes_read = i2s_at.readBytes(loop_buf, kLoopBufBytes);

  i2s_at.end();
  cfg_loop.rx_tx_mode = TX_MODE;
  i2s_at.begin(cfg_loop);
  if (bytes_read > 0) {
    i2s_at.write(loop_buf, bytes_read);
  }
}

#else

constexpr size_t kLoopBufBytes = 4096;
uint8_t loop_buf[kLoopBufBytes];

void setup_mic_loopback() {
  AudioLogger::instance().begin(Serial, AudioLogger::Info);
  cfg_loop = i2s_at.defaultConfig(RXTX_MODE);
  cfg_loop.copyFrom(info_loop);
  cfg_loop.pin_bck = 8;
  cfg_loop.pin_ws = 7;
  cfg_loop.pin_data = 43;
  cfg_loop.pin_data_rx = 44;
  cfg_loop.is_master = false;
  if (!i2s_at.begin(cfg_loop)) {
    Serial.println("I2S RXTX begin failed — try #define MIC_LOOPBACK_USE_TOGGLE 1");
    while (1) {
      delay(1000);
    }
  }
  Serial.println("Mic loopback (RXTX_MODE): speak into mics; audio to speaker.");
}

void loop_mic_loopback() {
  poll_usr_button();
  size_t n = i2s_at.readBytes(loop_buf, kLoopBufBytes);
  if (n > 0) {
    i2s_at.write(loop_buf, n);
  }
}

#endif

/* ----------- WiFi + ESP_I2S playback ----------- */
#else

#include <ESP_I2S.h>

I2SClass I2S;

#if PLAY_SINE_TEST
constexpr int kToneHz = 440;
constexpr int kAmplitude = 500;
int32_t sq_sample = kAmplitude;
int sq_count = 0;
const int half_wavelength = SAMPLE_RATE / kToneHz;

void write_square_chunk() {
  constexpr int kFrames = 256;
  int16_t pcm[kFrames * 2];
  for (int i = 0; i < kFrames; i++) {
    if (sq_count % half_wavelength == 0) {
      sq_sample = -sq_sample;
    }
    int16_t s = static_cast<int16_t>(sq_sample);
    pcm[i * 2] = s;
    pcm[i * 2 + 1] = s;
    sq_count++;
  }
  I2S.write(reinterpret_cast<uint8_t*>(pcm), sizeof(pcm));
}
#endif

bool init_i2s_like_tester() {
  I2S.setPins(8, 7, 43, 44);
  if (!I2S.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO)) {
    Serial.println("I2S begin failed");
    return false;
  }
  return true;
}

#endif

void setup() {
  Serial.begin(115200);
  delay(1000);
  pinMode(kUsrButtonPin, INPUT_PULLUP);

#if WIFI_DUAL_AUDIO
  setup_wifi_dual();
#elif MIC_STREAM_WIFI
  setup_mic_wifi_stream();
#elif MIC_LOOPBACK_TEST
  setup_mic_loopback();
#elif PLAY_SINE_TEST
  if (!init_i2s_like_tester()) {
    while (1) {
      delay(1000);
    }
  }
  Serial.println("I2S OK (16-bit stereo @ 16 kHz)");
  Serial.println("PLAY_SINE_TEST: square wave. Set PLAY_SINE_TEST 0 for TCP.");
#else
  if (!init_i2s_like_tester()) {
    while (1) {
      delay(1000);
    }
  }
  Serial.println("I2S OK (16-bit stereo @ 16 kHz)");
  WiFi.setSleep(false);
  Serial.println("Connecting to WiFi...");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("");
  Serial.println("WiFi connected!");
  Serial.print("IP Address: ");
  Serial.println(WiFi.localIP());
  server.begin();
  Serial.print("Audio receiver listening on tcp://");
  Serial.print(WiFi.localIP());
  Serial.print(":");
  Serial.println(AUDIO_PORT);
  Serial.println("Send raw PCM: 16kHz, stereo, 32-bit little-endian (converted to 16-bit for I2S)");
#endif
}

void loop() {
#if WIFI_DUAL_AUDIO
  loop_wifi_dual();
#elif MIC_STREAM_WIFI
  loop_mic_wifi_stream();
#elif MIC_LOOPBACK_TEST
  loop_mic_loopback();
#elif PLAY_SINE_TEST
  poll_usr_button();
  write_square_chunk();
#else
  poll_usr_button();
  if (!client || !client.connected()) {
    WiFiClient next_client = server.available();
    if (next_client) {
      client = next_client;
      Serial.println("Audio client connected");
    } else {
      delay(10);
      return;
    }
  }

  int available_bytes = client.available();
  if (available_bytes <= 0) {
    if (!client.connected()) {
      client.stop();
      Serial.println("Audio client disconnected");
    }
    delay(1);
    return;
  }

  size_t bytes_to_read =
      available_bytes > (int)AUDIO_BUFFER_SIZE ? AUDIO_BUFFER_SIZE : (size_t)available_bytes;
  
  // FIX applied here for the base mode: Multiples of 8 bytes for 32-bit stereo
  bytes_to_read -= bytes_to_read % 8;
  if (bytes_to_read == 0) {
    delay(1);
    return;
  }

  size_t total_read = 0;
  while (total_read < bytes_to_read && client.connected()) {
    int bytes_read = client.read(audio_buffer + total_read, bytes_to_read - total_read);
    if (bytes_read > 0) {
      total_read += bytes_read;
    } else {
      delay(1);
    }
  }

  if (total_read <= 0) {
    return;
  }

  const size_t frames = total_read / 8;
  int16_t pcm_stereo[128 * 2];
  size_t offset = 0;
  while (offset < frames) {
    size_t batch = frames - offset;
    if (batch > 128) {
      batch = 128;
    }
    for (size_t i = 0; i < batch; i++) {
      size_t b = (offset + i) * 8;
      int32_t L;
      int32_t R;
      memcpy(&L, &audio_buffer[b], 4);
      memcpy(&R, &audio_buffer[b + 4], 4);
      pcm_stereo[i * 2] = static_cast<int16_t>(L);
      pcm_stereo[i * 2 + 1] = static_cast<int16_t>(R);
    }
    I2S.write(reinterpret_cast<uint8_t*>(pcm_stereo), batch * 4);
    offset += batch;
  }
#endif
}
