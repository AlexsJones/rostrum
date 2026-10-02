// TX-26 bench diagnostics: prints a line a second about the audio chip, the audio stream and pin 2.
//   arduino-cli upload --fqbn teensy:avr:teensy40:usb=serialmidiaudio firmware/diag
//
//   CODEC ok|FAIL   the SGTL5000 answered on I2C (address 0x0A, pins 18/19) and took its setup
//   AUDIO n         audio blocks that arrived from the chip in the last second (about 344 when working)
//   RMS x           the mic's level (0 means no data at all; a working chip shows at least hiss)
//   PIN2 lows/edges low samples and changes in 20000 fast reads; edges > 0 means it's being driven by
//                   a signal, and the rate (Hz) says which: ~88k LRCLK (pin 20), ~2.8M BCLK (21), ~11M MCLK (23)
#include <Audio.h>
#include <Wire.h>

AudioInputI2S        mic;
AudioAnalyzeRMS      rms;
AudioRecordQueue     queue;
AudioConnection      a(mic, 0, rms, 0);
AudioConnection      b(mic, 0, queue, 0);
AudioControlSGTL5000 codec;
bool codecOk;

void setup() {
  pinMode(2, INPUT_PULLUP);
  pinMode(3, OUTPUT);
  AudioMemory(40);
  codecOk = codec.enable();
  codec.inputSelect(AUDIO_INPUT_MIC);
  codec.micGain(36);
  queue.begin();
  Serial.begin(115200);
}

void loop() {
  static elapsedMillis t;
  static int blocks;
  while (queue.available()) { queue.readBuffer(); queue.freeBuffer(); blocks++; }
  if (t < 1000) return;
  t = 0;

  Wire.beginTransmission(0x0A);
  bool i2c = Wire.endTransmission() == 0;

  int lows = 0, edges = 0, last = digitalReadFast(2);
  uint32_t start = micros();
  for (int i = 0; i < 20000; i++) {
    int v = digitalReadFast(2);
    lows += !v;
    edges += v != last;
    last = v;
  }
  uint32_t us = micros() - start;
  digitalWrite(3, lows > 0);

  Serial.printf("CODEC %s I2C %s  AUDIO %d  RMS %.4f  PIN2 lows %d edges %d in %lu us (~%.0f Hz)\n",
                codecOk ? "ok" : "FAIL", i2c ? "ok" : "none", blocks, rms.available() ? rms.read() : -1.0,
                lows, edges, (unsigned long)us, edges * 1e6 / 2 / (us ? us : 1));
  blocks = 0;
}
