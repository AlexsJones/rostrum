// TX-26 audio path test. Every 2 s it switches what the SGTL5000 sends back to the Teensy:
//   LOOP   the Teensy's own 1 kHz tone, out on pin 7 and straight back on pin 8 (tests pins 7, 8, 20, 21, 23)
//   MIC    the capsule through the chip's mic preamp and converter
// and prints the level it receives, 0..1 (0.0000 means nothing arrives at all).
#include <Audio.h>
#include <Wire.h>

AudioSynthWaveformSine sine;
AudioOutputI2S         out;
AudioInputI2S          in;
AudioAnalyzeRMS        rms;
AudioConnection        a(sine, 0, out, 0), b(sine, 0, out, 1), c(in, 0, rms, 0);
AudioControlSGTL5000   codec;

void chipRoute(bool loop) {           // CHIP_SSS_CTRL: the I2S output's source, ADC (0) or I2S input (1)
  Wire.beginTransmission(0x0A);
  Wire.write(0x00); Wire.write(0x0A);
  Wire.write(0x00); Wire.write(loop ? 0x11 : 0x10);
  Wire.endTransmission();
}

void setup() {
  AudioMemory(12);
  codec.enable();
  codec.inputSelect(AUDIO_INPUT_MIC);
  codec.micGain(36);
  sine.frequency(1000);
  sine.amplitude(0.5);
  Serial.begin(115200);
}

void loop() {
  static bool looped = false;
  static elapsedMillis t;
  if (t < 2000) return;
  t = 0;
  looped = !looped;
  chipRoute(looped);
  delay(300);
  rms.read();
  delay(300);
  Serial.printf("%s  level %.4f\n", looped ? "LOOP (tone via pins 7 -> chip -> 8)" : "MIC  (capsule via chip)          ",
                rms.available() ? rms.read() : -1.0);
}
