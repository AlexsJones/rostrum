// TX-26 voice unit: a USB microphone (the electret on the Audio Adaptor) with a push-to-talk
// switch: the mic is silent unless the switch is held (10 ms fades, no clicks), and the switch
// is reported over USB serial.
//
//   USB Type: "Serial + MIDI + Audio"   (arduino-cli: --fqbn teensy:avr:teensy40:usb=serialmidiaudio)
//
// Serial lines, 115200 (any rate; it's USB):
//   PTT 1 / PTT 0      the switch was pressed / released (debounced 20 ms); the TX lamp follows it
//   LEVEL 0.123        the mic's peak level over the last 250 ms, 0..1 (switch held or not)
// Commands it takes (one per line):
//   GAIN 44            set the mic preamp, 0..63 dB (answers GAIN 44); lost at power-off
// Wiring: switch between pin 2 and pin 3 (pin 3 is driven low, standing in for GND),
//         lamp: pin 4 -> 330R -> LED -> GND (glows dimly when on, bright while transmitting),
//         capsule on the adaptor's MIC and GND pads.
#include <Audio.h>
#include <Bounce.h>

const int PTT_PIN = 2, PTT_GND_PIN = 3, LAMP_PIN = 4;
const int MIC_GAIN = 44;             // dB, 0..63: speech peaked at 0.02 (-34 dB) with 24; aim for about 0.25

AudioInputI2S        mic;            // from the adaptor's SGTL5000
AudioEffectFade      gate;           // opens while the switch is held
AudioOutputUSB       usb;            // to the laptop, as a USB microphone
AudioAnalyzePeak     peak;
AudioConnection      toGate(mic, 0, gate, 0);
AudioConnection      toLeft(gate, 0, usb, 0);
AudioConnection      toRight(gate, 0, usb, 1);
AudioConnection      toPeak(mic, 0, peak, 0);
const int FADE_MS = 10;
const int LAMP_IDLE = 6, LAMP_TX = 255;   // PWM brightness, 0..255: on but idle / transmitting
AudioControlSGTL5000 codec;
Bounce               ptt(PTT_PIN, 20);   // 20 ms stable: lever switches flutter when pressed slowly
elapsedMillis        sinceLevel;

void setup() {
  pinMode(PTT_GND_PIN, OUTPUT);       // the switch's other side: a ground for it
  digitalWrite(PTT_GND_PIN, LOW);
  pinMode(PTT_PIN, INPUT_PULLUP);
  pinMode(LAMP_PIN, OUTPUT);
  analogWrite(LAMP_PIN, LAMP_IDLE);
  AudioMemory(12);
  codec.enable();
  codec.inputSelect(AUDIO_INPUT_MIC);
  codec.micGain(MIC_GAIN);
  gate.fadeOut(1);                    // closed until the switch is pressed
  Serial.begin(115200);
}

void loop() {
  if (ptt.update()) {
    bool down = ptt.fallingEdge();   // pressed pulls the pin to ground
    analogWrite(LAMP_PIN, down ? LAMP_TX : LAMP_IDLE);
    if (down) gate.fadeIn(FADE_MS); else gate.fadeOut(FADE_MS);
    Serial.println(down ? "PTT 1" : "PTT 0");
  }
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c != '\n') { line += c; continue; }
    line.trim();
    if (line.startsWith("GAIN ")) {
      int db = constrain(line.substring(5).toInt(), 0, 63);
      codec.micGain(db);
      Serial.printf("GAIN %d\n", db);
    }
    line = "";
  }
  if (sinceLevel >= 250 && peak.available()) {
    sinceLevel = 0;
    Serial.print("LEVEL ");
    Serial.println(peak.read(), 3);
  }
}
